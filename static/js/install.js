// Instalacja aplikacji (PWA) jednym przyciskiem:
// - Android (Chrome, Edge, Samsung Internet), Mac i Windows (Chrome, Edge): systemowe okno „Zainstaluj aplikację”
//   (zdarzenie beforeinstallprompt — przechwytywane na każdej stronie, przycisk pokazuje się tam, gdzie jest karta),
// - iPhone/iPad i Safari na Macu: przeglądarka nie pozwala instalować przyciskiem — karta pokazuje dwa kroki instrukcji.
// Karta znika, gdy aplikacja jest już zainstalowana (tryb standalone) albo po „Nie teraz” (30 dni, tylko na tym urządzeniu).
(function () {
  "use strict";
  var KEY = "satok-install-dismissed";
  var DAYS = 30;
  var deferred = null;
  var ua = navigator.userAgent || "";
  var standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  var ios = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var macSafari = !ios && /Macintosh/.test(ua) && /Safari\//.test(ua) && !/Chrome|Chromium|CriOS|Edg\/|OPR\/|Firefox/.test(ua);

  function dismissed() {
    try {
      var at = parseInt(localStorage.getItem(KEY) || "0", 10);
      return at && Date.now() - at < DAYS * 86400000;
    } catch (e) { return false; }
  }
  function cards() { return document.querySelectorAll("[data-install-card]"); }
  function show(mode) {
    if (standalone || dismissed()) return;
    cards().forEach(function (card) {
      card.hidden = false;
      card.querySelectorAll("[data-install-mode]").forEach(function (el) { el.hidden = el.getAttribute("data-install-mode") !== mode; });
    });
  }
  function hideAll() { cards().forEach(function (card) { card.hidden = true; }); }

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    show("button");
  });
  window.addEventListener("appinstalled", function () { deferred = null; hideAll(); });

  document.addEventListener("DOMContentLoaded", function () {
    if (standalone) return;
    if (ios) show("ios");
    else if (macSafari) show("mac-safari");
    document.querySelectorAll("[data-install-button]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!deferred) return;
        deferred.prompt();
        deferred.userChoice.then(function (choice) {
          deferred = null;
          if (choice && choice.outcome === "accepted") hideAll();
        });
      });
    });
    document.querySelectorAll("[data-install-dismiss]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        try { localStorage.setItem(KEY, String(Date.now())); } catch (e) { /* tryb prywatny — tylko ukrycie */ }
        hideAll();
      });
    });
  });
})();
