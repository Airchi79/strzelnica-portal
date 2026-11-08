// Kiosk książki pobytu: tablet z kamerą albo Raspberry Pi z czytnikiem kodów (czytnik działa jak klawiatura).
// Wpisy bez połączenia trafiają do kolejki w pamięci urządzenia i są wysyłane po odzyskaniu sieci (z identyfikatorem,
// więc ponowne wysłanie nie tworzy duplikatu). Po wysłaniu dane znikają z urządzenia.
(function () {
  "use strict";
  const body = document.body;
  if (!body.hasAttribute("data-kiosk")) return;
  const API = body.dataset.api;
  const QUEUE_KEY = "satok_kiosk_queue";
  const STATE_KEY = "satok_kiosk_state";
  const IDLE_MS = 120000;
  // Dłużej na ekranach rezerwacji na miejscu (kod z e-maila na telefonie, weryfikacja legitymacji, płatność).
  const IDLE_BY_VIEW = { "os-person": 300000, "os-code": 300000, "os-rules": 300000, "os-ps": 300000, "os-camera": 180000,
    "os-preview": 180000, "os-wait": 1500000 };
  const showHooks = [];
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const store = {
    get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (e) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch (e) { return false; } },
  };
  let state = store.get(STATE_KEY, { reservations: [], window_minutes: 30 });
  let online = navigator.onLine;
  let current = "home";
  let idleTimer = null;
  let formContext = {};

  // --- widoki --------------------------------------------------------------------------------------
  function show(view) {
    current = view;
    $$("[data-view]").forEach((s) => { s.hidden = s.dataset.view !== view; });
    if (view !== "scan") stopCamera();
    if (view === "home") { resetForm(); renderReservations(); renderQueue(); }
    if (view === "exit") { const q = $("[data-exit-q]"); q.value = ""; $("[data-exit-list]").innerHTML = ""; q.focus(); }
    showHooks.forEach((fn) => fn(view));
    window.scrollTo(0, 0);
    bumpIdle();
  }
  function bumpIdle() {
    clearTimeout(idleTimer);
    if (current !== "home") idleTimer = setTimeout(() => show("home"), IDLE_BY_VIEW[current] || IDLE_MS); // dane nie zostają na ekranie
  }
  ["pointerdown", "keydown", "input"].forEach((ev) => document.addEventListener(ev, bumpIdle, { passive: true }));
  document.addEventListener("click", (e) => {
    const go = e.target.closest("[data-go]");
    if (go) { e.preventDefault(); if (go.dataset.go === "form") openForm({}); else show(go.dataset.go); }
  });

  function done(title, text) {
    $("[data-done-title]").textContent = title;
    $("[data-done-text]").textContent = text;
    show("done");
    setTimeout(() => { if (current === "done") show("home"); }, 10000);
  }

  // --- stan połączenia i dane z serwera -------------------------------------------------------------
  function setOnline(value) {
    online = value;
    const el = $("[data-status]");
    el.textContent = online ? "online" : "bez połączenia — wpisy w kolejce";
    el.classList.toggle("offline", !online);
  }
  async function api(path, options = {}) {
    const res = await fetch(API + path, {
      credentials: "same-origin", cache: "no-store", ...options,
      headers: { "X-Kiosk": "1", "Content-Type": "application/json", ...(options.headers || {}) },
    });
    if (res.status === 401) { location.reload(); throw new Error("unpaired"); }
    let data = {};
    try { data = await res.json(); } catch (e) { /* pusta odpowiedź */ }
    return { status: res.status, data };
  }
  async function refreshState() {
    try {
      const { status, data } = await api("stan/");
      if (status === 200) { state = data; store.set(STATE_KEY, state); setOnline(true); renderReservations(); }
      else setOnline(false);
    } catch (e) { setOnline(false); }
  }

  function openReservations(at = new Date()) {
    const before = (state.window_minutes || 30) * 60000;
    return (state.reservations || []).filter((r) => new Date(r.starts).getTime() - before <= at.getTime() && at < new Date(r.ends));
  }
  function renderReservations() {
    const list = $("[data-reservations]");
    const items = state.reservations || [];
    list.innerHTML = "";
    if (!items.length) { list.innerHTML = '<li class="muted">Brak rezerwacji na dziś.</li>'; return; }
    const open = new Set(openReservations().map((r) => r.id));
    items.forEach((r) => {
      const li = document.createElement("li");
      li.textContent = r.label + (open.has(r.id) ? " — wpisy otwarte" : "");
      list.appendChild(li);
    });
  }

  // --- kolejka offline --------------------------------------------------------------------------------
  function queue() { return store.get(QUEUE_KEY, []); }
  function renderQueue() {
    const n = queue().length;
    const el = $("[data-queue]");
    el.hidden = n === 0;
    el.textContent = n ? `Wpisy czekające na wysłanie: ${n}. Zostaną wysłane automatycznie po odzyskaniu połączenia.` : "";
  }
  let syncing = false;
  async function syncQueue() {
    if (syncing) return;
    syncing = true;
    try {
      for (const item of queue()) {
        let result;
        try { result = await api("wpis/", { method: "POST", body: JSON.stringify(item) }); }
        catch (e) { setOnline(false); break; }
        if (result.status >= 500 || result.status === 429) break;
        setOnline(true);
        if (result.status === 400) {
          try { await api("odrzucony/", { method: "POST", body: JSON.stringify({ reason: result.data.error || "błąd danych" }) }); } catch (e) { /* zgłoszenie ponowimy przy następnym błędzie */ }
        }
        store.set(QUEUE_KEY, queue().filter((q) => q.ref !== item.ref)); // wysłany albo odrzucony — usuwamy z urządzenia
      }
    } finally { syncing = false; renderQueue(); }
  }
  window.addEventListener("online", () => { setOnline(true); syncQueue(); refreshState(); });
  window.addEventListener("offline", () => setOnline(false));

  // --- podpis ---------------------------------------------------------------------------------------------
  const canvas = $("[data-sig]");
  const ctx = canvas.getContext("2d");
  let drawing = false, inked = 0, last = null;
  function sizeCanvas() {
    const ratio = Math.max(window.devicePixelRatio || 1, 1);
    const w = canvas.parentElement.clientWidth || 300;
    canvas.width = w * ratio; canvas.height = 200 * ratio;
    canvas.style.width = w + "px"; canvas.style.height = "200px";
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.lineWidth = 2.5; ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.strokeStyle = "#111";
    inked = 0;
  }
  const pos = (e) => { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  canvas.addEventListener("pointerdown", (e) => { drawing = true; last = pos(e); canvas.setPointerCapture(e.pointerId); e.preventDefault(); });
  canvas.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    const p = pos(e);
    ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke();
    inked += Math.hypot(p.x - last.x, p.y - last.y); last = p; e.preventDefault();
  });
  ["pointerup", "pointercancel"].forEach((ev) => canvas.addEventListener(ev, () => { drawing = false; }));
  $("[data-sig-clear]").addEventListener("click", sizeCanvas);

  // --- formularz wpisu --------------------------------------------------------------------------------
  const form = $("[data-kiosk-form]");
  const field = (name) => form.elements[name];
  const radio = (name) => (form.querySelector(`input[name=${name}]:checked`) || {}).value || "";
  function toggleFields() {
    const permit = radio("has_permit"), purpose = radio("purpose");
    const vis = (name, on) => { const el = form.querySelector(`[data-field="${name}"]`); if (el) el.hidden = !on; };
    vis("permit_number", permit === "tak"); vis("permit_issuer", permit === "tak"); vis("address", permit === "nie");
    vis("reservation", purpose === "strzelanie" || purpose === "obserwator");
    vis("purpose_note", purpose === "prace" || purpose === "inne");
  }
  form.addEventListener("change", toggleFields);
  function resetForm() {
    form.reset(); formContext = {};
    $("[data-form-error]").hidden = true; $("[data-form-info]").hidden = true;
    toggleFields();
  }
  function openForm(ctx) {
    resetForm();
    formContext = ctx;
    const select = field("reservation");
    select.innerHTML = '<option value="">— wybierz —</option>';
    openReservations().forEach((r) => { const o = document.createElement("option"); o.value = r.id; o.textContent = r.label; select.appendChild(o); });
    if (ctx.reservation) { select.value = String(ctx.reservation); form.querySelector('input[name=purpose][value="strzelanie"]').checked = true; }
    const p = ctx.prefill || {};
    ["first_name", "last_name", "permit_number", "permit_issuer", "address"].forEach((k) => { if (p[k]) field(k).value = p[k]; });
    if (p.has_permit) { const r = form.querySelector(`input[name=has_permit][value="${p.has_permit}"]`); if (r) r.checked = true; }
    if (ctx.info) { const info = $("[data-form-info]"); info.textContent = ctx.info; info.hidden = false; }
    toggleFields();
    show("form");
    sizeCanvas();
  }
  function validate() {
    const errors = [];
    const purpose = radio("purpose"), permit = radio("has_permit");
    if (!purpose) errors.push("Wybierz cel pobytu.");
    if (purpose === "strzelanie" && !field("reservation").value) errors.push("Przy strzelaniu wybierz rezerwację.");
    if ((purpose === "prace" || purpose === "inne") && !field("purpose_note").value.trim()) errors.push("Opisz krótko cel pobytu.");
    if (!field("first_name").value.trim() || !field("last_name").value.trim()) errors.push("Podaj imię i nazwisko.");
    if (!permit) errors.push("Zaznacz, czy masz pozwolenie na broń.");
    if (permit === "tak" && (!field("permit_number").value.trim() || !field("permit_issuer").value.trim())) errors.push("Podaj numer pozwolenia i organ, który je wydał.");
    if (permit === "nie" && !field("address").value.trim()) errors.push("Bez pozwolenia na broń wymagany jest adres zamieszkania.");
    if (!field("statement").checked) errors.push("Potwierdź oświadczenie.");
    if (inked < 60) errors.push("Złóż podpis palcem w ramce.");
    return errors;
  }
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16)); b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const errors = validate();
    const box = $("[data-form-error]");
    if (errors.length) { box.textContent = errors.join(" "); box.hidden = false; box.scrollIntoView({ block: "center" }); return; }
    const item = {
      ref: uuid(), entered_at: new Date().toISOString(), purpose: radio("purpose"), reservation: field("reservation").value,
      purpose_note: field("purpose_note").value.trim(), first_name: field("first_name").value.trim(), last_name: field("last_name").value.trim(),
      has_permit: radio("has_permit"), permit_number: field("permit_number").value.trim(), permit_issuer: field("permit_issuer").value.trim(),
      address: field("address").value.trim(), statement: true, signature: canvas.toDataURL("image/png"), person: formContext.person || "",
    };
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const { status, data } = await api("wpis/", { method: "POST", body: JSON.stringify(item) });
      if (status === 200) { setOnline(true); done("Zapisano wpis", `Numer wpisu: ${data.number}. Miłego strzelania i pamiętaj o zasadach bezpieczeństwa!`); return; }
      if (status === 400) { box.textContent = data.error || "Sprawdź dane."; box.hidden = false; return; }
      throw new Error("server");
    } catch (err) {
      if (err.message === "unpaired") return;
      const q = queue(); q.push(item);
      if (!store.set(QUEUE_KEY, q)) { box.textContent = "Brak połączenia i brak miejsca w pamięci urządzenia — poproś obsługę o wpis."; box.hidden = false; return; }
      setOnline(false);
      done("Zapisano na urządzeniu", "Brak połączenia z serwerem — wpis zostanie wysłany automatycznie, gdy wróci sieć.");
    } finally { submit.disabled = false; }
  });

  // --- wyjście -------------------------------------------------------------------------------------------
  let exitTimer = null;
  $("[data-exit-q]").addEventListener("input", (e) => {
    clearTimeout(exitTimer);
    exitTimer = setTimeout(async () => {
      const list = $("[data-exit-list]");
      const q = e.target.value.trim();
      list.innerHTML = "";
      if (q.length < 2) return;
      try {
        const { data } = await api("wyjscie/?nazwisko=" + encodeURIComponent(q));
        if (!data.entries || !data.entries.length) { list.innerHTML = '<li class="muted">Nie znaleziono otwartego pobytu.</li>'; return; }
        data.entries.forEach((en) => {
          const li = document.createElement("li");
          const b = document.createElement("button");
          b.className = "btn secondary"; b.type = "button"; b.textContent = `${en.name} — wejście ${en.at}`;
          b.addEventListener("click", async () => {
            const r = await api(`wyjscie/${en.id}/`, { method: "POST", body: "{}" });
            if (r.status === 200) done("Do zobaczenia!", `Zapisano wyjście o ${r.data.at}.`);
            else { const li2 = document.createElement("li"); li2.className = "msg error"; li2.textContent = r.data.error || "Nie udało się zapisać wyjścia."; list.replaceChildren(li2); }
          });
          li.appendChild(b); list.appendChild(li);
        });
      } catch (err) { list.innerHTML = '<li class="msg error">Brak połączenia — wyjście zapisze obsługa albo spróbuj za chwilę.</li>'; }
    }, 350);
  });

  // --- kody QR: czytnik USB (klawiatura) i kamera ----------------------------------------------------------
  async function handleCode(text) {
    const res = /\/ksiazka\/wpis\/r\/([^/?#\s]+)/.exec(text);
    const person = /\/ksiazka\/osoba\/([^/?#\s]+)/.exec(text);
    if (res) {
      const r = (state.reservations || []).find((x) => x.token === res[1]);
      const isOpen = r && openReservations().some((x) => x.id === r.id);
      if (isOpen) openForm({ reservation: r.id, info: `Rezerwacja: ${r.label}` });
      else done("Kod rezerwacji nieaktywny", "Ta rezerwacja dziś nie trwa albo wpisy jeszcze się nie zaczęły. Sprawdź godzinę rezerwacji lub poproś obsługę.");
    } else if (person) {
      try {
        const { status, data } = await api(`osoba/${encodeURIComponent(person[1])}/`);
        if (status === 200) openForm({ prefill: data.prefill, person: person[1], info: "Dane z Twojego konta — sprawdź je i podpisz się." });
        else done("Nieznany kod", "Ten kod osobisty jest nieaktualny. Wygeneruj go ponownie w aplikacji (Mój kod QR).");
      } catch (e) { openForm({ info: "Brak połączenia — wpisz dane ręcznie." }); }
    } else {
      done("Nieznany kod QR", "To nie jest kod rezerwacji ani osobisty kod strzelnicy.");
    }
  }
  let buffer = "", bufferTime = 0;
  document.addEventListener("keydown", (e) => {
    const typingInField = e.target.matches && e.target.matches("input, select, textarea");
    if (typingInField && current !== "home") return;
    const now = Date.now();
    if (now - bufferTime > 120) buffer = ""; // czytnik „pisze” bardzo szybko — wolne pisanie to człowiek
    bufferTime = now;
    if (e.key === "Enter") { if (buffer.length > 10) { e.preventDefault(); handleCode(buffer); } buffer = ""; return; }
    if (e.key.length === 1) buffer += e.key;
  });

  let stream = null, scanTimer = null;
  function stopCamera() {
    clearInterval(scanTimer); scanTimer = null;
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  }
  const scanButton = $("[data-scan]");
  if ("BarcodeDetector" in window && navigator.mediaDevices) {
    scanButton.hidden = false;
    scanButton.addEventListener("click", async () => {
      try {
        const detector = new window.BarcodeDetector({ formats: ["qr_code"] });
        // Kamera wybrana w panelu dla urządzenia: przednia (kiosk na ścianie) albo tylna (tablet w ręku).
        const facing = body.dataset.camera === "environment" ? "environment" : "user";
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: facing } } });
        const video = $("[data-video]");
        video.srcObject = stream; await video.play();
        show("scan");
        scanTimer = setInterval(async () => {
          try {
            const codes = await detector.detect(video);
            if (codes.length) { const value = codes[0].rawValue; stopCamera(); handleCode(value); }
          } catch (e) { /* klatka bez kodu */ }
        }, 300);
      } catch (e) { stopCamera(); done("Kamera niedostępna", "Zezwól na użycie kamery w ustawieniach przeglądarki albo użyj przycisku wpisu."); }
    });
  }

  // Interfejs dla modułu rezerwacji na miejscu (kiosk-onsite.js).
  window.SatokKiosk = {
    api, show, done, openForm, refreshState, onShow: (fn) => showHooks.push(fn), startSaver: () => startSaver(),
    openReservationIds: () => openReservations().map((r) => r.id),
    isOnline: () => online, current: () => current, camera: () => (body.dataset.camera === "environment" ? "environment" : "user"),
  };

  // --- zabezpieczenia (uzupełnienie przypięcia aplikacji w Androidzie — wyjście z kiosku tylko z PIN-em admina) -----------
  // Wstecz nie opuszcza kiosku: zamyka okienko albo wraca do ekranu startowego (dane z formularza są czyszczone).
  history.pushState({ kiosk: 1 }, "", location.href);
  window.addEventListener("popstate", () => {
    history.pushState({ kiosk: 1 }, "", location.href);
    const dlg = $("dialog[open]");
    if (dlg) dlg.close(); else show("home");
  });
  // Żaden link nie wyprowadza poza kiosk (np. w treści regulaminu); bez menu przytrzymania.
  document.addEventListener("click", (e) => { if (e.target.closest("a[href]")) e.preventDefault(); }, true);
  document.addEventListener("contextmenu", (e) => e.preventDefault());
  // Ekran nie gaśnie systemowo (po wygaszeniu telefon pokazałby blokadę PIN i kiosk byłby nieużywalny) —
  // zamiast tego czarny wygaszacz po bezczynności (na ekranie OLED czarny = prawie bez poboru prądu).
  async function keepAwake() {
    try { if ("wakeLock" in navigator && document.visibilityState === "visible") await navigator.wakeLock.request("screen"); } catch (e) { /* brak wsparcia */ }
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") keepAwake(); });
  keepAwake();
  const SAVER_MS = 180000;
  const saver = $("[data-saver]");
  let saverTimer = null, saverMove = null, swallowClick = false;
  function moveSaverText() { $("[data-saver-text]").style.top = `${10 + Math.random() * 75}vh`; } // bez wypalania ekranu
  function armSaver() { clearTimeout(saverTimer); if (current === "home") saverTimer = setTimeout(startSaver, SAVER_MS); }
  function startSaver() { if (current !== "home") return; saver.hidden = false; moveSaverText(); saverMove = setInterval(moveSaverText, 20000); }
  function wake(e) {
    if (!saver.hidden) {
      saver.hidden = true; clearInterval(saverMove);
      if (e.type === "pointerdown") { // dotknięcie budzi ekran — nie naciska przycisku pod spodem
        swallowClick = true;
        setTimeout(() => { swallowClick = false; }, 700);
      }
    }
    armSaver();
  }
  ["pointerdown", "keydown"].forEach((ev) => document.addEventListener(ev, wake, { passive: true, capture: true }));
  document.addEventListener("click", (e) => { if (swallowClick) { swallowClick = false; e.preventDefault(); e.stopPropagation(); } }, true);
  showHooks.push(armSaver);

  // --- start ---------------------------------------------------------------------------------------------
  setOnline(online);
  show("home");
  refreshState().then(syncQueue);
  setInterval(refreshState, 60000);
  setInterval(syncQueue, 30000);
  setInterval(renderReservations, 60000);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  // Kopia strony kiosku i jej zasobów na wypadek braku sieci (service worker poda ją przy otwarciu offline).
  if ("caches" in window && navigator.onLine) {
    const assets = $$("script[src], link[rel=stylesheet]").map((el) => el.src || el.href);
    caches.open("satok-kiosk").then((c) => Promise.all([
      fetch(location.pathname, { credentials: "same-origin" }).then((r) => { if (r.ok && r.headers.get("X-Kiosk-App") === "1") return c.put(location.pathname, r); }),
      c.addAll(assets),
    ])).catch(() => {});
  }
})();

// Logo: bez sieci (kiosk offline) niewczytany obrazek znika zamiast pustej ramki.
document.querySelectorAll("img[data-hide-broken]").forEach((img) => {
  const hide = () => img.remove();
  if (img.complete && img.naturalWidth === 0) hide(); else img.addEventListener("error", hide);
});
