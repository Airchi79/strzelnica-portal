// Kiosk: rezerwacja na miejscu (dziś). Termin → dane → kod z e-maila → regulamin i oświadczenie → legitymacja PS ze zdjęciem
// (ramka czerwona/zielona, zdjęcie robione samo, podgląd: Potwierdź / Ponów próbę) → weryfikacja przez obsługę (do 15 min)
// → płatność kodem QR na własnym telefonie → wpis do książki pobytu. Wymaga połączenia z internetem.
(function () {
  "use strict";
  const K = window.SatokKiosk;
  if (!K || !document.querySelector("[data-os-open]")) return;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const pln = (gr) => (gr / 100).toFixed(2).replace(".", ",") + " zł";
  const DIAG = location.hash === "#diag"; // podgląd wskaźników kamery (strojenie progów na urządzeniu)

  let flow = null; // { ref, secret } — przebieg na serwerze
  let st = null; // ostatni stan przebiegu z serwera
  let pick = {}; // wybrany termin: lane, start, end, persons
  let slots = [];
  let maxPersons = 10;
  let psData = null;
  let photo = null;
  let pollTimer = null;
  let tickTimer = null;
  let secondsLeft = 0;

  // --- pomocnicze --------------------------------------------------------------------------------------------------
  function err(name, text) {
    const el = $(`[data-os-error="${name}"]`);
    if (el) { el.textContent = text || ""; el.hidden = !text; if (text) el.scrollIntoView({ block: "center" }); }
  }
  function clearErrors() { $$("[data-os-error]").forEach((el) => { el.hidden = true; el.textContent = ""; }); }
  function busy(on) { $$("[data-view^='os-'] button").forEach((b) => { if (!b.matches("[data-go]")) b.disabled = on; }); }
  const NET = "Brak połączenia z serwerem. Sprawdź internet i spróbuj ponownie.";

  async function get(path) {
    const { status, data } = await K.api("na-miejscu/" + path);
    if (status !== 200) throw new Error(data.error || NET);
    return data;
  }
  async function step(name, extra) {
    let res;
    try {
      res = await K.api(`na-miejscu/${name}/`, { method: "POST", body: JSON.stringify({ ref: flow.ref, secret: flow.secret, ...(extra || {}) }) });
    } catch (e) { throw new Error(NET); }
    if (res.status === 404 && res.data.restart) {
      reset();
      K.done("Rezerwacja wygasła", res.data.error || "Zacznij od nowa.");
      throw Object.assign(new Error(""), { silent: true });
    }
    if (res.status !== 200) throw new Error(res.data.error || "Coś poszło nie tak. Spróbuj ponownie.");
    st = res.data.state;
    return st;
  }
  async function run(errName, fn) {
    clearErrors();
    busy(true);
    try { await fn(); } catch (e) { if (!e.silent) err(errName, e.message); } finally { busy(false); }
  }
  function chips(container, items, selected, onPick) {
    container.innerHTML = "";
    items.forEach(({ value, label }) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn secondary"; b.textContent = label;
      b.setAttribute("aria-pressed", String(value === selected));
      b.addEventListener("click", () => onPick(value));
      container.appendChild(b);
    });
  }
  function reset() {
    flow = null; st = null; pick = {}; psData = null; photo = null;
    stopPolling(); stopCam();
    $$("[data-os-form]").forEach((f) => f.reset());
    $("[data-os-cash-accept]").checked = false;
    clearErrors();
  }

  K.onShow((view) => {
    if (view === "home") reset();
    if (view !== "os-camera") stopCam();
    if (view !== "os-wait") stopPolling();
  });

  // --- 1. termin ---------------------------------------------------------------------------------------------------
  $("[data-os-open]").addEventListener("click", async () => {
    if (!K.isOnline()) { K.done("Brak połączenia", "Rezerwacja na miejscu wymaga internetu. Spróbuj za chwilę albo zarezerwuj przez stronę strzelnicy."); return; }
    reset();
    K.show("os-slots");
    await loadSlots();
  });
  async function loadSlots() {
    const box = $("[data-os-lanes]");
    $("[data-os-when]").hidden = true;
    $("[data-os-price]").textContent = "";
    $("[data-os-next=slots]").disabled = true;
    try {
      const data = await get("terminy/");
      slots = data.lanes; maxPersons = Math.min(data.max_persons || 10, 12);
    } catch (e) { box.innerHTML = ""; err("slots", e.message); return; }
    if (!slots.length) { box.innerHTML = '<span class="muted">Dziś nie ma już wolnych terminów.</span>'; return; }
    renderSlots();
  }
  function contiguous(lane, start) {
    const night = lane.night || [];
    const free = new Set(night.includes(start) ? night : lane.hours); // bez łączenia godzin otwarcia z zamknięciem
    let n = 0;
    while (free.has(start + n) && n < 12) n += 1;
    return n;
  }
  function renderSlots() {
    chips($("[data-os-lanes]"), slots.map((l) => ({ value: l.lane, label: l.name })), pick.lane, (v) => { pick = { lane: v }; renderSlots(); });
    const lane = slots.find((l) => l.lane === pick.lane);
    $("[data-os-when]").hidden = !lane;
    if (!lane) return;
    const hourChips = (lane.night || []).slice(0, 1).map((h) => ({ value: h, label: `${String(h).padStart(2, "0")}:00 · teraz, poza godzinami` }))
      .concat(lane.hours.map((h) => ({ value: h, label: `${String(h).padStart(2, "0")}:00` })));
    chips($("[data-os-hours]"), hourChips, pick.start,
      (v) => { pick = { lane: pick.lane, start: v, persons: pick.persons }; renderSlots(); });
    const durations = pick.start === undefined ? [] : Array.from({ length: contiguous(lane, pick.start) }, (_, i) => i + 1);
    chips($("[data-os-durations]"), durations.map((d) => ({ value: d, label: `${d} h` })), pick.end !== undefined ? pick.end - pick.start : undefined,
      (v) => { pick.end = pick.start + v; renderSlots(); });
    chips($("[data-os-persons]"), Array.from({ length: maxPersons }, (_, i) => ({ value: i + 1, label: String(i + 1) })), pick.persons,
      (v) => { pick.persons = v; renderSlots(); });
    updatePrice();
  }
  let priceSeq = 0;
  async function updatePrice() {
    const out = $("[data-os-price]");
    const next = $("[data-os-next=slots]");
    next.disabled = true;
    out.textContent = "";
    if (pick.lane === undefined || pick.start === undefined || pick.end === undefined || !pick.persons) return;
    const seq = ++priceSeq;
    try {
      const q = await get(`cena/?os=${pick.lane}&od=${pick.start}&do=${pick.end}&osoby=${pick.persons}`);
      if (seq !== priceSeq) return;
      pick.label = q.label;
      out.textContent = `${q.label} · ${pick.persons} os. · do zapłaty ${pln(q.amount)}` + (q.night
        ? " · POZA GODZINAMI OTWARCIA: tylko prowadzący ze zweryfikowaną legitymacją PS, płatność od razu." : "");
      next.disabled = false;
      clearErrors();
    } catch (e) { if (seq === priceSeq) err("slots", e.message); }
  }
  $("[data-os-next=slots]").addEventListener("click", () => {
    if (flow && st && st.status === "potwierdzony") { afterRules(); return; } // zmiana terminu po odmowie — dane już są
    K.show("os-person");
    $("#os-fn").focus();
  });
  $$("[data-os-back]").forEach((b) => b.addEventListener("click", () => K.show(b.dataset.osBack)));

  // --- 2. dane osoby i kod z e-maila -------------------------------------------------------------------------------
  $("[data-os-form=person]").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target.elements;
    const data = { first_name: f.first_name.value.trim(), last_name: f.last_name.value.trim(), email: f.email.value.trim(), phone: f.phone.value.trim() };
    if (!data.first_name || !data.last_name) { err("person", "Podaj imię i nazwisko."); return; }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(data.email)) { err("person", "Podaj prawidłowy adres e-mail."); return; }
    if (data.phone.replace(/\D/g, "").length < 9) { err("person", "Podaj numer telefonu."); return; }
    run("person", async () => {
      let res;
      try { res = await K.api("na-miejscu/start/", { method: "POST", body: JSON.stringify(data) }); } catch (x) { throw new Error(NET); }
      if (res.status !== 200) throw new Error(res.data.error || "Nie udało się wysłać kodu.");
      flow = { ref: res.data.ref, secret: res.data.secret };
      st = res.data.state;
      $("[data-os-email]").textContent = data.email;
      K.show("os-code");
      $("#os-code").focus();
    });
  });
  let qrWanted = false;
  $("[data-os-qr]").addEventListener("click", () => {
    qrWanted = true;
    if (K.canScan()) K.scan(); // kamera; czytnik USB działa bez tego
    else err("person", "Przyłóż „Mój kod QR” do czytnika pod ekranem.");
  });
  K.onCode((text) => {
    const m = /\/ksiazka\/osoba\/([^/?#\s]+)/.exec(text);
    if (!m || !(qrWanted || K.current() === "os-person" || K.current() === "os-lookup")) return false;
    qrWanted = false;
    K.show("os-person");
    run("person", async () => {
      let res;
      try { res = await K.api("na-miejscu/start/", { method: "POST", body: JSON.stringify({ mode: "qr", token: m[1] }) }); } catch (x) { throw new Error(NET); }
      if (res.status !== 200) throw new Error(res.data.error || "Nieznany kod.");
      flow = { ref: res.data.ref, secret: res.data.secret };
      st = res.data.state;
      showRules();
    });
    return true;
  });
  $("[data-os-form=lookup]").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target.elements;
    const data = { mode: "ps", last_name: f.last_name.value.trim(), number: f.number.value.trim() };
    if (!data.last_name || !data.number) { err("lookup", "Podaj nazwisko i numer legitymacji."); return; }
    run("lookup", async () => {
      let res;
      try { res = await K.api("na-miejscu/start/", { method: "POST", body: JSON.stringify(data) }); } catch (x) { throw new Error(NET); }
      if (res.status !== 200) throw new Error(res.data.error || "Nie znaleziono.");
      flow = { ref: res.data.ref, secret: res.data.secret };
      st = res.data.state;
      $("[data-os-email]").textContent = res.data.masked_email;
      K.show("os-code");
      $("#os-code").focus();
    });
  });

  $("[data-os-form=code]").addEventListener("submit", (e) => {
    e.preventDefault();
    const code = e.target.elements.code.value.replace(/\D/g, "");
    if (code.length !== 6) { err("code", "Wpisz 6 cyfr kodu z e-maila."); return; }
    run("code", async () => { await step("kod", { code }); showRules(); });
  });

  // --- 3. regulamin i oświadczenie ----------------------------------------------------------------------------------
  function showRules() {
    $("[data-os-name]").textContent = st.name || "";
    const regs = st.regulations || [];
    const list = $("[data-os-regs]");
    list.innerHTML = "";
    regs.forEach((r) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn secondary"; b.textContent = `Przeczytaj: ${r.title}`;
      b.addEventListener("click", () => openRegulation(r.url));
      li.appendChild(b); list.appendChild(li);
    });
    $("[data-os-regs-box]").hidden = !regs.length;
    K.show("os-rules");
  }
  async function openRegulation(url) {
    const dlg = $("[data-os-dialog]");
    const body = $("[data-os-dialog-body]");
    body.textContent = "Wczytywanie…";
    dlg.showModal();
    try {
      const res = await fetch(url, { credentials: "same-origin" });
      const doc = new DOMParser().parseFromString(await res.text(), "text/html");
      const main = doc.querySelector("main") || doc.body;
      $$("script, form, nav", main).forEach((el) => el.remove());
      body.replaceChildren(...Array.from(main.childNodes).map((n) => document.importNode(n, true)));
    } catch (e) { body.textContent = "Nie udało się wczytać regulaminu. Treść jest też na stronie strzelnicy."; }
  }
  $("[data-os-dialog-close]").addEventListener("click", () => $("[data-os-dialog]").close());
  $("[data-os-form=rules]").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target.elements;
    const regs = (st.regulations || []).length > 0;
    if (regs && !f.regs.checked) { err("rules", "Zaakceptuj regulamin."); return; }
    if (!f.statement.checked) { err("rules", "Zaznacz oświadczenie."); return; }
    run("rules", async () => {
      if (regs) await step("regulamin");
      await step("oswiadczenie", { accept: true });
      await afterRules();
    });
  });
  async function afterRules() {
    if (st.ps === "zweryfikowana" || st.ps === "weryfikacja") { await run("slots", book); return; }
    K.show("os-ps");
    $("#os-pn").focus();
  }

  // --- 4. legitymacja PS: dane, kamera z ramką, podgląd ---------------------------------------------------------------
  // Uprawnienia PS są bezterminowe — bez pola „ważna do”. Najczęstsi wystawcy jako przyciski (szybciej, pewniejszy odczyt).
  const ISSUERS = [["PZSS", "Polski Związek Strzelectwa Sportowego"], ["PZŁ", "Polski Związek Łowiecki"], ["LOK", "Liga Obrony Kraju"], ["Inny", ""]];
  let issuerPick = "";
  function renderIssuers() {
    chips($("[data-os-issuers]"), ISSUERS.map(([label]) => ({ value: label, label })), issuerPick, (v) => {
      issuerPick = v;
      const other = $("#os-pi");
      other.hidden = v !== "Inny";
      if (v === "Inny") { other.value = ""; other.focus(); }
      renderIssuers();
    });
  }
  renderIssuers();
  K.onShow((view) => { if (view === "home") { issuerPick = ""; $("#os-pi").hidden = true; renderIssuers(); } });
  $("[data-os-form=ps]").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target.elements;
    const known = ISSUERS.find(([label]) => label === issuerPick && label !== "Inny");
    const issuer = known ? `${known[0]} (${known[1]})` : f.issuer.value.trim();
    psData = { number: f.number.value.trim(), issuer, issued_on: f.issued_on.value, valid_until: "" };
    if (!psData.number || !psData.issuer || !psData.issued_on) { err("ps", "Podaj numer legitymacji, kto ją wydał i datę wydania."); return; }
    if (psData.issued_on > new Date().toISOString().slice(0, 10)) { err("ps", "Data wydania nie może być z przyszłości."); return; }
    startCam();
  });

  const CARD = 85.6 / 53.98; // proporcje karty ID-1 (legitymacja)
  // Progi do dostrojenia na docelowym urządzeniu (podgląd wskaźników: adres kiosku z #diag).
  // Odblask = odsetek pikseli całkowicie prześwietlonych (jasna, dobrze naświetlona karta nie jest odblaskiem).
  const LIMITS = { sharp: 40, edge: 12, motion: 6, darkMax: 70, brightMax: 248, glareLevel: 252, glare: 0.06, steadyChecks: 5, manualAfterMs: 12000 };
  const video = $("[data-os-video]");
  const frame = $("[data-os-frame]");
  const hint = $("[data-os-hint]");
  const small = document.createElement("canvas");
  const sctx = small.getContext("2d", { willReadFrequently: true });
  const cam = { stream: null, timer: null, okRun: 0, prev: null, started: 0 };

  function frameRect(vw, vh) {
    let w = vw * 0.82, h = w / CARD;
    if (h > vh * 0.82) { h = vh * 0.82; w = h * CARD; }
    return { x: (vw - w) / 2, y: (vh - h) / 2, w, h };
  }
  function placeFrame() {
    const vw = video.videoWidth, vh = video.videoHeight;
    if (!vw) return;
    const box = $("[data-os-cam]");
    const scale = Math.min((box.parentElement.clientWidth || vw) / vw, (window.innerHeight * 0.6) / vh);
    Object.assign(video.style, { width: vw * scale + "px", height: vh * scale + "px" });
    Object.assign(box.style, { width: vw * scale + "px", margin: "0 auto" });
    const r = frameRect(vw, vh);
    Object.assign(frame.style, { left: r.x * scale + "px", top: r.y * scale + "px", width: r.w * scale + "px", height: r.h * scale + "px" });
  }
  async function startCam() {
    stopCam();
    K.show("os-camera");
    frame.classList.remove("ok");
    hint.textContent = "Pokaż legitymację do kamery tak, aby wypełniła ramkę.";
    try {
      cam.stream = await navigator.mediaDevices.getUserMedia({
        audio: false, video: { facingMode: { ideal: K.camera() }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      });
      video.srcObject = cam.stream;
      await video.play();
    } catch (e) {
      stopCam();
      K.show("os-ps");
      err("ps", "Kamera niedostępna — zezwól przeglądarce na jej użycie albo poproś obsługę o pomoc.");
      return;
    }
    cam.okRun = 0; cam.prev = null; cam.started = Date.now();
    $("[data-os-manual]").hidden = true;
    $("[data-os-diag]").hidden = !DIAG;
    placeFrame();
    cam.timer = setInterval(analyze, 200);
  }
  function stopCam() {
    clearInterval(cam.timer); cam.timer = null;
    if (cam.stream) { cam.stream.getTracks().forEach((t) => t.stop()); cam.stream = null; }
    video.srcObject = null;
  }
  window.addEventListener("resize", () => { if (cam.stream) placeFrame(); });

  // Ocena klatki (bez bibliotek): jasność, odblaski, ostrość (wariancja laplasjanu), krawędzie karty przy krawędziach ramki,
  // bezruch. Wszystko na pomniejszonym wycinku — szybko także na tanim tablecie.
  function analyze() {
    const vw = video.videoWidth, vh = video.videoHeight;
    if (!vw || video.readyState < 2) return;
    if (!frame.style.width) placeFrame();
    const r = frameRect(vw, vh);
    const m = 0.12;
    const sx = Math.max(r.x - r.w * m, 0), sy = Math.max(r.y - r.h * m, 0);
    const sw = Math.min(r.w * (1 + 2 * m), vw - sx), sh = Math.min(r.h * (1 + 2 * m), vh - sy);
    const W = 240, H = Math.max(Math.round((W * sh) / sw), 40);
    small.width = W; small.height = H;
    sctx.drawImage(video, sx, sy, sw, sh, 0, 0, W, H);
    const px = sctx.getImageData(0, 0, W, H).data;
    const g = new Float32Array(W * H);
    for (let i = 0, j = 0; i < g.length; i += 1, j += 4) g[i] = 0.299 * px[j] + 0.587 * px[j + 1] + 0.114 * px[j + 2];
    const fx0 = Math.round(((r.x - sx) / sw) * W), fx1 = Math.round(((r.x + r.w - sx) / sw) * W);
    const fy0 = Math.round(((r.y - sy) / sh) * H), fy1 = Math.round(((r.y + r.h - sy) / sh) * H);
    // jasność, odblaski, ostrość — wnętrze ramki
    let sum = 0, glare = 0, n = 0, ls = 0, ls2 = 0, ln = 0, motion = 0, mn = 0;
    for (let y = fy0 + 2; y < fy1 - 2; y += 1) {
      for (let x = fx0 + 2; x < fx1 - 2; x += 1) {
        const i = y * W + x, v = g[i];
        sum += v; n += 1; if (v >= LIMITS.glareLevel) glare += 1;
        const lap = 4 * v - g[i - 1] - g[i + 1] - g[i - W] - g[i + W];
        ls += lap; ls2 += lap * lap; ln += 1;
        if (cam.prev && cam.prev.length === g.length && (x & 3) === 0 && (y & 3) === 0) { motion += Math.abs(v - cam.prev[i]); mn += 1; }
      }
    }
    cam.prev = g;
    const mean = sum / Math.max(n, 1);
    const glareFrac = glare / Math.max(n, 1);
    const sharp = ls2 / Math.max(ln, 1) - (ls / Math.max(ln, 1)) ** 2;
    const move = mn ? motion / mn : 99;
    // krawędzie karty: najsilniejsza linia w pasie wokół każdej krawędzi ramki
    const band = Math.max(Math.round(W * 0.06), 3);
    const colEdge = (x0) => {
      let best = 0;
      for (let x = Math.max(x0 - band, 1); x <= Math.min(x0 + band, W - 2); x += 1) {
        let s = 0, c = 0;
        for (let y = fy0; y < fy1; y += 2) { s += Math.abs(g[y * W + x + 1] - g[y * W + x - 1]); c += 1; }
        best = Math.max(best, s / Math.max(c, 1));
      }
      return best;
    };
    const rowEdge = (y0) => {
      let best = 0;
      for (let y = Math.max(y0 - band, 1); y <= Math.min(y0 + band, H - 2); y += 1) {
        let s = 0, c = 0;
        for (let x = fx0; x < fx1; x += 2) { s += Math.abs(g[(y + 1) * W + x] - g[(y - 1) * W + x]); c += 1; }
        best = Math.max(best, s / Math.max(c, 1));
      }
      return best;
    };
    const edges = [colEdge(fx0), colEdge(fx1), rowEdge(fy0), rowEdge(fy1)];
    const edgesOk = edges.filter((e) => e >= LIMITS.edge).length >= 3;
    let problem = "";
    if (mean < LIMITS.darkMax) problem = "Za ciemno — zbliż legitymację do światła.";
    else if (mean > LIMITS.brightMax || glareFrac > LIMITS.glare) problem = "Odblask — lekko przechyl legitymację.";
    else if (!edgesOk) problem = "Ustaw legitymację dokładnie w ramce (krawędzie przy krawędziach ramki).";
    else if (sharp < LIMITS.sharp) problem = "Obraz nieostry — przytrzymaj legitymację nieruchomo, nieco dalej.";
    else if (move > LIMITS.motion) problem = "Nie ruszaj legitymacją…";
    const ok = !problem;
    cam.okRun = ok ? cam.okRun + 1 : 0;
    frame.classList.toggle("ok", ok);
    hint.textContent = ok ? "Dobrze — nie ruszaj, robię zdjęcie…" : problem;
    if (DIAG) $("[data-os-diag]").textContent = `jasność ${mean.toFixed(0)} · odblask ${(glareFrac * 100).toFixed(1)}% · ostrość ${sharp.toFixed(0)} · krawędzie ${edges.map((e) => e.toFixed(0)).join("/")} · ruch ${move.toFixed(1)}`;
    if (Date.now() - cam.started > LIMITS.manualAfterMs) $("[data-os-manual]").hidden = false;
    if (cam.okRun >= LIMITS.steadyChecks) capture();
  }
  function capture() {
    const vw = video.videoWidth, vh = video.videoHeight;
    if (!vw) return;
    const r = frameRect(vw, vh);
    const m = 0.04;
    const sx = Math.max(r.x - r.w * m, 0), sy = Math.max(r.y - r.h * m, 0);
    const sw = Math.min(r.w * (1 + 2 * m), vw - sx), sh = Math.min(r.h * (1 + 2 * m), vh - sy);
    const scale = Math.min(1, 1600 / sw);
    const c = document.createElement("canvas");
    c.width = Math.round(sw * scale); c.height = Math.round(sh * scale);
    c.getContext("2d").drawImage(video, sx, sy, sw, sh, 0, 0, c.width, c.height); // bez odbicia — tekst czytelny
    photo = c.toDataURL("image/jpeg", 0.9);
    stopCam();
    $("[data-os-photo]").src = photo;
    K.show("os-preview");
  }
  $("[data-os-manual]").addEventListener("click", capture);
  $("[data-os-retry]").addEventListener("click", startCam);
  $("[data-os-confirm]").addEventListener("click", () => {
    run("preview", async () => {
      try {
        await step("legitymacja", { ...psData, photo });
      } catch (e) {
        if (e.silent) throw e;
        K.show("os-ps"); // błędne dane (np. data) — popraw i zrób zdjęcie ponownie
        err("ps", e.message);
        return;
      }
      await book();
    });
  });

  // --- 5. rezerwacja, weryfikacja, płatność, wpis ----------------------------------------------------------------------
  async function book() {
    try {
      await step("rezerwacja", { lane: pick.lane, start_hour: pick.start, end_hour: pick.end, persons: pick.persons });
    } catch (e) {
      if (e.silent) throw e;
      K.show("os-slots"); // np. termin właśnie zajęty — wybierz inny (dane osoby zostają)
      await loadSlots();
      err("slots", e.message);
      return;
    }
    showWait();
  }
  function showWait() {
    K.show("os-wait");
    render();
    stopPolling();
    pollTimer = setInterval(async () => { try { await step("stan"); render(); } catch (e) { /* brak sieci — spróbujemy za chwilę */ } }, 5000);
    tickTimer = setInterval(() => {
      if (secondsLeft > 0) { secondsLeft -= 1; drawCountdown(); }
    }, 1000);
  }
  function stopPolling() { clearInterval(pollTimer); clearInterval(tickTimer); pollTimer = tickTimer = null; }
  function drawCountdown() {
    const el = $("[data-os-countdown]");
    el.textContent = `${Math.floor(secondsLeft / 60)}:${String(secondsLeft % 60).padStart(2, "0")}`;
  }
  const LEAVE = "Bez opłaconej rezerwacji strzelanie na strzelnicy jest zabronione. Opuść teren strzelnicy — pobyt bez opłaty jest nieuprawniony. Zdarzenie zostało zapisane i przekazane obsłudze.";
  function showLeave() {
    stopPolling();
    $("[data-os-leave-text]").textContent = LEAVE;
    K.show("os-leave");
  }
  $("[data-os-refuse]").addEventListener("click", () => {
    if (!window.confirm("Zrezygnować z rezerwacji? Bez opłaty strzelanie jest zabronione i trzeba opuścić teren strzelnicy.")) return;
    run("slots", async () => { await step("rezygnacja"); showLeave(); });
  });
  $("[data-os-stay]").addEventListener("click", () => {
    const prefill = st ? { first_name: st.first_name, last_name: st.last_name } : {};
    K.openForm({ prefill, info: "Pobyt bez strzelania — wybierz cel: prace, obserwator albo inny." });
  });
  $("[data-os-cash-confirm]").addEventListener("click", () => {
    if (!$("[data-os-cash-accept]").checked) { err("cash", "Zaznacz oświadczenie o wpłacie do skarbonki."); return; }
    run("cash", async () => { await step("gotowka", { accept: true }); render(); });
  });
  function render() {
    const r = st.reservation || {};
    $("[data-os-refuse]").hidden = true;
    $("[data-os-cash]").hidden = true;
    if (st.cash_declared) {
      stopPolling();
      $("[data-os-wait-title]").textContent = "Wpłata gotówki zgłoszona";
      $("[data-os-wait-text]").textContent = `${r.number}: ${r.label}. Dziękujemy — wpłatę rozliczy obsługa po opróżnieniu skarbonki. Teraz wpisz się do książki pobytu.`;
      $("[data-os-pay]").hidden = true; $("[data-os-countdown]").hidden = true;
      $("[data-os-entry]").hidden = false;
      return;
    }
    if (st.status === "rezygnacja" || (st.after_hours && r.status === "anulowana" && !r.paid)) { showLeave(); return; }
    const title = $("[data-os-wait-title]"), text = $("[data-os-wait-text]");
    const pay = $("[data-os-pay]"), entry = $("[data-os-entry]"), cd = $("[data-os-countdown]");
    pay.hidden = true; entry.hidden = true; cd.hidden = true;
    if (st.status === "weryfikacja") {
      title.textContent = "Sprawdzamy legitymację";
      text.textContent = `${r.label || ""}. Obsługa porównuje dane ze zdjęciem — nie odchodź od kiosku. Bez decyzji w tym czasie rezerwacja anuluje się bez opłat.`;
      secondsLeft = st.seconds_left || 0; cd.hidden = false; drawCountdown();
      return;
    }
    if (st.status === "odrzucona" || st.status === "wygasla" || r.status === "anulowana") {
      stopPolling();
      title.textContent = "Rezerwacja anulowana";
      text.textContent = st.status === "wygasla"
        ? "Nie udało się sprawdzić legitymacji w wyznaczonym czasie. Rezerwacja została anulowana bez opłat — zarezerwuj przez stronę strzelnicy; legitymacja pozostaje w weryfikacji."
        : st.status === "odrzucona"
          ? "Legitymacja nie została zweryfikowana. Szczegóły wysłaliśmy e-mailem. W razie pytań skontaktuj się z obsługą strzelnicy."
          : "Rezerwacja została anulowana (np. minął czas na płatność). Możesz zarezerwować ponownie.";
      return;
    }
    if (r.paid) {
      stopPolling();
      title.textContent = "Rezerwacja opłacona";
      text.textContent = `${r.number}: ${r.label}. Dziękujemy! Teraz wpisz się do książki pobytu.`;
      entry.hidden = false;
      return;
    }
    title.textContent = "Zapłać za rezerwację";
    if (st.pay) {
      text.textContent = `${r.number}: ${r.label}.`;
      $("[data-os-qr]").src = st.pay.qr;
      $("[data-os-amount]").textContent = pln(st.pay.amount_due);
      $("[data-os-until]").textContent = st.pay.until ? `Termin jest trzymany do ${st.pay.until}. Po zapłacie ten ekran zmieni się sam.` : "Po zapłacie ten ekran zmieni się sam.";
      pay.hidden = false;
      $("[data-os-refuse]").hidden = false;
      showCash();
    } else if (st.pay_info) {
      text.textContent = `${r.number}: ${r.label}. Do zapłaty ${pln(st.pay_info.amount_due)}. ${st.pay_info.text}`;
      $("[data-os-refuse]").hidden = false;
      showCash();
    } else {
      text.textContent = `${r.number}: ${r.label} — ${r.status_label || ""}.`;
    }
  }
  function showCash() {
    if (!st.cash) return;
    $("[data-os-cash-amount]").textContent = pln(st.cash.amount);
    $("[data-os-cash-statement]").textContent = st.cash.statement;
    $("[data-os-cash]").hidden = false;
  }
  $("[data-os-entry]").addEventListener("click", async () => {
    const r = st.reservation;
    const prefill = { first_name: st.first_name, last_name: st.last_name };
    await K.refreshState();
    if (K.openReservationIds().includes(r.id)) {
      K.openForm({ reservation: r.id, prefill, info: `Rezerwacja ${r.number}: ${r.label}` });
    } else {
      K.done("Rezerwacja opłacona", `${r.number}: ${r.label}. Wpisu do książki pobytu dokonasz krótko przed rozpoczęciem rezerwacji — w kiosku albo kodem QR z e-maila.`);
    }
  });
})();
