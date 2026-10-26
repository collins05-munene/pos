
(function () {
  'use strict';
  var form = document.querySelector('form[data-live-form]');
  var body = document.getElementById('live-body');
  var pager = document.getElementById('live-pager');
  if (!form || !body || !pager) return;

  var table = body.closest('table');
  var cols = table.querySelectorAll('thead th').length;
  var csrfInput = document.querySelector('#live-csrf input[name="csrfmiddlewaretoken"]');
  var csrf = csrfInput ? csrfInput.value : '';
  var DELAY = 300, timer = null, controller = null;

  var initial = new URLSearchParams(window.location.search);
  var show = initial.get('show') || '';
  var page = parseInt(initial.get('page'), 10) || 1;
  Array.prototype.forEach.call(form.elements, function (f) {
    if (f.name && initial.has(f.name)) f.value = initial.get(f.name);
  });

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;   // textContent => no HTML injection
    return e;
  }

  function queryString(pg) {
    var params = new URLSearchParams(new FormData(form));
    Array.from(new Set(params.keys())).forEach(function (k) { if (!params.get(k)) params.delete(k); });
    if (show) params.set('show', show);
    if (pg > 1) params.set('page', pg);
    return params.toString();
  }

  function cell(c) {
    var td = el('td', 'brand-name');
    if (typeof c === 'string') { td.textContent = c; return td; }
    var node = el(c.code ? 'code' : 'span', null, c.t);
    if (c.code) node.style.fontFamily = 'monospace';
    if (c.c) node.style.color = c.c;
    if (c.b) node.style.fontWeight = '600';
    td.appendChild(node);
    if (c.s) {
      td.appendChild(document.createElement('br'));
      var small = el('small', null, c.s);
      small.style.color = '#6b7280';
      td.appendChild(small);
    }
    return td;
  }

  function action(a) {
    var cls = 'action-link ' + (a.cls || 'edit-link');
    if (!a.post) {
      var link = el('a', cls, a.label);
      link.href = a.url;
      return link;
    }
    var f = el('form'); f.method = 'post'; f.action = a.url; f.style.display = 'inline';
    var t = document.createElement('input');
    t.type = 'hidden'; t.name = 'csrfmiddlewaretoken'; t.value = csrf;
    var b = el('button', cls, a.label); b.type = 'submit';
    f.appendChild(t); f.appendChild(b);
    return f;
  }

  function emptyRow(empty) {
    var tr = el('tr'), td = el('td', 'empty-state-row'), box = el('div', 'empty-state');
    td.colSpan = cols;
    box.appendChild(el('h3', null, empty.title));
    if (empty.hint) box.appendChild(el('p', null, empty.hint));
    if (empty.action) {
      var a = el('a', 'btn btn-secondary', empty.action.label);
      a.href = empty.action.url;
      box.appendChild(a);
    }
    td.appendChild(box); tr.appendChild(td);
    return tr;
  }

  function renderRows(data) {
    body.textContent = '';
    if (!data.rows.length) { body.appendChild(emptyRow(data.empty)); return; }
    data.rows.forEach(function (row) {
      var tr = el('tr');
      row.cells.forEach(function (c) { tr.appendChild(cell(c)); });
      var td = el('td', 'brand-actions');
      row.actions.forEach(function (a) { td.appendChild(action(a)); td.appendChild(document.createTextNode(' ')); });
      tr.appendChild(td);
      body.appendChild(tr);
    });
  }

  function pageItem(label, target, kind) {
    var li = el('li', 'pagination-item'), node;
    if (target) {
      node = el('a', 'pagination-link', label);
      node.href = '#';
      node.setAttribute('data-page', target);
    } else {
      node = el('span', 'pagination-link ' + kind, label);
    }
    li.appendChild(node);
    return li;
  }

  function renderPager(data) {
    pager.textContent = '';
    if (data.num_pages <= 1) return;
    var box = el('div', 'pagination-container');
    var info = el('span', 'pagination-info');
    info.appendChild(document.createTextNode('Page '));
    info.appendChild(el('strong', null, data.page));
    info.appendChild(document.createTextNode(' of '));
    info.appendChild(el('strong', null, data.num_pages));
    info.appendChild(document.createTextNode(' \u00b7 ' + data.count + ' result' + (data.count === 1 ? '' : 's')));
    var nav = el('nav', 'pagination-nav'), ul = el('ul', 'pagination-list');
    ul.appendChild(pageItem('Previous', data.page > 1 ? data.page - 1 : 0, 'disabled'));
    ul.appendChild(pageItem(String(data.page), 0, 'active'));
    ul.appendChild(pageItem('Next', data.page < data.num_pages ? data.page + 1 : 0, 'disabled'));
    nav.appendChild(ul); box.appendChild(info); box.appendChild(nav); pager.appendChild(box);
  }

  function showError() {
    body.textContent = '';
    body.appendChild(emptyRow({ title: 'Could not load results', hint: 'Check your connection and try again.' }));
    pager.textContent = '';
  }

  function load(pg) {
    page = pg;
    if (controller) controller.abort();
    var mine = controller = new AbortController();
    var qs = queryString(pg);
    var url = window.location.pathname + (qs ? '?' + qs : '');
    table.style.opacity = '0.5';
    table.setAttribute('aria-busy', 'true');

    fetch(url, {
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin',
      signal: mine.signal
    })
      .then(function (r) {
        if (r.redirected) { window.location.reload(); return null; }   // session expired -> login
        if (r.status === 404 && pg > 1) { load(1); return null; }       // page no longer exists
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        renderRows(data);
        renderPager(data);
        history.replaceState(null, '', url);
      })
      .catch(function (err) { if (err.name !== 'AbortError') showError(); })
      .finally(function () {
        if (controller === mine) { table.style.opacity = ''; table.removeAttribute('aria-busy'); }
      });
  }

  form.addEventListener('input', function (e) {
    if (e.target.matches('input[type="search"], input[type="text"]')) {
      clearTimeout(timer);
      timer = setTimeout(function () { load(1); }, DELAY);
    }
  });
  form.addEventListener('change', function (e) {
    if (e.target.matches('select, input[type="date"]')) { clearTimeout(timer); load(1); }
  });
  form.addEventListener('submit', function (e) { e.preventDefault(); clearTimeout(timer); load(1); });
  pager.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-page]');
    if (!a) return;
    e.preventDefault();
    load(parseInt(a.getAttribute('data-page'), 10));
  });

  load(page);
})();
