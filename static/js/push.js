// Powiadomienia push na tym urządzeniu: włączanie / wyłączanie / test (pole [data-push] w szablonie).
// Stan zgody czytamy z usługi push (pushManager.permissionState) — w zainstalowanej aplikacji Android przekazuje tam
// zgodę z „Ustawienia → Aplikacje → … → Powiadomienia”. Rejestracja (subscribe) sama pokazuje pytanie o zgodę, gdy jej brak;
// nie używamy Notification.requestPermission, które w części przeglądarek w trybie aplikacji nigdy nie odpowiada.
(() => {
  const box = document.querySelector("[data-push]");
  if (!box) return;
  const onBtn = box.querySelector("[data-push-on]");
  const offBtn = box.querySelector("[data-push-off]");
  const testBtn = box.querySelector("[data-push-test]");
  const status = box.querySelector("[data-push-status]");
  const diag = box.querySelector("[data-push-diag]");
  const url = box.dataset.push;
  const csrf = box.dataset.csrf;
  const ua = navigator.userAgent;
  const ios = /iphone|ipad|ipod/i.test(ua);
  const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const browser = /SamsungBrowser\/[\d.]+/.exec(ua)?.[0] || /EdgA?\/[\d.]+/.exec(ua)?.[0] || /OPR\/[\d.]+/.exec(ua)?.[0]
    || /Firefox\/[\d.]+/.exec(ua)?.[0] || /Chrome\/[\d.]+/.exec(ua)?.[0] || (/Safari/.test(ua) ? "Safari" : "inna");
  const settingsHint = "Sprawdź: Ustawienia telefonu → Aplikacje → Sątok (albo przeglądarka, w której otwierasz stronę) → Powiadomienia → Dozwolone. "
    + "W przeglądarce: ⋮ → Informacje o stronie / Ustawienia strony → Powiadomienia → Zezwalaj. Potem spróbuj ponownie.";

  const setDiag = (extra) => {
    if (!diag) return;
    let perm = "brak";
    try { perm = Notification.permission; } catch (e) { /* brak API */ }
    diag.textContent = `Przeglądarka: ${browser} · tryb aplikacji: ${standalone ? "tak" : "nie"} · zgoda strony: ${perm}${extra ? " · " + extra : ""}`;
  };
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
  }).then((r) => r.json().catch(() => ({ ok: false, error: "serwer odpowiedział błędem " + r.status })));
  const keyBytes = (b64) => {
    const s = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(s, (c) => c.charCodeAt(0));
  };
  const withTimeout = (promise, ms, message) => {
    let timer;
    const t = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); });
    return Promise.race([promise, t]).finally(() => clearTimeout(timer));
  };

  setDiag();
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    show(ios && !standalone
      ? "Na iPhonie: w Safari stuknij „Udostępnij” → „Do ekranu początkowego”, otwórz aplikację z ikony i włącz powiadomienia tutaj."
      : "Ta przeglądarka nie obsługuje powiadomień push — dostaniesz je e-mailem.", null);
    return;
  }
  let reg = null;
  let serverKey = null;
  const ready = navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready).then((r) => { reg = r; return r; });
  // klucz serwera pobrany zawczasu — po stuknięciu przycisku rejestracja rusza od razu (pytanie o zgodę wymaga „świeżego” stuknięcia)
  fetch(url, { credentials: "same-origin" }).then((r) => r.json()).then((d) => { serverKey = d.key; }).catch(() => {});
  const permState = () => ready.then((r) => (r.pushManager.permissionState
    ? r.pushManager.permissionState({ userVisibleOnly: true }) : Promise.resolve(Notification.permission))).catch(() => "nieznany");

  const refresh = () => ready.then((r) => Promise.all([r.pushManager.getSubscription(), permState()])).then(([sub, state]) => {
    setDiag(`zgoda usługi push: ${state}`);
    if (sub) {
      post(sub.toJSON()); // przypisanie urządzenia do zalogowanego konta (np. po zmianie konta na tym telefonie)
      show("Powiadomienia na tym urządzeniu są włączone.", true);
    } else if (state === "denied") {
      show("Powiadomienia są zablokowane dla tej strony. " + settingsHint, false);
    } else if (state === "granted") {
      show("Zgoda na powiadomienia jest udzielona — stuknij, aby zarejestrować to urządzenie.", false);
    } else {
      show("Włącz przyciskiem poniżej — albo jak w zwykłej aplikacji: Ustawienia telefonu → Aplikacje → Sątok → Powiadomienia → "
        + "Dozwolone; po otwarciu aplikacji powiadomienia włączą się same.", false);
    }
  }).catch((e) => { setDiag("błąd: " + (e && e.message)); show("Nie udało się sprawdzić powiadomień na tym urządzeniu.", null); });

  const subscribe = (r) => {
    const doIt = (key) => r.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
    if (serverKey) return doIt(serverKey); // bez czekania — w tej samej chwili co stuknięcie
    return fetch(url, { credentials: "same-origin" }).then((x) => {
      if (!x.ok) throw new Error("serwer odpowiedział błędem " + x.status);
      return x.json();
    }).then((d) => { serverKey = d.key; return doIt(d.key); });
  };

  onBtn.addEventListener("click", () => {
    onBtn.disabled = true;
    status.textContent = "Rejestruję telefon w usłudze powiadomień… (jeśli pojawi się pytanie o zgodę — wybierz „Zezwalaj”)";
    const start = reg ? Promise.resolve(reg) : ready;
    withTimeout(start.then(subscribe), 30000, "telefon nie odpowiedział w ciągu 30 s. " + settingsHint)
      .then((sub) => withTimeout(post(sub.toJSON()), 15000, "serwer nie odpowiada — spróbuj ponownie."))
      .then((res) => {
        if (!res.ok) throw new Error(res.error || "serwer nie zapisał urządzenia");
        try { localStorage.removeItem("satok-push-off"); localStorage.setItem("satok-push-sync", String(Date.now())); } catch (e) { /* bez pamięci */ }
        return refresh();
      })
      .catch((e) => {
        let msg = e && e.message ? e.message : String(e);
        const name = e && e.name ? e.name : "";
        if (name === "NotAllowedError" || /permission denied|not allowed/i.test(msg)) msg = "nie ma zgody na powiadomienia. " + settingsHint;
        else if (name === "AbortError" || /push service|gcm|fcm/i.test(msg)) {
          msg = "usługa powiadomień telefonu odrzuciła rejestrację (" + msg + "). Na Androidzie potrzebne są aktualne usługi Google Play.";
        }
        setDiag("błąd: " + (name ? name + ": " : "") + (e && e.message ? e.message : e));
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
    ready.then((r) => r.pushManager.getSubscription()).then((sub) => {
      try { localStorage.setItem("satok-push-off", "1"); } catch (e) { /* bez pamięci */ }
      if (!sub) return null;
      const endpoint = sub.endpoint;
      return sub.unsubscribe().then(() => post({ action: "remove", endpoint }));
    }).then(refresh);
  });

  refresh();
})();
