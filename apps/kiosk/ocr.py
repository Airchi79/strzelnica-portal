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
        img = Image.open(BytesIO(image_bytes)).convert("L")
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


def check(image_bytes, user, credential):
    """Wynik do zapisania: RESULT_OK, RESULT_UNAVAILABLE albo „niezgodne: …” (bez odczytanego tekstu)."""
    text = read_text(image_bytes)
    if text is None:
        return RESULT_UNAVAILABLE
    missing = compare(text, user.first_name, user.last_name, credential.number, credential.issuer)
    del text  # tekst (z PESEL) nie wychodzi poza tę funkcję
    return RESULT_OK if not missing else "nie znaleziono: " + ", ".join(missing)
