

const CACHE_VERSION = "v3";
const STATIC_CACHE = `pos-static-${CACHE_VERSION}`;
const OFFLINE_CACHE = `pos-offline-${CACHE_VERSION}`;
const OFFLINE_URL = "/offline/";
const SHELL_URL = "/pos/offline/";
const NAV_TIMEOUT_MS = 6000;

const APP_SHELL = [
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

const POS_FALLBACK_PREFIXES = ["/pos/terminal/", "/payments/pos/checkout/"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const offlineCache = await caches.open(OFFLINE_CACHE);
      await offlineCache.add(OFFLINE_URL);
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

// Product thumbnails are addressed by content hash (?v=...), so a cached copy is never stale.
const PRODUCT_THUMB_RE = /^\/products\/product\/\d+\/image\/thumb\/$/;

function isStaticAsset(pathname) {
  return pathname.startsWith("/static/") || pathname.startsWith("/media/") || PRODUCT_THUMB_RE.test(pathname);
}

function fetchWithTimeout(request, ms) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  return fetch(request, { signal: controller.signal })
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

  // CSS/JS: serve the cached copy instantly (works offline) but always refresh it in the background,
  // so a deploy reaches users on their next visit instead of never.
  if (url.pathname.startsWith("/static/") && /\.(css|js)$/.test(url.pathname)) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(STATIC_CACHE);
        const cached = await cache.match(request);
        const network = fetch(request)
          .then((response) => {
            if (response && response.ok) cache.put(request, response.clone());
            return response;
          })
          .catch(() => null);
        if (cached) {
          event.waitUntil(network);
          return cached;
        }
        return (await network) || Response.error();
      })()
    );
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
          // A thumbnail request that bounces to the login page returns 200 HTML: never cache that.
          const isThumb = PRODUCT_THUMB_RE.test(url.pathname);
          const looksRight = !isThumb ||
            (!response.redirected && (response.headers.get("content-type") || "").startsWith("image/"));
          if (response && response.ok && looksRight) {
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
        return await fetchWithTimeout(request, posPage ? NAV_TIMEOUT_MS : 30000);
      } catch (err) {
        return posPage ? offlineShell() : genericOffline();
      }
    })()
  );
});