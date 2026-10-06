
const Duka = (() => {
  function getCookie(name) {
    const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    return match ? decodeURIComponent(match[2]) : null;
  }

  async function api(path, { method = 'GET', body = null, json = true } = {}) {
    const headers = { Accept: 'application/json' };
    if (json && body) headers['Content-Type'] = 'application/json';
    if (method !== 'GET') {
      const token = getCookie('csrftoken');
      if (token) headers['X-CSRFToken'] = token;
    }
    const res = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      body: body ? (json ? JSON.stringify(body) : body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) {
      const err = new Error((data && (data.error || (data.errors && flattenErrors(data.errors)))) || `Request failed (${res.status})`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function flattenErrors(errors) {
    if (Array.isArray(errors)) return errors.join(' ');
    return Object.values(errors).flat().map(e => (e && e.message) || e).join(' ');
  }

  function money(amount, currency = 'KES') {
    const n = Number(amount || 0);
    return `${currency} ${n.toLocaleString('en-KE', { maximumFractionDigits: 0 })}`;
  }

  function dt(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleDateString('en-KE', { day: '2-digit', month: 'short', year: 'numeric' });
  }

  function daysUntil(iso) {
    if (!iso) return null;
    const ms = new Date(iso) - new Date();
    return Math.ceil(ms / 86400000);
  }

  function statusBadge(status) {
    const span = document.createElement('span');
    span.className = `status status--${status}`;
    span.textContent = (status || '').replace(/_/g, ' ');
    return span;
  }

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    (Array.isArray(children) ? children : [children]).forEach(c => {
      if (c == null) return;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return node;
  }

  return { api, money, dt, daysUntil, statusBadge, el, getCookie };
})();
