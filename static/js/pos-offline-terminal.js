/*
 * pos-offline-terminal.js -- the UI of /pos/offline/. All data comes from POSOffline (IndexedDB).
 */
(function () {
  'use strict';
  var P = window.POSOffline;
  var $ = function (id) { return document.getElementById(id); };
  var view = $('view');

  var cat = null, state = null;
  var cart = [];                 // [{variantId, qty, price?}]
  var category = 0, query = '';
  var payTotalCents = 0, paying = false;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function ksh(cents) { return 'KSH ' + P.fmtCents(cents); }
  var toastTimer;
  function toast(msg) {
    var t = $('toast'); t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.hidden = true; }, 3500);
  }
  function lineCents(l) { var v = cat.byId.get(l.variantId); return P.toCents(l.price != null ? l.price : v.price) * l.qty; }
  function totalCents() { return cart.reduce(function (n, l) { return n + lineCents(l); }, 0); }
  function persist() { P.saveDraftCart(cart).catch(function () {}); }

  /* ------------------------------------------------------------ header/status */
  function renderStatus(s) {
    var net = $('net');
    if (!s.online) { net.className = 'chip off'; net.textContent = 'Offline'; }
    else if (s.syncing) { net.className = 'chip sync'; net.textContent = 'Syncing\u2026'; }
    else if (s.failed || s.authRequired || s.paymentRequired) { net.className = 'chip warn'; net.textContent = s.authRequired ? 'Sign in to sync' : 'Needs attention'; }
    else { net.className = 'chip on'; net.textContent = 'Online'; }
    var n = s.pending + s.failed;
    $('pendingBtn').textContent = n ? 'Unsynced sales (' + n + ')' : 'Unsynced sales';
    $('onlineLink').hidden = !s.online;
    if ($('pendingOverlay').hidden === false) renderPending();
  }

  /* ----------------------------------------------------------------- blocked */
  var BLOCKED = {
    NO_SNAPSHOT: ['This device is not set up for offline selling yet', 'Sign in while you are online and open the POS terminal once. The product list is then saved on this device.'],
    LOCKED: ['You are signed out', 'Sign in again (this needs a connection) to keep selling offline. Sales already saved on this device are safe and will sync.'],
    EXPIRED: ['The saved product list is too old', 'Connect to the internet once so prices and stock can be refreshed. Sales already saved on this device are safe.'],
    NO_REGISTER: ['The cash register is not open', 'Open the register while you are online first. Offline sales need an open register to record the cash.']
  };
  function renderBlocked(reason) {
    var b = BLOCKED[reason] || ['Offline selling is unavailable', reason];
    view.innerHTML = '<div class="blocked"><h2>' + esc(b[0]) + '</h2><p>' + esc(b[1]) + '</p>' +
      '<a class="btn primary" href="/users/pos-login/">Sign in</a></div>';
    $('who').textContent = '';
  }

  /* ----------------------------------------------------------------- selling */
  function renderShell() {
    var snap = state.snap;
    $('who').textContent = snap.tenant.name + (snap.branch ? ' \u00b7 ' + snap.branch.name : '') + ' \u00b7 ' + snap.user.name;
    view.innerHTML =
      '<div class="main"><section class="catalog">' +
      '<input id="q" type="search" placeholder="Search by product or scan SKU, then Enter" autocomplete="off">' +
      '<div class="cats" id="cats"></div><div class="grid" id="grid"></div></section>' +
      '<section class="cart"><h2>Current sale</h2><div class="lines" id="lines"></div>' +
      '<div class="foot"><div class="total"><span>Total</span><span id="total">KSH 0.00</span></div>' +
      '<button class="btn go" id="charge" type="button" style="width:100%;padding:.9rem" disabled>Charge</button></div></section></div>';

    $('q').addEventListener('input', debounce(function () { query = $('q').value; renderGrid(); }, 150));
    $('q').addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      var hit = P.search(cat, $('q').value, 0, 1)[0];
      if (hit && hit.sku.toLowerCase() === $('q').value.trim().toLowerCase()) { add(hit.id); $('q').value = ''; query = ''; renderGrid(); }
    });
    $('charge').addEventListener('click', openPay);
    renderCats(); renderGrid(); renderCart();
  }

  function renderCats() {
    var html = '<button data-c="0" class="' + (category ? '' : 'active') + '">All</button>';
    cat.categories.filter(function (c) { return !c.parent_id; }).forEach(function (c) {
      html += '<button data-c="' + c.id + '" class="' + (category === c.id ? 'active' : '') + '">' + esc(c.name) + '</button>';
    });
    $('cats').innerHTML = html;
    $('cats').onclick = function (e) {
      var b = e.target.closest('button'); if (!b) return;
      category = Number(b.dataset.c); renderCats(); renderGrid();
    };
  }

  function renderGrid() {
    var list = P.search(cat, query, category, (query || category) ? 60 : 24);
    $('grid').innerHTML = list.length ? list.map(function (v) {
      var free = P.freeUnits(cat, v, cart, v.id);
      var inCart = (cart.filter(function (l) { return l.variantId === v.id; })[0] || { qty: 0 }).qty;
      var left = free - inCart;
      return '<div class="tile' + (left <= 0 ? ' out' : '') + '"><b>' + esc(v.name) + '</b>' +
        (v.pack ? '<span class="pack">' + esc(v.pack) + '</span>' : '') +
        (v.detail ? '<small>' + esc(v.detail) + '</small>' : '') +
        '<span class="sku">' + esc(v.sku) + '</span><span class="price">KSH ' + esc(v.price) + '</span>' +
        '<small style="font-weight:600;color:' + (left <= 0 ? '#991b1b' : left <= 5 ? '#b45309' : '#166534') + '">' +
        (left <= 0 ? 'Out of stock' : left + ' available') + '</small>' +
        '<button data-add="' + v.id + '"' + (left <= 0 ? ' disabled' : '') + '>Add to cart</button></div>';
    }).join('') : '<div class="empty" style="grid-column:1/-1">No products match.</div>';
    $('grid').onclick = function (e) {
      var b = e.target.closest('button[data-add]'); if (b) add(Number(b.dataset.add));
    };
  }

  function renderCart() {
    var box = $('lines');
    if (!cart.length) box.innerHTML = '<div class="empty">&#128722;<br>Cart is empty.<br>Add items to start a sale.</div>';
    else box.innerHTML = cart.map(function (l) {
      var v = cat.byId.get(l.variantId);
      return '<div class="line"><div class="nm">' + esc(P.displayName(v)) + '<small>' + esc(v.sku) + '</small></div>' +
        '<input type="number" min="1" value="' + l.qty + '" data-qty="' + v.id + '" inputmode="numeric">' +
        '<div style="min-width:70px;text-align:right">' + P.fmtCents(lineCents(l)) + '</div>' +
        '<button class="x" data-rm="' + v.id + '" aria-label="Remove">&times;</button></div>';
    }).join('');
    $('total').textContent = ksh(totalCents());
    $('charge').disabled = !cart.length;
    box.onchange = function (e) {
      var i = e.target.closest('input[data-qty]'); if (i) setQty(Number(i.dataset.qty), parseInt(i.value, 10));
    };
    box.onclick = function (e) {
      var b = e.target.closest('button[data-rm]');
      if (b) { cart = cart.filter(function (l) { return l.variantId !== Number(b.dataset.rm); }); changed(); }
    };
  }

  function changed() { persist(); renderCart(); renderGrid(); }

  function add(id, opts) {
    var v = cat.byId.get(id); if (!v) { toast('That product is not in the offline list.'); return; }
    var line = cart.filter(function (l) { return l.variantId === id; })[0];
    var want = (line ? line.qty : 0) + 1;
    if (!(opts && opts.force) && want > P.freeUnits(cat, v, cart, id)) { toast('No more ' + P.displayName(v) + ' in stock.'); return; }
    if (line) line.qty = want; else cart.push({ variantId: id, qty: 1 });
    changed();
  }
  function setQty(id, qty) {
    var v = cat.byId.get(id);
    if (!(qty > 0)) { cart = cart.filter(function (l) { return l.variantId !== id; }); return changed(); }
    var free = P.freeUnits(cat, v, cart, id);
    if (qty > free) { toast('Only ' + free + ' of ' + P.displayName(v) + ' available.'); qty = free; }
    var line = cart.filter(function (l) { return l.variantId === id; })[0];
    if (line) line.qty = qty;
    if (!qty) cart = cart.filter(function (l) { return l.variantId !== id; });
    changed();
  }

  /* ----------------------------------------------------------------- payment */
  function openPay() {
    payTotalCents = totalCents();
    $('payTotal').textContent = ksh(payTotalCents);
    $('payAmount').value = ''; $('payErr').textContent = '';
    var q = $('payQuick'); q.innerHTML = '<button class="btn" data-amt="' + P.fmtCents(payTotalCents) + '">Exact</button>';
    [100, 200, 500, 1000, 2000].forEach(function (n) {
      if (n * 100 >= payTotalCents) q.insertAdjacentHTML('beforeend', '<button class="btn" data-amt="' + n + '">' + n + '</button>');
    });
    q.onclick = function (e) { var b = e.target.closest('button[data-amt]'); if (b) { $('payAmount').value = b.dataset.amt; payChanged(); } };
    $('payOverlay').hidden = false; payChanged(); $('payAmount').focus();
  }
  function payChanged() {
    var received = P.toCents($('payAmount').value), has = $('payAmount').value.trim() !== '';
    var ok = has && received >= payTotalCents;
    $('payChange').className = 'change' + (has && !ok ? ' bad' : '');
    $('payChangeAmt').textContent = ok ? ksh(received - payTotalCents) : 'KSH 0.00';
    $('payConfirm').disabled = !ok || paying;
  }
  function confirmPay() {
    if (paying) return; paying = true; $('payConfirm').disabled = true;
    P.recordSale({ lines: cart, amountReceived: $('payAmount').value }).then(function (sale) {
      $('payOverlay').hidden = true; cart = []; persist(); showReceipt(sale);
    }).catch(function (err) {
      $('payErr').textContent = err.message || 'Could not save the sale.';
      if (err.code === 'INSUFFICIENT_STOCK') { renderCart(); renderGrid(); }
    }).then(function () { paying = false; payChanged(); });
  }

  /* ----------------------------------------------------------------- receipt */
  function showReceipt(sale) {
    history.replaceState(null, '', '#receipt=' + sale.uuid);
    $('who').textContent = sale.shop + ' \u00b7 ' + sale.cashierName;
    var d = new Date(sale.occurredAt);
    var when = ('0' + d.getDate()).slice(-2) + '/' + ('0' + (d.getMonth() + 1)).slice(-2) + '/' + d.getFullYear() + ' ' +
      ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
    view.innerHTML = '<div style="max-width:420px;margin:1.5rem auto;padding:0 1rem">' +
      '<div class="row" style="margin-bottom:.8rem"><button class="btn primary" id="printBtn" type="button">Print receipt</button>' +
      '<button class="btn" id="newSale" type="button">New sale</button></div>' +
      '<div class="paper"><div class="tag">OFFLINE SALE</div><div class="c"><h2>' + esc(sale.shop) + '</h2>' +
      (sale.branchName ? '<div>' + esc(sale.branchName) + '</div>' : '') + '</div><hr>' +
      '<div class="r"><span>Invoice</span><span>' + esc(sale.invoice) + '</span></div>' +
      '<div class="r"><span>Date</span><span>' + when + '</span></div>' +
      '<div class="r"><span>Served by</span><span>' + esc(sale.cashierName) + '</span></div><hr>' +
      sale.lines.map(function (l) {
        return '<div>' + esc(l.name) + '</div><div class="r"><span>' + l.qty + ' x ' + esc(l.price) + '</span><span>' + esc(l.lineTotal) + '</span></div>';
      }).join('') + '<hr>' +
      '<div class="r g"><span>TOTAL (KSH)</span><span>' + esc(sale.total) + '</span></div>' +
      '<div class="r"><span>Cash received</span><span>' + esc(sale.amountReceived) + '</span></div>' +
      '<div class="r"><span>Change</span><span>' + esc(sale.change) + '</span></div><hr>' +
      '<div class="c">Thank you for shopping with us.</div></div></div>';
    $('printBtn').onclick = function () { window.print(); };
    $('newSale').onclick = function () { history.replaceState(null, '', location.pathname); start(); };
  }
  function showReceiptByHash(uuid) {
    return P.getSale(uuid).then(function (sale) {
      if (sale) return showReceipt(sale), true;
      return false;
    });
  }

  /* -------------------------------------------------------- unsynced sales */
  function renderPending() {
    Promise.all([P.listOutbox(), P.getRecent()]).then(function (r) {
      var out = r[0], recent = r[1].slice(0, 5);
      var html = out.length ? out.map(function (s) {
        var d = new Date(s.occurredAt);
        return '<div class="it"><div><b>' + esc(s.invoice) + '</b><small>' + d.toLocaleString() + ' \u00b7 ' + esc(s.cashierName) + '</small>' +
          (s.status === 'failed' ? '<small class="bad">Not accepted: ' + esc(s.reason) + '</small>' : '<small>Waiting to sync</small>') +
          '</div><div>KSH ' + esc(s.total) + '</div></div>';
      }).join('') : '<p style="color:#6b7280">Nothing waiting. Everything on this device has reached the server.</p>';
      if (recent.length) html += '<p style="margin:1rem 0 .3rem;color:#6b7280;font-size:.85rem">Recently synced</p>' + recent.map(function (s) {
        return '<div class="it"><div><b>' + esc(s.invoice) + '</b><small>' + (s.receiptNumber ? 'Receipt ' + esc(s.receiptNumber) : '') + '</small></div><div>KSH ' + esc(s.total) + '</div></div>';
      }).join('');
      $('pendingList').innerHTML = html;
      $('retryFailed').hidden = !out.some(function (s) { return s.status === 'failed'; });
    });
  }
  function openPending() { $('pendingOverlay').hidden = false; renderPending(); }
  function exportBackup() {
    P.exportOutbox().then(function (json) {
      var a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
      a.download = 'unsynced-sales-' + new Date().toISOString().slice(0, 10) + '.json';
      document.body.appendChild(a); a.click(); a.remove();
    });
  }

  /* ------------------------------------------------------------------- start */
  function debounce(fn, ms) { var t; return function () { clearTimeout(t); t = setTimeout(fn, ms); }; }

  function start() {
    return P.getState().then(function (st) {
      state = st;
      if (!st.ok) return renderBlocked(st.reason);
      return P.loadCatalog(true).then(function (c) {
        cat = c; category = 0; query = '';
        return Promise.all([P.getDraftCart(), P.takeHandoff()]);
      }).then(function (r) {
        cart = (r[0] || []).filter(function (l) { return cat.byId.has(l.variantId); });
        var h = r[1], addId = 0;
        if (h) {                                     // the online terminal dropped: carry its cart over
          cart = (h.lines || []).filter(function (l) { return cat.byId.has(Number(l.variantId)); })
            .map(function (l) { return { variantId: Number(l.variantId), qty: Number(l.qty), price: l.price }; });
          addId = h.add && h.add.variantId ? Number(h.add.variantId) : 0;
          if (!addId && h.add && h.add.sku) { var hit = P.search(cat, h.add.sku, 0, 1)[0]; if (hit && hit.sku.toLowerCase() === h.add.sku.toLowerCase()) addId = hit.id; }
          persist();
          toast('Connection lost: your cart was moved to the offline terminal.');
        }
        renderShell();
        if (h && addId && cat.byId.has(addId)) add(addId, { force: true });   // after the screen exists
      });
    });
  }

  $('payAmount').addEventListener('input', payChanged);
  $('payAmount').addEventListener('keydown', function (e) { if (e.key === 'Enter' && !$('payConfirm').disabled) confirmPay(); });
  $('payConfirm').addEventListener('click', confirmPay);
  $('payCancel').addEventListener('click', function () { $('payOverlay').hidden = true; });
  $('pendingBtn').addEventListener('click', openPending);
  $('pendingClose').addEventListener('click', function () { $('pendingOverlay').hidden = true; });
  $('syncNow').addEventListener('click', function () { P.flush().then(function (r) { toast(r.stopped ? 'Could not reach the server yet.' : 'Synced ' + r.sent + ' sale(s).'); }); });
  $('retryFailed').addEventListener('click', function () { P.retryFailed(); });
  $('exportBtn').addEventListener('click', exportBackup);

  P.listen(renderStatus);
  P.status().then(renderStatus);

  // When a connection is available, refresh prices/stock and send anything waiting; if the
  // catalogue changed underneath an open screen, redraw it.
  P.refreshSnapshot().then(function (r) { if (r.status === 'updated' && state && state.ok && !paying) { P.loadCatalog(true).then(function (c) { cat = c; if ($('grid')) { renderGrid(); renderCart(); } }); } });

  var m = location.hash.match(/receipt=([0-9a-f-]+)/i);
  (m ? showReceiptByHash(m[1]) : Promise.resolve(false)).then(function (shown) {
    if (shown) return;
    if (m) toast('That sale has already synced. Find it under Receipts.');
    return start();
  }).then(function () { if (/pending/.test(location.hash)) openPending(); });
})();
