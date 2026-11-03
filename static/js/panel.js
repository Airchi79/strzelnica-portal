// Panel: menu boczne na telefonie i przełącznik motywu (systemowy → ciemny → jasny).
(function () {
  const body = document.body;
  document.querySelectorAll("[data-sidebar-toggle]").forEach((b) => b.addEventListener("click", () => body.classList.toggle("sidebar-open")));
  document.querySelectorAll("[data-sidebar-close]").forEach((b) => b.addEventListener("click", () => body.classList.remove("sidebar-open")));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") body.classList.remove("sidebar-open"); });
  const order = [null, "dark", "light"];
  const labels = { null: "systemowy", dark: "ciemny", light: "jasny" };
  document.querySelectorAll("[data-theme-toggle]").forEach((b) => b.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const next = order[(order.indexOf(current) + 1) % order.length];
    window.satokSetTheme(next);
    b.title = "Motyw: " + labels[next];
  }));
})();
