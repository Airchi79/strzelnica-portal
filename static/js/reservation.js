// Strona rezerwacji: udostępnianie linku do książki pobytu, kopiowanie, odliczanie do terminu płatności.
(function () {
  "use strict";
  function flash(btn, text) { const old = btn.textContent; btn.textContent = text; setTimeout(() => { btn.textContent = old; }, 1800); }
  async function copy(text, btn) {
    try { await navigator.clipboard.writeText(text); flash(btn, "Skopiowano ✓"); }
    catch (e) { window.prompt("Skopiuj:", text); }
  }
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => copy(b.dataset.copy, b)));
  document.querySelectorAll("[data-share]").forEach((b) => {
    b.addEventListener("click", async () => {
      const url = b.dataset.share, title = b.dataset.shareTitle || document.title;
      if (navigator.share) {
        try { await navigator.share({ title, text: title + " — wpisz się do książki pobytu:", url }); } catch (e) { /* anulowano */ }
      } else copy(url, b);
    });
  });
  const countdowns = document.querySelectorAll("[data-countdown]");
  function tick() {
    countdowns.forEach((el) => {
      const left = new Date(el.dataset.countdown).getTime() - Date.now();
      if (left <= 0) { el.textContent = "(termin minął)"; return; }
      const m = Math.floor(left / 60000), h = Math.floor(m / 60);
      el.textContent = h >= 1 ? `(zostało ${h} h ${m % 60} min)` : `(zostało ${m} min ${Math.floor(left / 1000) % 60} s)`;
    });
  }
  if (countdowns.length) { tick(); setInterval(tick, 1000); }
})();
