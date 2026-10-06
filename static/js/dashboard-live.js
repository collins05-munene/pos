(function () {
  'use strict';

  const root = document.getElementById('dash-live');
  if (!root) return;

  const indicator = document.getElementById('live-indicator');
  const indicatorText = document.getElementById('live-text');

  const POLL_MS = 5000;          
  const FORCE_MS = 120000;       
  const MAX_BACKOFF = 5;         
  let version = root.dataset.version;
  let busy = false;
  let failures = 0;
  let lastFull = Date.now();
  let timer = null;

  function setState(state) {
    if (!indicator) return;
    indicator.classList.remove('is-live', 'is-paused', 'is-offline');
    indicator.classList.add('is-' + state);
    indicatorText.textContent =
      state === 'live' ? 'Live' :
      state === 'paused' ? 'Paused (tab hidden)' :
      'Reconnecting…';
  }

  function sessionEnded(response) {
    return response.redirected || response.status === 401 || response.status === 403;
  }

  async function refreshBody() {
    const response = await fetch(root.dataset.bodyUrl, { cache: 'no-store', credentials: 'same-origin' });
    if (sessionEnded(response)) { window.location.reload(); return; }
    if (!response.ok) throw new Error('Body request failed: ' + response.status);

    const html = await response.text();
    version = response.headers.get('X-Dash-Version') || version;
    lastFull = Date.now();

    root.style.minHeight = root.offsetHeight + 'px';
    root.innerHTML = html;
    root.style.minHeight = '';

    root.classList.remove('dash-flash');
    void root.offsetWidth;                    
    root.classList.add('dash-flash');
    setTimeout(function () { root.classList.remove('dash-flash'); }, 900);

    document.dispatchEvent(new CustomEvent('dashboard:updated'));
  }

  async function tick() {
    if (busy) { schedule(); return; }
    if (document.hidden) { setState('paused'); schedule(); return; }

    busy = true;
    try {
      const response = await fetch(root.dataset.versionUrl, { cache: 'no-store', credentials: 'same-origin' });
      if (sessionEnded(response)) { window.location.reload(); return; }
      if (!response.ok) throw new Error('Version request failed: ' + response.status);

      const data = await response.json();
      const changed = String(data.v) !== String(version);
      const stale = Date.now() - lastFull > FORCE_MS;
      if (changed || stale) await refreshBody();

      failures = 0;
      setState('live');
    } catch (err) {
      failures = Math.min(failures + 1, MAX_BACKOFF);
      setState('offline');
    } finally {
      busy = false;
      schedule();
    }
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(tick, POLL_MS * (1 + failures));
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) { clearTimeout(timer); tick(); }
  });
  window.addEventListener('online', function () { failures = 0; clearTimeout(timer); tick(); });

  schedule();
})();