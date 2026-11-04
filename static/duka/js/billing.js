(() => {
  const statusLine = document.getElementById('status-line');
  const alertBox = document.getElementById('alert');
  const invoicePanel = document.getElementById('invoice-panel');
  const termPicker = document.getElementById('term-picker');
  const plansEl = document.getElementById('plans');
  const planNote = document.getElementById('plan-note');
  const payBtn = document.getElementById('pay-btn');
  const phoneInput = document.getElementById('phone');
  const stkStatus = document.getElementById('stk-status');
  const rowsEl = document.getElementById('invoice-rows');
  const shopMeta = document.getElementById('shop-meta');
  const selectPlanBtn = document.getElementById('select-plan-btn');
  const stkBlock = document.getElementById('stk-block');

  let pollTimer = null;
  let currentPlans = [];
  let selectedTerm = null;
  let lastData = null;
  let proceeded = false;      // owner clicked a term and is on the pay panel, but no invoice exists yet
  let quietLoading = false;
  let autoTimer = null;
  const AUTO_REFRESH_MS = 10000;
  const noticesEl = document.getElementById('notices');

  function showAlert(msg, kind = 'bad') {
    alertBox.className = `alert show ${kind}`;
    alertBox.textContent = msg;
  }
  function clearAlert() { alertBox.className = 'alert'; alertBox.textContent = ''; }

  // /billing/invoices/<number>/receipt/ -- the PDF download for a paid invoice
  function receiptUrl(number) {
    return `${rowsEl.dataset.receiptBase}${encodeURIComponent(number)}/receipt/`;
  }

  function renderInvoiceRows(invoices) {
    rowsEl.innerHTML = '';
    if (!invoices || invoices.length === 0) {
      rowsEl.appendChild(Duka.el('tr', { class: 'empty-row' }, Duka.el('td', { colspan: '6', text: 'No invoices yet.' })));
      return;
    }
    invoices.forEach(inv => {
      const tr = Duka.el('tr', {}, [
        Duka.el('td', { text: inv.number }),
        Duka.el('td', { text: `${inv.term_months} mo` }),
        Duka.el('td', { text: Duka.dt(inv.issued_at) }),
        Duka.el('td', {}, Duka.statusBadge(inv.status)),
        Duka.el('td', { class: 'num', text: Duka.money(inv.total) }),
        Duka.el('td', {}, inv.status === 'PAID'
          ? Duka.el('a', { href: receiptUrl(inv.number), download: '', text: 'Download',
                           style: 'text-decoration:underline;' })
          : null),
      ]);
      rowsEl.appendChild(tr);
    });
  }
  function renderPlanPicker(plans) {
    currentPlans = plans;
    plansEl.innerHTML = '';
    plans.forEach(p => {
      const btn = Duka.el('button', {
        type: 'button', class: 'plan', 'aria-pressed': String(p.term_months === selectedTerm),
        onclick: () => {
          selectedTerm = p.term_months;
          renderPlanPicker(currentPlans);   // just selects; "Proceed to Payment" moves on
        },
      }, [
        Duka.el('span', { class: 'months', text: `${p.term_months} mo` }),
        Duka.el('span', { class: 'price', text: Duka.money(p.subscription_amount) }),
        Number(p.discount_percent) > 0 ? Duka.el('span', { class: 'save', text: `save ${p.discount_percent}%` }) : null,
      ]);
      plansEl.appendChild(btn);
    });
    const p = plans.find(x => x.term_months === selectedTerm);
    planNote.textContent = p ? `Renewing for ${p.term_months} month(s) costs ${Duka.money(p.total)}.` : '';
  }
  function creditNow() { return Number((lastData && lastData.credit_balance) || 0); }

  function showAmounts(total, due, credit) {
    const fullRow = document.getElementById('inv-full-row');
    const creditRow = document.getElementById('inv-credit-row');
    const hasCredit = credit > 0;
    fullRow.hidden = creditRow.hidden = !hasCredit;
    if (hasCredit) {
      document.getElementById('inv-full').textContent = Duka.money(total);
      document.getElementById('inv-credit').textContent = '- ' + Duka.money(credit);
    }
    document.getElementById('inv-total').textContent = Duka.money(due);
  }

  function renderOpenInvoice(inv, plans) {
    if (!inv) {
      if (proceeded) return;
      invoicePanel.hidden = true;
      termPicker.hidden = false;
      selectedTerm = selectedTerm || (plans[0] && plans[0].term_months);
      renderPlanPicker(plans);
      return;
    }
    proceeded = false;
    termPicker.hidden = true;
    invoicePanel.hidden = false;
    document.getElementById('inv-number').textContent = inv.number;
    document.getElementById('inv-term').textContent = `${inv.term_months} month(s)`;
    showAmounts(Number(inv.total), Number(inv.amount_due), Number(inv.credit_applied || 0));
  }

 
  function renderNotices(d) {
    noticesEl.innerHTML = '';
    const add = (card) => noticesEl.appendChild(card);
    const inv = d.open_invoice;
    const credit = Number(d.credit_balance || 0);
    const daysLeft = Duka.daysUntil(d.current_period_end);

    if (d.status === 'SUSPENDED' || d.status === 'CANCELLED' || (!d.has_access && d.status !== 'PENDING')) {
      add(Duka.notice('bad', 'Account locked',
        'Your team can\u2019t use the system until the subscription is paid. ' +
        (inv ? `Pay ${Duka.money(inv.amount_due)} below to restore access.` : 'Choose a plan below to restore access.')));
    } else if (d.status === 'PAST_DUE') {
      const left = Duka.daysUntil(d.access_deadline);
      add(Duka.notice('bad', 'Payment overdue',
        `Your plan ended on ${Duka.dt(d.current_period_end)}. ` +
        (left !== null && left > 0
          ? `You can keep working for ${left} more day${left === 1 ? '' : 's'}, then the account locks.`
          : 'The account is about to lock.') +
        (inv ? ` Pay ${Duka.money(inv.amount_due)} to stay active.` : '')));
    } else if (d.status === 'ACTIVE' && inv && daysLeft !== null && daysLeft <= 7) {
      add(Duka.notice('warn', `Renewal due in ${Math.max(daysLeft, 0)} day${daysLeft === 1 ? '' : 's'}`,
        `Your plan runs until ${Duka.dt(d.current_period_end)}. Pay ${Duka.money(inv.amount_due)} to renew.`));
    }

   
    if (inv && Number(inv.credit_applied) > 0 && Number(inv.amount_due) > 0) {
      add(Duka.notice('warn', `${Duka.money(inv.amount_due)} still due on ${inv.number}`,
        `We\u2019ve received ${Duka.money(inv.credit_applied)} so far. Pay the rest, then enter the new M-Pesa code below.`,
        { meter: { value: Number(inv.credit_applied), max: Number(inv.total),
                   label: `${Duka.money(inv.credit_applied)} of ${Duka.money(inv.total)} received` } }));
    } else if (credit > 0 && !inv) {
      add(Duka.notice('good', `You have ${Duka.money(credit)} in credit`,
        'It will be deducted from your next invoice, and paid automatically if it covers the whole amount.'));
    }

    (d.manual_requests || []).filter((r) => r.status === 'PENDING').forEach((r) => {
      add(Duka.notice('info', 'Waiting for payment confirmation',
        `We haven\u2019t seen M-Pesa code ${r.code} yet. Your account switches on automatically once we confirm it. You can leave this page.`));
    });
  }

  function renderCreditLedger(d) {
    const box = document.getElementById('credit-ledger');
    const rows = document.getElementById('credit-rows');
    const entries = d.credit_entries || [];
    rows.innerHTML = '';
    box.hidden = entries.length === 0;
    entries.forEach((e) => {
      const amt = Number(e.amount);
      const what = e.kind === 'TOPUP' ? `Payment received${e.mpesa_code__code ? ' (' + e.mpesa_code__code + ')' : ''}`
        : e.kind === 'APPLIED' ? `Applied to ${e.invoice__number || 'an invoice'}`
        : (e.note || 'Adjustment');
      rows.appendChild(Duka.el('tr', {}, [
        Duka.el('td', { text: Duka.dt(e.created_at) }),
        Duka.el('td', { text: what }),
        Duka.el('td', { class: 'num', text: (amt > 0 ? '+ ' : '- ') + Duka.money(Math.abs(amt)) }),
      ]));
    });
  }
  selectPlanBtn.addEventListener('click', () => {
    if (!selectedTerm) {
      showAlert('Please select a term option.');
      return;
    }
    const plan = currentPlans.find(p => p.term_months === selectedTerm);
    if (!plan) return;
    proceeded = true;

    termPicker.hidden = true;
    invoicePanel.hidden = false;

    document.getElementById('inv-number').textContent = 'Pending...';
    document.getElementById('inv-term').textContent = `${plan.term_months} month(s)`;
    showAmounts(Number(plan.total), Math.max(Number(plan.total) - creditNow(), 0), Math.min(creditNow(), Number(plan.total)));
    openPayPopup(plan.term_months);
  });

  // The popup itself lives in manual-pay.js; this just tells it which term to show.
  function openPayPopup(term) {
    window.dispatchEvent(new CustomEvent('billing:pay', { detail: { term } }));
  }
  document.getElementById('open-pay-btn').addEventListener('click', () => {
    const inv = lastData && lastData.open_invoice;
    openPayPopup(inv && !proceeded ? inv.term_months : selectedTerm);
  });

  // What the owner can see changing on this page. If this is identical, a background refresh does nothing.
  function signature(d) {
    return JSON.stringify([
      d.status, d.has_access, d.current_period_end, d.credit_balance, d.open_invoice,
      (d.invoices || []).map(i => [i.number, i.status]),
      (d.manual_requests || []).map(r => [r.id, r.status]),
      (d.credit_entries || []).map(e => e.id),
    ]);
  }

  async function load({ quiet = false } = {}) {
    try {
      const data = await Duka.api('/billing/');
      const prev = lastData;
      if (quiet && prev && signature(prev) === signature(data)) return;   // nothing new
      lastData = data;
      stkBlock.hidden = !data.stk_enabled;     // STK push stays hidden until it is switched on

      // Did the platform admin just confirm the payment while this page was open?
      const activated = !!prev && data.has_access && data.status === 'ACTIVE'
        && (!prev.has_access || prev.status !== 'ACTIVE' || prev.current_period_end !== data.current_period_end);
      if (activated) proceeded = false;
      const daysLeft = Duka.daysUntil(data.current_period_end);
      shopMeta.textContent = data.has_access ? 'Account active' : 'Account locked';

      let line;
      if (data.status === 'PENDING') line = 'Your account is not yet active -- pay the invoice below to switch on.';
      else if (data.status === 'TRIALING') line = `You're on a free trial until ${Duka.dt(data.current_period_end)}.`;
      else if (data.status === 'ACTIVE' && daysLeft !== null && daysLeft <= 7)
        line = `Your plan runs out in ${daysLeft} day(s), on ${Duka.dt(data.current_period_end)}.`;
      else if (data.status === 'ACTIVE') line = `Active until ${Duka.dt(data.current_period_end)}.`;
      else if (data.status === 'PAST_DUE') line = `Payment is overdue since ${Duka.dt(data.current_period_end)} -- pay now to avoid being locked out.`;
      else if (data.status === 'SUSPENDED') line = 'Your account is locked. Pay the invoice below to restore access.';
      else if (data.status === 'CANCELLED') line = 'Your subscription was cancelled. Choose a plan below to start again.';
      else line = '';
      statusLine.textContent = line;

      renderOpenInvoice(data.open_invoice, data.plans);
      renderInvoiceRows(data.invoices);
      renderNotices(data);
      renderCreditLedger(data);

      if (quiet && prev) {
        if (activated) {
          const paid = (data.invoices || []).find(i => i.status === 'PAID');
          showAlert('Payment confirmed -- your account is active.', 'good');
          if (paid) {
            alertBox.appendChild(document.createTextNode(' '));
            alertBox.appendChild(Duka.el('a', { href: receiptUrl(paid.number), download: '',
                                                text: 'Download your receipt', style: 'text-decoration:underline;' }));
          }
          window.dispatchEvent(new CustomEvent('billing:activated', { detail: data }));
        }
        window.dispatchEvent(new Event('billing:changed'));   // let the M-Pesa code section refresh too
      }
    } catch (e) {
      if (!quiet) { showAlert(e.message); return; }
      if (e.status === 401 || e.status === 403) {      // session ended: stop polling and say so
        clearInterval(autoTimer);
        showAlert('Your session has expired. Please log in again.');
      }                                               // other errors (offline, 5xx): try again next tick
    }
  }

  // Keep the page current: the shop owner never has to reload after the platform admin confirms a payment.
  async function quietRefresh() {
    if (document.hidden || quietLoading) return;
    quietLoading = true;
    try { await load({ quiet: true }); } finally { quietLoading = false; }
  }

  payBtn.addEventListener('click', async () => {
    clearAlert();
    const phone = phoneInput.value.trim();
    if (!phone) { showAlert('Enter the M-Pesa number to pay from.'); return; }
    payBtn.disabled = true;
    payBtn.textContent = 'Sending\u2026';
    stkStatus.innerHTML = '';
    try {
      const openInvoiceTerm = document.getElementById('inv-term').textContent;
      const data = await Duka.api('/billing/checkout/', {
        method: 'POST', json: false,
        body: new URLSearchParams({ term_months: parseInt(openInvoiceTerm) || '', phone }),
      });
      stkStatus.innerHTML = '<span class="spinner"></span>Check your phone and enter your M-Pesa PIN\u2026';
      pollPayment(data.payment_id);
    } catch (e) {
      showAlert(e.message);
      payBtn.disabled = false;
      payBtn.textContent = 'Send STK push';
    }
  });

  function pollPayment(paymentId) {
    let attempts = 0;
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      attempts += 1;
      try {
        const data = await Duka.api(`/billing/payments/${paymentId}/`);
        if (data.status === 'SUCCESS') {
          clearInterval(pollTimer);
          stkStatus.textContent = '';
          showAlert('Payment received -- your account is active.', 'good');
          if (data.receipt_url) {
            alertBox.appendChild(document.createTextNode(' '));
            alertBox.appendChild(Duka.el('a', { href: data.receipt_url, download: '',
                                                text: 'Download your receipt', style: 'text-decoration:underline;' }));
          }
          payBtn.disabled = false;
          payBtn.textContent = 'Send STK push';
          load();
        } else if (data.status === 'FAILED' || data.status === 'CANCELLED') {
          clearInterval(pollTimer);
          stkStatus.textContent = '';
          showAlert(data.result_desc || 'The payment did not go through -- try again.');
          payBtn.disabled = false;
          payBtn.textContent = 'Send STK push';
        } else if (attempts > 40) {
          clearInterval(pollTimer);
          stkStatus.textContent = "Still waiting -- if you approved it on your phone, refresh in a minute.";
          payBtn.disabled = false;
          payBtn.textContent = 'Send STK push';
        }
      } catch (e) {
        clearInterval(pollTimer);
        stkStatus.textContent = '';
        showAlert(e.message);
        payBtn.disabled = false;
        payBtn.textContent = 'Send STK push';
      }
    }, 3000);
  }

  window.addEventListener('billing:refresh', () => load());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) quietRefresh(); });
  window.addEventListener('online', quietRefresh);
  autoTimer = setInterval(quietRefresh, AUTO_REFRESH_MS);
  load();
})();
