// Weryfikacja dokumentu: obracanie zdjęcia, powiększanie, gotowe powody odrzucenia.
(function () {
  "use strict";
  const img = document.querySelector("[data-scan-img]"), stage = document.querySelector("[data-stage]");
  let deg = 0;
  const rot = document.querySelector("[data-rotate]");
  if (img && rot) rot.addEventListener("click", () => { deg += 90; img.style.transform = `rotate(${deg}deg)`; stage.classList.toggle("sideways", deg % 180 !== 0); });
  if (img && stage) img.addEventListener("click", () => stage.classList.toggle("zoom"));

  const note = document.getElementById("id_note");
  document.querySelectorAll("[data-reason]").forEach((b) => b.addEventListener("click", () => {
    const r = b.dataset.reason;
    if (!note.value.includes(r)) note.value = (note.value.trim() ? note.value.trim() + " " : "") + r;
    note.focus();
  }));
  const form = document.querySelector("[data-review]");
  if (form) form.addEventListener("submit", (e) => {
    const d = e.submitter && e.submitter.value;
    if (d === "reject" && !note.value.trim()) { e.preventDefault(); note.closest(".field").classList.add("has-error"); note.placeholder = "Wpisz powód albo kliknij gotowy powód poniżej"; note.focus(); }
    if (d === "approve") {
      const open = form.closest(".verify-decision").querySelectorAll(".verify-check input:not(:checked)").length;
      if (open && !window.confirm("Nie wszystkie punkty są odhaczone. Zatwierdzić mimo to?")) e.preventDefault();
    }
  });
})();
