
(function () {
  'use strict';
  var P = window.POSOffline;
  if (!P) return;

  var body = document.body;
  var onShell = body && body.dataset.posShell === '1';
  var signedIn = body && body.dataset.posUser === '1';
  var OFFLINE_URL = '/pos/offline/';

  
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

  
  var modal = null, dismissed = false;
  function injectModalCss() {
    if (document.getElementById('posOfflineModalCss')) return;
    var css = document.createElement('style');
    css.id = 'posOfflineModalCss';
    css.textContent = '#posOfflineModal{position:fixed;inset:0;background:rgba(15,23,42,.6);z-index:10000;display:flex;' +
      'align-items:center;justify-content:center;padding:1rem;font-family:system-ui,sans-serif}' +
      '#posOfflineModal .box{background:#fff;border-radius:14px;padding:1.5rem;max-width:400px;width:100%;text-align:center;' +
      'box-shadow:0 20px 40px rgba(0,0,0,.3)}#posOfflineModal h3{margin:.2rem 0 .6rem;font-size:1.2rem;color:#111827}' +
      '#posOfflineModal p{color:#4b5563;line-height:1.5;margin:0 0 1.2rem;font-size:.95rem}' +
      '#posOfflineModal .btns{display:flex;flex-direction:column;gap:.6rem}' +
      '#posOfflineModal button{border:0;border-radius:8px;padding:.8rem 1rem;font:600 1rem system-ui,sans-serif;cursor:pointer}' +
      '#posOfflineModal .go{background:#10b981;color:#fff}#posOfflineModal .wait{background:#e2e8f0;color:#334155}';
    document.head.appendChild(css);
  }
  function closePrompt() { if (modal) { modal.remove(); modal = null; } }
  function pageCart() {
    var panel = document.getElementById('cart-panel');
    try { return JSON.parse((panel && panel.dataset.cart) || '[]'); } catch (e) { return []; }
  }

  function prompt(lines, add) {
    if (onShell || !signedIn || modal) return Promise.resolve();
    if (/\/checkout\/pay/.test(location.pathname)) return Promise.resolve();
    return P.getState().then(function (st) {
      injectModalCss();
      modal = document.createElement('div');
      modal.id = 'posOfflineModal';
      modal.setAttribute('role', 'alertdialog');
      modal.setAttribute('aria-modal', 'true');
      var ready = st.ok;
      modal.innerHTML = '<div class="box"><div style="font-size:2rem">&#128246;</div><h3>You are offline</h3><p>' +
        (ready
          ? 'The internet connection was lost. You can keep selling on the offline POS terminal; your cart comes with you and sales sync automatically when the connection is back.'
          : 'The internet connection was lost. The offline POS is not ready on this device (sign in and open the terminal once while online), so please wait for the connection to return.') +
        '</p><div class="btns">' +
        (ready ? '<button type="button" class="go">Go to offline POS terminal</button>' : '') ;
      document.body.appendChild(modal);
      var go = modal.querySelector('.go');
      if (go) go.onclick = function () {
        closePrompt();
        window.POSOfflineBoot.handoff(lines || pageCart(), add || null);
      };
      modal.querySelector('.wait').onclick = function () { dismissed = true; closePrompt(); };
    });
  }
  window.addEventListener('offline', function () { dismissed = false; prompt(); });
  window.addEventListener('online', function () { dismissed = false; closePrompt(); });

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

  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href*="/users/logout"]');
    if (!a) return;
    e.preventDefault();
    var go = function () { location.href = a.href; };
    Promise.race([P.lock(), new Promise(function (r) { setTimeout(r, 400); })]).then(go, go);
  }, true);

  window.POSOfflineBoot = {
    handoff: function (lines, add) {
      var mapped = (lines || []).map(function (l) { return { variantId: l.variant_id, qty: l.quantity, price: l.price }; });
      return P.setHandoff({ lines: mapped, add: add || null }).then(function () { location.href = OFFLINE_URL; });
    },
    prompt: function (lines, add) { return dismissed ? Promise.resolve() : prompt(lines, add); },
    lock: function () { return P.lock(); }
  };
  document.addEventListener('DOMContentLoaded', function () { if (P.isOnline() === false) prompt(); });
})();
