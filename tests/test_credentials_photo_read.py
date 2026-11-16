"""Legitymacja PS ze zdjęcia: odczyt numeru, wystawcy i dat (wspólne rozpoznawanie tekstu z kiosku) i wypełnienie
formularza. Teksty wzorowane na legitymacjach PZSS, LOK i PZŁ (fikcyjne dane, PESEL zastąpiony zerami)."""

from datetime import date
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageDraw, ImageFont

from apps.kiosk import ocr

from .conftest import make_user
from .test_kiosk_ocr import LOK, PZSS

pytestmark = pytest.mark.django_db
URL = "/dokumenty/odczyt-zdjecia/"
PZL = """POLSKI ZWIĄZEK ŁOWIECKI
LEGITYMACJA
uprawnienia prowadzącego strzelanie
Nr 12/PS/2019
Data nabycia uprawnień: 05.04.2019"""


def jpeg(color=(230, 230, 230)):
    buf = BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "JPEG")
    return SimpleUploadedFile("legitymacja.jpg", buf.getvalue(), content_type="image/jpeg")


def test_extract_pzss_lok_pzl():
    assert ocr.extract(PZSS) == {"number": "98655/2024", "issuer": "PZSS (Polski Związek Strzelectwa Sportowego)",
                                 "issued_on": date(2024, 10, 10), "valid_until": None}
    lok = ocr.extract(LOK)
    assert lok["number"] == "0079/BZGLOK/2022" and lok["issuer"] == "LOK (Liga Obrony Kraju)" and lok["issued_on"] == date(2022, 1, 11)
    assert ocr.extract(PZL) == {"number": "12/PS/2019", "issuer": "PZŁ (Polski Związek Łowiecki)", "issued_on": date(2019, 4, 5),
                                "valid_until": None}


def test_extract_skips_pesel_and_reads_validity():
    text = "LIGA OBRONY KRAJU\nNumer PESEL 00000000000\nnr legitymacji: AB-1234\nData wydania 01.02.2023\nważna do 01.02.2028"
    out = ocr.extract(text, today=date(2026, 11, 16))
    assert out["number"] == "AB-1234" and out["issued_on"] == date(2023, 2, 1) and out["valid_until"] == date(2028, 2, 1)
    assert ocr.extract("") == {"number": "", "issuer": "", "issued_on": None, "valid_until": None}
    assert ocr.extract("jakiś tekst bez danych 99.99.2020")["issued_on"] is None


def test_endpoint_returns_only_document_fields(client, monkeypatch):
    user = make_user("foto@example.com")
    client.force_login(user)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_text", lambda data: LOK)
    r = client.post(URL, {"scan": jpeg()})
    data = r.json()
    assert r.status_code == 200 and data["ok"]
    assert data["fields"] == {"number": "0079/BZGLOK/2022", "issuer": "LOK (Liga Obrony Kraju)", "issued_on": "2022-01-11"}
    assert "00000000000" not in r.content.decode() and "Nowak" not in r.content.decode()  # bez PESEL i danych osoby
    gif = SimpleUploadedFile("x.gif", b"GIF89a....", content_type="image/gif")
    assert client.post(URL, {"scan": gif}).status_code == 400  # tylko JPG, PNG i PDF
    for _ in range(8):
        client.post(URL, {"scan": jpeg()})
    assert client.post(URL, {"scan": jpeg()}).status_code == 429  # limit odczytów


def test_endpoint_requires_login_and_post(client):
    assert client.post(URL, {"scan": jpeg()}).status_code == 302
    client.force_login(make_user("foto2@example.com"))
    assert client.get(URL).status_code == 405


def test_form_shows_photo_step_first(client):
    client.force_login(make_user("foto3@example.com"))
    page = client.get("/dokumenty/dodaj/?rodzaj=ps").content.decode()
    assert page.index("2. Zdjęcie dokumentu") < page.index("3. Dane z dokumentu") and 'data-ocr-url="/dokumenty/odczyt-zdjecia/"' in page


@pytest.mark.skipif(not ocr.available(), reason="brak programu tesseract")
def test_real_ocr_extracts_from_generated_card():
    img = Image.new("RGB", (1600, 1010), (232, 232, 228))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 58)
    except OSError:
        pytest.skip("brak czcionki DejaVu")
    for i, line in enumerate(["POLSKI ZWIĄZEK STRZELECTWA SPORTOWEGO", "LEGITYMACJA PROWADZĄCEGO STRZELANIE",
                              "nr 12345/2024", "NOWAK Anna", "Warszawa, 10.10.2024"]):
        d.text((70, 90 + i * 150), line, fill=(25, 25, 25), font=font)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=90)
    out = ocr.extract(ocr.read_text(buf.getvalue()))
    assert out["number"] == "12345/2024" and out["issuer"].startswith("PZSS") and out["issued_on"] == date(2024, 10, 10)


def _pdf_with_text(lines):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    try:
        pdfmetrics.registerFont(TTFont("DejaVu", "DejaVuSans.ttf"))
        font = "DejaVu"
    except Exception:  # noqa: BLE001 — bez czcionki z polskimi znakami
        font = "Helvetica"
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont(font, 14)
    for i, line in enumerate(lines):
        c.drawString(60, 760 - i * 24, line)
    c.save()
    return buf.getvalue()


def test_pdf_with_text_layer_fills_fields(client, monkeypatch):
    """PDF z portalu (warstwa tekstowa) — odczyt bez programu do rozpoznawania obrazu."""
    client.force_login(make_user("pdf@example.com"))
    monkeypatch.setattr(ocr, "available", lambda: False)
    pdf = _pdf_with_text(["POLSKI ZWIAZEK STRZELECTWA SPORTOWEGO", "LEGITYMACJA PROWADZACEGO STRZELANIE", "Nr 98655/2024",
                          "Imie i nazwisko: Janusz Kowalski", "PESEL 00000000000", "Data wydania: 2024-10-10"])
    r = client.post(URL, {"scan": SimpleUploadedFile("legitymacja.pdf", pdf, content_type="application/pdf")})
    data = r.json()
    assert r.status_code == 200 and data["ok"], data
    assert data["fields"] == {"number": "98655/2024", "issuer": "PZSS (Polski Związek Strzelectwa Sportowego)", "issued_on": "2024-10-10"}
    assert "00000000000" not in r.content.decode()
    broken = SimpleUploadedFile("zly.pdf", b"%PDF-1.4 to nie jest pdf", content_type="application/pdf")
    r = client.post(URL, {"scan": broken})
    assert r.status_code == 200 and r.json() == {"ok": True, "fields": {}}  # nieczytelny — osoba wpisze dane


@pytest.mark.skipif(not ocr.available(), reason="brak programu tesseract")
def test_scanned_pdf_without_text_layer_uses_ocr():
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    img = Image.new("RGB", (1600, 1010), (232, 232, 228))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 58)
    except OSError:
        pytest.skip("brak czcionki DejaVu")
    for i, line in enumerate(["LIGA OBRONY KRAJU", "PROWADZĄCY STRZELANIE", "Nr uprawnienia 0079/BZGLOK/2022", "Data wydania 11.01.2022"]):
        d.text((70, 90 + i * 150), line, fill=(25, 25, 25), font=font)
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(800, 505))
    c.drawImage(ImageReader(img), 0, 0, 800, 505)
    c.save()
    out = ocr.extract(ocr.read_pdf_text(buf.getvalue()))
    assert out["number"] == "0079/BZGLOK/2022" and out["issuer"].startswith("LOK") and out["issued_on"] == date(2022, 1, 11)
