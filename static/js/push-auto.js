// Automatyczne włączenie powiadomień push, gdy zgoda jest już udzielona (np. w ustawieniach telefonu dla aplikacji
// „Strzelnica”): przy otwarciu dowolnej strony urządzenie rejestruje się po cichu — bez przycisku i bez pytania.
// Pomijane, gdy użytkownik sam wyłączył powiadomienia na tym urządzeniu (przycisk „Wyłącz”).
(() => {
  const url = document.body.dataset.pushUrl;
  if (!url || document.querySelector("[data-push]")) return; // strona Powiadomień ma własną obsługę (push.js)
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return;
  if (Notification.permission !== "granted") return;
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } return null; };
  if (store("satok-push-off")) return;
  const csrf = document.querySelector("[name=csrfmiddlewaretoken]");
  const keyBytes = (b64) => {
    const s = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(s, (c) => c.charCodeAt(0));
  };
  navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready).then((reg) =>
    reg.pushManager.getSubscription().then((sub) => {
      if (sub) return Date.now() - Number(store("satok-push-sync") || 0) < 864e5 ? null : sub; // potwierdzenie raz na dobę
      return fetch(url, { credentials: "same-origin" }).then((r) => r.json())
        .then((d) => reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(d.key) }));
    })
  ).then((sub) => {
    if (!sub) return null;
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf ? csrf.value : "" }, body: JSON.stringify(sub.toJSON()),
    }).then((r) => { if (r.ok) store("satok-push-sync", String(Date.now())); });
  }).catch(() => {});
})();
