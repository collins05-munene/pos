/*
 * pos-offline-boot.js
 *
 * Loaded on every signed-in page (via users/base.html) and on the offline terminal.
 *  - keeps the on-device catalogue fresh while online
 *  - sends waiting offline sales whenever there is a connection (page load, 'online' event,
 *    tab becomes visible, every 30s while something is waiting)
 *  - shows a small status pill when something needs the cashier's attention
 *  - locks offline selling when the person signs out
 *  - gives the online terminal a way to hand its cart over when the connection drops
 */
(function () {
  'use strict';
  var P = window.POSOffline;
  if (!P) return;

  var body = document.body;
  var onShell = body && body.dataset.posShell === '1';
  var signedIn = body && body.dataset.posUser === '1';
  var OFFLINE_URL = '/pos/offline/';

  /* ---- status pill (not on the offline terminal, which has its own header) ---- */
  var pill = null;
  function ensurePill() {
    if (pill || onShell) return pill;
    var css = document.createElement('style');
    css.textContent = '#posOfflinePill{position:fixed;top:.5rem;left:50%;transform:translateX(-50%);z-index:9998;' +
      'padding:.35rem .9rem;border-radius:999px;font:600 .8rem system-ui,sans-serif;border:0;cursor:pointer;' +
      'box-shadow:0 4px 14px rgba(0,0,0,.2)}#posOfflinePill[hidden]{display:none}' +
      '#posOfflinePill.off{background:#fef3c7;color:#92400e}#posOfflinePill.sync{background:#dbeafe;color:#1e40af}' +
      '#posOfflinePill.warn{background:#fee2e2;color:#991b1b}';
    document.head.appendChild(css);
    pill = document.createElement('button');
    pill.id = 'posOfflinePill'; pill.type = 'button'; pill.hidden = true;
    document.body.appendChild(pill);
    return pill;
  }
  function plural(n) { return n + ' sale' + (n === 1 ? '' : 's'); }

  function render(s) {
    if (onShell || !signedIn) return;
    var el = ensurePill(), text = '', cls = '', action = null;
    var waiting = s.pending;
    if (s.authRequired && (waiting || s.failed)) {
      text = 'Sign in to sync ' + plural(waiting + s.failed); cls = 'warn';
      action = function () { location.href = '/users/pos-login/'; };
    } else if (s.paymentRequired && waiting) {
      text = plural(waiting) + ' waiting: subscription needs attention'; cls = 'warn';
      action = function () { location.href = '/billing/'; };
    } else if (s.failed) {
      text = plural(s.failed) + ' need attention'; cls = 'warn';
      action = function () { location.href = OFFLINE_URL + '#pending'; };
    } else if (!s.online) {
      text = 'Offline' + (waiting ? ' \u00b7 ' + plural(waiting) + ' saved' : ''); cls = 'off';
      action = function () { location.href = OFFLINE_URL; };
    } else if (s.syncing) {
      text = 'Syncing\u2026'; cls = 'sync';
    } else if (waiting) {
      text = plural(waiting) + ' waiting to sync'; cls = 'sync';
      action = function () { P.flush(); };
    }
    if (!text) { el.hidden = true; return; }
    el.textContent = text; el.className = cls; el.hidden = false; el.onclick = action;
  }
  P.listen(render);

  /* ---- keep catalogue fresh and the outbox empty ---- */
  function sync() {
    if (!signedIn && !onShell) return;
    if (!P.isOnline()) { P.emit(false); return; }
    P.refreshSnapshot().catch(function () {}).then(function () { return P.flush(); }).catch(function () {});
  }
  window.addEventListener('online', sync);
  window.addEventListener('offline', function () { P.emit(false); });
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') sync(); });
  setInterval(function () {
    if (!P.isOnline()) return;
    P.status().then(function (s) { if (s.pending && !s.authRequired && !s.paymentRequired) P.flush().catch(function () {}); });
  }, 30000);
  document.addEventListener('DOMContentLoaded', function () { sync(); P.emit(false); });

  /* ---- signing out locks offline selling on this device (unsynced sales are kept) ---- */
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href*="/users/logout"]');
    if (!a) return;
    e.preventDefault();
    var go = function () { location.href = a.href; };
    Promise.race([P.lock(), new Promise(function (r) { setTimeout(r, 400); })]).then(go, go);
  }, true);

  /* ---- online terminal -> offline terminal, carrying the cart ---- */
  window.POSOfflineBoot = {
    // lines: [{variant_id, price, quantity}] as embedded in the cart panel; add: {variantId} the click that failed
    handoff: function (lines, add) {
      var mapped = (lines || []).map(function (l) { return { variantId: l.variant_id, qty: l.quantity, price: l.price }; });
      return P.setHandoff({ lines: mapped, add: add || null }).then(function () { location.href = OFFLINE_URL; });
    },
    lock: function () { return P.lock(); }
  };
})();
