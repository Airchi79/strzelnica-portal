// Formularz rezerwacji: karty osi, wybór dnia, godziny klikane na siatce, licznik osób, cena na żywo i podsumowanie.
// Bez JavaScriptu formularz działa w zwykłej postaci (pola „Od/Do”); serwer i tak sprawdza wszystko ponownie.
(function () {
  "use strict";
  const form = document.getElementById("booking-form");
  if (!form) return;
  form.classList.add("js");
  const url = form.dataset.slotsUrl;
  const $ = (s) => form.querySelector(s);
  const grid = $("[data-slot-grid]"), hint = $("[data-slot-hint]");
  const dayInput = form.elements.day, startSel = form.elements.start_hour, endSel = form.elements.end_hour, persons = form.elements.persons;
  const fmt = (gr) => (gr / 100).toLocaleString("pl-PL", { minimumFractionDigits: gr % 100 ? 2 : 0, maximumFractionDigits: 2 }) + " zł";
  const pad = (h) => String(h).padStart(2, "0") + ":00";
  let data = null, start = null, end = null, maxPersons = parseInt(persons.max || "50", 10);
  const hintText = hint.textContent;

  const laneValue = () => (form.querySelector("input[name=lane]:checked") || {}).value || "";

  function selectFromFields() {
    const s = parseInt(startSel.value, 10), e = parseInt(endSel.value, 10);
    if (!Number.isNaN(s) && !Number.isNaN(e) && e > s) { start = s; end = e; }
  }

  async function load() {
    const lane = laneValue(), day = dayInput.value;
    markDayChips();
    if (!lane || !day) { render(); return; }
    grid.setAttribute("aria-busy", "true");
    try {
      const res = await fetch(`${url}?os=${encodeURIComponent(lane)}&dzien=${encodeURIComponent(day)}`, { headers: { Accept: "application/json" } });
      data = res.ok ? await res.json() : null;
    } catch (e) { data = null; }
    grid.removeAttribute("aria-busy");
    if (data) {
      maxPersons = data.max_persons || maxPersons;
      // Wybór, który nie mieści się w wolnych godzinach nowego dnia/osi, jest kasowany.
      if (start !== null && !rangeFree(start, end)) { start = end = null; }
    }
    render();
  }

  function stateOf(h) { const s = data && data.slots.find((x) => x.hour === h); return s ? s.state : null; }
  function rangeFree(s, e) { for (let h = s; h < e; h++) if (stateOf(h) !== "wolne") return false; return true; }

  function render() {
    grid.innerHTML = "";
    if (!data) { grid.innerHTML = '<span class="muted">Wybierz oś i dzień.</span>'; update(); return; }
    if (!data.slots.length) { grid.innerHTML = '<span class="muted">Oś jest tego dnia nieczynna — wybierz inny dzień.</span>'; update(); return; }
    data.slots.forEach((s) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "slot " + s.state; b.dataset.hour = s.hour; b.textContent = pad(s.hour);
      b.disabled = s.state !== "wolne";
      b.title = { wolne: "wolne", zajete: "zajęte", blokada: "niedostępne", minione: "minęło", za_pozno: "za krótko przed terminem — rezerwacja online niedostępna" }[s.state] || "";
      if (start !== null && s.hour >= start && s.hour < end) b.classList.add("sel");
      grid.appendChild(b);
    });
    update();
  }

  grid.addEventListener("click", (e) => {
    const b = e.target.closest("button.slot");
    if (!b || b.disabled) return;
    const h = parseInt(b.dataset.hour, 10);
    hint.textContent = hintText; hint.classList.remove("error");
    if (start === null || end - start > 1 || h < start) { start = h; end = h + 1; }
    else if (h === start) { start = end = null; }
    else if (rangeFree(start, h + 1)) { end = h + 1; }
    else { hint.textContent = "W tym zakresie jest zajęta godzina — wybierz krótszy czas albo inny termin."; hint.classList.add("error"); start = h; end = h + 1; }
    render();
  });

  function update() {
    // Pola formularza (ukryte przy włączonym JS) dostają wybrane wartości.
    if (start !== null) { startSel.value = String(start); endSel.value = String(end); }
    const n = Math.max(1, Math.min(maxPersons, parseInt(persons.value || "1", 10) || 1));
    if (String(n) !== persons.value) persons.value = String(n);
    const laneCard = form.querySelector("input[name=lane]:checked");
    const laneName = laneCard ? laneCard.closest(".lane-card").querySelector(".lane-name").textContent : "";
    const hours = start !== null ? end - start : 0;
    const main = $("[data-sum-main]"), sumPrice = $("[data-sum-price]"), info = $("[data-price-info]"), submit = $("[data-sum-submit]");
    const code = (form.elements.access_code.value || "").trim();
    let priceText = "", infoText = "";
    if (data && data.price) {
      const pp = data.price.per_person, flat = data.price.flat;
      const hourly = pp == null ? flat : Math.min(n * pp, flat);
      if (pp == null) infoText = `Ryczałt ${fmt(flat)} za godzinę, niezależnie od liczby osób.`;
      else if (n * pp >= flat) infoText = `Od ${Math.ceil(flat / pp)} osób obowiązuje korzystniejszy ryczałt ${fmt(flat)}/h zamiast ${fmt(pp)} za osobę.`;
      else infoText = `${n} × ${fmt(pp)} = ${fmt(hourly)} za godzinę. Od ${Math.ceil(flat / pp)} osób ryczałt ${fmt(flat)}/h.`;
      if (hours) priceText = code ? "z kodem — rozliczenie po sprawdzeniu kodu" : `${fmt(hourly * hours)} (${hours} h × ${fmt(hourly)})`;
    } else if (data && !data.price) infoText = "Ta oś nie ma jeszcze cennika — rezerwacja nie jest możliwa.";
    info.textContent = infoText;
    const day = dayInput.value ? new Date(dayInput.value + "T12:00:00").toLocaleDateString("pl-PL", { weekday: "short", day: "numeric", month: "numeric" }) : "";
    if (laneName && day && hours) main.textContent = `${laneName} · ${day} · ${pad(start)}–${pad(end)} · ${n} os.`;
    else if (!laneName) main.textContent = "Wybierz oś";
    else if (!day) main.textContent = `${laneName} · wybierz dzień`;
    else main.textContent = `${laneName} · ${day} · wybierz godziny`;
    sumPrice.textContent = priceText;
    submit.disabled = !(laneName && day && hours && data && data.price);
  }

  function markDayChips() {
    form.querySelectorAll(".day-chip").forEach((c) => c.classList.toggle("active", c.dataset.day === dayInput.value));
  }

  form.querySelectorAll("input[name=lane]").forEach((r) => r.addEventListener("change", load));
  dayInput.addEventListener("change", load);
  form.querySelectorAll(".day-chip").forEach((c) => c.addEventListener("click", () => { dayInput.value = c.dataset.day; load(); }));
  form.querySelectorAll("[data-persons]").forEach((b) => b.addEventListener("click", () => {
    persons.value = String((parseInt(persons.value || "1", 10) || 1) + parseInt(b.dataset.persons, 10)); update();
  }));
  persons.addEventListener("input", update);
  form.elements.access_code.addEventListener("input", update);

  selectFromFields();
  load();
})();
