(function () {
  'use strict';

  const root = document.getElementById('dash-live');
  if (!root) return;

  const indicator = document.getElementById('live-indicator');
  const indicatorText = document.getElementById('live-text');

  const POLL_MS = 5000;          // how often to ask "has anything changed?"
  const FORCE_MS = 120000;       // full refresh at least every 2 minutes, so idle/offline times stay fresh
  const MAX_BACKOFF = 5;         // slow down to 5x the poll interval when offline

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
    // A redirect to the login page, or 401/403, means the session is gone.
    return response.redirected || response.status === 401 || response.status === 403;
  }

  async function refreshBody() {
    const response = await fetch(root.dataset.bodyUrl, { cache: 'no-store', credentials: 'same-origin' });
    if (sessionEnded(response)) { window.location.reload(); return; }
    if (!response.ok) throw new Error('Body request failed: ' + response.status);

    const html = await response.text();
    version = response.headers.get('X-Dash-Version') || version;
    lastFull = Date.now();

    // Hold the current height during the swap so the page doesn't jump or lose its scroll position.
    root.style.minHeight = root.offsetHeight + 'px';
    root.innerHTML = html;
    root.style.minHeight = '';

    root.classList.remove('dash-flash');
    void root.offsetWidth;                     // restart the animation
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

  // Catch up immediately when the admin comes back to the tab.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) { clearTimeout(timer); tick(); }
  });
  window.addEventListener('online', function () { failures = 0; clearTimeout(timer); tick(); });

  schedule();
})();