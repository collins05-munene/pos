(() => {
  const addForm = document.getElementById('add-form');
  const alertBox = document.getElementById('alert');
  const shopLine = document.getElementById('shop-line');
  const rowsEl = document.getElementById('staff-rows');
  const roleSelect = document.getElementById('role');
  const credentialInput = document.getElementById('credential');
  const credentialLabel = document.getElementById('credential-label');
  const addBtn = document.getElementById('add-btn');
  const drawer = document.getElementById('drawer');
  const drawerBackdrop = document.getElementById('drawer-backdrop');
  const drawerContent = document.getElementById('drawer-content');
  const drawerClose = document.getElementById('drawer-close');

  const COLS = 10;  // keep in sync with the <th> count in staff.html

  function showAlert(msg, kind = 'bad') { alertBox.className = `alert show ${kind}`; alertBox.textContent = msg; }
  function clearAlert() { alertBox.className = 'alert'; alertBox.textContent = ''; }

  function clearFieldErrors() {
    addForm.querySelectorAll('.field').forEach(f => {
      f.classList.remove('invalid');
      f.querySelector('.err').textContent = '';
    });
  }
  function applyFieldErrors(errors) {
    Object.entries(errors || {}).forEach(([name, msgs]) => {
      const field = addForm.querySelector(`[data-field="${name}"]`);
      if (!field) return;
      field.classList.add('invalid');
      field.querySelector('.err').textContent = (Array.isArray(msgs) ? msgs : [msgs]).join(' ');
    });
  }
  // "last_name: This field is required." style text for the drawer, which has no per-field slots
  function flattenErrors(errors) {
    return Object.values(errors || {}).map(m => (Array.isArray(m) ? m : [m]).join(' ')).join(' ');
  }

  const branchSelect = document.getElementById('branch_id');
  let branches = [];
  function fillBranchSelect(select, selectedId, allowNone) {
    select.innerHTML = '';
    if (allowNone) select.appendChild(Duka.el('option', { value: '', text: 'No fixed branch (starts at main)' }));
    branches.forEach(b => {
      const o = Duka.el('option', { value: String(b.id), text: b.name });
      if (String(selectedId) === String(b.id)) o.selected = true;
      select.appendChild(o);
    });
  }

  function syncCredentialField() {
    const isCashier = roleSelect.value === 'CASHIER';
    credentialLabel.textContent = isCashier ? 'PIN (4-6 digits)' : 'Password';
    credentialInput.setAttribute('maxlength', isCashier ? '6' : '128');
    credentialInput.setAttribute('inputmode', isCashier ? 'numeric' : 'text');
  }
  roleSelect.addEventListener('change', syncCredentialField);
  syncCredentialField();

  function renderRow(s) {
    const actionsCell = Duka.el('td');
    const manage = Duka.el('button', { class: 'row-action', text: 'Manage', onclick: () => openDrawer(s) });
    actionsCell.appendChild(manage);
    return Duka.el('tr', {}, [
      Duka.el('td', { text: s.full_name || '\u2014' }),
      Duka.el('td', { text: s.handle }),
      Duka.el('td', { text: s.role === 'CASHIER' ? 'Cashier' : 'Manager' }),
      Duka.el('td', { text: s.branch_name || '\u2014' }),
      Duka.el('td', { text: s.phone || '\u2014' }),
      Duka.el('td', { text: s.national_id || '\u2014' }),
      Duka.el('td', { text: s.email || '\u2014' }),
      Duka.el('td', { text: s.login_method === 'PIN' ? 'PIN' : 'Password' }),
      Duka.el('td', {}, Duka.statusBadge(s.is_active ? 'ACTIVE' : 'SUSPENDED')),
      actionsCell,
    ]);
  }

  async function load() {
    try {
      const data = await Duka.api('/staff/');
      shopLine.textContent = `${data.shop.name} \u00b7 code: ${data.shop.slug}`;
      branches = data.branches || [];
      fillBranchSelect(branchSelect, branchSelect.value, false);
      rowsEl.innerHTML = '';
      if (data.staff.length === 0) {
        rowsEl.appendChild(Duka.el('tr', { class: 'empty-row' }, Duka.el('td', { colspan: String(COLS), text: "No staff yet -- add your first cashier above." })));
      } else {
        data.staff.forEach(s => rowsEl.appendChild(renderRow(s)));
      }
    } catch (e) { showAlert(e.message); }
  }

  addForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    clearAlert();
    clearFieldErrors();
    addBtn.disabled = true;
    addBtn.textContent = 'Adding\u2026';
    try {
      await Duka.api('/staff/', {
        method: 'POST', json: false,
        body: new URLSearchParams({
          handle: addForm.handle.value.trim(),
          first_name: addForm.first_name.value.trim(),
          last_name: addForm.last_name.value.trim(),
          phone: addForm.phone.value.trim(),
          national_id: addForm.national_id.value.trim(),
          email: addForm.email.value.trim(),
          role: roleSelect.value,
          credential: credentialInput.value,
          branch_id: branchSelect.value,
        }),
      });
      addForm.reset();
      syncCredentialField();
      showAlert('Added.', 'good');
      load();
    } catch (e) {
      if (e.data && e.data.errors) applyFieldErrors(e.data.errors);
      showAlert(e.message);
    } finally {
      addBtn.disabled = false;
      addBtn.textContent = 'Add to shop';
    }
  });

  function closeDrawer() { drawer.classList.remove('open'); drawerBackdrop.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); }
  drawerClose.addEventListener('click', closeDrawer);
  drawerBackdrop.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  async function act(id, action, params) {
    try {
      await Duka.api(`/staff/${id}/${action}/`, { method: 'POST', json: false, body: new URLSearchParams(params) });
      closeDrawer();
      load();
    } catch (e) {
      const drawerAlert = drawerContent.querySelector('.drawer-alert');
      if (drawerAlert) {
        drawerAlert.textContent = (e.data && e.data.errors && flattenErrors(e.data.errors)) || e.message;
      }
    }
  }

  function labelled(text, input) {
    return Duka.el('div', { class: 'field', style: 'margin-bottom:10px;' }, [
      Duka.el('label', { text }), input,
    ]);
  }

  function openDrawer(s) {
    drawer.classList.add('open');
    drawerBackdrop.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    drawerContent.innerHTML = '';
    drawerContent.appendChild(Duka.el('h2', { text: s.full_name || s.handle }));
    drawerContent.appendChild(Duka.el('div', { class: 'drawer-alert', style: 'color:var(--brick);font-size:13px;margin-bottom:10px;' }));

    // ---- Details
    drawerContent.appendChild(Duka.el('h2', { text: 'Details', style: 'font-size:15px;' }));
    const inputs = {
      first_name: Duka.el('input', { type: 'text', value: s.first_name || '' }),
      last_name: Duka.el('input', { type: 'text', value: s.last_name || '' }),
      phone: Duka.el('input', { type: 'tel', value: s.phone || '' }),
      national_id: Duka.el('input', { type: 'text', value: s.national_id || '' }),
      email: Duka.el('input', { type: 'email', value: s.email || '' }),
    };
    drawerContent.appendChild(labelled('First name', inputs.first_name));
    drawerContent.appendChild(labelled('Last name', inputs.last_name));
    drawerContent.appendChild(labelled('Phone number', inputs.phone));
    drawerContent.appendChild(labelled('ID number', inputs.national_id));
    drawerContent.appendChild(labelled('Email (optional)', inputs.email));
    drawerContent.appendChild(Duka.el('button', {
      class: 'secondary', text: 'Save details',
      onclick: () => act(s.id, 'update-details', Object.fromEntries(
        Object.entries(inputs).map(([k, el]) => [k, el.value.trim()])
      )),
    }));

    // ---- Branch
    drawerContent.appendChild(Duka.el('hr', { class: 'rule' }));
    drawerContent.appendChild(Duka.el('h2', { text: 'Branch', style: 'font-size:15px;' }));
    const branchPick = Duka.el('select');
    fillBranchSelect(branchPick, s.branch_id, s.role !== 'CASHIER');
    drawerContent.appendChild(Duka.el('div', { class: 'override-form' }, [
      branchPick,
      Duka.el('button', { class: 'secondary', text: 'Move', onclick: () => act(s.id, 'set-branch', { branch_id: branchPick.value }) }),
    ]));
    if (s.role !== 'CASHIER') {
      drawerContent.appendChild(Duka.el('p', { class: 'plan-note', text: 'Managers can switch between branches from the top bar; this is where they start.' }));
    }

    // ---- Credential
    drawerContent.appendChild(Duka.el('hr', { class: 'rule' }));
    const isCashier = s.role === 'CASHIER';
    drawerContent.appendChild(Duka.el('h2', { text: isCashier ? 'Reset PIN' : 'Reset password', style: 'font-size:15px;' }));
    const valueInput = Duka.el('input', {
      type: 'password', placeholder: isCashier ? 'New 4-6 digit PIN' : 'New password',
      maxlength: isCashier ? '6' : '128',
    });
    drawerContent.appendChild(Duka.el('div', { class: 'override-form' }, [
      valueInput,
      Duka.el('button', { class: 'secondary', text: 'Reset', onclick: () => act(s.id, 'reset-credential', { value: valueInput.value }) }),
    ]));

    // ---- Status
    drawerContent.appendChild(Duka.el('hr', { class: 'rule' }));
    drawerContent.appendChild(Duka.el('button', {
      class: 'secondary', text: s.is_active ? 'Deactivate' : 'Activate',
      onclick: () => act(s.id, s.is_active ? 'deactivate' : 'activate', {}),
    }));
  }

  load();
})();