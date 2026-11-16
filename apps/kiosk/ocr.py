"""Automatyczne porównanie danych wpisanych w kiosku z tekstem odczytanym ze zdjęcia legitymacji PS (Tesseract).

Zasady (decyzje właściciela):
- automat tylko ZATWIERDZA — gdy zgadzają się imię, nazwisko, numer, rodzaj dokumentu i wystawca; w każdym innym
  przypadku decyzję podejmuje obsługa (art. 22 RODO: bez decyzji odmownych podejmowanych wyłącznie automatycznie),
- odczytany tekst nie jest nigdzie zapisywany (legitymacje LOK i PZSS zawierają PESEL) — zostaje tylko wynik
  porównania (lista niezgodnych pól),
- odczyt nie zależy od układu legitymacji (PZSS, PZŁ, LOK i inne): wpisane dane są szukane w całym tekście.
Tak samo sprawdza dziś obsługa: porównuje dane ze zdjęciem; ani człowiek, ani automat nie wykryją dobrze podrobionej
lub cudzej legitymacji (temu służy oświadczenie przed rezerwacją)."""

import difflib
import logging
import re
import shutil
import subprocess
import time
import unicodedata
from io import BytesIO

logger = logging.getLogger(__name__)

# Łączny czas odczytu (oba przebiegi): osoba czeka przy kiosku, a serwer ma limit 60 s na żądanie (gunicorn --timeout 60).
BUDGET_SECONDS = 20
MIN_PASS_SECONDS = 4  # drugi przebieg tylko, gdy zostało na niego sensownie dużo czasu
MIN_NUMBER_LEN = 4
# Wystawcy wybierani w kiosku przyciskami → słowa, które muszą być na legitymacji (wystarczy jedno z grupy).
ISSUER_WORDS = {
    "PZSS": ["PZSS", "STRZELECTWA", "SPORTOWEGO"],
    "PZŁ": ["LOWIECKI", "LOWIECKIEGO"],
    "LOK": ["LOK", "OBRONY", "KRAJU"],
}
PS_WORDS = ["PROWADZACEGO", "PROWADZACY", "PROWADZACA"]
# Typowe pomyłki odczytu w numerach — stosowane po obu stronach porównania.
NUMBER_MAP = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "J": "1", "S": "5", "B": "8", "Z": "2", "G": "6"})

RESULT_OK = "zgodne"
RESULT_UNAVAILABLE = "niedostępne"


def available():
    return shutil.which("tesseract") is not None


