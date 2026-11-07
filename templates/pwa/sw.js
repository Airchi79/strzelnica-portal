{% load static %}// Service worker: zasoby statyczne z pamięci podręcznej, strony zawsze z sieci (dane muszą być aktualne).
const CACHE = "satok-v3-{{ BRAND.version }}"; // nowa wersja przy zmianie logo — stare zasoby są usuwane
const KIOSK_URL = "{% url 'kiosk:home' %}";
const KIOSK_CACHE = "satok-kiosk"; // osobna pamięć — nie jest czyszczona przy zmianie wersji zasobów
const OFFLINE_URL = "{% url 'core:offline' %}";
const ASSETS = [OFFLINE_URL, "{% static 'css/app.css' %}", "{% static 'js/app.js' %}", "{{ BRAND.icon_192 }}", "{{ BRAND.icon_512 }}", "{% static 'js/kiosk.js' %}"];

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
    return;
  }
  const path = new URL(req.url).pathname;
  if (path === "/logo/" || path.startsWith("/ikona/")) {
    // Logo i ikony (adres z ?v= zmienia się przy każdej zmianie logo): z pamięci, a gdy brak — z sieci i do pamięci.
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return res;
    })));
  }
});

// --- powiadomienia push (treść szyfrowana przez serwer, odszyfrowuje przeglądarka) ---
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: event.data ? event.data.text() : "" }; }
  const title = data.title || "Strzelnica Sątok";
  const tasks = [self.registration.showNotification(title, {
    body: data.body || "",
    icon: "{{ BRAND.icon_192 }}",
    badge: "{{ BRAND.icon_192 }}",
    tag: (data.tag || "satok") + ":" + (data.url || "/") + ":" + Date.now(),
    data: { url: data.url || "/" },
  })];
  if (typeof data.badge === "number" && self.navigator && "setAppBadge" in self.navigator) {
    tasks.push((data.badge ? self.navigator.setAppBadge(data.badge) : self.navigator.clearAppBadge()).catch(() => {}));
  }
  event.waitUntil(Promise.all(tasks));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin);
  if (target.origin !== self.location.origin) return;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    for (const w of wins) {
      if (new URL(w.url).origin === self.location.origin && "focus" in w) return w.navigate(target.href).then((c) => (c || w).focus());
    }
    return self.clients.openWindow(target.href);
  }));
});
