// Panel: menu boczne na telefonie i przełącznik motywu ciemny ↔ jasny.
(function () {
  const body = document.body;
  document.querySelectorAll("[data-sidebar-toggle]").forEach((b) => b.addEventListener("click", () => body.classList.toggle("sidebar-open")));
  document.querySelectorAll("[data-sidebar-close]").forEach((b) => b.addEventListener("click", () => body.classList.remove("sidebar-open")));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") body.classList.remove("sidebar-open"); });
  // Przełącznik motywu: ciemny ↔ jasny względem tego, co jest widoczne (do pierwszego kliknięcia — motyw systemu).
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const effective = () => document.documentElement.getAttribute("data-theme") || (media && media.matches ? "dark" : "light");
  function sync() {
    const dark = effective() === "dark";
    document.documentElement.classList.toggle("is-dark", dark);
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => {
      const label = dark ? "Włącz jasny motyw" : "Włącz ciemny motyw";
      b.title = label; b.setAttribute("aria-label", label); b.setAttribute("aria-pressed", dark ? "true" : "false");
    });
  }
  document.querySelectorAll("[data-theme-toggle]").forEach((b) => b.addEventListener("click", () => {
    window.satokSetTheme(effective() === "dark" ? "light" : "dark");
    sync();
  }));
  if (media && media.addEventListener) media.addEventListener("change", sync);
  sync();
})();

// Odliczanie do terminu (np. rezerwacja na miejscu czeka przy kiosku na weryfikację legitymacji): <el data-countdown-to="ISO">.
document.querySelectorAll("[data-countdown-to]").forEach((el) => {
  const end = new Date(el.dataset.countdownTo).getTime();
  const tick = () => {
    const s = Math.max(0, Math.round((end - Date.now()) / 1000));
    el.textContent = s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "czas minął";
    if (s > 0) setTimeout(tick, 1000);
  };
  tick();
});
