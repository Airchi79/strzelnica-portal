/* Strzelnica Sątok — widżet na stronę. Wstaw w miejscu widżetu:
   <div data-satok-widget="zajetosc" data-motyw="jasny"></div>   (rodzaje: zajetosc, teraz, wydarzenia)
   i raz na stronie: <script src="{{ site }}/widget.js" defer></script> */
(function () {
  "use strict";
  var SITE = "{{ site|escapejs }}";
  var n = 0;
  document.querySelectorAll("[data-satok-widget]").forEach(function (el) {
    if (el.dataset.satokReady) return;
    el.dataset.satokReady = "1";
    var kind = el.dataset.satokWidget, q = [];
    ["motyw", "os", "liczba"].forEach(function (k) { if (el.dataset[k]) q.push(k + "=" + encodeURIComponent(el.dataset[k])); });
    var f = document.createElement("iframe");
    f.name = "satok-widget-" + (n++);
    f.src = SITE + "/widget/" + encodeURIComponent(kind) + "/" + (q.length ? "?" + q.join("&") : "");
    f.title = "Strzelnica Sątok — " + kind;
    f.loading = "lazy";
    f.style.cssText = "width:100%;border:0;min-height:220px;display:block;";
    el.appendChild(f);
  });
  window.addEventListener("message", function (e) {
    if (e.origin !== SITE || !e.data || !e.data.satokWidget) return;
    document.querySelectorAll("iframe[name^='satok-widget-']").forEach(function (f) {
      if (f.name === e.data.name) f.style.height = e.data.height + "px";
    });
  });
})();
