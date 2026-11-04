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
  function showResult(kind, title, body, autoHideMs, receiptUrl) {
    clearResult();
    const card = Duka.notice(kind, title, body, { dismiss: clearResult });
    if (receiptUrl) {
      card.querySelector('.notice-main').appendChild(
        Duka.el('a', { href: receiptUrl, download: '', text: 'Download receipt (PDF)',
                       style: 'display:inline-block;margin-top:8px;text-decoration:underline;' }));
    }
    result.appendChild(card);
    if (autoHideMs) hideTimer = setTimeout(clearResult, autoHideMs);
  }

  function currentPlan() {
    return (overview.plans || []).find((p) => String(p.term_months) === termSel.value);
  } 

  function dueFor(term) {
    const plan = (overview.plans || []).find((p) => String(p.term_months) === String(term));
    if (!plan) return null;
    const inv = overview.open_invoice;
    if (inv && String(inv.term_months) === String(term)) return Number(inv.amount_due);
    return Math.max(Number(plan.total) - Number(overview.credit_balance || 0), 0);
  }
  function dueForSelected() { return dueFor(termSel.value); }

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

  async function post(code, term) {
    const res = await fetch(panel.dataset.url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify({ code, term_months: Number(term) }),
    });
    let d = {};
    try { d = await res.json(); } catch (_) { /* non-JSON error */ }
    return { res, d };
  }

  // One place decides what each server answer means, for both the inline form and the popup.
  function outcome(res, d) {
    if (res.status === 429 || (res.status === 403 && !d.error)) {
      return { kind: 'bad', title: 'Too many attempts', body: 'Please wait a minute and try again.' };
    }
    if (d.status === 'APPROVED') {
      return { kind: 'good', title: 'Payment confirmed', ok: true, clear: true, receipt: d.receipt_url,
        body: 'Your subscription is active' + (d.period_end ? ' until ' + when(d.period_end) : '') + '.' +
              (d.reason ? ' ' + d.reason : '') };
    }
    if (d.status === 'PARTIAL') return { kind: 'warn', title: 'Part payment received', body: d.reason, clear: true };
    if (d.status === 'PENDING') {
      return { kind: 'info', title: 'Waiting for confirmation', clear: true,
        body: 'We haven\u2019t received confirmation of that payment yet. Your account will switch on automatically as soon as we do. You can leave this page.' };
    }
    return { kind: 'bad', title: 'Code not accepted', body: d.reason || d.error || 'Something went wrong. Please try again.' };
  }

  async function submit(useCredit) {
    const code = codeIn.value.trim();
    if (!useCredit && !code) {
      showResult('bad', 'Enter your code', 'Type the code from your M-Pesa message.');
      return;
    }
    btn.disabled = creditBtn.disabled = true;
    showResult('info', 'Checking\u2026', '');
    try {
      const { res, d } = await post(useCredit ? '' : code, termSel.value);
      const o = outcome(res, d);
      showResult(o.kind, o.title, o.body, o.ok && !o.receipt ? 12000 : 0, o.receipt);
      if (o.clear) codeIn.value = '';
      window.dispatchEvent(new Event('billing:refresh'));
      load();
    } catch (_) {
      showResult('bad', 'No connection', 'Could not reach the server. Check your connection and try again.');
    } finally { btn.disabled = creditBtn.disabled = false; }
  }

  // ---------- Pay popup: how to pay the pre-selected amount + code field ----------
  const modal = $('pay-modal'), pmCode = $('pm-code'), pmBtn = $('pm-btn'), pmCreditBtn = $('pm-credit-btn');
  const pmResult = $('pm-result'), pmSteps = $('pm-steps');
  let pmTerm = null, pmReturnFocus = null, pmDone = false;

  function pmShow(kind, title, body, receiptUrl) {
    pmResult.textContent = '';
    const card = Duka.notice(kind, title, body);
    if (receiptUrl) {
      card.querySelector('.notice-main').appendChild(
        Duka.el('a', { href: receiptUrl, download: '', text: 'Download receipt (PDF)',
                       style: 'display:inline-block;margin-top:8px;text-decoration:underline;' }));
    }
    pmResult.appendChild(card);
  }

  function pmRender() {
    const due = dueFor(pmTerm);
    const plan = (overview.plans || []).find((p) => String(p.term_months) === String(pmTerm));
    const mp = overview.manual_pay || {};
    const credit = Number(overview.credit_balance || 0);
    const inv = overview.open_invoice && String(overview.open_invoice.term_months) === String(pmTerm)
      ? overview.open_invoice : null;
    const covered = due !== null && due <= 0;

    $('pm-title').textContent = 'Pay for ' + pmTerm + (Number(pmTerm) === 1 ? ' month' : ' months');
    $('pm-amount').textContent = money(due === null ? 0 : due);
    $('pm-sub').textContent = credit > 0 && plan && !covered
      ? 'Plan price ' + money(plan.total) + ' minus your credit of ' + money(Math.min(credit, Number(plan.total))) + '.' : '';
    $('pm-code-row').hidden = covered;
    $('pm-credit-row').hidden = !covered;
    $('pm-foot').hidden = covered;

    pmSteps.textContent = '';
    if (covered) {
      pmSteps.appendChild(Duka.el('li', {}, 'Your credit of ' + money(credit) + ' covers this term. No payment is needed. Press the button below to renew from your credit.'));
      return;
    }
    const where = mp.number ? (mp.label || 'Number') + ' ' : '';
    const payLi = Duka.el('li', {}, ['Open M-Pesa on your phone and pay ', Duka.el('strong', { text: money(due) }),
      mp.number ? ' to ' + where : ' to the number given to you by support']);
    if (mp.number) {
      payLi.appendChild(Duka.el('strong', { text: mp.number }));
      if (mp.name) payLi.appendChild(document.createTextNode(' (' + mp.name + ')'));
      payLi.appendChild(Duka.el('button', { type: 'button', class: 'copy', text: 'Copy', onclick: (e) => {
        const b = e.currentTarget;
        const done = () => { b.textContent = 'Copied'; setTimeout(() => { b.textContent = 'Copy'; }, 1500); };
        try { navigator.clipboard.writeText(mp.number).then(done, () => {}); } catch (_) { /* no clipboard */ }
      } }));
    }
    pmSteps.appendChild(payLi);
    if (inv) {
      pmSteps.appendChild(Duka.el('li', {}, ['If it asks for an account number, use ', Duka.el('strong', { text: inv.number }), '.']));
    }
    pmSteps.appendChild(Duka.el('li', {}, 'M-Pesa sends you an SMS that starts with a 10-character code (for example SJK3L9X2QP).'));
    pmSteps.appendChild(Duka.el('li', {}, 'Type that code below and press Confirm payment.'));
  }

  async function openPay(term) {
    await load();                                  // fresh amounts, credit and invoice
    if (!overview || !overview.manual_pay || !overview.manual_pay.enabled) return;
    pmTerm = term || termSel.value;
    termSel.value = String(pmTerm);
    renderSteps();
    pmDone = false;
    pmResult.textContent = '';
    pmCode.value = '';
    pmBtn.textContent = 'Confirm payment';
    pmCreditBtn.textContent = 'Pay with my credit';
    pmCode.disabled = false;
    pmBtn.disabled = pmCreditBtn.disabled = false;
    pmRender();
    pmReturnFocus = document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
    setTimeout(() => { (pmCode.offsetParent ? pmCode : pmCreditBtn).focus(); }, 30);
  }

  function closePay() {
    if (modal.hidden) return;
    modal.hidden = true;
    document.body.style.overflow = '';
    if (pmReturnFocus && pmReturnFocus.focus) pmReturnFocus.focus();
  }

  async function pmSubmit(useCredit) {
    if (pmDone) { closePay(); return; }
    const code = pmCode.value.trim();
    if (!useCredit && !code) { pmShow('bad', 'Enter your code', 'Type the code from your M-Pesa message.'); return; }
    pmBtn.disabled = pmCreditBtn.disabled = true;
    pmShow('info', 'Checking\u2026', '');
    try {
      const { res, d } = await post(useCredit ? '' : code, pmTerm);
      const o = outcome(res, d);
      pmShow(o.kind, o.title, o.body, o.receipt);
      if (o.clear) pmCode.value = '';
      if (o.ok) {
        pmDone = true; pmBtn.textContent = pmCreditBtn.textContent = 'Done';
        pmCode.disabled = true;
      }
      window.dispatchEvent(new Event('billing:refresh'));
      await load();
      if (!o.ok && !modal.hidden) pmRender();   // e.g. a part payment lowers the amount still due
    } catch (_) {
      pmShow('bad', 'No connection', 'Could not reach the server. Check your connection and try again.');
    } finally {
      pmBtn.disabled = pmCreditBtn.disabled = false;
    }
  }

  pmBtn.addEventListener('click', () => pmSubmit(false));
  pmCreditBtn.addEventListener('click', () => pmSubmit(true));
  pmCode.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); pmSubmit(false); } });
  pmCode.addEventListener('input', () => { pmResult.textContent = ''; });
  $('pm-close').addEventListener('click', closePay);
  $('pm-backdrop').addEventListener('click', closePay);
  document.addEventListener('keydown', (e) => {
    if (modal.hidden) return;
    if (e.key === 'Escape') { closePay(); return; }
    if (e.key === 'Tab') {                        // keep keyboard focus inside the popup
      const f = [...modal.querySelectorAll('button, input')].filter((x) => !x.hidden && x.offsetParent !== null && !x.disabled);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  window.addEventListener('billing:pay', (e) => openPay(e.detail && e.detail.term));
  // Admin confirmed the payment while the popup was open
  window.addEventListener('billing:activated', () => {
    if (modal.hidden || pmDone) return;
    pmDone = true; pmBtn.textContent = pmCreditBtn.textContent = 'Done';
    pmCode.disabled = true;
    pmShow('good', 'Payment confirmed', 'Your subscription is now active.');
  });

  // Background refresh from billing.js: re-read the overview; clear the stale "waiting" card once confirmed.
  window.addEventListener('billing:changed', () => load());
  window.addEventListener('billing:activated', () => clearResult());

  btn.addEventListener('click', () => submit(false));
  creditBtn.addEventListener('click', () => submit(true));
  codeIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(false); } });
  codeIn.addEventListener('input', clearResult);      
  load();
})();
