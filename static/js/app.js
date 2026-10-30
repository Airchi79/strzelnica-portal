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
