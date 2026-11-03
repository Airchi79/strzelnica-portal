// Motyw (jasny / ciemny / systemowy) — ładowany w <head>, żeby strona nie mignęła złym kolorem.
(function () {
  var t = null;
  try { t = localStorage.getItem("satok-theme"); } catch (e) { /* brak dostępu do pamięci */ }
  if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
  window.satokSetTheme = function (value) {
    try { if (value) localStorage.setItem("satok-theme", value); else localStorage.removeItem("satok-theme"); } catch (e) { /* ignoruj */ }
    if (value) document.documentElement.setAttribute("data-theme", value); else document.documentElement.removeAttribute("data-theme");
  };
})();
