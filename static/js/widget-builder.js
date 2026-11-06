// Panel: generator kodu widżetu z podglądem.
(function () {
  "use strict";
  const box = document.querySelector("[data-widget-builder]");
  if (!box) return;
  const site = box.dataset.site, $ = (s) => box.querySelector(s);
  const kind = $("#w-kind"), theme = $("#w-theme"), lane = $("#w-lane"), count = $("#w-count");
  const preview = document.querySelector("[data-w-preview]");
  function params() {
    const q = new URLSearchParams({ motyw: theme.value });
    if (kind.value === "zajetosc" && lane.value) q.set("os", lane.value);
    if (kind.value === "wydarzenia") q.set("liczba", count.value || "5");
    return q;
  }
  function update() {
    box.querySelector("[data-w-lane-field]").hidden = kind.value !== "zajetosc";
    box.querySelector("[data-w-count-field]").hidden = kind.value !== "wydarzenia";
    const q = params();
    let attrs = `data-satok-widget="${kind.value}" data-motyw="${theme.value}"`;
    if (q.get("os")) attrs += ` data-os="${q.get("os")}"`;
    if (q.get("liczba")) attrs += ` data-liczba="${q.get("liczba")}"`;
    const url = `${site}/widget/${kind.value}/?${q}`;
    $("[data-w-code]").textContent = `<div ${attrs}></div>\n<script src="${site}/widget.js" defer></script>`;
    $("[data-w-iframe]").textContent = `<iframe src="${url}" title="Strzelnica Sątok" style="width:100%;height:560px;border:0" loading="lazy"></iframe>`;
    preview.src = `/widget/${kind.value}/?${q}`;
  }
  [kind, theme, lane, count].forEach((el) => el.addEventListener("change", update));
  box.querySelectorAll("[data-w-copy]").forEach((b) => b.addEventListener("click", async () => {
    const text = box.querySelector(b.dataset.wCopy === "code" ? "[data-w-code]" : "[data-w-iframe]").textContent;
    try { await navigator.clipboard.writeText(text); const t = b.textContent; b.textContent = "Skopiowano ✓"; setTimeout(() => { b.textContent = t; }, 1600); }
    catch (e) { window.prompt("Skopiuj:", text); }
  }));
  update();
})();
