// Powiadomienia push na tym urządzeniu: włączanie / wyłączanie (pole [data-push] w szablonie).
(() => {
  const box = document.querySelector("[data-push]");
  if (!box) return;
  const onBtn = box.querySelector("[data-push-on]");
  const offBtn = box.querySelector("[data-push-off]");
  const status = box.querySelector("[data-push-status]");
  const url = box.dataset.push;
  const csrf = box.dataset.csrf;
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

  const show = (text, on) => {
    status.textContent = text;
    onBtn.hidden = on !== false;
    offBtn.hidden = on !== true;
    box.hidden = false;
  };
  const post = (data) => fetch(url, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf }, body: JSON.stringify(data),
  }).then((r) => r.json().catch(() => ({ ok: false })));
  const keyBytes = (b64) => {
    const s = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(s, (c) => c.charCodeAt(0));
  };

  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    show(ios && !standalone
      ? "Na iPhonie: w Safari stuknij „Udostępnij” → „Do ekranu początkowego”, otwórz aplikację z ikony i włącz powiadomienia tutaj."
      : "Ta przeglądarka nie obsługuje powiadomień push — dostaniesz je e-mailem.", null);
    return;
  }
  const ready = navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready);

  const refresh = () => ready.then((reg) => reg.pushManager.getSubscription()).then((sub) => {
    if (sub) {
      post(sub.toJSON()); // przypisanie urządzenia do zalogowanego konta (np. po zmianie konta na tym telefonie)
      show("Powiadomienia na tym urządzeniu są włączone.", true);
    } else if (Notification.permission === "denied") {
      show("Powiadomienia są zablokowane w ustawieniach przeglądarki lub telefonu — odblokuj je dla tej strony.", null);
    } else {
      show("Włącz, aby dostawać powiadomienia na telefon, także gdy aplikacja jest zamknięta.", false);
    }
  }).catch(() => show("Nie udało się sprawdzić powiadomień na tym urządzeniu.", null));

  onBtn.addEventListener("click", () => {
    onBtn.disabled = true;
    Notification.requestPermission().then((perm) => {
      if (perm !== "granted") throw new Error("odmowa");
      return Promise.all([ready, fetch(url, { credentials: "same-origin" }).then((r) => r.json())]);
    }).then(([reg, data]) => reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(data.key) }))
      .then((sub) => post(sub.toJSON()))
      .then((res) => { if (!res.ok) throw new Error(res.error || "błąd"); return refresh(); })
      .catch((e) => show(e.message === "odmowa" ? "Nie zezwolono na powiadomienia." : "Nie udało się włączyć powiadomień: " + e.message, false))
      .finally(() => { onBtn.disabled = false; });
  });

  offBtn.addEventListener("click", () => {
    ready.then((reg) => reg.pushManager.getSubscription()).then((sub) => {
      if (!sub) return null;
      const endpoint = sub.endpoint;
      return sub.unsubscribe().then(() => post({ action: "remove", endpoint }));
    }).then(refresh);
  });

  refresh();
})();
