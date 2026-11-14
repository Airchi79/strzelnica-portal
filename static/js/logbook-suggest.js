// Formularz wpisu przy wejściu (kod QR bramki): po wpisaniu imienia i nazwiska rezerwującego zaznacza jego trwającą
// rezerwację — tylko gdy pole rezerwacji jest puste. Serwer zwraca sam identyfikator rezerwacji (bez nazwisk).
(() => {
  const form = document.querySelector("[data-entry-form][data-suggest-url]");
  if (!form) return;
  const select = form.elements.reservation, first = form.elements.first_name, last = form.elements.last_name;
  if (!select || !first || !last) return;
  let note = null;
  async function suggest() {
    const f = first.value.trim(), l = last.value.trim();
    const purpose = (form.querySelector("input[name=purpose]:checked") || {}).value;
    if (select.value || f.length < 2 || l.length < 2 || purpose === "prace" || purpose === "inne") return;
    try {
      const url = `${form.dataset.suggestUrl}?imie=${encodeURIComponent(f)}&nazwisko=${encodeURIComponent(l)}`;
      const res = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!res.ok) return;
      const data = await res.json();
      const option = data.reservation && [...select.options].find((o) => o.value === String(data.reservation));
      if (!option || select.value) return;
      select.value = option.value;
      const shooting = form.querySelector('input[name=purpose][value="strzelanie"]');
      if (shooting && !form.querySelector("input[name=purpose]:checked")) {
        shooting.checked = true;
        shooting.dispatchEvent(new Event("change", { bubbles: true }));  // pokazuje pole rezerwacji (signature.js)
      }
      if (!note) {
        note = document.createElement("div");
        note.className = "msg";
        select.closest(".field").appendChild(note);
      }
      note.textContent = `Zaznaczono rezerwację: ${option.textContent} — sprawdź, czy to Twoja.`;
    } catch (e) { /* bez podpowiedzi */ }
  }
  first.addEventListener("change", suggest);
  last.addEventListener("change", suggest);
})();
