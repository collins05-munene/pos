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

const APP_SHELL = [
  OFFLINE_URL,
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

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
      await cache.add(OFFLINE_URL);
      const staticCache = await caches.open(STATIC_CACHE);
      await Promise.allSettled(
        APP_SHELL.filter((url) => url !== OFFLINE_URL).map((url) =>
          staticCache.add(url)
        )
      );
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

  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }

  if (isNeverCachePath(url.pathname)) {
    return;
  }

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

  if (request.mode === "navigate") {
  event.respondWith(
    (async () => {
      try {
        return await fetch(request.url, {
          credentials: "same-origin",
          redirect: "follow",
        });
      } catch (err) {
        const offlineCache = await caches.open(OFFLINE_CACHE);
        const offlinePage = await offlineCache.match(OFFLINE_URL);
        return offlinePage || Response.error();
      }
    })()
  );
  return;
}
});