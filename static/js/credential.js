// Formularz dokumentu: podpowiedzi zależne od rodzaju, zdjęcie prosto z aparatu, podgląd i zmniejszanie zdjęcia.
// Bez JavaScriptu działa zwykłe pole pliku; serwer i tak sprawdza typ (sygnaturę) i rozmiar pliku.
(function () {
  "use strict";
  const form = document.querySelector("#credential-form, [data-photo-form]");
  if (!form) return;
  const hintsEl = document.getElementById("kind-hints");
  const hints = hintsEl ? JSON.parse(hintsEl.textContent) : {};

  // --- podpowiedzi zależne od rodzaju ---
  const setText = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text || ""; };
  function applyHints(kind) {
    const h = hints[kind];
    if (!h) return;
    const num = form.elements.number, issuer = form.elements.issuer, details = form.elements.details;
    const lbl = document.querySelector("#id_number_label .label-text");
    if (lbl) lbl.textContent = h.number_label;
    if (num) num.placeholder = h.number_placeholder;
    if (issuer) issuer.placeholder = h.issuer_placeholder;
    if (details) details.placeholder = h.details_placeholder;
    setText("id_number_help", h.number_help);
    setText("id_issuer_help", h.issuer_help);
    setText("id_valid_until_help", h.valid_help);
  }
  form.querySelectorAll("input[name=kind]").forEach((r) => r.addEventListener("change", () => applyHints(r.value)));
  const checked = form.querySelector("input[name=kind]:checked");
  if (checked) applyHints(checked.value);

  // --- zdjęcie / skan ---
  const box = form.querySelector("[data-scan]");
  if (!box) return;
  const input = box.querySelector("input[type=file]");
  const max = parseInt(box.dataset.max || "5242880", 10);
  const preview = box.querySelector("[data-scan-preview]"), img = box.querySelector("[data-scan-img]");
  const nameEl = box.querySelector("[data-scan-name]"), sizeEl = box.querySelector("[data-scan-size]");
  const msg = box.querySelector("[data-scan-msg]"), defaultMsg = msg.textContent;
  const canReplace = typeof DataTransfer === "function";
  const MAX_SIDE = 2200; // wystarczy do odczytania dokumentu, a plik jest kilkukrotnie mniejszy
  let objectUrl = null, busy = false;
  form.addEventListener("submit", (e) => { if (busy) { e.preventDefault(); setMsg("Chwila — zdjęcie jest jeszcze przygotowywane.", false); } });

  box.classList.add("js-scan");
  box.querySelector(".scan-buttons").hidden = false;
  box.querySelector("[data-scan-camera]").addEventListener("click", () => { input.setAttribute("capture", "environment"); input.click(); });
  box.querySelector("[data-scan-pick]").addEventListener("click", () => { input.removeAttribute("capture"); input.click(); });
  box.querySelector("[data-scan-clear]").addEventListener("click", () => { input.value = ""; show(null); });

  const kb = (n) => n >= 1048576 ? (n / 1048576).toLocaleString("pl-PL", { maximumFractionDigits: 1 }) + " MB" : Math.round(n / 1024) + " kB";
  function setMsg(text, error) { msg.textContent = text; box.classList.toggle("has-error", !!error); }

  function show(file) {
    if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
    if (!file) { preview.hidden = true; img.hidden = true; setMsg(defaultMsg, false); return; }
    preview.hidden = false;
    nameEl.textContent = file.type === "application/pdf" ? "Plik PDF: " + file.name : "Zdjęcie";
    sizeEl.textContent = kb(file.size);
    if (file.type.startsWith("image/")) { objectUrl = URL.createObjectURL(file); img.src = objectUrl; img.hidden = false; }
    else img.hidden = true;
  }

  function loadImage(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file), im = new Image();
      im.onload = () => { URL.revokeObjectURL(url); resolve(im); };
      im.onerror = () => { URL.revokeObjectURL(url); reject(new Error("decode")); };
      im.src = url;
    });
  }

  // Zdjęcie z aparatu bywa duże (8–15 MB) albo w formacie, którego serwer nie przyjmuje (HEIC, WebP).
  // Przeskalowanie do JPG usuwa też metadane zdjęcia (m.in. lokalizację GPS).
  async function shrink(file) {
    const im = await loadImage(file);
    const scale = Math.min(1, MAX_SIDE / Math.max(im.naturalWidth, im.naturalHeight));
    const c = document.createElement("canvas");
    c.width = Math.round(im.naturalWidth * scale); c.height = Math.round(im.naturalHeight * scale);
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, c.width, c.height);
    ctx.drawImage(im, 0, 0, c.width, c.height);
    const blob = await new Promise((r) => c.toBlob(r, "image/jpeg", 0.85));
    if (!blob) throw new Error("encode");
    return new File([blob], "dokument.jpg", { type: "image/jpeg", lastModified: Date.now() });
  }

  input.addEventListener("change", async () => {
    let file = input.files && input.files[0];
    if (!file) { show(null); return; }
    const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
    const isImage = file.type.startsWith("image/") || /\.(heic|heif|jpe?g|png|webp)$/i.test(file.name);
    if (!isPdf && !isImage) { input.value = ""; show(null); setMsg("Ten plik nie jest zdjęciem ani PDF-em — wybierz JPG, PNG lub PDF.", true); return; }
    if (isImage && canReplace) {
      setMsg("Przygotowuję zdjęcie…", false);
      busy = true;
      try {
        const small = await shrink(file);
        if (small.size < file.size || !/^image\/(jpeg|png)$/.test(file.type)) {
          const dt = new DataTransfer(); dt.items.add(small); input.files = dt.files; file = small;
        }
      } catch (e) {
        if (!/^image\/(jpeg|png)$/.test(file.type)) {
          input.value = ""; show(null);
          setMsg("Nie udało się odczytać tego zdjęcia (np. HEIC). Użyj przycisku „Zrób zdjęcie” albo wybierz JPG/PNG.", true);
          return;
        }
      } finally { busy = false; }
    }
    if (file.size > max) {
      input.value = ""; show(null);
      setMsg(`Plik ma ${kb(file.size)} — maksymalnie ${kb(max)}. ${isPdf ? "Zapisz PDF w mniejszej rozdzielczości albo zrób zdjęcie." : "Zrób zdjęcie ponownie."}`, true);
      return;
    }
    show(file);
    setMsg("Gotowe — zdjęcie zostanie wysłane razem z formularzem.", false);
  });
})();
