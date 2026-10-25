(() => {
  const root = document.querySelector('[data-api]');
  const $ = (id) => document.getElementById(id);
  let page = 1, pages = 1, searchTimer = null;

  function csrf() {
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    const input = document.querySelector('input[name=csrfmiddlewaretoken]');
    return input ? input.value : '';
  }

  async function call(url, { method = 'GET', body } = {}) {
    const res = await fetch(url, {
      method,
      credentials: 'same-origin',
      headers: Object.assign({ 'Accept': 'application/json', 'X-CSRFToken': csrf() },
                             body ? { 'Content-Type': 'application/json' } : {}),
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (!res.ok) {
      const err = new Error(data.error || 'Something went wrong (' + res.status + ').');
      err.data = data;
      throw err;
    }
    return data;
  }

  const money = (v) => 'KES ' + Number(v || 0).toLocaleString('en-KE', { maximumFractionDigits: 2 });
  const when = (iso) => iso ? new Date(iso).toLocaleString('en-KE', { dateStyle: 'medium', timeStyle: 'short' }) : '';

  function cell(text, cls) {
    const td = document.createElement('td');
    if (cls) td.className = cls;
    td.textContent = text == null ? '' : text;
    return td;
  }
  function button(label, cls, onclick) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = cls; b.textContent = label; b.onclick = onclick;
    b.style.marginRight = '6px';
    return b;
  }
  function chip(status) {
    const s = document.createElement('span');
    s.className = 'chip ' + status; s.textContent = status.toLowerCase();
    return s;
  }
  function emptyRow(tbody, cols, text) {
    tbody.innerHTML = '';
    const tr = document.createElement('tr'); tr.className = 'empty-row';
    const td = cell(text); td.colSpan = cols; tr.appendChild(td); tbody.appendChild(tr);
  }

  // ---------------------------------------------------------------- list
  function renderPending(rows) {
    const tbody = $('pending-rows');
    if (!rows.length) return emptyRow(tbody, 6, 'No shops are waiting on a code.');
    tbody.innerHTML = '';
    rows.forEach((r) => {
      const tr = document.createElement('tr');
      const shop = cell(r.tenant); const sub = document.createElement('div');
      sub.style.cssText = 'font-size:12px;color:var(--ink-soft);'; sub.textContent = r.phone || r.slug; shop.appendChild(sub);
      tr.appendChild(shop);
      tr.appendChild(cell(r.code, 'mono'));
      tr.appendChild(cell(r.invoice));
      tr.appendChild(cell(money(r.amount_due), 'num'));
      tr.appendChild(cell(when(r.created_at)));
      const actions = document.createElement('td');
      if (r.code_listed) {
        actions.appendChild(button('Approve', 'primary', () => requestAction(r, 'approve')));
      } else {
        const hint = document.createElement('span');
        hint.style.cssText = 'font-size:12px;color:var(--ink-soft);margin-right:8px;'; hint.textContent = 'code not listed yet';
        actions.appendChild(hint);
      }
      actions.appendChild(button('Reject', 'secondary', () => requestAction(r, 'reject')));
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
  }

  function renderCodes(rows) {
    const tbody = $('code-rows');
    if (!rows.length) return emptyRow(tbody, 6, 'Nothing here yet.');
    tbody.innerHTML = '';
    rows.forEach((c) => {
      const tr = document.createElement('tr');
      tr.appendChild(cell(c.code, 'mono'));
      tr.appendChild(cell(money(c.amount), 'num'));
      tr.appendChild(cell([c.sender_name, c.sender_phone].filter(Boolean).join(' ')));
      tr.appendChild(cell(when(c.transaction_at || c.created_at)));
      const st = document.createElement('td'); st.appendChild(chip(c.state));
      if (c.used_by) {
        const u = document.createElement('div');
        u.style.cssText = 'font-size:12px;color:var(--ink-soft);'; u.textContent = c.used_by + (c.invoice ? ' / ' + c.invoice : '');
        st.appendChild(u);
      }
      tr.appendChild(st);
      const actions = document.createElement('td');
      if (c.state === 'UNUSED') actions.appendChild(button('Void', 'secondary', () => voidCode(c)));
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
  }

  async function load() {
    const params = new URLSearchParams({ state: $('state-filter').value, q: $('search').value.trim(), page });
    try {
      const d = await call(root.dataset.api + '?' + params);
      $('kpi-unused').textContent = d.summary.unused_count;
      $('kpi-total').textContent = money(d.summary.unused_total);
      $('kpi-pending').textContent = d.summary.pending_count;
      pages = d.pages;
      renderPending(d.pending);
      renderCodes(d.codes);
      $('prev').hidden = d.page <= 1; $('next').hidden = d.page >= pages;
    } catch (e) {
      emptyRow($('code-rows'), 6, e.message);
    }
  }

  // ---------------------------------------------------------------- actions
  async function requestAction(r, action) {
    let body;
    if (action === 'reject') {
      const reason = window.prompt('Reason shown to the shop (optional):', '');
      if (reason === null) return;
      body = { reason };
    } else if (!window.confirm('Approve ' + r.tenant + '\'s payment of code ' + r.code + '?')) return;
    try {
      await call(root.dataset.requestsBase + r.id + '/' + action + '/', { method: 'POST', body: body || {} });
    } catch (e) { window.alert(e.message); }
    load();
  }

  async function voidCode(c) {
    if (!window.confirm('Void ' + c.code + '? It will never be accepted for a payment.')) return;
    try { await call(root.dataset.codesBase + c.id + '/void/', { method: 'POST', body: {} }); }
    catch (e) { window.alert(e.message); }
    load();
  }

  // ---------------------------------------------------------------- add form
  function clearErrors() { ['e-code', 'e-amount', 'e-form'].forEach((id) => { $(id).textContent = ''; }); }

  $('read-btn').onclick = async () => {
    clearErrors(); $('add-result').textContent = '';
    try {
      const d = await call(root.dataset.parse, { method: 'POST', body: { message: $('f-message').value } });
      $('f-code').value = d.code || '';
      $('f-amount').value = d.amount || '';
      $('f-name').value = d.name || '';
      $('f-phone').value = d.phone || '';
      if (!d.amount) $('e-amount').textContent = 'Amount not found in the message. Type it in.';
      if (d.exists) $('e-code').textContent = 'This code is already in your list.';
    } catch (e) { $('e-form').textContent = e.message; }
  };

  $('add-btn').onclick = async () => {
    clearErrors(); $('add-result').textContent = '';
    const btn = $('add-btn'); btn.disabled = true;
    try {
      const d = await call(root.dataset.add, { method: 'POST', body: {
        message: $('f-message').value, code: $('f-code').value, amount: $('f-amount').value,
        name: $('f-name').value, phone: $('f-phone').value } });
      let msg = 'Added ' + d.code.code + ' (' + money(d.code.amount) + ').';
      if (d.approved.length) msg += ' Approved: ' + d.approved.map((a) => a.tenant + ' (' + a.invoice + ')').join(', ') + '.';
      $('add-result').textContent = msg;
      ['f-message', 'f-code', 'f-amount', 'f-name', 'f-phone'].forEach((id) => { $(id).value = ''; });
      page = 1; load();
    } catch (e) {
      const errs = (e.data && e.data.errors) || {};
      if (errs.code) $('e-code').textContent = errs.code;
      if (errs.amount) $('e-amount').textContent = errs.amount;
      if (!errs.code && !errs.amount) $('e-form').textContent = e.message;
    } finally { btn.disabled = false; }
  };

  $('state-filter').onchange = () => { page = 1; load(); };
  $('search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { page = 1; load(); }, 300); };
  $('prev').onclick = () => { page = Math.max(1, page - 1); load(); };
  $('next').onclick = () => { page = Math.min(pages, page + 1); load(); };

  load();
})();
