const CACHE_NAME = "prj-00e1-shell-v1";
const APP_SHELL = ["/", "/index.html"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
    )),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) return;
  const requestUrl = new URL(request.url);
  if (requestUrl.pathname.startsWith("/assets/")) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_NAME);
      const cachedVideo = await cache.match(requestUrl.href);
      if (cachedVideo) return cachedVideo;

      const fullRequest = new Request(requestUrl.href, {credentials: request.credentials, cache: "no-store"});
      const response = await fetch(fullRequest);
      if (response.ok) await cache.put(requestUrl.href, response.clone());
      return response;
    })());
    return;
  }
  if (requestUrl.pathname.startsWith("/api/") || requestUrl.pathname === "/media") return;

  event.respondWith(
    fetch(request).then((response) => {
      if (response.ok && request.mode === "navigate") {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put("/", copy));
      }
      return response;
    }).catch(async () => {
      const cached = await caches.match(request, {ignoreSearch: true});
      return cached || caches.match("/");
    }),
  );
});
