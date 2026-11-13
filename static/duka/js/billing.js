(() => {
  const statusLine = document.getElementById('status-line');
  const alertBox = document.getElementById('alert');
  const invoicePanel = document.getElementById('invoice-panel');
  const termPicker = document.getElementById('term-picker');
  const plansEl = document.getElementById('plans');
  const planNote = document.getElementById('plan-note');
  const payBtn = document.getElementById('pay-btn');
  const payStatus = document.getElementById('pay-status');
  const termSelect = document.getElementById('inv-term-select');
  const rowsEl = document.getElementById('invoice-rows');
  const shopMeta = document.getElementById('shop-meta');
  const selectPlanBtn = document.getElementById('select-plan-btn');

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
        `We\u2019ve received ${Duka.money(inv.credit_applied)} so far. Pay the rest with Paystack to finish.`,
        { meter: { value: Number(inv.credit_applied), max: Number(inv.total),
                   label: `${Duka.money(inv.credit_applied)} of ${Duka.money(inv.total)} received` } }));
    } else if (credit > 0 && !inv) {
      add(Duka.notice('good', `You have ${Duka.money(credit)} in credit`,
        'It will be deducted from your next invoice, and paid automatically if it covers the whole amount.'));
    }

    if (d.pending_payment && !(inv && Number(inv.credit_applied) > 0 && Number(inv.amount_due) > 0)) {
      add(Duka.notice('info', 'Waiting for your Paystack payment',
        'If you finished paying, this page updates by itself within a few seconds. If you closed the Paystack page, press Pay again.'));
    }
  }

  function renderCreditLedger(d) {
    const box = document.getElementById('credit-ledger');
    const rows = document.getElementById('credit-rows');
    const entries = d.credit_entries || [];
    rows.innerHTML = '';
    box.hidden = entries.length === 0;
    entries.forEach((e) => {
      const amt = Number(e.amount);
      const ref = e.paystack_payment__reference || e.mpesa_code__code;
      const what = e.kind === 'TOPUP' ? `Payment received${ref ? ' (' + ref + ')' : ''}`
        : e.kind === 'APPLIED' ? `Applied to ${e.invoice__number || 'an invoice'}`
        : (e.note || 'Adjustment');
      rows.appendChild(Duka.el('tr', {}, [
        Duka.el('td', { text: Duka.dt(e.created_at) }),
        Duka.el('td', { text: what }),
        Duka.el('td', { class: 'num', text: (amt > 0 ? '+ ' : '- ') + Duka.money(Math.abs(amt)) }),
      ]));
    });
  }
  // ---- paying: everything goes through one Paystack checkout --------------------------------
  function setBusy(busy, label) {
    [payBtn, selectPlanBtn].forEach((b) => { if (b) b.disabled = busy; });
    payBtn.textContent = busy ? (label || 'Please wait\u2026') : 'Pay with Paystack';
  }

  async function startCheckout(term) {
    clearAlert();
    setBusy(true, 'Opening Paystack\u2026');
    try {
      const body = new URLSearchParams();
      if (term) body.set('term_months', String(term));
      const data = await Duka.api('/billing/checkout/', { method: 'POST', json: false, body });
      if (data.status === 'PAID') {                      // credit covered the whole invoice
        showAlert('Paid from your credit -- your account is active.', 'good');
        if (data.receipt_url) {
          alertBox.appendChild(document.createTextNode(' '));
          alertBox.appendChild(Duka.el('a', { href: data.receipt_url, download: '',
                                              text: 'Download your receipt', style: 'text-decoration:underline;' }));
        }
        proceeded = false;
        setBusy(false);
        load();
        return;
      }
      if (!/^https:\/\//.test(data.authorization_url || '')) throw new Error('Could not open the payment page. Please try again.');
      payStatus.textContent = 'Taking you to Paystack\u2026';
      window.location.assign(data.authorization_url);
    } catch (e) {
      showAlert(e.message);
      payStatus.textContent = '';
      setBusy(false);
    }
  }

  payBtn.addEventListener('click', () => startCheckout(termSelect.value || (lastData && lastData.term_months)));
  selectPlanBtn.addEventListener('click', () => {
    if (!selectedTerm) { showAlert('Please select a term option.'); return; }
    startCheckout(selectedTerm);
  });

  // The term dropdown on the invoice panel: switching it re-prices the screen (the invoice is re-issued on pay).
  function fillTermSelect(plans, current) {
    const keep = termSelect.value;
    termSelect.innerHTML = '';
    (plans || []).forEach((p) => {
      termSelect.appendChild(Duka.el('option', { value: String(p.term_months),
        text: `${p.term_months} ${p.term_months === 1 ? 'month' : 'months'} - ${Duka.money(p.total)}` }));
    });
    const want = keep || String(current || '');
    if (want && (plans || []).some((p) => String(p.term_months) === want)) termSelect.value = want;
  }
  termSelect.addEventListener('change', () => {
    if (!lastData) return;
    const plan = (lastData.plans || []).find((p) => String(p.term_months) === termSelect.value);
    const inv = lastData.open_invoice;
    if (!plan) return;
    document.getElementById('inv-term').textContent = `${plan.term_months} month(s)`;
    if (inv && String(inv.term_months) === termSelect.value) {
      showAmounts(Number(inv.total), Number(inv.amount_due), Number(inv.credit_applied || 0));
    } else {
      showAmounts(Number(plan.total), Math.max(Number(plan.total) - creditNow(), 0), Math.min(creditNow(), Number(plan.total)));
    }
    const covered = Math.max(Number(plan.total) - creditNow(), 0) <= 0 && !(inv && String(inv.term_months) === termSelect.value);
    payBtn.textContent = (inv && String(inv.term_months) === termSelect.value ? Number(inv.amount_due) <= 0 : covered)
      ? 'Pay with my credit' : 'Pay with Paystack';
  });

  // What the owner can see changing on this page. If this is identical, a background refresh does nothing.
  function signature(d) {
    return JSON.stringify([
      d.status, d.has_access, d.current_period_end, d.credit_balance, d.open_invoice, d.pending_payment,
      (d.invoices || []).map(i => [i.number, i.status]),
      (d.credit_entries || []).map(e => e.id),
    ]);
  }

  async function load({ quiet = false } = {}) {
    try {
      const data = await Duka.api('/billing/');
      const prev = lastData;
      if (quiet && prev && signature(prev) === signature(data)) return;   // nothing new
      lastData = data;

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
      fillTermSelect(data.plans, data.open_invoice ? data.open_invoice.term_months : data.term_months);
      termSelect.dispatchEvent(new Event('change'));
      if (!data.paystack_enabled) payStatus.textContent = 'Online payment is not set up yet -- please contact support.';
      else if (payStatus.textContent.startsWith('Online payment')) payStatus.textContent = '';
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

  // ---- coming back from Paystack (?paystack=success|pending|failed|unknown&ref=...) ----------
  function handleReturn() {
    const params = new URLSearchParams(window.location.search);
    const outcome = params.get('paystack');
    if (!outcome) return;
    const ref = params.get('ref');
    history.replaceState(null, '', window.location.pathname);   // a refresh shouldn't repeat the message

    if (outcome === 'success') {
      showAlert('Payment received -- thank you. Your account is up to date.', 'good');
    } else if (outcome === 'failed') {
      showAlert('The payment did not go through, and you have not been charged. You can try again below.');
    } else if (outcome === 'unknown') {
      showAlert('We could not match that payment. If money left your account, contact support with your Paystack reference.');
    } else if (outcome === 'pending' && ref) {
      showAlert('Confirming your payment with Paystack\u2026', 'info');
      pollPayment(ref);
    }
  }

  function pollPayment(reference) {
    let attempts = 0;
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      attempts += 1;
      try {
        const data = await Duka.api(`/billing/payments/${encodeURIComponent(reference)}/`);
        if (data.outcome === 'success') {
          clearInterval(pollTimer);
          showAlert('Payment received -- your account is active.', 'good');
          if (data.receipt_url) {
            alertBox.appendChild(document.createTextNode(' '));
            alertBox.appendChild(Duka.el('a', { href: data.receipt_url, download: '',
                                                text: 'Download your receipt', style: 'text-decoration:underline;' }));
          }
          load();
        } else if (data.outcome === 'failed') {
          clearInterval(pollTimer);
          showAlert(data.reason || 'The payment did not go through -- try again.');
        } else if (attempts > 40) {
          clearInterval(pollTimer);
          showAlert('Still waiting for Paystack. If you completed the payment, this page updates by itself once it is confirmed.', 'info');
        }
      } catch (e) {
        clearInterval(pollTimer);
        showAlert(e.message);
      }
    }, 3000);
  }

  window.addEventListener('billing:refresh', () => load());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) quietRefresh(); });
  window.addEventListener('online', quietRefresh);
  autoTimer = setInterval(quietRefresh, AUTO_REFRESH_MS);
  handleReturn();
  load();
})();
