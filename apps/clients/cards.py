"""Karty osób z kartoteki (bez konta) do wpisu w książce pobytu: format karty płatniczej (ID-1, 85,6 × 54 mm),
po 10 na arkuszu A4 z linią cięcia. Kod QR zawiera tylko podpisany odnośnik (bez danych osobowych); na karcie jest
imię, nazwisko i numer karty."""

from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.urls import reverse

CARD_W_MM, CARD_H_MM = 85.6, 54.0
COLS, ROWS = 2, 5


def card_url(person):
    from apps.logbook.services import card_token

    return settings.SITE_URL + reverse("logbook:card", args=[card_token(person)])


def card_number(person):
    return f"K-{person.pk:06d}"


def _fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    fonts = Path(__file__).resolve().parent.parent / "reports" / "fonts"
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", str(fonts / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(fonts / "DejaVuSans-Bold.ttf")))


def _fit(c, text, font, size, max_width, min_size=6):
    """Zmniejsza czcionkę, aż tekst zmieści się w szerokości (długie nazwiska)."""
    while size > min_size and c.stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def _draw_card(c, x, y, person, association_name):
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.units import mm

    w, h = CARD_W_MM * mm, CARD_H_MM * mm
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.setLineWidth(0.4)
    c.setDash(2, 2)
    c.roundRect(x, y, w, h, 3 * mm)  # linia cięcia
    c.setDash()
    # pasek u góry
    c.setFillColorRGB(0.19, 0.37, 0.23)
    c.rect(x, y + h - 9 * mm, w, 9 * mm, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("DejaVu-Bold", 9)
    c.drawString(x + 4 * mm, y + h - 6 * mm, association_name[:48])
    # kod QR (prawa strona)
    qr_size = 33 * mm
    widget = QrCodeWidget(card_url(person), barLevel="M")
    x0, y0, x1, y1 = widget.getBounds()
    drawing = Drawing(qr_size, qr_size, transform=[qr_size / (x1 - x0), 0, 0, qr_size / (y1 - y0), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, c, x + w - qr_size - 2.5 * mm, y + 5 * mm)
    # tekst (lewa strona)
    text_w = w - qr_size - 9 * mm
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.setFont("DejaVu", 7)
    c.drawString(x + 4 * mm, y + h - 14.5 * mm, "Karta strzelca — książka pobytu")
    for i, line in enumerate((person.first_name, person.last_name)):
        size = _fit(c, line, "DejaVu-Bold", 12, text_w)
        c.setFont("DejaVu-Bold", size)
        c.drawString(x + 4 * mm, y + h - (21 + i * 5.5) * mm, line)
    c.setFont("DejaVu", 6)
    for i, line in enumerate(("Przyłóż kod do czytnika w kiosku", "przy wejściu albo pokaż go", "prowadzącemu strzelanie.")):
        c.drawString(x + 4 * mm, y + (16 - i * 3) * mm, line)
    c.setFont("DejaVu-Bold", 7)
    c.drawString(x + 4 * mm, y + 5.5 * mm, card_number(person))


def render_cards(persons, association_name):
    """PDF z kartami (A4, 2 × 5 kart na stronie)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    _fonts()
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Karty strzelców")
    page_w, page_h = A4
    w, h = CARD_W_MM * mm, CARD_H_MM * mm
    left = (page_w - COLS * w) / 2
    top = page_h - (page_h - ROWS * h) / 2
    persons = list(persons)
    for i, person in enumerate(persons):
        slot = i % (COLS * ROWS)
        if i and slot == 0:
            c.showPage()
        col, row = slot % COLS, slot // COLS
        _draw_card(c, left + col * w, top - (row + 1) * h, person, association_name)
    c.showPage()
    c.save()
    return buf.getvalue()
