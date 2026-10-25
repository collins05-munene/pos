{% load static %}/*
 * Service Worker for the POS System PWA.
 *
 * NOTE: this file is rendered as a Django template (see config/pwa_views.py), so it must never
 * contain the two-character sequences  open-brace open-brace  or  open-brace percent  except for real tags.
 *
 * Strategy
 *
 * 1. STATIC ASSETS (/static/, /media/): cache-first, filled as they are requested.
 *
 * 2. OFFLINE TERMINAL SHELL (/pos/offline/): stale-while-revalidate. It is public and carries no
 *    user data (everything it shows comes from IndexedDB), so it is safe to cache for everybody.
 *    A redirected response (login page, billing lock) is never stored as the shell.
 *
 * 3. PAGE NAVIGATIONS: network-first, HTML is never cached. If the network fails (or is so slow
 *    that it may as well have), the cashier is NOT shown an error page:
 *      - the POS terminal and checkout pages fall back to the offline terminal shell
 *      - everything else falls back to the generic offline page, which offers the offline
 *        terminal if this device is set up for it.
 *
 * 4. EVERYTHING ELSE (POST/PUT/DELETE, /admin/, /users/, /payments/, JSON endpoints incl.
 *    /pos/offline/snapshot/ and /pos/offline/sync/ ...): straight to the network, never cached.
 *    Stock, sales and sessions are always the live server's answer; offline sales are replayed
 *    by the page (pos-offline-core.js), not by the worker.
 */

const CACHE_VERSION = "v2";
const STATIC_CACHE = `pos-static-${CACHE_VERSION}`;
const OFFLINE_CACHE = `pos-offline-${CACHE_VERSION}`;
const OFFLINE_URL = "/offline/";
const SHELL_URL = "/pos/offline/";
const NAV_TIMEOUT_MS = 6000;

const APP_SHELL = [
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "{% static 'js/pos-offline-core.js' %}",
  "{% static 'js/pos-offline-boot.js' %}",
  "{% static 'js/pos-offline-terminal.js' %}",
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

// Pages that must still work with no connection: they fall back to the offline terminal.
const POS_FALLBACK_PREFIXES = ["/pos/terminal/", "/payments/pos/checkout/"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const offlineCache = await caches.open(OFFLINE_CACHE);
      await offlineCache.add(OFFLINE_URL);
      // The shell is best-effort here: a browser that is not signed in still gets the public page.
      await offlineCache.add(SHELL_URL).catch(() => {});
      const staticCache = await caches.open(STATIC_CACHE);
      await Promise.allSettled(APP_SHELL.map((url) => staticCache.add(url)));
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

function fetchWithTimeout(request, ms) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  return fetch(request, { credentials: "same-origin", redirect: "follow", signal: controller.signal })
    .finally(() => clearTimeout(timer));
}

async function offlineShell() {
  const cache = await caches.open(OFFLINE_CACHE);
  return (await cache.match(SHELL_URL)) || (await cache.match(OFFLINE_URL)) || Response.error();
}

async function genericOffline() {
  const cache = await caches.open(OFFLINE_CACHE);
  return (await cache.match(OFFLINE_URL)) || Response.error();
}

// stale-while-revalidate for the public shell
async function shellResponse(request) {
  const cache = await caches.open(OFFLINE_CACHE);
  const cached = await cache.match(SHELL_URL);
  const refresh = fetchWithTimeout(request, NAV_TIMEOUT_MS)
    .then((response) => {
      if (response && response.ok && !response.redirected) cache.put(SHELL_URL, response.clone());
      return response;
    })
    .catch(() => null);
  if (cached) {
    refresh.catch(() => {});
    return cached;
  }
  return (await refresh) || Response.error();
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }

  if (url.pathname === SHELL_URL && request.mode === "navigate") {
    event.respondWith(shellResponse(request));
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

  if (request.mode !== "navigate") {
    return;
  }

  const posPage = POS_FALLBACK_PREFIXES.some((prefix) => url.pathname.startsWith(prefix));

  if (isNeverCachePath(url.pathname) && !posPage) {
    return;
  }

  event.respondWith(
    (async () => {
      try {
        return await fetchWithTimeout(request.url, posPage ? NAV_TIMEOUT_MS : 30000);
      } catch (err) {
        return posPage ? offlineShell() : genericOffline();
      }
    })()
  );
});
