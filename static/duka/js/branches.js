(() => {
  const addForm = document.getElementById('add-form');
  const addSection = document.getElementById('add-section');
  const addNote = document.getElementById('add-note');
  const addBtn = document.getElementById('add-btn');
  const alertBox = document.getElementById('alert');
  const planLine = document.getElementById('plan-line');
  const rowsEl = document.getElementById('branch-rows');
  const drawer = document.getElementById('drawer');
  const drawerBackdrop = document.getElementById('drawer-backdrop');
  const drawerContent = document.getElementById('drawer-content');
  const COLS = 7;
  let state = null;

  const STATUS = { ACTIVE: 'ACTIVE', AWAITING_PAYMENT: 'PENDING', ARCHIVED: 'SUSPENDED' };
  const STATUS_TEXT = { ACTIVE: 'Active', AWAITING_PAYMENT: 'Awaiting payment', ARCHIVED: 'Archived' };

  function showAlert(msg, kind = 'bad') { alertBox.className = `alert show ${kind}`; alertBox.textContent = msg; }
  function clearAlert() { alertBox.className = 'alert'; alertBox.textContent = ''; }
  function clearFieldErrors() {
    addForm.querySelectorAll('.field').forEach(f => { f.classList.remove('invalid'); f.querySelector('.err').textContent = ''; });
  }
  function errText(e) {
    const errs = e.data && e.data.errors;
    if (!errs) return e.message;
    return Object.values(errs).map(m => (Array.isArray(m) ? m : [m]).map(x => (x && x.message) || x).join(' ')).join(' ');
  }

  function planText(s) {
    const extra = Duka.money(s.extra_branch_monthly, s.currency);
    return `${s.in_use} active of ${s.paid_slots} paid branch${s.paid_slots === 1 ? '' : 'es'} ` +
      `(max ${s.max}). One branch is included in your plan; each extra branch is ${extra}/month.`;
  }

  function renderRow(b) {
    const actions = Duka.el('td', {}, Duka.el('button', { class: 'row-action', text: 'Manage', onclick: () => openDrawer(b) }));
    return Duka.el('tr', {}, [
      Duka.el('td', { text: b.name + (b.is_main ? ' (main)' : '') }),
      Duka.el('td', { text: b.code || '\u2014' }),
      Duka.el('td', { text: b.location || '\u2014' }),
      Duka.el('td', { text: b.phone || '\u2014' }),
      Duka.el('td', { text: String(b.staff_count) }),
      Duka.el('td', {}, Duka.el('span', { class: `status status--${STATUS[b.status]}`, text: STATUS_TEXT[b.status] })),
      actions,
    ]);
  }

  async function load() {
    try {
      state = await Duka.api('/branches/');
      planLine.textContent = `${state.shop.name} \u00b7 ${planText(state.summary)}`;
      rowsEl.innerHTML = '';
      state.branches.forEach(b => rowsEl.appendChild(renderRow(b)));
      addSection.style.display = state.can_add ? '' : 'none';
      const s = state.summary;
      addNote.textContent = Number(s.in_use) < Number(s.paid_slots)
        ? 'You have a free branch slot -- this branch will be active straight away.'
        : `This goes beyond your paid branches. It costs ${Duka.money(s.extra_branch_monthly, s.currency)}/month ` +
          '(pro-rated for the rest of your current term) and switches on once the invoice is paid.';
    } catch (e) { showAlert(errText(e)); }
  }

  addForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    clearAlert(); clearFieldErrors();
    addBtn.disabled = true; addBtn.textContent = 'Adding\u2026';
    try {
      const data = await Duka.api('/branches/', {
        method: 'POST',
        body: {
          name: addForm.name.value.trim(), code: addForm.code.value.trim(),
          location: addForm.location.value.trim(), phone: addForm.phone.value.trim(),
        },
      });
      addForm.reset();
      if (data.invoice) {
        showAlert(`Branch added -- pay invoice ${data.invoice.number} (${Duka.money(data.invoice.amount_due)}) ` +
          'from Billing to switch it on.', 'good');
        setTimeout(() => { window.location.href = data.next || '/billing/'; }, 2500);
      } else {
        showAlert('Branch added.', 'good');
      }
      load();
    } catch (e) {
      const errs = e.data && e.data.errors;
      if (errs) Object.entries(errs).forEach(([n, m]) => {
        const f = addForm.querySelector(`[data-field="${n}"]`);
        if (f) { f.classList.add('invalid'); f.querySelector('.err').textContent = [].concat(m).join(' '); }
      });
      showAlert(errText(e));
    } finally {
      addBtn.disabled = false; addBtn.textContent = 'Add branch';
    }
  });

  function closeDrawer() { drawer.classList.remove('open'); drawerBackdrop.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); }
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  drawerBackdrop.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  async function act(id, action, params) {
    try {
      const data = await Duka.api(`/branches/${id}/${action}/`, { method: 'POST', body: params || {} });
      closeDrawer(); load();
      if (data.branch && data.branch.status === 'AWAITING_PAYMENT') {
        showAlert('Restored -- it switches on once the upgrade invoice in Billing is paid.', 'good');
      }
    } catch (e) {
      drawerContent.querySelector('.drawer-alert').textContent = errText(e);
    }
  }

  function labelled(text, input) {
    return Duka.el('div', { class: 'field', style: 'margin-bottom:10px;' }, [Duka.el('label', { text }), input]);
  }

  function openDrawer(b) {
    drawer.classList.add('open'); drawerBackdrop.classList.add('open'); drawer.setAttribute('aria-hidden', 'false');
    drawerContent.innerHTML = '';
    drawerContent.appendChild(Duka.el('h2', { text: b.name }));
    drawerContent.appendChild(Duka.el('div', { class: 'drawer-alert', style: 'color:var(--brick);font-size:13px;margin-bottom:10px;' }));
    const inputs = {
      name: Duka.el('input', { type: 'text', value: b.name }),
      code: Duka.el('input', { type: 'text', value: b.code || '', maxlength: '10' }),
      location: Duka.el('input', { type: 'text', value: b.location || '' }),
      phone: Duka.el('input', { type: 'tel', value: b.phone || '' }),
    };
    drawerContent.appendChild(labelled('Branch name', inputs.name));
    drawerContent.appendChild(labelled('Code', inputs.code));
    drawerContent.appendChild(labelled('Location', inputs.location));
    drawerContent.appendChild(labelled('Phone', inputs.phone));
    drawerContent.appendChild(Duka.el('button', {
      class: 'secondary', text: 'Save details',
      onclick: () => act(b.id, 'update', Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value.trim()]))),
    }));

    if (state.can_add && !b.is_main) {
      drawerContent.appendChild(Duka.el('hr', { class: 'rule' }));
      if (b.status === 'ARCHIVED') {
        drawerContent.appendChild(Duka.el('button', { class: 'secondary', text: 'Restore branch', onclick: () => act(b.id, 'restore') }));
      } else {
        drawerContent.appendChild(Duka.el('p', { class: 'plan-note',
          text: 'Archiving keeps all its sales and stock history. Its staff move to the main branch, and it stops counting toward your bill at the next renewal.' }));
        drawerContent.appendChild(Duka.el('button', { class: 'secondary', text: 'Archive branch', onclick: () => act(b.id, 'archive') }));
      }
    }
  }

  load();
})();
