// Widżet w ramce: zgłasza stronie-gospodarzowi swoją wysokość, żeby ramka nie miała przewijania.
(function () {
  "use strict";
  if (window.parent === window) return;
  let last = 0;
  function report() {
    const h = Math.ceil(document.documentElement.scrollHeight);
    if (h !== last) { last = h; window.parent.postMessage({ satokWidget: true, height: h, name: window.name }, "*"); }
  }
  window.addEventListener("load", report);
  window.addEventListener("resize", report);
  if (window.ResizeObserver) new ResizeObserver(report).observe(document.body);
  report();
})();
