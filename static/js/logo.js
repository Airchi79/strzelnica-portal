// Logo: podgląd i generowanie ikon aplikacji w przeglądarce (canvas). Dla SVG serwer przyjmuje ikony stąd
// (sprawdza wymiary i format), dla PNG/JPG robi je sam — te z przeglądarki służą wtedy tylko do podglądu.
(function () {
  "use strict";
  const form = document.getElementById("logo-form");
  if (!form) return;
  const file = form.elements.logo, bg = form.elements.icon_background;
  const box = form.querySelector("[data-logo-new]"), msg = form.querySelector("[data-logo-msg]");
  const submit = form.querySelector("[data-logo-submit]");
  const MAX = 1024 * 1024, R = 1024;
  let square = null; // canvas z kwadratowym, przyciętym logo

  function setMsg(t, err) { msg.textContent = t; msg.style.color = err ? "var(--bad)" : ""; }

  function load(f) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(f), img = new Image();
      img.onload = () => resolve({ img, url });
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("decode")); };
      img.src = url;
    });
  }

  function contentBox(ctx, w, h) {
    const d = ctx.getImageData(0, 0, w, h).data;
    let x0 = w, y0 = h, x1 = -1, y1 = -1, transparent = false;
    for (let i = 3; i < d.length; i += 4) if (d[i] < 250) { transparent = true; break; }
    const c0 = [d[0], d[1], d[2]];
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const on = transparent ? d[i + 3] > 8 : (Math.abs(d[i] - c0[0]) + Math.abs(d[i + 1] - c0[1]) + Math.abs(d[i + 2] - c0[2]) > 48);
      if (on) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    }
    return x1 < 0 ? [0, 0, w, h] : [x0, y0, x1 + 1, y1 + 1];
  }

  function render(size, scale, fill) {
    const c = document.createElement("canvas"); c.width = c.height = size;
    const ctx = c.getContext("2d");
    if (fill) { ctx.fillStyle = fill; ctx.fillRect(0, 0, size, size); }
    const inner = Math.round(size * scale), off = (size - inner) / 2;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(square, off, off, inner, inner);
    return c.toDataURL("image/png");
  }

  function regenerate() {
    if (!square) return;
    const icons = { icon_192: render(192, 0.96), icon_512: render(512, 0.96), icon_maskable: render(512, 0.80, bg.value) };
    Object.entries(icons).forEach(([k, v]) => { form.elements[k].value = v; });
    box.querySelector('[data-prev="icon_maskable"]').src = icons.icon_maskable;
    box.querySelector('[data-prev="icon_maskable2"]').src = icons.icon_maskable;
    box.querySelector('[data-prev="icon_192"]').src = icons.icon_192;
  }

  file.addEventListener("change", async () => {
    square = null; box.hidden = true; ["icon_192", "icon_512", "icon_maskable"].forEach((k) => { form.elements[k].value = ""; });
    const f = file.files && file.files[0];
    if (!f) { setMsg(""); return; }
    if (f.size > MAX) { setMsg("Plik ma ponad 1 MB — zmniejsz go albo zapisz jako SVG.", true); file.value = ""; return; }
    setMsg("Przygotowuję podgląd…");
    try {
      const { img, url } = await load(f);
      const c = document.createElement("canvas"); c.width = c.height = R;
      const ctx = c.getContext("2d");
      const w = img.naturalWidth || R, h = img.naturalHeight || R, k = R / Math.max(w, h);
      ctx.drawImage(img, (R - w * k) / 2, (R - h * k) / 2, w * k, h * k);
      const [x0, y0, x1, y1] = contentBox(ctx, R, R);
      const side = Math.max(x1 - x0, y1 - y0);
      square = document.createElement("canvas"); square.width = square.height = side;
      square.getContext("2d").drawImage(c, x0, y0, x1 - x0, y1 - y0, (side - (x1 - x0)) / 2, (side - (y1 - y0)) / 2, x1 - x0, y1 - y0);
      box.querySelector('[data-prev="logo"]').src = url;
      box.querySelector('[data-prev="logo2"]').src = url;
      regenerate();
      box.hidden = false;
      setMsg("Sprawdź podgląd. Na ikonie aplikacji puste brzegi logo są przycinane automatycznie.");
    } catch (e) {
      setMsg("Nie udało się odczytać pliku — wybierz SVG, PNG albo JPG.", true); file.value = "";
    }
  });
  bg.addEventListener("input", regenerate);
  form.addEventListener("submit", (e) => {
    if (file.files[0] && !form.elements.icon_512.value) { e.preventDefault(); setMsg("Poczekaj na podgląd.", true); }
  });
})();
