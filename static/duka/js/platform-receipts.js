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
    if (!res.ok) throw new Error(data.error || 'Something went wrong (' + res.status + ').');
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
  function sub(td, text) {
    if (!text) return td;
    const d = document.createElement('div'); d.className = 'sub'; d.textContent = text; td.appendChild(d);
    return td;
  }
  function link(label, href) {
    const a = document.createElement('a');
    a.className = 'dl'; a.href = href; a.textContent = label;
    return a;
  }
  function button(label, cls, onclick) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = cls; b.textContent = label; b.onclick = onclick;
    return b;
  }
  function emptyRow(tbody, cols, text) {
    tbody.innerHTML = '';
    const tr = document.createElement('tr'); tr.className = 'empty-row';
    const td = cell(text); td.colSpan = cols; tr.appendChild(td); tbody.appendChild(tr);
  }

  function renderReceipts(rows) {
    const tbody = $('receipt-rows');
    if (!rows.length) return emptyRow(tbody, 6, 'No receipts found.');
    tbody.innerHTML = '';
    rows.forEach((r) => {
      const tr = document.createElement('tr');
      if (r.is_void) tr.style.opacity = '0.65';
      const num = sub(cell(r.number, 'mono'), r.invoice);
      if (r.is_void) {
        const v = document.createElement('div'); v.className = 'sub'; v.style.color = '#9a3327';
        v.textContent = 'VOID' + (r.void_reason ? ': ' + r.void_reason : '');
        num.appendChild(v);
      }
      tr.appendChild(num);
      tr.appendChild(sub(cell(r.tenant), r.slug));
      tr.appendChild(cell(when(r.paid_at)));
      tr.appendChild(sub(cell(r.method), r.reference));
      tr.appendChild(cell(money(r.amount), 'num'));
      const actions = document.createElement('td');
      actions.appendChild(link('Download', r.download_url));
      const view = link('View', r.download_url + '?view=1'); view.target = '_blank'; view.rel = 'noopener';
      actions.appendChild(view);
      const check = link('Verify page', r.verify_url); check.target = '_blank'; check.rel = 'noopener';
      check.title = 'Public page the QR code opens. Code: ' + r.verify_code;
      actions.appendChild(check);
      actions.appendChild(r.is_void
        ? button('Restore', 'secondary', () => setVoid(r, 'restore'))
        : button('Void', 'secondary', () => setVoid(r, 'void')));
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
  }

  async function setVoid(r, action) {
    let body = {};
    if (action === 'void') {
      const reason = window.prompt('Why is ' + r.number + ' being withdrawn? (shown on the PDF)', 'Payment reversed');
      if (reason === null) return;
      body = { reason };
    } else if (!window.confirm('Make ' + r.number + ' valid again?')) return;
    try {
      await call(action === 'void' ? r.void_url : r.restore_url, { method: 'POST', body });
    } catch (e) { window.alert(e.message); }
    load();
  }

  function renderMissing(rows, total) {
    $('missing-card').hidden = total === 0 && rows.length === 0;
    const tbody = $('missing-rows');
    tbody.innerHTML = '';
    rows.forEach((m) => {
      const tr = document.createElement('tr');
      tr.appendChild(sub(cell(m.tenant), m.slug));
      tr.appendChild(cell(m.invoice, 'mono'));
      tr.appendChild(cell(when(m.paid_at)));
      tr.appendChild(cell(money(m.amount), 'num'));
      const actions = document.createElement('td');
      actions.appendChild(button('Generate', 'secondary', () => generate(m)));
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
    if (total > rows.length) {
      const tr = document.createElement('tr'); tr.className = 'empty-row';
      const td = cell('Showing ' + rows.length + ' of ' + total + '. Search to narrow the list.'); td.colSpan = 5;
      tr.appendChild(td); tbody.appendChild(tr);
    }
  }

  async function load() {
    const params = new URLSearchParams({ q: $('search').value.trim(), page });
    try {
      const d = await call(root.dataset.api + '?' + params);
      $('kpi-count').textContent = d.summary.receipts;
      $('kpi-total').textContent = money(d.summary.total);
      $('kpi-missing').textContent = d.summary.missing;
      pages = d.pages;
      renderReceipts(d.receipts);
      renderMissing(d.missing, d.summary.missing);
      $('prev').hidden = d.page <= 1; $('next').hidden = d.page >= pages;
    } catch (e) {
      emptyRow($('receipt-rows'), 6, e.message);
    }
  }

  async function generate(m) {
    $('e-missing').textContent = ''; $('ok-missing').textContent = '';
    try {
      const d = await call(root.dataset.generate, { method: 'POST', body: { invoice: m.invoice } });
      $('ok-missing').textContent = 'Receipt ' + d.number + ' created for ' + m.tenant + '.';
    } catch (e) { $('e-missing').textContent = e.message; }
    load();
  }

  $('gen-all').onclick = async () => {
    if (!window.confirm('Generate a receipt for every paid invoice that does not have one?')) return;
    $('e-missing').textContent = ''; $('ok-missing').textContent = '';
    const btn = $('gen-all'); btn.disabled = true;
    try {
      const d = await call(root.dataset.generate, { method: 'POST', body: { all: true } });
      $('ok-missing').textContent = 'Created ' + d.generated + ' receipt' + (d.generated === 1 ? '' : 's') +
        (d.remaining ? '. ' + d.remaining + ' still to do: press the button again.' : '.');
    } catch (e) { $('e-missing').textContent = e.message; }
    finally { btn.disabled = false; }
    load();
  };

  $('search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { page = 1; load(); }, 300); };
  $('prev').onclick = () => { page = Math.max(1, page - 1); load(); };
  $('next').onclick = () => { page = Math.min(pages, page + 1); load(); };

  load();
})();
