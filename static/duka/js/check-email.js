// The "check your email" page: move on by itself the moment the emailed link has been clicked
// (on this phone/computer or any other), instead of leaving the owner staring at a stale page.
(() => {
  const root = document.getElementById('check-email');
  if (!root) return;
  const statusUrl = root.dataset.status;
  const confirmedBox = document.getElementById('confirmed');
  const notYet = document.getElementById('not-yet');
  const waiting = document.getElementById('waiting');
  const button = document.getElementById('confirmed-btn');

  const POLL_MS = 4000;
  const GIVE_UP_AFTER_MS = 30 * 60 * 1000;
  const startedAt = Date.now();
  let timer = null;
  let leaving = false;
  let inFlight = false;

  function leave(url) {
    if (leaving) return;
    leaving = true;
    clearInterval(timer);
    if (confirmedBox) confirmedBox.classList.add('show');
    if (waiting) waiting.hidden = true;
    if (notYet) notYet.hidden = true;
    if (button) button.disabled = true;
    setTimeout(() => window.location.assign(url), 1200);
  }

  async function check(manual) {
    if (leaving || inFlight) return;
    inFlight = true;
    try {
      const res = await fetch(statusUrl, {
        credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data.state === 'verified' && data.next) {
        leave(data.next);
      } else if (data.state === 'none' && data.next) {
        // Nothing left to wait for here (e.g. the sign-up was replaced): don't strand them on this page.
        window.location.assign(data.next);
      } else if (data.state === 'expired') {
        window.location.reload();      // the server renders the "link expired, send a new one" banner
      } else if (manual && notYet) {
        notYet.hidden = false;
      }
    } catch (_) {
      /* offline or a hiccup: the next tick tries again */
    } finally {
      inFlight = false;
    }
  }

  function tick() {
    if (document.hidden) return;                       // don't poll from a background tab
    if (Date.now() - startedAt > GIVE_UP_AFTER_MS) { clearInterval(timer); return; }
    check(false);
  }

  timer = setInterval(tick, POLL_MS);
  // Coming back from the mail app / other tab is exactly when the link has just been clicked.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(false); });
  window.addEventListener('focus', () => check(false));
  if (button) button.addEventListener('click', () => check(true));
  check(false);
})();
