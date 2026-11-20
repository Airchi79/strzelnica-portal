// Promocja: kopiowanie tekstu publikacji (pole formularza) do schowka.
(function () {
  "use strict";
  function flash(btn, text) { var old = btn.textContent; btn.textContent = text; setTimeout(function () { btn.textContent = old; }, 1800); }
  document.querySelectorAll("[data-copy-from]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var el = document.getElementById(btn.getAttribute("data-copy-from"));
      if (!el) return;
      if (navigator.clipboard) {
        navigator.clipboard.writeText(el.value).then(function () { flash(btn, "Skopiowano ✓"); }, function () { el.select(); });
      } else { el.select(); }
    });
  });
  // Formularz generowania: blokada przycisku i komunikat na czas przygotowania szkicu (do minuty).
  document.querySelectorAll("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function () {
      form.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = true; b.textContent = form.getAttribute("data-busy"); });
    });
  });
})();
