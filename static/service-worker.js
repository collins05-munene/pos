/*
 * Service Worker for the POS System PWA.
 *
 * Strategy (deliberately conservative for a POS/inventory/payments app):
 *
 * 1. STATIC ASSETS (css/js/images/icons/manifest under /static/):
 *      cache-first, falling back to network, and populate the cache
 *      as new static files are requested. Safe because Django's
 *      collectstatic + WhiteNoise fingerprint static files, so a new
 *      deploy gets a new URL rather than reusing a stale one.
 *
 * 2. PAGE NAVIGATIONS (a user opening/reloading any HTML page):
 *      network-first. We NEVER cache the HTML response itself, so a
 *      signed-in user always gets a fresh, correctly-authenticated,
 *      up-to-date page when online. Only if the network request
 *      completely fails (device is offline) do we fall back to the
 *      cached offline page.
 *
 * 3. EVERYTHING ELSE (all POST/PUT/PATCH/DELETE requests, and any GET
 *    to admin/, users/, pos/, payments/, inventory/, products/,
 *    suppliers/, or any API/JSON-style endpoint):
 *      network-only, completely bypassed by the service worker. This
 *      guarantees sales, stock levels, M-Pesa/payment callbacks,
 *      authentication and session state are always the live
 *      server response and are never read from cache.
 *
 * Bump CACHE_VERSION whenever the list of app-shell assets changes so
 * old caches are cleaned up automatically on activate.
 */

const CACHE_VERSION = "v1";
const STATIC_CACHE = `pos-static-${CACHE_VERSION}`;
const OFFLINE_CACHE = `pos-offline-${CACHE_VERSION}`;
const OFFLINE_URL = "/offline/";

// Core "app shell" assets pre-cached on install. Keep this list small
// and limited to assets that rarely change in content (only in URL,
// via fingerprinting) — never cache actual business-data pages here.
const APP_SHELL = [
  OFFLINE_URL,
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

// Path prefixes that must ALWAYS go straight to the network, no
// matter the HTTP method. Add new business-critical app prefixes
// here if you add new Django apps.
const NEVER_CACHE_PREFIXES = [
  "/admin/",
  "/users/",
  "/pos/",
  "/payments/",
  "/inventory/",
  "/products/",
  "/suppliers/",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(OFFLINE_CACHE);
      // Cache the offline fallback page individually so a failure to
      // pre-cache an optional static asset never blocks install.
      await cache.add(OFFLINE_URL);
      const staticCache = await caches.open(STATIC_CACHE);
      await Promise.allSettled(
        APP_SHELL.filter((url) => url !== OFFLINE_URL).map((url) =>
          staticCache.add(url)
        )
      );
      // Take over immediately on next load; existing tabs keep their
      // current worker until they navigate again.
      self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => key !== STATIC_CACHE && key !== OFFLINE_CACHE)
          .map((key) => caches.delete(key))
      );
      await self.clients.claim();
    })()
  );
});

// Allow the page to trigger an immediate activation after it has
// shown an "update available" prompt to the user.
self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

function isNeverCachePath(pathname) {
  return NEVER_CACHE_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

function isStaticAsset(pathname) {
  return pathname.startsWith("/static/") || pathname.startsWith("/media/");
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // Only handle same-origin GET requests. Everything else (cross-origin
  // requests, POST/PUT/PATCH/DELETE — sale completion, payments,
  // stock adjustments, login, M-Pesa callbacks, CSRF-protected forms)
  // is left completely untouched so it always hits the real server.
  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }

  // Explicitly business-critical / dynamic areas: never intercept,
  // even though they are GET requests, because they render
  // authenticated, frequently-changing data.
  if (isNeverCachePath(url.pathname)) {
    return;
  }

  // Static assets: cache-first, network fallback, cache what we fetch.
  if (isStaticAsset(url.pathname)) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(STATIC_CACHE);
        const cached = await cache.match(request);
        if (cached) return cached;
        try {
          const response = await fetch(request);
          if (response && response.ok) {
            cache.put(request, response.clone());
          }
          return response;
        } catch (err) {
          return cached || Response.error();
        }
      })()
    );
    return;
  }

  // Page navigations: network-first, offline fallback. The response
  // is deliberately NOT written to any cache, so authenticated/
  // dynamic pages are never replayed stale.
  if (request.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          return await fetch(request);
        } catch (err) {
          const offlineCache = await caches.open(OFFLINE_CACHE);
          const offlinePage = await offlineCache.match(OFFLINE_URL);
          return offlinePage || Response.error();
        }
      })()
    );
    return;
  }

  // Any other same-origin GET (e.g. a JSON endpoint under a future
  // app not listed above): network-only, no caching, but still let a
  // network failure surface normally rather than throwing inside the
  // service worker.
});