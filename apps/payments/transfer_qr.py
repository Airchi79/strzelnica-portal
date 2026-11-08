"""Kod QR do przelewu — standard 2D Związku Banków Polskich („Zeskanuj i zapłać” w aplikacjach banków).

Format (pola oddzielone „|”, łącznie do 160 znaków):
NIP odbiorcy (10 cyfr lub puste) | kod kraju | numer rachunku (26 cyfr) | kwota w groszach (co najmniej 6 cyfr, 0 = kwotę
wpisuje płacący) | nazwa odbiorcy (do 20 znaków) | tytuł (do 32 znaków) | 3 pola zarezerwowane (puste).
Nazwa i tytuł bez polskich znaków — nie każda aplikacja bankowa poprawnie je odczytuje."""

import re
import unicodedata

from apps.core.qr import qr_svg_data_uri

MAX_LEN = 160
NAME_MAX = 20
TITLE_MAX = 32
_PL = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def _ascii(text):
    text = (text or "").translate(_PL)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[|\s]+", " ", text).strip()


def recipient_name(association_name):
    """Krótka nazwa odbiorcy: bez słowa „Stowarzyszenie” i cudzysłowów, np. „STRZELNICA SATOK”."""
    name = re.sub(r"(?i)^stowarzyszenie\s+", "", association_name or "")
    name = re.sub(r"[\"„”“'«»]", "", name)
    return _ascii(name)[:NAME_MAX].strip()


def payload(account, amount_grosze, title, nip="", name=""):
    """Treść kodu QR albo '' (brak poprawnego rachunku)."""
    digits = re.sub(r"\D", "", account or "")
    if len(digits) != 26:
        return ""
    nip = re.sub(r"\D", "", nip or "")
    if len(nip) != 10:
        nip = ""
    amount = max(int(amount_grosze or 0), 0)
    fields = [nip, "PL", digits, f"{amount:06d}", recipient_name(name), _ascii(title)[:TITLE_MAX].strip(), "", "", ""]
    text = "|".join(fields)
    return text if len(text) <= MAX_LEN else ""


def qr_for(association, amount_grosze, title):
    """Kod QR (obraz SVG w adresie data:) dla danych stowarzyszenia albo '' (brak rachunku)."""
    text = payload(association.bank_account, amount_grosze, title, association.nip, association.name)
    return qr_svg_data_uri(text, box_size=5) if text else ""
