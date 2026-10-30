// Pole podpisu: rysowanie palcem / myszą na <canvas>, wynik jako PNG w ukrytym polu formularza.
(function () {
  document.querySelectorAll("[data-signature]").forEach((box) => {
    const canvas = box.querySelector("canvas");
    const input = document.getElementById(box.dataset.signature);
    const ctx = canvas.getContext("2d");
    let drawing = false, dirty = false, last = null;

    function resize() {
      const ratio = Math.max(window.devicePixelRatio || 1, 1);
      const w = box.clientWidth;
      canvas.width = w * ratio;
      canvas.height = 180 * ratio;
      canvas.style.width = w + "px";
      canvas.style.height = "180px";
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.lineWidth = 2.5;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.strokeStyle = "#111";
      dirty = false;
      input.value = "";
    }
    const pos = (e) => { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    canvas.addEventListener("pointerdown", (e) => { drawing = true; last = pos(e); canvas.setPointerCapture(e.pointerId); e.preventDefault(); });
    canvas.addEventListener("pointermove", (e) => {
      if (!drawing) return;
      const p = pos(e);
      ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke();
      last = p; dirty = true; e.preventDefault();
    });
    const end = () => { if (drawing && dirty) input.value = canvas.toDataURL("image/png"); drawing = false; };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    box.querySelector("[data-signature-clear]").addEventListener("click", resize);
    box.closest("form").addEventListener("submit", (e) => {
      if (!input.value) { e.preventDefault(); box.classList.add("sig-error"); box.scrollIntoView({ block: "center" }); }
    });
    resize();
  });

  // Pola zależne od odpowiedzi „pozwolenie: tak/nie” i od celu pobytu.
  const form = document.querySelector("form[data-entry-form]");
  if (!form) return;
  const show = (name, visible) => { const el = form.querySelector(`[data-field="${name}"]`); if (el) el.hidden = !visible; };
  function update() {
    const permit = (form.querySelector("input[name=has_permit]:checked") || {}).value;
    show("permit_number", permit === "tak"); show("permit_issuer", permit === "tak"); show("address", permit === "nie");
    const purpose = (form.querySelector("input[name=purpose]:checked") || {}).value;
    show("reservation", purpose === "strzelanie" || purpose === "obserwator");
    show("purpose_note", purpose === "prace" || purpose === "inne");
  }
  form.addEventListener("change", update);
  update();
})();
