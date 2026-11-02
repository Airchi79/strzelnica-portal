{% load static %}// Service worker: zasoby statyczne z pamięci podręcznej, strony zawsze z sieci (dane muszą być aktualne).
const CACHE = "satok-v2";
const KIOSK_URL = "{% url 'kiosk:home' %}";
const KIOSK_CACHE = "satok-kiosk"; // osobna pamięć — nie jest czyszczona przy zmianie wersji zasobów
const OFFLINE_URL = "{% url 'core:offline' %}";
const ASSETS = [OFFLINE_URL, "{% static 'css/app.css' %}", "{% static 'js/app.js' %}", "{% static 'icons/icon-192.png' %}", "{% static 'js/kiosk.js' %}"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== KIOSK_CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;
  if (req.mode === "navigate" && new URL(req.url).pathname === KIOSK_URL) {
    // Kiosk działa także bez sieci: ostatnia wersja strony kiosku z pamięci (tylko sparowany kiosk — nagłówek X-Kiosk-App).
    event.respondWith(
      fetch(req).then((res) => {
        if (res.ok && res.headers.get("X-Kiosk-App") === "1") { const copy = res.clone(); caches.open(KIOSK_CACHE).then((c) => c.put(KIOSK_URL, copy)); }
        return res;
      }).catch(() => caches.match(KIOSK_URL).then((hit) => hit || caches.match(OFFLINE_URL)))
    );
    return;
  }
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
    return;
  }
  if (req.url.includes("/static/")) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});
