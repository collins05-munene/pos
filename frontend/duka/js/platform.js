(() => {
  const kpisEl = document.getElementById('kpis');
  const rowsEl = document.getElementById('tenant-rows');
  const alertBox = document.getElementById('alert');
  const searchInput = document.getElementById('search');
  const statusFilter = document.getElementById('status-filter');
  const loadMoreBtn = document.getElementById('load-more');
  const refreshedAt = document.getElementById('refreshed-at');
  const drawer = document.getElementById('drawer');
  const drawerBackdrop = document.getElementById('drawer-backdrop');
  const drawerContent = document.getElementById('drawer-content');
  const drawerClose = document.getElementById('drawer-close');

  let page = 1;
  let searchTimer = null;

  function showAlert(msg) { alertBox.className = 'alert bad show'; alertBox.textContent = msg; }

  function debounce(fn, ms) {
    return (...args) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => fn(...args), ms); };
  }

  async function loadOverview() {
    try {
      const data = await Duka.api('/platform/');
      kpisEl.innerHTML = '';
      kpisEl.appendChild(kpiBox('MRR', Duka.money(data.mrr)));
      kpisEl.appendChild(kpiBox('ARR', Duka.money(data.arr)));
      const activeCount = (data.tenants_by_status && data.tenants_by_status.ACTIVE) || 0;
      kpisEl.appendChild(kpiBox('Active shops', `${activeCount} / ${data.tenants_total}`));
      refreshedAt.textContent = `updated ${new Date().toLocaleTimeString('en-KE')}`;
    } catch (e) { showAlert(e.message); }
  }

  function kpiBox(label, value) {
    return Duka.el('div', { class: 'kpi' }, [
      Duka.el('div', { class: 'label', text: label }),
      Duka.el('div', { class: 'value', text: value }),
    ]);
  }

  function renderRow(t) {
    const daysLeft = Duka.daysUntil(t.period_end);
    let renewText = '--';
    if (t.period_end) {
      renewText = daysLeft >= 0 ? `${Duka.dt(t.period_end)}` : `expired ${Duka.dt(t.period_end)}`;
    }
    return Duka.el('tr', {}, [
      Duka.el('td', {}, [
        Duka.el('div', { text: t.name }),
        Duka.el('div', { style: 'font-family:var(--mono);font-size:11.5px;color:var(--ink-soft);', text: t.slug }),
      ]),
      Duka.el('td', {}, t.status ? Duka.statusBadge(t.status) : document.createTextNode('--')),
      Duka.el('td', { class: 'num', text: String(t.users) }),
      Duka.el('td', { text: renewText }),
      Duka.el('td', {}, Duka.el('button', { class: 'row-action', text: 'Manage', onclick: () => openDrawer(t.id) })),
    ]);
  }

  async function loadTenants(reset = true) {
    if (reset) { page = 1; rowsEl.innerHTML = '<tr class="empty-row"><td colspan="5">Loading&hellip;</td></tr>'; }
    const params = new URLSearchParams({ page: String(page) });
    if (searchInput.value.trim()) params.set('q', searchInput.value.trim());
    if (statusFilter.value) params.set('status', statusFilter.value);

    try {
      const data = await Duka.api(`/platform/tenants/?${params}`);
      if (reset) rowsEl.innerHTML = '';
      if (data.results.length === 0 && reset) {
        rowsEl.appendChild(Duka.el('tr', { class: 'empty-row' }, Duka.el('td', { colspan: '5', text: 'No shops match.' })));
      }
      data.results.forEach(t => rowsEl.appendChild(renderRow(t)));
      loadMoreBtn.hidden = data.page >= data.pages;
    } catch (e) { showAlert(e.message); }
  }

  loadMoreBtn.addEventListener('click', () => { page += 1; loadTenants(false); });
  searchInput.addEventListener('input', debounce(() => loadTenants(true), 300));
  statusFilter.addEventListener('change', () => loadTenants(true));

  // ---------------------------------------------------------- drawer
  async function openDrawer(id) {
    drawer.classList.add('open');
    drawerBackdrop.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    drawerContent.textContent = 'Loading\u2026';
    try {
      const d = await Duka.api(`/platform/tenants/${id}/`);
      renderDrawer(id, d);
    } catch (e) {
      drawerContent.textContent = e.message;
    }
  }

  function closeDrawer() {
    drawer.classList.remove('open');
    drawerBackdrop.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
  }
  drawerClose.addEventListener('click', closeDrawer);
  drawerBackdrop.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  async function act(id, action, params, onDone) {
    try {
      await Duka.api(`/platform/tenants/${id}/${action}/`, { method: 'POST', json: false, body: new URLSearchParams(params) });
      onDone && onDone();
      openDrawer(id);
      loadTenants(true);
      loadOverview();
    } catch (e) {
      showAlert(e.message);
    }
  }

  function renderDrawer(id, d) {
    drawerContent.innerHTML = '';
    const t = d.tenant, sub = d.subscription;

    drawerContent.appendChild(Duka.el('h2', { text: t.name }));
    drawerContent.appendChild(Duka.el('div', { style: 'font-family:var(--mono);font-size:12px;color:var(--ink-soft);margin-bottom:16px;',
      text: `${t.slug} \u00b7 ${t.phone || 'no phone'} \u00b7 ${t.email || 'no email'}` }));

    const statusRow = Duka.el('div', { class: 'invoice-row' }, [
      Duka.el('span', { class: 'k', text: 'Status' }), Duka.statusBadge(sub.status),
    ]);
    drawerContent.appendChild(statusRow);
    drawerContent.appendChild(Duka.el('div', { class: 'invoice-row' }, [
      Duka.el('span', { class: 'k', text: 'Period ends' }), Duka.el('span', { class: 'v', text: Duka.dt(sub.period_end) }),
    ]));
    if (sub.comp_until) {
      drawerContent.appendChild(Duka.el('div', { class: 'invoice-row' }, [
        Duka.el('span', { class: 'k', text: 'Comped until' }), Duka.el('span', { class: 'v', text: Duka.dt(sub.comp_until) }),
      ]));
    }
    if (sub.custom_monthly_price) {
      drawerContent.appendChild(Duka.el('div', { class: 'invoice-row' }, [
        Duka.el('span', { class: 'k', text: 'Custom price' }), Duka.el('span', { class: 'v', text: Duka.money(sub.custom_monthly_price) + '/mo' }),
      ]));
    }

    drawerContent.appendChild(Duka.el('hr', { class: 'rule' }));

    // --- grant free access
    drawerContent.appendChild(Duka.el('h2', { text: 'Grant free access' }));
    const compDays = Duka.el('input', { type: 'number', min: '1', value: '30', placeholder: 'days' });
    const compForm = Duka.el('div', { class: 'override-form' }, [
      compDays,
      Duka.el('button', { class: 'secondary', text: 'Comp', onclick: () => act(id, 'comp', { days: compDays.value, note: 'via platform admin' }) }),
    ]);
    drawerContent.appendChild(compForm);

    // --- custom price
    drawerContent.appendChild(Duka.el('h2', { text: 'Custom monthly price', style: 'margin-top:20px;' }));
    const priceInput = Duka.el('input', { type: 'number', min: '0', placeholder: 'KES / month, blank to clear' });
    drawerContent.appendChild(Duka.el('div', { class: 'override-form' }, [
      priceInput,
      Duka.el('button', { class: 'secondary', text: 'Set', onclick: () => act(id, 'set-price', { monthly_price: priceInput.value, note: 'via platform admin' }) }),
    ]));

    drawerContent.appendChild(Duka.el('hr', { class: 'rule' }));

    // --- account actions
    drawerContent.appendChild(Duka.el('h2', { text: 'Account' }));
    const actionsRow = Duka.el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px;' });
    actionsRow.appendChild(Duka.el('button', { class: 'secondary', text: 'Suspend now', onclick: () => act(id, 'suspend', {}) }));
    actionsRow.appendChild(Duka.el('button', { class: 'secondary', text: t.is_active ? 'Disable shop' : 'Enable shop',
      onclick: () => act(id, t.is_active ? 'disable' : 'enable', {}) }));
    actionsRow.appendChild(Duka.el('button', { class: 'secondary', text: 'Start support session',
      onclick: () => act(id, 'support-start', {}, () => { window.location.href = '/'; }) }));
    drawerContent.appendChild(actionsRow);

    drawerContent.appendChild(Duka.el('hr', { class: 'rule' }));

    // --- invoices
    drawerContent.appendChild(Duka.el('h2', { text: 'Recent invoices' }));
    const table = Duka.el('table', { class: 'ledger' });
    const tbody = Duka.el('tbody');
    if (!d.invoices || d.invoices.length === 0) {
      tbody.appendChild(Duka.el('tr', { class: 'empty-row' }, Duka.el('td', { colspan: '3', text: 'None yet.' })));
    } else {
      d.invoices.slice(0, 8).forEach(inv => {
        tbody.appendChild(Duka.el('tr', {}, [
          Duka.el('td', { text: inv.number }),
          Duka.el('td', {}, Duka.statusBadge(inv.status)),
          Duka.el('td', { class: 'num', text: Duka.money(inv.total) }),
        ]));
      });
    }
    table.appendChild(tbody);
    drawerContent.appendChild(table);
  }

  loadOverview();
  loadTenants(true);
})();
