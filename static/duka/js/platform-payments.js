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

  async function call(url, { method = 'GET' } = {}) {
    const res = await fetch(url, {
      method,
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json', 'X-CSRFToken': csrf() },
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
    if (!res.ok) throw new Error(data.error || 'Something went wrong (' + res.status + ').');
    return data;
  }

  const money = (v) => 'KES ' + Number(v || 0).toLocaleString('en-KE', { maximumFractionDigits: 2 });
  const when = (iso) => iso ? new Date(iso).toLocaleString('en-KE', { dateStyle: 'medium', timeStyle: 'short' }) : '';
  const LABEL = { SUCCESS: 'paid', INITIATED: 'waiting', FAILED: 'failed', CANCELLED: 'not completed' };

  function cell(text, cls) {
    const td = document.createElement('td');
    if (cls) td.className = cls;
    td.textContent = text == null ? '' : text;
    return td;
  }

  function render(rows) {
    const tbody = $('payment-rows');
    tbody.innerHTML = '';
    if (!rows.length) {
      const tr = document.createElement('tr'); tr.className = 'empty-row';
      const td = cell('No payments match.'); td.colSpan = 8; tr.appendChild(td); tbody.appendChild(tr);
      return;
    }
    rows.forEach((p) => {
      const tr = document.createElement('tr');
      tr.appendChild(cell(when(p.completed_at || p.created_at)));
      tr.appendChild(cell(p.tenant));
      tr.appendChild(cell(p.invoice));
      tr.appendChild(cell(p.reference, 'mono'));
      tr.appendChild(cell((p.channel || '').replace('_', ' ')));
      tr.appendChild(cell(money(p.paid_amount != null ? p.paid_amount : p.amount), 'num'));
      const st = document.createElement('td');
      const chip = document.createElement('span');
      chip.className = 'chip ' + p.status; chip.textContent = LABEL[p.status] || p.status.toLowerCase();
      if (p.reason) chip.title = p.reason;
      st.appendChild(chip); tr.appendChild(st);
      const act = document.createElement('td');
      if (p.status !== 'SUCCESS') {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'secondary'; b.textContent = 'Re-check';
        b.onclick = async () => {
          b.disabled = true; b.textContent = 'Checking\u2026'; $('page-error').textContent = '';
          try { await call(root.dataset.recheckBase + p.id + '/recheck/', { method: 'POST' }); }
          catch (e) { $('page-error').textContent = e.message; }
          load();
        };
        act.appendChild(b);
      }
      tr.appendChild(act);
      tbody.appendChild(tr);
    });
  }

  async function load() {
    const params = new URLSearchParams({ status: $('status-filter').value, page: String(page) });
    const q = $('search').value.trim();
    if (q) params.set('q', q);
    try {
      const d = await call(root.dataset.api + '?' + params.toString());
      $('cfg-warning').hidden = d.configured;
      $('kpi-month').textContent = money(d.summary.month_total);
      $('kpi-month-count').textContent = d.summary.month_count;
      $('kpi-all').textContent = money(d.summary.all_time_total);
      $('kpi-waiting').textContent = d.summary.waiting_count;
      pages = d.pages; page = d.page;
      $('prev').hidden = page <= 1; $('next').hidden = page >= pages;
      render(d.payments);
    } catch (e) {
      $('page-error').textContent = e.message;
    }
  }

  $('status-filter').onchange = () => { page = 1; load(); };
  $('search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { page = 1; load(); }, 300); };
  $('prev').onclick = () => { if (page > 1) { page -= 1; load(); } };
  $('next').onclick = () => { if (page < pages) { page += 1; load(); } };
  load();
})();
