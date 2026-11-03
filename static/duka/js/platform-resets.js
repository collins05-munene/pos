(() => {
  const root = document.querySelector('[data-api]');
  const $ = (id) => document.getElementById(id);

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

  const when = (iso) => iso ? new Date(iso).toLocaleString('en-KE', { dateStyle: 'medium', timeStyle: 'short' }) : '';

  function cell(text, cls) {
    const td = document.createElement('td');
    if (cls) td.className = cls;
    td.textContent = text == null ? '' : text;
    return td;
  }
  function line(parent, text, cls) {
    if (!text) return;
    const d = document.createElement('div');
    d.className = cls || 'sub'; d.textContent = text; parent.appendChild(d);
  }
  function button(label, cls, onclick) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = cls; b.textContent = label; b.onclick = onclick;
    b.style.marginRight = '6px';
    return b;
  }
  function emptyRow(tbody, cols, text) {
    tbody.innerHTML = '';
    const tr = document.createElement('tr'); tr.className = 'empty-row';
    const td = cell(text); td.colSpan = cols; tr.appendChild(td); tbody.appendChild(tr);
  }
  function whoCell(r) {
    const td = cell(r.person);
    line(td, r.role + ' \u00b7 ' + r.username);
    line(td, r.phone ? 'Phone ' + r.phone : '');
    return td;
  }
  function shopCell(r) {
    const td = cell(r.shop);
    line(td, r.slug + (r.shop_phone ? ' \u00b7 ' + r.shop_phone : ''));
    return td;
  }

  function renderPending(rows) {
    const tbody = $('pending-rows');
    if (!rows.length) return emptyRow(tbody, 5, 'Nobody is waiting for a reset.');
    tbody.innerHTML = '';
    rows.forEach((r) => {
      const tr = document.createElement('tr');
      tr.appendChild(whoCell(r));
      tr.appendChild(shopCell(r));
      const asked = cell(when(r.requested_at));
      line(asked, r.requested_ip ? 'from ' + r.requested_ip : '');
      tr.appendChild(asked);
      const to = cell(r.has_email ? r.email_to : 'no email on file');
      if (!r.has_email) to.className = 'warn';
      if (r.has_email && r.email_is_owners) line(to, "owner's email (staff has none)", 'warn');
      tr.appendChild(to);
      const actions = document.createElement('td');
      if (r.has_email) actions.appendChild(button('Approve', 'primary', () => act(r, 'approve')));
      actions.appendChild(button('Reject', 'secondary', () => act(r, 'reject')));
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
  }

  function renderRecent(rows) {
    const tbody = $('recent-rows');
    if (!rows.length) return emptyRow(tbody, 5, 'Nothing decided yet.');
    tbody.innerHTML = '';
    rows.forEach((r) => {
      const tr = document.createElement('tr');
      tr.appendChild(whoCell(r));
      tr.appendChild(shopCell(r));
      tr.appendChild(cell(when(r.requested_at)));
      const dec = cell(r.decided_by ? r.decided_by + ' \u00b7 ' + when(r.decided_at) : '');
      line(dec, r.link_sent_to ? 'link sent to ' + r.link_sent_to : '');
      line(dec, r.decision_note ? 'Note: ' + r.decision_note : '');
      tr.appendChild(dec);
      const st = document.createElement('td');
      const chip = document.createElement('span');
      chip.className = 'chip ' + r.status; chip.textContent = r.status.toLowerCase();
      st.appendChild(chip);
      if (r.status === 'COMPLETED') line(st, when(r.completed_at));
      tr.appendChild(st);
      tbody.appendChild(tr);
    });
  }

  function showAlert(msg) {
    const box = $('alert');
    box.textContent = msg || '';
    box.classList.toggle('show', !!msg);
  }

  async function load() {
    try {
      const d = await call(root.dataset.api);
      $('kpi-pending').textContent = d.summary.pending_count;
      renderPending(d.pending);
      renderRecent(d.recent);
    } catch (e) {
      emptyRow($('pending-rows'), 5, e.message);
    }
  }

  async function act(r, action) {
    showAlert('');
    let note = '';
    if (action === 'reject') {
      const n = window.prompt('Reason (optional, kept on record):', '');
      if (n === null) return;
      note = n;
    } else if (!window.confirm('Approve the reset for ' + r.person + ' at ' + r.shop + '?\n\nA one-time link will be emailed to ' + r.email_to + '.')) {
      return;
    }
    try {
      await call(root.dataset.actionBase + r.id + '/' + action + '/', { method: 'POST', body: { note } });
    } catch (e) { showAlert(e.message); }
    load();
  }

  load();
  setInterval(() => { if (!document.hidden) load(); }, 30000);
})();
