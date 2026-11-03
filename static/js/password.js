// Podpowiedzi przy ustawianiu hasła: zasady (te same co na serwerze), siła, zgodność powtórzenia, pokaż/ukryj.
// Działa na każdym formularzu z polami autocomplete="new-password" (rejestracja, zmiana i reset hasła).
(function () {
  "use strict";
  const COMMON = ["haslo", "hasło", "password", "qwerty", "qwertyuiop", "zaq12wsx", "1qaz2wsx", "abc123", "iloveyou", "polska", "strzelnica",
    "admin", "letmein", "monkey", "dragon", "football", "kochamcie", "zaqwsx", "asdfgh", "123qwe", "qwe123"];
  const MIN = 12;

  function addToggle(input) {
    const wrap = document.createElement("div");
    wrap.className = "pw-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const b = document.createElement("button");
    b.type = "button"; b.className = "pw-toggle"; b.textContent = "Pokaż";
    b.addEventListener("click", () => { const show = input.type === "password"; input.type = show ? "text" : "password"; b.textContent = show ? "Ukryj" : "Pokaż"; });
    wrap.appendChild(b);
    return wrap;
  }

  function hints(form) {
    const val = (sel) => { const el = form.querySelector(sel); return el ? el.value.trim().toLowerCase() : ""; };
    const email = val("input[name=email]");
    return [val("input[name=first_name]"), val("input[name=last_name]"), email.split("@")[0]].filter((h) => h.length >= 3);
  }

  function rules(pw, form) {
    const low = pw.toLowerCase();
    const stripped = low.replace(/[^a-ząćęłńóśźż]/g, "");
    return [
      { ok: pw.length >= MIN, text: `co najmniej ${MIN} znaków (teraz: ${pw.length})` },
      { ok: pw.length > 0 && !/^\d+$/.test(pw), text: "nie same cyfry" },
      { ok: pw.length > 0 && !hints(form).some((h) => low.includes(h)), text: "nie zawiera imienia, nazwiska ani nazwy z e-maila" },
      { ok: pw.length > 0 && !COMMON.some((c) => low === c || (stripped && stripped === c) || low.startsWith(c) && low.length - c.length < 4) && !/^(.)\1+$/.test(pw) && !/^(0123|1234|2345|abcd|qwer)/.test(low),
        text: "nie jest popularnym ani oczywistym hasłem" },
    ];
  }

  function strength(pw) {
    let score = 0;
    if (pw.length >= MIN) score++;
    if (pw.length >= 16) score++;
    const kinds = [/[a-ząćęłńóśźż]/, /[A-ZĄĆĘŁŃÓŚŹŻ]/, /\d/, /[^\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ]/].filter((r) => r.test(pw)).length;
    if (kinds >= 3) score++;
    if (kinds === 4 || pw.length >= 20) score++;
    return score; // 0–4
  }

  document.querySelectorAll("form").forEach((form) => {
    const fields = form.querySelectorAll('input[autocomplete="new-password"]');
    if (!fields.length) return;
    const main = fields[0], confirm = fields[1];
    const mainWrap = addToggle(main);
    const box = document.createElement("div");
    box.setAttribute("aria-live", "polite");
    box.innerHTML = '<div class="pw-meter"><span></span></div><div class="pw-meter-label"></div><ul class="pw-rules"></ul>';
    mainWrap.insertAdjacentElement("afterend", box);
    const list = box.querySelector(".pw-rules"), bar = box.querySelector(".pw-meter span"), label = box.querySelector(".pw-meter-label");
    let match = null;
    if (confirm) {
      const confirmWrap = addToggle(confirm);
      match = document.createElement("ul");
      match.className = "pw-rules";
      confirmWrap.insertAdjacentElement("afterend", match);
    }
    function update() {
      const pw = main.value;
      const rs = rules(pw, form);
      list.innerHTML = "";
      rs.forEach((r) => { const li = document.createElement("li"); li.textContent = r.text; if (r.ok) li.className = "ok"; list.appendChild(li); });
      const allOk = rs.every((r) => r.ok);
      const s = allOk ? Math.max(strength(pw), 2) : Math.min(strength(pw), 1);
      const colors = ["var(--bad)", "var(--bad)", "var(--warn)", "var(--ok)", "var(--ok)"];
      bar.style.width = pw ? `${Math.max(10, (s / 4) * 100)}%` : "0";
      bar.style.background = colors[s];
      label.textContent = !pw ? "" : allOk ? (s >= 3 ? "Hasło silne" : "Hasło spełnia zasady — dłuższe lub z różnymi znakami będzie silniejsze") : "Hasło nie spełnia jeszcze zasad";
      if (match) {
        match.innerHTML = "";
        if (confirm.value) {
          const li = document.createElement("li");
          const same = confirm.value === pw;
          li.textContent = same ? "hasła są identyczne" : "hasła się różnią";
          if (same) li.className = "ok";
          match.appendChild(li);
        }
      }
    }
    form.addEventListener("input", update);
    update();
  });
})();
