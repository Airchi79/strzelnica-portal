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
