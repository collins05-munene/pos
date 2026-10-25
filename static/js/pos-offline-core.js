/*
 * pos-offline-core.js
 *
 * Everything the till needs to keep selling with no connection and to catch up
 * afterwards. No DOM in here, so it can be unit-tested under Node
 * (see tests/pos-offline-core.test.mjs).
 *
 *   snapshot   /pos/offline/snapshot/ -> IndexedDB (catalogue, stock, register, who is signed in)
 *   outbox     every sale rung up offline waits here until the server confirms it
 *   flush()    POST the outbox to /pos/offline/sync/. Safe to call any time, from any tab,
 *              as often as you like: each sale carries its own uuid and the server records
 *              a uuid once.
 *
 * Why IndexedDB and not localStorage: base.html's inactivity logout runs
 * localStorage.clear(); that must never be able to delete money.
 *
 * Money is handled in integer cents and stock in integer milli-units, never floats.
 */
(function (global) {
  'use strict';

  function create(env) {
    var DB_NAME = 'pos-offline';
    var DB_VERSION = 1;
    var SNAPSHOT_URL = '/pos/offline/snapshot/';
    var SYNC_URL = '/pos/offline/sync/';
    var MIN_REFRESH_MS = 2 * 60 * 1000;
    var DENIED_BACKOFF_MS = 60 * 60 * 1000;
    var BATCH = 10;
    var FETCH_TIMEOUT_MS = 20000;
    var RECENT_KEEP = 30;

    var listeners = [];
    var dbPromise = null;
    var flushing = null;
    var catalog = null;
    var channel = null;

    try {
      if (typeof env.BroadcastChannel === 'function') {
        channel = new env.BroadcastChannel('pos-offline');
        channel.onmessage = function () { emit(false); };
      }
    } catch (e) { channel = null; }

    /* ------------------------------------------------------------------ utils */
    function coded(code, message, extra) {
      var err = new Error(message || code);
      err.code = code;
      if (extra) err.details = extra;
      return err;
    }
    function isOnline() { return !env.navigator || env.navigator.onLine !== false; }
    function toCents(v) { return Math.round(parseFloat(v) * 100) || 0; }
    function toMilli(v) { return Math.round(parseFloat(v) * 1000) || 0; }
    function fmtCents(c) { return (c / 100).toFixed(2); }

    function randomHex(bytes) {
      var arr = new Uint8Array(bytes);
      if (env.crypto && env.crypto.getRandomValues) env.crypto.getRandomValues(arr);
      else for (var i = 0; i < bytes; i++) arr[i] = Math.floor(Math.random() * 256);
      return Array.prototype.map.call(arr, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
    }
    function newUuid() {
      if (env.crypto && typeof env.crypto.randomUUID === 'function') return env.crypto.randomUUID();
      var h = randomHex(16).split('');
      h[12] = '4'; h[16] = '89ab'[parseInt(h[16], 16) & 3];
      h = h.join('');
      return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
    }
    // Same shape the server generates (INV-YYYYMMDD-XXXXXX, UTC date) so the number on the
    // paper receipt is the number that ends up in the books.
    function makeInvoice(date) {
      var d = (date || new Date()).toISOString().slice(0, 10).replace(/-/g, '');
      return 'INV-' + d + '-' + randomHex(3).toUpperCase();
    }
    function displayName(v) {
      return v.name + (v.detail ? ' - ' + v.detail : '') + (v.pack ? ' [' + v.pack + ']' : '');
    }
    function getCsrf() {
      var m = env.document && env.document.cookie && env.document.cookie.match(/csrftoken=([^;]+)/);
      return m ? decodeURIComponent(m[1]) : '';
    }
    function timedFetch(url, init, ms) {
      var ctl = typeof env.AbortController === 'function' ? new env.AbortController() : null;
      var timer = ctl ? env.setTimeout(function () { ctl.abort(); }, ms || FETCH_TIMEOUT_MS) : null;
      var opts = Object.assign({ credentials: 'same-origin' }, init || {});
      if (ctl) opts.signal = ctl.signal;
      return env.fetch(url, opts).then(
        function (r) { if (timer) env.clearTimeout(timer); return r; },
        function (e) { if (timer) env.clearTimeout(timer); throw e; });
    }

    /* -------------------------------------------------------------- IndexedDB */
    function openDb() {
      if (dbPromise) return dbPromise;
      dbPromise = new Promise(function (resolve, reject) {
        var req = env.indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = function () {
          var db = req.result;
          if (!db.objectStoreNames.contains('meta')) db.createObjectStore('meta');
          if (!db.objectStoreNames.contains('variants')) db.createObjectStore('variants', { keyPath: 'id' });
          if (!db.objectStoreNames.contains('stock')) db.createObjectStore('stock', { keyPath: 'id' });
          if (!db.objectStoreNames.contains('outbox')) db.createObjectStore('outbox', { keyPath: 'uuid' });
        };
        req.onsuccess = function () { resolve(req.result); };
        req.onerror = function () { dbPromise = null; reject(req.error); };
      });
      return dbPromise;
    }
    function wrap(request) {
      return new Promise(function (res, rej) {
        request.onsuccess = function () { res(request.result); };
        request.onerror = function () { rej(request.error); };
      });
    }
    // fn(tx) may return a request-promise; the value is delivered once the transaction commits.
    function run(stores, mode, fn) {
      return openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
          var t = db.transaction(stores, mode);
          var out;
          t.oncomplete = function () { resolve(out); };
          t.onerror = function () { reject(t.error); };
          t.onabort = function () { reject(t.error || new Error('Transaction aborted')); };
          try { out = fn(t); } catch (e) { try { t.abort(); } catch (_) {} reject(e); }
        });
      });
    }
    function kvGet(key) { return run(['meta'], 'readonly', function (t) { return wrap(t.objectStore('meta').get(key)); }); }
    function kvSet(key, value) { return run(['meta'], 'readwrite', function (t) { t.objectStore('meta').put(value, key); }); }
    function kvDelete(key) { return run(['meta'], 'readwrite', function (t) { t.objectStore('meta').delete(key); }); }
    function getAll(store) { return run([store], 'readonly', function (t) { return wrap(t.objectStore(store).getAll()); }); }

    /* --------------------------------------------------------------- status */
    function listen(fn) { listeners.push(fn); return function () { listeners = listeners.filter(function (f) { return f !== fn; }); }; }

    function status() {
      return Promise.all([getAll('outbox'), kvGet('snapshot'), kvGet('authRequired'), kvGet('paymentRequired')]).then(function (r) {
        var out = r[0], snap = r[1];
        var me = snap && snap.user ? snap.user : null;
        var waiting = out.filter(function (s) { return s.status !== 'failed'; });
        return {
          online: isOnline(),
          pending: waiting.length,
          failed: out.length - waiting.length,
          // sales that belong to a cashier who has to sign in to send them
          foreign: me && me.role === 'CASHIER' ? waiting.filter(function (s) { return s.cashierId !== me.id; }).length : 0,
          syncing: !!flushing,
          authRequired: !!r[2],
          paymentRequired: !!r[3]
        };
      }).catch(function () {
        return { online: isOnline(), pending: 0, failed: 0, foreign: 0, syncing: !!flushing, authRequired: false, paymentRequired: false };
      });
    }
    function emit(broadcast) {
      return status().then(function (s) {
        listeners.forEach(function (fn) { try { fn(s); } catch (e) { /* a bad listener must not stop the rest */ } });
        if (broadcast !== false && channel) { try { channel.postMessage('changed'); } catch (e) {} }
        return s;
      });
    }

    /* ----------------------------------------------------- state / lock / expiry */
    // Can this device sell offline right now, and if not, why not?
    function getState() {
      return Promise.all([kvGet('snapshot'), kvGet('locked')]).then(function (r) {
        var snap = r[0];
        if (!snap) return { ok: false, reason: 'NO_SNAPSHOT' };
        if (r[1]) return { ok: false, reason: 'LOCKED', snap: snap };
        var ageHours = (Date.now() - snap.fetchedAt) / 3600000;
        if (ageHours > (snap.maxOfflineHours || 72)) return { ok: false, reason: 'EXPIRED', snap: snap };
        if (!snap.session) return { ok: false, reason: 'NO_REGISTER', snap: snap };
        return { ok: true, snap: snap };
      });
    }
    // Called when someone logs out (or the inactivity timer does it for them). Unsynced sales are untouched.
    function lock() { return kvSet('locked', true).then(function () { return emit(); }); }

    /* ------------------------------------------------------------- catalogue */
    function loadCatalog(force) {
      if (catalog && !force) return Promise.resolve(catalog);
      return Promise.all([getAll('variants'), getAll('stock'), kvGet('snapshot')]).then(function (r) {
        var byId = new Map();
        r[0].forEach(function (v) {
          v._hay = (v.name + ' ' + v.sku + ' ' + (v.detail || '') + ' ' + (v.pack || '')).toLowerCase();
          v._per = toMilli(v.per_pack) || 1000;
          byId.set(v.id, v);
        });
        var stock = new Map();
        r[1].forEach(function (row) { stock.set(row.id, row.qty); });
        catalog = { byId: byId, list: r[0], stock: stock, categories: (r[2] && r[2].categories) || [] };
        return catalog;
      });
    }

    // Whole units of THIS variant's own unit that the shared pool can still supply,
    // after what is already in the cart (other lines drawing on the same pool).
    function freeUnits(cat, v, cartLines, ignoreVariantId) {
      var free = cat.stock.get(v.base) || 0;
      (cartLines || []).forEach(function (l) {
        if (l.variantId === ignoreVariantId) return;
        var lv = cat.byId.get(l.variantId);
        if (lv && lv.base === v.base) free -= l.qty * lv._per;
      });
      return Math.max(0, Math.floor(free / v._per));
    }
    // Mirrors POSCart.validate_stock on the server.
    function checkStock(cat, lines) {
      var used = new Map(), problems = [];
      lines.forEach(function (l) {
        var v = cat.byId.get(l.variantId);
        if (!v) { problems.push({ variantId: l.variantId, name: l.name || '', requested: l.qty, available: 0 }); return; }
        var before = used.get(v.base) || 0;
        var free = Math.max(0, Math.floor(((cat.stock.get(v.base) || 0) - before) / v._per));
        if (l.qty > free) problems.push({ variantId: v.id, name: displayName(v), requested: l.qty, available: free });
        used.set(v.base, before + l.qty * v._per);
      });
      return problems;
    }
    // Everything on one screenful: SKU hit first, then name/SKU contains. categoryId matches the category or its children.
    function search(cat, q, categoryId, limit) {
      var needle = (q || '').trim().toLowerCase();
      var out = [], exact = null;
      for (var i = 0; i < cat.list.length; i++) {
        var v = cat.list[i];
        if (categoryId && v.cat !== categoryId && v.pcat !== categoryId) continue;
        if (needle) {
          if (v.sku.toLowerCase() === needle) { exact = v; continue; }
          if (v._hay.indexOf(needle) === -1) continue;
        }
        out.push(v);
        if (out.length >= (limit || 60)) break;
      }
      if (exact) out.unshift(exact);
      return out;
    }

    /* -------------------------------------------------------------- snapshot */
    function refreshSnapshot(opts) {
      opts = opts || {};
      return Promise.all([kvGet('snapshot'), kvGet('deniedAt')]).then(function (r) {
        var prev = r[0], denied = r[1];
        if (!opts.force && prev && Date.now() - prev.fetchedAt < MIN_REFRESH_MS) return { status: 'fresh' };
        if (!opts.force && denied && Date.now() - denied < DENIED_BACKOFF_MS) return { status: 'denied' };
        if (!isOnline()) return { status: 'offline' };
        var headers = { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' };
        if (prev && prev.etag) headers['If-None-Match'] = '"' + prev.etag + '"';
        return timedFetch(SNAPSHOT_URL, { headers: headers }).then(function (resp) {
          if (resp.status === 401) return { status: 'unauthenticated' };
          if (resp.status === 403) return kvSet('deniedAt', Date.now()).then(function () { return { status: 'denied' }; });
          if (resp.status === 402) return kvSet('paymentRequired', true).then(function () { emit(); return { status: 'payment_required' }; });
          if (resp.status === 304 && prev) {
            prev.fetchedAt = Date.now();
            return Promise.all([kvSet('snapshot', prev), kvSet('locked', false), kvDelete('authRequired')])
              .then(function () { emit(); return { status: 'unchanged' }; });
          }
          if (!resp.ok) return { status: 'error' };
          return resp.json().then(applySnapshot).then(function () { return { status: 'updated' }; });
        }, function () { return { status: 'offline' }; });
      });
    }

    function applySnapshot(data) {
      return Promise.all([getAll('outbox'), kvGet('snapshot')]).then(function (r) {
        var pending = r[0], prev = r[1];
        var perPackById = new Map();
        data.variants.forEach(function (v) { perPackById.set(v.id, v); });

        // Server stock does not yet include sales still waiting in the outbox.
        var stock = {};
        Object.keys(data.stock || {}).forEach(function (k) { stock[k] = toMilli(data.stock[k]); });
        pending.forEach(function (sale) {
          sale.lines.forEach(function (l) {
            var v = perPackById.get(l.variantId);
            if (v) stock[v.base] = (stock[v.base] || 0) - l.qty * (toMilli(v.per_pack) || 1000);
          });
        });

        var snap = {
          version: data.version, etag: data.etag, generatedAt: data.generated_at, fetchedAt: Date.now(),
          tenant: data.tenant, branch: data.branch, user: data.user, session: data.session,
          maxOfflineHours: data.max_offline_hours, categories: data.categories
        };
        var userChanged = prev && prev.user && prev.user.id !== data.user.id;

        return run(['variants', 'stock', 'meta'], 'readwrite', function (t) {
          var vs = t.objectStore('variants'), ss = t.objectStore('stock'), ms = t.objectStore('meta');
          vs.clear(); ss.clear();
          data.variants.forEach(function (v) { vs.put(v); });
          Object.keys(stock).forEach(function (k) { ss.put({ id: Number(k), qty: stock[k] }); });
          ms.put(snap, 'snapshot');
          ms.put(false, 'locked');
          ms.delete('deniedAt'); ms.delete('authRequired'); ms.delete('paymentRequired');
          if (userChanged) { ms.delete('draftCart'); ms.delete('handoff'); }
        }).then(function () { catalog = null; return emit(); });
      });
    }

    /* ----------------------------------------------------------------- sales */
    // Rings up a sale with no server involved. Throws an Error with .code:
    //   NO_SNAPSHOT | LOCKED | EXPIRED | NO_REGISTER   this till may not sell offline right now
    //   UNKNOWN_PRODUCT | BAD_QUANTITY | INSUFFICIENT_STOCK (.details = problems) | SHORT_PAYMENT
    // opts.fromOnlineCart: the customer has ALREADY paid at a till that lost its connection mid-checkout.
    // Never refuse that sale on a stock check; the server will record it and flag it.
    function recordSale(opts) {
      return getState().then(function (st) {
        if (!st.ok) throw coded(st.reason, 'Offline selling is not available: ' + st.reason);
        return loadCatalog().then(function (cat) {
          var snap = st.snap;
          var lines = (opts.lines || []).map(function (l) {
            var v = cat.byId.get(Number(l.variantId));
            if (!v) throw coded('UNKNOWN_PRODUCT', 'A product in this sale is not in the offline catalogue.');
            var qty = parseInt(l.qty, 10);
            if (!(qty > 0)) throw coded('BAD_QUANTITY', 'Quantities must be whole numbers above zero.');
            return { variantId: v.id, sku: v.sku, name: displayName(v), qty: qty, price: l.price != null ? String(l.price) : v.price };
          });
          if (!lines.length) throw coded('BAD_QUANTITY', 'The cart is empty.');

          if (!opts.fromOnlineCart) {
            var problems = checkStock(cat, lines);
            if (problems.length) {
              var p = problems[0];
              throw coded('INSUFFICIENT_STOCK', 'Only ' + p.available + ' of ' + p.name + ' left (you asked for ' + p.requested + ').', problems);
            }
          }

          var totalCents = lines.reduce(function (n, l) { return n + toCents(l.price) * l.qty; }, 0);
          var receivedCents = toCents(opts.amountReceived);
          if (receivedCents < totalCents) throw coded('SHORT_PAYMENT', 'The amount received is less than the total due.');

          var sale;
          return (opts.uuid ? run(['outbox'], 'readonly', function (t) { return wrap(t.objectStore('outbox').get(opts.uuid)); }) : Promise.resolve(null))
            .then(function (existing) {
              if (existing) return existing;      // double tap on the same checkout: same sale
              var now = new Date();
              sale = {
                uuid: opts.uuid || newUuid(),
                invoice: makeInvoice(now),
                occurredAt: now.toISOString(),
                createdAt: now.getTime(),
                cashierId: snap.user.id, cashierName: snap.user.name,
                sessionId: snap.session.id,
                shop: snap.tenant.name, branchName: snap.branch ? snap.branch.name : '',
                lines: lines.map(function (l) {
                  return { variantId: l.variantId, sku: l.sku, name: l.name, qty: l.qty, price: l.price,
                           lineTotal: fmtCents(toCents(l.price) * l.qty) };
                }),
                total: fmtCents(totalCents),
                amountReceived: fmtCents(receivedCents),
                change: fmtCents(receivedCents - totalCents),
                fromOnlineCart: !!opts.fromOnlineCart,
                status: 'pending', attempts: 0, reason: ''
              };
              var usage = new Map();
              lines.forEach(function (l) {
                var v = cat.byId.get(l.variantId);
                usage.set(v.base, (usage.get(v.base) || 0) + l.qty * v._per);
              });
              return run(['outbox', 'stock'], 'readwrite', function (t) {
                t.objectStore('outbox').put(sale);
                var ss = t.objectStore('stock');
                usage.forEach(function (milli, base) {
                  var g = ss.get(base);
                  g.onsuccess = function () {
                    var row = g.result || { id: base, qty: 0 };
                    row.qty -= milli;
                    ss.put(row);
                    cat.stock.set(base, row.qty);
                  };
                });
              }).then(function () { return sale; });
            });
        });
      }).then(function (sale) {
        emit();
        if (isOnline()) env.setTimeout(function () { flush().catch(function () {}); }, 1500);
        return sale;
      });
    }

    function getSale(uuid) { return run(['outbox'], 'readonly', function (t) { return wrap(t.objectStore('outbox').get(uuid)); }); }
    function listOutbox() { return getAll('outbox').then(function (rows) { return rows.sort(function (a, b) { return a.createdAt - b.createdAt; }); }); }
    function getRecent() { return kvGet('recent').then(function (r) { return r || []; }); }

    /* ------------------------------------------------------------------ sync */
    function toWire(s) {
      return {
        uuid: s.uuid, invoice: s.invoice, occurred_at: s.occurredAt, session_id: s.sessionId,
        cashier_id: s.cashierId, amount_received: s.amountReceived, from_online_cart: !!s.fromOnlineCart,
        lines: s.lines.map(function (l) { return { variant_id: l.variantId, quantity: l.qty, price: l.price }; })
      };
    }
    function withLock(fn) {
      var locks = env.navigator && env.navigator.locks;
      if (locks && locks.request) {
        return locks.request('pos-offline-flush', { ifAvailable: true }, function (lock) {
          return lock ? fn() : { skipped: true };
        });
      }
      return fn();
    }

    // Send everything that is waiting. Resolves { sent, failed, stopped } and never rejects on a network problem.
    function flush() {
      if (flushing) return flushing;
      flushing = withLock(doFlush).then(function (r) { flushing = null; emit(); return r; },
                                        function (e) { flushing = null; emit(); throw e; });
      emit();
      return flushing;
    }

    function doFlush() {
      return Promise.all([listOutbox(), kvGet('snapshot')]).then(function (r) {
        var me = r[1] && r[1].user;
        var queue = r[0].filter(function (s) {
          if (s.status === 'failed') return false;
          // A cashier only sends their own sales; a manager/admin may send anyone's.
          return me && (me.role !== 'CASHIER' || s.cashierId === me.id);
        });
        if (!queue.length) return { sent: 0, failed: 0 };
        if (!isOnline()) return { sent: 0, failed: 0, stopped: 'offline' };

        var sent = 0, failed = 0, stopped = null;
        function nextBatch(i) {
          if (i >= queue.length || stopped) return Promise.resolve();
          var batch = queue.slice(i, i + BATCH);
          return timedFetch(SYNC_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json',
                       'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': getCsrf() },
            body: JSON.stringify({ sales: batch.map(toWire) })
          }).then(function (resp) {
            if (resp.status === 401 || resp.status === 403) { stopped = 'auth'; return kvSet('authRequired', true); }
            if (resp.status === 402) { stopped = 'payment'; return kvSet('paymentRequired', true); }
            if (!resp.ok) { stopped = 'server'; return bumpAttempts(batch); }
            return resp.json().then(function (body) { return applyResults(batch, body.results || []); })
              .then(function (res) {
                sent += res.sent; failed += res.failed;
                if (res.stopped) stopped = 'server';
              });
          }, function () { stopped = 'network'; return bumpAttempts(batch); })
            .then(function () { return nextBatch(i + BATCH); });
        }
        return nextBatch(0).then(function () {
          var after = sent ? refreshSnapshot({ force: true }).catch(function () {}) : Promise.resolve();
          return after.then(function () {
            if (!stopped) return Promise.all([kvDelete('authRequired'), kvDelete('paymentRequired')]);
          }).then(function () { return { sent: sent, failed: failed, stopped: stopped }; });
        });
      });
    }

    function bumpAttempts(batch) {
      return run(['outbox'], 'readwrite', function (t) {
        var os = t.objectStore('outbox');
        batch.forEach(function (s) { s.attempts = (s.attempts || 0) + 1; os.put(s); });
      });
    }

    function applyResults(batch, results) {
      var byUuid = new Map();
      results.forEach(function (r) { byUuid.set(r.uuid, r); });
      var sent = 0, failed = 0, stopped = false, recent = [];
      return kvGet('recent').then(function (old) {
        recent = old || [];
        return run(['outbox'], 'readwrite', function (t) {
          var os = t.objectStore('outbox');
          batch.forEach(function (s) {
            var r = byUuid.get(s.uuid);
            if (!r) { stopped = true; return; }                       // the server stopped before reaching it
            if (r.status === 'created' || r.status === 'duplicate') {
              os.delete(s.uuid); sent++;
              recent.unshift({ uuid: s.uuid, invoice: r.invoice, total: s.total, at: s.createdAt,
                               receiptId: r.receipt_id, receiptNumber: r.receipt_number, flags: r.flags || [] });
            } else if (r.status === 'rejected') {
              s.status = 'failed'; s.reason = r.reason || 'Rejected by the server.'; s.code = r.code || '';
              os.put(s); failed++;
            } else {                                                   // 'error': retry later, keep order
              s.attempts = (s.attempts || 0) + 1; os.put(s); stopped = true;
            }
          });
        });
      }).then(function () {
        return kvSet('recent', recent.slice(0, RECENT_KEEP));
      }).then(function () { return { sent: sent, failed: failed, stopped: stopped }; });
    }

    // Put a rejected sale back in the queue (e.g. after the register was opened).
    function retryFailed() {
      return run(['outbox'], 'readwrite', function (t) {
        var os = t.objectStore('outbox');
        var req = os.getAll();
        req.onsuccess = function () {
          req.result.forEach(function (s) {
            if (s.status === 'failed') { s.status = 'pending'; s.reason = ''; s.attempts = 0; os.put(s); }
          });
        };
      }).then(function () { return emit(); }).then(function () { return flush(); });
    }
    function exportOutbox() {
      return listOutbox().then(function (rows) { return JSON.stringify(rows, null, 2); });
    }

    /* -------------------------------------------- cart drafts and hand-off */
    function saveDraftCart(lines) { return kvSet('draftCart', lines || []); }
    function getDraftCart() { return kvGet('draftCart').then(function (r) { return r || []; }); }
    // The online terminal lost its connection mid-sale: leave its cart for the offline terminal to pick up.
    function setHandoff(payload) { return kvSet('handoff', Object.assign({ at: Date.now() }, payload)); }
    function takeHandoff() {
      return kvGet('handoff').then(function (h) {
        if (!h) return null;
        return kvDelete('handoff').then(function () { return Date.now() - h.at < 10 * 60 * 1000 ? h : null; });
      });
    }

    return {
      // state
      getState: getState, lock: lock, listen: listen, status: status, emit: emit, isOnline: isOnline,
      // catalogue
      refreshSnapshot: refreshSnapshot, loadCatalog: loadCatalog, search: search,
      freeUnits: freeUnits, checkStock: checkStock, displayName: displayName,
      // sales
      recordSale: recordSale, getSale: getSale, listOutbox: listOutbox, getRecent: getRecent,
      flush: flush, retryFailed: retryFailed, exportOutbox: exportOutbox,
      // drafts
      saveDraftCart: saveDraftCart, getDraftCart: getDraftCart, setHandoff: setHandoff, takeHandoff: takeHandoff,
      // helpers
      toCents: toCents, toMilli: toMilli, fmtCents: fmtCents, newUuid: newUuid, makeInvoice: makeInvoice
    };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { create: create };
  else global.POSOffline = create(global);
})(typeof self !== 'undefined' ? self : this);
