{% load static %}// Service worker: zasoby statyczne z pamięci podręcznej, strony zawsze z sieci (dane muszą być aktualne).
const CACHE = "satok-v1";
const OFFLINE_URL = "{% url 'core:offline' %}";
const ASSETS = [OFFLINE_URL, "{% static 'css/app.css' %}", "{% static 'js/app.js' %}", "{% static 'icons/icon-192.png' %}"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
    return;
  }
  if (req.url.includes("/static/")) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});
