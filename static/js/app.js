// Potwierdzenia operacji, dodawanie wierszy formularzy, rejestracja service workera.
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

document.addEventListener("click", (e) => {
  const confirmBtn = e.target.closest("[data-confirm-click]");
  if (confirmBtn && !window.confirm(confirmBtn.dataset.confirmClick)) {
    e.preventDefault();
    return;
  }
  const btn = e.target.closest("[data-add-form]");
  if (!btn) return;
  const prefix = btn.dataset.addForm;
  const total = document.getElementById(`id_${prefix}-TOTAL_FORMS`);
  const tpl = document.getElementById(`${prefix}-empty`);
  const table = document.getElementById(`${prefix}-table`);
  if (!total || !tpl || !table) return;
  const index = Number(total.value);
  const row = tpl.innerHTML.replace(/__prefix__/g, String(index));
  (table.querySelector("tbody") || table).insertAdjacentHTML("beforeend", row);
  total.value = String(index + 1);
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}

// Ogłoszenia: ukrycie zapamiętane w tej przeglądarce (ważne ogłoszenia, np. zamknięcie strzelnicy, nie mają przycisku).
(function () {
  let hidden = [];
  try { hidden = JSON.parse(localStorage.getItem("satok-hidden-announcements") || "[]"); } catch (e) { hidden = []; }
  document.querySelectorAll("[data-announce]").forEach((el) => {
    const id = el.dataset.announce;
    if (hidden.includes(id) && el.querySelector("[data-announce-close]")) { el.remove(); return; }
    const btn = el.querySelector("[data-announce-close]");
    if (btn) btn.addEventListener("click", () => {
      el.remove(); hidden.push(id);
      try { localStorage.setItem("satok-hidden-announcements", JSON.stringify(hidden.slice(-50))); } catch (e) { /* brak pamięci */ }
    });
  });
})();

// Drobne akcje bez wbudowanych skryptów (polityka CSP: skrypty tylko z plików):
// data-print — drukuj, data-reload — odśwież, data-back — wstecz, data-autosubmit — wyślij formularz po zmianie,
// data-pick="#pole" + data-pick-from=".klasa" — wpisz zaznaczone wartości do ukrytego pola, data-hide-broken — ukryj niewczytany obrazek.
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-print],[data-reload],[data-back],[data-pick]");
  if (!t) return;
  if (t.hasAttribute("data-print")) { e.preventDefault(); window.print(); }
  else if (t.hasAttribute("data-reload")) { e.preventDefault(); location.reload(); }
  else if (t.hasAttribute("data-back")) { e.preventDefault(); history.back(); }
  else if (t.dataset.pick) {
    const field = document.querySelector(t.dataset.pick);
    if (field) field.value = [...document.querySelectorAll(t.dataset.pickFrom || ".pick")].filter((c) => c.checked).map((c) => c.value).join(",");
  }
});
document.addEventListener("change", (e) => {
  const el = e.target.closest("[data-autosubmit]");
  if (el && el.form) el.form.submit();
});
document.querySelectorAll("img[data-hide-broken]").forEach((img) => {
  const hide = () => img.remove();
  if (img.complete && img.naturalWidth === 0) hide(); else img.addEventListener("error", hide);
});

// Liczba nowych powiadomień na ikonie zainstalowanej aplikacji (Android/Chrome, iOS 16.4+, Edge; inne — bez zmian).
(() => {
  const n = document.body.dataset.unread;
  if (n === undefined || !("setAppBadge" in navigator)) return;
  const count = Number(n) || 0;
  (count ? navigator.setAppBadge(count) : navigator.clearAppBadge()).catch(() => {});
})();

// Wylogowanie: to urządzenie przestaje dostawać powiadomienia push tego konta (np. wspólny telefon).
document.addEventListener("submit", (e) => {
  const form = e.target.closest("form[data-push-logout]");
  if (!form || form.dataset.pushDone || !("serviceWorker" in navigator) || !navigator.serviceWorker.controller) return;
  e.preventDefault();
  form.dataset.pushDone = "1";
  const csrf = form.querySelector("[name=csrfmiddlewaretoken]");
  const finish = () => form.submit();
  const timer = setTimeout(finish, 1500); // wylogowanie nie może czekać dłużej
  navigator.serviceWorker.ready.then((reg) => reg.pushManager && reg.pushManager.getSubscription()).then((sub) => {
    if (!sub) return null;
    const endpoint = sub.endpoint;
    return fetch(form.dataset.pushLogout, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf ? csrf.value : "" },
      body: JSON.stringify({ action: "remove", endpoint }),
    }).then(() => sub.unsubscribe());
  }).catch(() => {}).finally(() => { clearTimeout(timer); finish(); });
});
