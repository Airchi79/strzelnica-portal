// Powiadomienia push na tym urządzeniu: włączanie / wyłączanie (pole [data-push] w szablonie).
(() => {
  const box = document.querySelector("[data-push]");
  if (!box) return;
  const onBtn = box.querySelector("[data-push-on]");
  const offBtn = box.querySelector("[data-push-off]");
  const testBtn = box.querySelector("[data-push-test]");
  const status = box.querySelector("[data-push-status]");
  const url = box.dataset.push;
  const csrf = box.dataset.csrf;
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

  const show = (text, on) => {
    status.textContent = text;
    onBtn.hidden = on !== false;
    offBtn.hidden = on !== true;
    testBtn.hidden = on !== true;
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
      show("Włącz przyciskiem poniżej — albo jak w zwykłej aplikacji: zainstaluj stronę (Chrome → ⋮ → „Zainstaluj aplikację”), "
        + "a potem Ustawienia telefonu → Aplikacje → Strzelnica → Powiadomienia → włącz. Po otwarciu aplikacji powiadomienia włączą się same.", false);
    }
  }).catch(() => show("Nie udało się sprawdzić powiadomień na tym urządzeniu.", null));

  // Krok z limitem czasu: gdy przeglądarka nie odpowiada (np. „ciche” pytanie o zgodę), pokazujemy, gdzie szukać przyczyny.
  const step = (text, promise, hint) => {
    status.textContent = text;
    let timer;
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(hint)), 20000); });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
  };
  const permissionHint = "Przeglądarka nie pokazała pytania o zgodę. Sprawdź: Ustawienia telefonu → Aplikacje → Chrome (lub aplikacja Strzelnica) → "
    + "Powiadomienia — muszą być włączone; w Chrome: ⋮ → Informacje o stronie → Uprawnienia → Powiadomienia → Zezwalaj. Potem spróbuj ponownie.";

  onBtn.addEventListener("click", () => {
    onBtn.disabled = true;
    step("Krok 1/3: czekam na Twoją zgodę na powiadomienia…", Notification.requestPermission(), permissionHint)
      .then((perm) => {
        if (perm !== "granted") throw new Error(perm === "denied"
          ? "Powiadomienia są zablokowane dla tej strony. " + permissionHint : "Nie udzielono zgody na powiadomienia.");
        return step("Krok 2/3: rejestruję telefon w usłudze powiadomień…",
          Promise.all([ready, fetch(url, { credentials: "same-origin" }).then((r) => {
            if (!r.ok) throw new Error("serwer odpowiedział błędem " + r.status);
            return r.json();
          })]).then(([reg, data]) => reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(data.key) })),
          "Usługa powiadomień telefonu nie odpowiada (na Androidzie wymagane są usługi Google Play). Spróbuj ponownie za chwilę albo w Chrome.");
      })
      .then((sub) => step("Krok 3/3: zapisuję urządzenie…", post(sub.toJSON()), "Serwer nie odpowiada — spróbuj ponownie."))
      .then((res) => {
        if (!res.ok) throw new Error(res.error || "serwer nie zapisał urządzenia");
        try { localStorage.removeItem("satok-push-off"); localStorage.setItem("satok-push-sync", String(Date.now())); } catch (e) { /* bez pamięci */ }
        return refresh();
      })
      .catch((e) => {
        let msg = e && e.message ? e.message : String(e);
        if (/permission denied|not allowed|NotAllowedError/i.test(msg) || (e && e.name === "NotAllowedError")) msg = permissionHint;
        else if (/push service|AbortError|gcm|fcm/i.test(msg) || (e && e.name === "AbortError")) {
          msg = "usługa powiadomień telefonu odrzuciła rejestrację (" + msg + "). Na Androidzie potrzebne są aktualne usługi Google Play; spróbuj w Chrome.";
        }
        show("Nie udało się włączyć powiadomień: " + msg, false);
      })
      .finally(() => { onBtn.disabled = false; });
  });

  testBtn.addEventListener("click", () => {
    testBtn.disabled = true;
    status.textContent = "Wysyłam powiadomienie testowe…";
    post({ action: "test" }).then((res) => {
      if (res.ok) status.textContent = `Wysłano na ${res.sent} urządzeń(e). Powinno pojawić się za kilka sekund — jeśli nie, sprawdź, czy telefon nie wycisza powiadomień tej aplikacji.`;
      else status.textContent = "Nie udało się wysłać: " + (res.error || (res.errors && res.errors.join("; ")) || (res.gone ? "subskrypcja wygasła — włącz powiadomienia ponownie" : "brak włączonych urządzeń"));
    }).catch(() => { status.textContent = "Brak połączenia z serwerem."; }).finally(() => { testBtn.disabled = false; });
  });

  offBtn.addEventListener("click", () => {
    ready.then((reg) => reg.pushManager.getSubscription()).then((sub) => {
      try { localStorage.setItem("satok-push-off", "1"); } catch (e) { /* bez pamięci */ }
      if (!sub) return null;
      const endpoint = sub.endpoint;
      return sub.unsubscribe().then(() => post({ action: "remove", endpoint }));
    }).then(refresh);
  });

  refresh();
})();
