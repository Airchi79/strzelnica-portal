"""Ekrany kiosku: element wskazywany w skrypcie przez `$("[data-…]")` (pierwsze trafienie) musi być w szablonie jeden.
Zdublowany znacznik `data-os-qr` (przycisk „Mój kod QR” i obrazek płatności) sprawiał, że kod QR płatności trafiał
do przycisku zamiast do obrazka — ekran płatności pokazywał pusty obrazek."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def duplicated_selectors(html, js):
    selectors = set(re.findall(r'(?<!\$)\$\("\[(data-[a-z0-9-]+)\]"\)', js))  # $(…) — pierwszy element; $$(…) — wszystkie
    counts = {s: len(re.findall(r"\s" + re.escape(s) + r"(?=[\s>=])", html)) for s in selectors}
    return selectors, {s: n for s, n in counts.items() if n != 1}


def test_single_element_selectors_are_unique_in_kiosk_template():
    html = (ROOT / "templates/kiosk/kiosk.html").read_text(encoding="utf-8")
    js = "".join((ROOT / f"static/js/{name}").read_text(encoding="utf-8") for name in ("kiosk.js", "kiosk-onsite.js"))
    selectors, dupes = duplicated_selectors(html, js)
    assert "data-os-pay-qr" in selectors
    assert not dupes, f"znaczniki użyte jako pojedynczy element, a w szablonie jest ich inna liczba: {dupes}"


def test_guard_detects_the_original_bug():
    html = '<button data-os-qr>Mój kod QR</button><img class="os-qr" data-os-qr alt="">'
    js = '$("[data-os-qr]").src = st.pay.qr; $$("[data-view]").forEach(f);'
    assert duplicated_selectors(html, js)[1] == {"data-os-qr": 2}