def read_text(image_bytes, budget=BUDGET_SECONDS):
    """Tekst ze zdjęcia legitymacji (dwa tryby układu strony — lepsza skuteczność na różnych wzorach), łącznie najwyżej
    `budget` sekund. Zwraca None, gdy odczyt jest niemożliwy (brak programu, błąd, przekroczony czas) — wtedy legitymację
    sprawdza obsługa."""
    from PIL import Image, ImageOps

    if not available():
        return None
    try:
        img = Image.open(BytesIO(image_bytes))
        if img.width * img.height > MAX_PIXELS:  # wymiary z nagłówka — przed rozpakowaniem (ochrona pamięci serwera)
            logger.warning("OCR legitymacji: obraz %sx%s px pominięty (za duży)", img.width, img.height)
            return None
        img = img.convert("L")
        scale = 2000 / max(img.size)
        if scale > 1:
            img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
        img = ImageOps.autocontrast(img, cutoff=1)
        buf = BytesIO()
        img.save(buf, "PNG")
        data = buf.getvalue()
    except OSError:
        return None
    texts = []
    deadline = time.monotonic() + budget
    for psm in ("6", "11"):
        left = deadline - time.monotonic()
        if left < MIN_PASS_SECONDS:
            logger.warning("OCR legitymacji: pominięto przebieg --psm %s (zostało %.1f s z %s s)", psm, max(left, 0), budget)
            break
        try:
            r = subprocess.run(["tesseract", "stdin", "stdout", "-l", "pol", "--psm", psm], input=data, capture_output=True,
                               timeout=left, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("OCR legitymacji: %s", type(e).__name__)
            continue
        if r.returncode == 0:
            texts.append(r.stdout.decode("utf-8", "replace"))
    return "\n".join(texts) if texts else None


MAX_PDF_PAGES = 2
MAX_PIXELS = 50_000_000  # zdjęcie z telefonu ma ok. 12–50 Mpx; większe pliki to błąd albo „bomba” w małym pliku
PDF_TEXT_MIN_LETTERS = 30  # mniej liter w warstwie tekstowej = PDF to skan (obraz) — wtedy odczyt z obrazu strony


def _has_issuer(text):
    words = _words(text)
    return any(_word_found(w, words, 0.84) for group in ISSUER_WORDS.values() for w in group)


def _render_first_page(pdf):
    page = pdf[0]
    try:
        # skala wg dłuższego boku: strona nietypowo wysoka nie da gigantycznej bitmapy w pamięci
        image = page.render(scale=min(8.0, 2000 / max(page.get_width(), page.get_height(), 1))).to_pil()
        buf = BytesIO()
        image.convert("RGB").save(buf, "PNG")
        return buf.getvalue()
    finally:
        page.close()


def read_pdf_text(pdf_bytes, budget=BUDGET_SECONDS):
    """Tekst legitymacji z pliku PDF (np. pobranego z portalu PZSS):
    - warstwa tekstowa (dokładna) — numer, daty, a jeśli jest, także wystawca,
    - gdy w warstwie nie ma wystawcy (logo i nazwa związku jako grafika — tak jest w PDF z portalu PZSS), obraz strony
      przez ten sam odczyt co zdjęcie; z tego odczytu bierzemy tylko słowa (bez cyfr), by nie mieszać numerów i dat,
    - PDF bez warstwy tekstowej (skan) — w całości odczyt obrazu strony.
    None, gdy pliku nie da się odczytać."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception:  # uszkodzony / zaszyfrowany PDF — pdfium zgłasza własne wyjątki
        logger.warning("OCR legitymacji: nie można otworzyć PDF")
        return None
    try:
        if not len(pdf):
            return None
        texts = []
        for i in range(min(len(pdf), MAX_PDF_PAGES)):
            page = pdf[i]
            textpage = page.get_textpage()
            texts.append(textpage.get_text_range())
            textpage.close()
            page.close()
        layer = "\n".join(texts)
        has_layer = sum(ch.isalpha() for ch in layer) >= PDF_TEXT_MIN_LETTERS
        if has_layer and _has_issuer(layer):
            return layer
        image = _render_first_page(pdf) if available() else None
    except Exception:  # błąd odczytu strony — dane wpisze osoba
        logger.warning("OCR legitymacji: błąd odczytu PDF")
        return None
    finally:
        pdf.close()
    seen = read_text(image, budget=budget) if image else None
    if not has_layer:
        return seen
    return layer + ("\n" + re.sub(r"\d", " ", seen) if seen else "")


def _fold(value):
    """Wielkie litery bez polskich znaków (odczyt często gubi ogonki)."""
    value = str(value or "").upper().replace("Ł", "L")
    return "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch))


def _number_key(value):
    return re.sub(r"[^A-Z0-9]", "", _fold(value)).translate(NUMBER_MAP)


def _number_found(number, text):
    key, hay = _number_key(number), _number_key(text)
    if len(key) < MIN_NUMBER_LEN:
        return False  # zbyt krótki numer pasowałby przypadkiem
    return key in hay  # tylko zamiany znaków typowo mylonych przy odczycie — inna cyfra to inny numer


def _words(text):
    return re.findall(r"[A-Z]{2,}", _fold(text))


def _word_found(target, words, ratio):
    """Słowa stałe (rodzaj dokumentu, wystawca) — z tolerancją błędów odczytu."""
    return any(w == target or difflib.SequenceMatcher(None, target, w).ratio() >= ratio for w in words)


def _name_found(name, words):
    """Imię / nazwisko identyczne (bez znaczenia wielkość liter i polskie znaki) — np. „Kowalska” ≠ „KOWALSKI”."""
    parts = re.findall(r"[A-Z]{2,}", _fold(name))
    return bool(parts) and all(p in words for p in parts)


def issuer_code(issuer):
    """„PZSS (Polski Związek…)” → „PZSS”; inny wystawca → ''."""
    head = str(issuer or "").split("(")[0].strip().upper()
    return head if head in ISSUER_WORDS else ""


def _holder_found(first_name, last_name, text):
    """Imię i nazwisko razem — w tej samej albo sąsiedniej linii (nie np. imię z podpisu wystawcy, a nazwisko gdzie indziej)."""
    lines = [_words(line) for line in str(text or "").splitlines()]
    lines = [w for w in lines if w]
    for i in range(len(lines)):
        words = lines[i] + (lines[i + 1] if i + 1 < len(lines) else [])
        if _name_found(first_name, words) and _name_found(last_name, words):
            return True
    return False


def compare(text, first_name, last_name, number, issuer):
    """Lista niezgodnych pól (pusta = wszystko się zgadza)."""
    words = _words(text)
    checks = {
        "imię i nazwisko": _holder_found(first_name, last_name, text),
        "numer": _number_found(number, text),
        "rodzaj dokumentu (prowadzący strzelanie)": any(_word_found(w, words, 0.8) for w in PS_WORDS),
    }
    code = issuer_code(issuer)
    if code:
        checks["wystawca"] = any(_word_found(w, words, 0.84) for w in ISSUER_WORDS[code])
    return [name for name, ok in checks.items() if not ok]


# --- odczyt danych do formularza (zdjęcie najpierw — dane wypełniają się same, osoba je sprawdza) -----------------------

ISSUER_LABELS = {"PZSS": "PZSS (Polski Związek Strzelectwa Sportowego)", "PZŁ": "PZŁ (Polski Związek Łowiecki)",
                 "LOK": "LOK (Liga Obrony Kraju)"}  # jak w kiosku (przyciski wystawców)
NUMBER_RE = re.compile(r"\b(?:NR|NUMER)\b[\s.:_\-|]*(?:UPRAWNIEN\w*|LEGITYMACJI|LEG\.?)?[\s.:_\-|]*([A-Z0-9][A-Z0-9/\-.]{2,40})")
DATE_RE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")  # PDF z portali: 2024-10-10
ISSUED_WORDS = ("WYDAN", "NABYC", "WYSTAWION")
VALID_WORDS = ("WAZN",)


def _clean_number(raw):
    """„0079/BZGL0K/2022 .” → „0079/BZGLOK/2022”: bez końcowej interpunkcji; w członach z literami zero → O."""
    raw = raw.strip(" .,;:_-|")
    parts = []
    for part in raw.split("/"):
        letters = sum(ch.isalpha() for ch in part)
        if letters >= 2 and letters >= len(part) - 1:
            part = part.replace("0", "O")
        parts.append(part)
    return "/".join(parts)[:64]


def _dates(lines):
    """[(data, numer linii)] — tylko poprawne daty kalendarzowe."""
    from datetime import date

    out = []
    for i, line in enumerate(lines):
        for d, m, y in DATE_RE.findall(line) + [(d, m, y) for y, m, d in ISO_DATE_RE.findall(line)]:
            try:
                out.append((date(int(y), int(m), int(d)), i))
            except ValueError:
                continue
    return out


def _near(lines, i, words):
    return any(w in lines[j] for j in (i - 1, i, i + 1) if 0 <= j < len(lines) for w in words)


def extract(text, today=None):
    """Podpowiedzi do formularza z odczytanego tekstu legitymacji: numer, wystawca, data wydania, ważna do.
    Nie zwraca imienia, nazwiska ani PESEL — tylko pola dokumentu; brak pewności = puste pole."""
    from django.utils import timezone

    today = today or timezone.localdate()
    out = {"number": "", "issuer": "", "issued_on": None, "valid_until": None}
    if not text:
        return out
    lines = [_fold(line) for line in str(text).splitlines()]
    words = _words(text)
    hits = {code: sum(_word_found(w, words, 0.84) for w in group) for code, group in ISSUER_WORDS.items()}
    best = max(hits, key=hits.get)
    if hits[best]:
        out["issuer"] = ISSUER_LABELS[best]
    for line in lines:
        if "PESEL" in line:
            continue
        m = NUMBER_RE.search(line)
        if m:
            number = _clean_number(m.group(1))
            if sum(ch.isdigit() for ch in number) >= 2 and len(re.sub(r"[^A-Z0-9]", "", number)) >= MIN_NUMBER_LEN:
                out["number"] = number
                break
    found = _dates(lines)
    valid = [d for d, i in found if _near(lines, i, VALID_WORDS)]
    issued = [d for d, i in found if _near(lines, i, ISSUED_WORDS) and d <= today]
    others = sorted({d for d, i in found if d <= today and d not in valid})  # odczyt w dwóch trybach powtarza daty
    if valid:
        out["valid_until"] = max(valid)
    if issued:
        out["issued_on"] = issued[0]
    elif len(others) == 1:
        out["issued_on"] = others[0]  # jedna data na legitymacji (np. PZSS: „Warszawa, 10.10.2024”) — data wydania
    return out


def check(image_bytes, user, credential):
    """Wynik do zapisania: RESULT_OK, RESULT_UNAVAILABLE albo „niezgodne: …” (bez odczytanego tekstu)."""
    text = read_text(image_bytes)
    if text is None:
        return RESULT_UNAVAILABLE
    missing = compare(text, user.first_name, user.last_name, credential.number, credential.issuer)
    del text  # tekst (z PESEL) nie wychodzi poza tę funkcję
    return RESULT_OK if not missing else "nie znaleziono: " + ", ".join(missing)
