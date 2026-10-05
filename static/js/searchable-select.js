(function () {
  'use strict';

  function csrfToken() {
    const el = document.querySelector('input[name=csrfmiddlewaretoken]');
    if (el) return el.value;
    const m = document.cookie.match(/csrftoken=([^;]+)/);
    return m ? m[1] : '';
  }

  function enhance(root) {
    root.querySelectorAll('select.searchable:not(.ts-done)').forEach(function (el) {
      el.classList.add('ts-done');

      const opts = {
        maxOptions: null,                // don't cap the list at 50
        allowEmptyOption: true,
        plugins: ['clear_button'],
        placeholder: el.dataset.placeholder || 'Type to search…',
        };

      const createUrl = el.dataset.createUrl;
      if (createUrl) {
        opts.createFilter = function (v) { return v.trim().length >= 2; };
        opts.render = {
          option_create: function (data, esc) {
            return '<div class="create">Add <strong>' + esc(data.input) + '</strong>&hellip;</div>';
          },
        };
        opts.create = function (input, callback) {
          fetch(createUrl, {
            method: 'POST',
            headers: {
              'X-CSRFToken': csrfToken(),
              'Content-Type': 'application/x-www-form-urlencoded',
            },
            body: 'name=' + encodeURIComponent(input),
          })
            .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
            .then(function (res) {
              if (!res.ok) { alert(res.d.error || 'Could not create this entry.'); return callback(); }
              callback({ value: String(res.d.id), text: res.d.name });
            })
            .catch(function () { alert('Network error - nothing was created.'); callback(); });
        };
      }

      new TomSelect(el, opts);
    });
  }

  // Also picks up rows added later (purchase / opening-stock "Add another item")
  let pending = false;
  new MutationObserver(function () {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () { pending = false; enhance(document); });
  }).observe(document.documentElement, { childList: true, subtree: true });

  document.addEventListener('DOMContentLoaded', function () { enhance(document); });
})();