/* "Already paid? Enter your M-Pesa code" panel on /billing/.
   Reads /billing/ as JSON and posts to the manual-payment endpoint.
   Feedback is a dismissible card under the button that clears as soon as the shop edits the
   code; what the shop needs to remember (balance due, credit, waiting codes) is shown in the
   notice cards at the top of the page, which billing.js draws from live figures. */
(() => {
  const panel = document.getElementById('manual-pay');
  if (!panel) return;
  const $ = (id) => document.getElementById(id);
  const termSel = $('mp-term'), codeIn = $('mp-code'), btn = $('mp-btn'), result = $('mp-result');
  const codeRow = $('mp-code-row'), creditRow = $('mp-credit-row'), creditBtn = $('mp-credit-btn');
  const attemptsBox = $('mp-attempts'), attemptList = $('mp-attempt-list');
  let overview = null, hideTimer = null;

  const money = (v) => Duka.money(v);
  const when = (iso) => iso ? new Date(iso).toLocaleDateString('en-KE', { dateStyle: 'medium' }) : '';

  /* ---- small helpers ---- */
  function csrf() {
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    const el = document.querySelector('input[name=csrfmiddlewaretoken]');
    return el ? el.value : '';
  }

  const DISMISS_KEY = 'billing.dismissedAttempts';
  function dismissed() {
    try { return JSON.parse(localStorage.getItem(DISMISS_KEY) || '[]'); } catch (_) { return []; }
  }
  function dismiss(ids) {
    try { localStorage.setItem(DISMISS_KEY, JSON.stringify(dismissed().concat(ids).slice(-50))); } catch (_) { /* storage off */ }
  }

  function clearResult() {
    clearTimeout(hideTimer);
    result.textContent = '';
  }
  function showResult(kind, title, body, autoHideMs) {
    clearResult();
    result.appendChild(Duka.notice(kind, title, body, { dismiss: clearResult }));
    if (autoHideMs) hideTimer = setTimeout(clearResult, autoHideMs);
  }

  function currentPlan() {
    return (overview.plans || []).find((p) => String(p.term_months) === termSel.value);
  }

  // What the shop would still pay for the selected term, after credit.
  function dueForSelected() {
    const plan = currentPlan();
    if (!plan) return null;
    const inv = overview.open_invoice;
    if (inv && String(inv.term_months) === termSel.value) return Number(inv.amount_due);
    return Math.max(Number(plan.total) - Number(overview.credit_balance || 0), 0);
  }

  /* ---- rendering ---- */
  function renderSteps() {
    const mp = overview.manual_pay || {};
    const due = dueForSelected();
    const credit = Number(overview.credit_balance || 0);
    const covered = due !== null && due <= 0;
    codeRow.hidden = covered;
    creditRow.hidden = !covered;

    const steps = $('mp-steps');
    if (covered) {
      steps.textContent = 'Your credit of ' + money(credit) + ' covers this term. No payment is needed: ' +
        'press the button to renew from your credit.';
      return;
    }
    const where = mp.number
      ? mp.label + ' ' + mp.number + (mp.name ? ' (' + mp.name + ')' : '')
      : 'the number given to you by support';
    const inv = overview.open_invoice ? ' Use ' + overview.open_invoice.number + ' as the account number if asked.' : '';
    steps.textContent = '1. Pay ' + (due !== null ? money(due) : 'the amount') + ' to ' + where + '.' + inv +
      ' 2. M-Pesa sends you an SMS that starts with a 10-character code. 3. Type that code below.' +
      (credit > 0 ? ' Your credit of ' + money(credit) + ' has already been deducted.' : '') +
      ' Paying less keeps the money as credit and you pay the rest the same way; paying more keeps the extra as credit for your next invoice.';
  }

  function renderAttempts() {
    const hidden = new Set(dismissed());
    const rows = (overview.manual_requests || []).filter((r) => r.status === 'REJECTED' && !hidden.has(r.id));
    attemptList.textContent = '';
    attemptsBox.hidden = rows.length === 0;
    if (!rows.length) return;
    $('mp-attempts-summary').textContent = 'Recent attempts that weren\u2019t accepted (' + rows.length + ')';
    rows.forEach((r) => {
      attemptList.appendChild(Duka.el('li', {}, [
        Duka.el('span', { class: 'code', text: r.code }),
        ' \u2013 ' + (r.reason || 'not approved') + ' (' + when(r.created_at) + ')',
      ]));
    });
    $('mp-attempts-clear').onclick = () => { dismiss(rows.map((r) => r.id)); renderAttempts(); };
  }

  async function load() {
    try {
      const res = await fetch(panel.dataset.overview, {
        credentials: 'same-origin', headers: { 'Accept': 'application/json' } });
      if (!res.ok) return;
      overview = await res.json();
    } catch (_) { return; }
    if (!overview.manual_pay || !overview.manual_pay.enabled) return;

    const keep = termSel.value;
    termSel.innerHTML = '';
    (overview.plans || []).forEach((p) => {
      const o = document.createElement('option');
      o.value = p.term_months;
      o.textContent = p.term_months + (p.term_months === 1 ? ' month' : ' months') + ' - ' + money(p.total);
      termSel.appendChild(o);
    });
    const preferred = keep || (overview.open_invoice ? overview.open_invoice.term_months : overview.term_months);
    if (preferred) termSel.value = String(preferred);
    termSel.onchange = renderSteps;

    renderSteps();
    renderAttempts();
    panel.hidden = false;
  }

  // STK push isn't live yet (Daraja pending): intercept the button before billing.js sees it.
  document.addEventListener('click', (e) => {
    const target = e.target.closest && e.target.closest('#pay-btn');
    if (!target || (overview && overview.stk_enabled)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    const box = document.getElementById('stk-status');
    const msg = (overview && overview.stk_message) ||
      'STK push payment is still in development. We\u2019ll make it available as soon as it\u2019s ready.';
    if (box) { box.textContent = msg; box.style.color = '#a15c00'; }
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, true);

  /* ---- submit ---- */
  async function submit(useCredit) {
    const code = codeIn.value.trim();
    if (!useCredit && !code) {
      showResult('bad', 'Enter your code', 'Type the code from your M-Pesa message.');
      return;
    }
    btn.disabled = creditBtn.disabled = true;
    showResult('info', 'Checking\u2026', '');
    try {
      const res = await fetch(panel.dataset.url, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Accept': 'application/json', 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
        body: JSON.stringify({ code: useCredit ? '' : code, term_months: Number(termSel.value) }),
      });
      let d = {};
      try { d = await res.json(); } catch (_) { /* non-JSON error */ }

      if (res.status === 429 || (res.status === 403 && !d.error)) {
        showResult('bad', 'Too many attempts', 'Please wait a minute and try again.');
      } else if (d.status === 'APPROVED') {
        showResult('good', 'Payment confirmed',
          'Your subscription is active' + (d.period_end ? ' until ' + when(d.period_end) : '') + '.' +
          (d.reason ? ' ' + d.reason : ''), 12000);
        codeIn.value = '';
      } else if (d.status === 'PARTIAL') {
        showResult('warn', 'Part payment received', d.reason);   // live balance sits in the card at the top
        codeIn.value = '';
      } else if (d.status === 'PENDING') {
        showResult('info', 'Waiting for confirmation',
          'We haven\u2019t received confirmation of that payment yet. Your account will switch on automatically as soon as we do. You can leave this page.');
        codeIn.value = '';
      } else {
        showResult('bad', 'Code not accepted', d.reason || d.error || 'Something went wrong. Please try again.');
      }
      window.dispatchEvent(new Event('billing:refresh'));
      load();
    } catch (_) {
      showResult('bad', 'No connection', 'Could not reach the server. Check your connection and try again.');
    } finally { btn.disabled = creditBtn.disabled = false; }
  }

  btn.addEventListener('click', () => submit(false));
  creditBtn.addEventListener('click', () => submit(true));
  codeIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(false); } });
  codeIn.addEventListener('input', clearResult);      // start a new attempt -> old verdict goes away
  load();
})();
