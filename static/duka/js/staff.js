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
      Duka.el('td', { text: s.handle }),
      Duka.el('td', { text: s.role === 'CASHIER' ? 'Cashier' : 'Manager' }),
      Duka.el('td', { text: s.login_method === 'PIN' ? 'PIN' : 'Password' }),
      Duka.el('td', {}, Duka.statusBadge(s.is_active ? 'ACTIVE' : 'SUSPENDED')),
      actionsCell,
    ]);
  }

  async function load() {
    try {
      const data = await Duka.api('/staff/');
      shopLine.textContent = `${data.shop.name} \u00b7 code: ${data.shop.slug}`;
      rowsEl.innerHTML = '';
      if (data.staff.length === 0) {
        rowsEl.appendChild(Duka.el('tr', { class: 'empty-row' }, Duka.el('td', { colspan: '5', text: "No staff yet -- add your first cashier above." })));
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
          role: roleSelect.value,
          credential: credentialInput.value,
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
      if (drawerAlert) drawerAlert.textContent = e.message;
    }
  }

  function openDrawer(s) {
    drawer.classList.add('open');
    drawerBackdrop.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    drawerContent.innerHTML = '';
    drawerContent.appendChild(Duka.el('h2', { text: s.handle }));
    drawerContent.appendChild(Duka.el('div', { class: 'drawer-alert', style: 'color:var(--brick);font-size:13px;margin-bottom:10px;' }));

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

    drawerContent.appendChild(Duka.el('hr', { class: 'rule' }));
    drawerContent.appendChild(Duka.el('button', {
      class: 'secondary', text: s.is_active ? 'Deactivate' : 'Activate',
      onclick: () => act(s.id, s.is_active ? 'deactivate' : 'activate', {}),
    }));
  }

  load();
})();