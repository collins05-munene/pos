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

  let pollTimer = null;
  let currentPlans = [];
  let selectedTerm = null;

  function showAlert(msg, kind = 'bad') {
    alertBox.className = `alert show ${kind}`;
    alertBox.textContent = msg;
  }
  function clearAlert() { alertBox.className = 'alert'; alertBox.textContent = ''; }

  function renderInvoiceRows(invoices) {
    rowsEl.innerHTML = '';
    if (!invoices || invoices.length === 0) {
      rowsEl.appendChild(Duka.el('tr', { class: 'empty-row' }, Duka.el('td', { colspan: '5', text: 'No invoices yet.' })));
      return;
    }
    invoices.forEach(inv => {
      const tr = Duka.el('tr', {}, [
        Duka.el('td', { text: inv.number }),
        Duka.el('td', { text: `${inv.term_months} mo` }),
        Duka.el('td', { text: Duka.dt(inv.issued_at) }),
        Duka.el('td', {}, Duka.statusBadge(inv.status)),
        Duka.el('td', { class: 'num', text: Duka.money(inv.total) }),
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
        onclick: () => { selectedTerm = p.term_months; renderPlanPicker(currentPlans); },
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

  function renderOpenInvoice(inv, plans) {
    if (!inv) {
      invoicePanel.hidden = true;
      termPicker.hidden = false;
      selectedTerm = selectedTerm || (plans[0] && plans[0].term_months);
      renderPlanPicker(plans);
      return;
    }
    termPicker.hidden = true;
    invoicePanel.hidden = false;
    document.getElementById('inv-number').textContent = inv.number;
    document.getElementById('inv-term').textContent = `${inv.term_months} month(s)`;
    document.getElementById('inv-total').textContent = Duka.money(inv.total);
  }

  async function load() {
    try {
      const data = await Duka.api('/billing/');
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
    } catch (e) {
      showAlert(e.message);
    }
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

  load();
})();
