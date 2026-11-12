"""Potwierdzenie wpłaty (PDF) dla strzelca: dane stowarzyszenia z logo, wpłacający, kwota, sposób, czego dotyczy.
To nie jest faktura ani rachunek — dokument potwierdza przyjęcie wpłaty."""

import io
from html import escape
from pathlib import Path

from django.utils import timezone

from apps.facilities.models import format_pln


def receipt_number(payment):
    return f"PW/{timezone.localtime(payment.completed_at):%Y}/{str(payment.pk)[:8].upper()}"


def _amount_words_hint(grosze):
    return f"{grosze // 100},{grosze % 100:02d} zł"


def to_pdf(payment, association):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from apps.core.branding import icon_png_for_pdf

    fonts = Path(__file__).resolve().parent.parent / "reports" / "fonts"
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", str(fonts / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(fonts / "DejaVuSans-Bold.ttf")))
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=base, fontSize=8, leading=10.5, textColor=colors.HexColor("#555555"))
    head = ParagraphStyle("h", parent=base, fontName="DejaVu-Bold", fontSize=15, leading=19)
    big = ParagraphStyle("big", parent=base, fontName="DejaVu-Bold", fontSize=18, leading=22)

    buf = io.BytesIO()
    number = receipt_number(payment)
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Potwierdzenie wpłaty {number}", author=association.name)
    org = [Paragraph(f"<b>{escape(association.name)}</b>", base)]
    if association.address:
        org.append(Paragraph(escape(association.address), base))
    ids = " · ".join(x for x in [f"NIP {association.nip}" if association.nip else "", f"REGON {association.regon}" if association.regon else ""] if x)
    if ids:
        org.append(Paragraph(ids, base))
    contact = " · ".join(x for x in [association.contact_email, association.contact_phone] if x)
    if contact:
        org.append(Paragraph(escape(contact), small))
    try:
        logo = Image(io.BytesIO(icon_png_for_pdf()), width=24 * mm, height=24 * mm)
    except Exception:  # noqa: BLE001
        logo = ""
    top = Table([[org, logo]], colWidths=[doc.width - 28 * mm, 28 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))

    what = "Wpłata na konto strzelca"
    if payment.reservation_id:
        r = payment.reservation
        what = (f"Rezerwacja {r.number}: {r.lane}, {timezone.localtime(r.starts_at):%d.%m.%Y %H:%M}–"
                f"{timezone.localtime(r.ends_at):%H:%M}, {r.persons} os.")
    elif payment.fee_id:
        what = payment.fee.label
    elif payment.event_registration_id:
        e = payment.event_registration.event
        what = f"Opłata startowa: {e.title} ({timezone.localtime(e.starts_at):%d.%m.%Y})"
    elif payment.title:
        what = payment.title
    payer = payment.user.full_name if payment.user else payment.payer_name
    rows = [
        ["Wpłacający", Paragraph(escape(payer or "—") + (f"<br/>{escape(payment.user.email)}" if payment.user else ""), base)],
        ["Tytułem", Paragraph(escape(what), base)],
        ["Sposób zapłaty", payment.get_method_display()],
        ["Data przyjęcia", f"{timezone.localtime(payment.completed_at):%d.%m.%Y %H:%M}"],
        ["Kwota", Paragraph(_amount_words_hint(payment.amount), big)],
    ]
    if payment.surcharge:
        rows.insert(-1, ["Opłata za płatność online", f"{format_pln(payment.surcharge)} — wliczona w kwotę, bezzwrotna"])
    if payment.method == payment.Method.TPAY and payment.external_id:
        rows.insert(3, ["Identyfikator transakcji", f"{payment.external_id.split(':', 1)[-1]} ({payment.provider_name or 'online'})"])
    table = Table(rows, colWidths=[42 * mm, doc.width - 42 * mm])
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "DejaVu", 9.5), ("FONT", (0, 0), (0, -1), "DejaVu-Bold", 9.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#cccccc")),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef4ef")),
    ]))
    story = [top, Spacer(1, 10 * mm), Paragraph("Potwierdzenie wpłaty", head),
             Paragraph(f"Nr {number}", base), Spacer(1, 6 * mm), table, Spacer(1, 8 * mm)]
    if payment.is_test:
        story.append(Paragraph("<b>PŁATNOŚĆ TESTOWA (sandbox Tpay) — bez prawdziwych pieniędzy, dokument nieważny.</b>", base))
        story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        "Dokument potwierdza przyjęcie wpłaty przez stowarzyszenie. Nie jest fakturą ani rachunkiem. "
        f"Wygenerowano {timezone.localtime():%d.%m.%Y %H:%M} w systemie rezerwacji strzelnicy; "
        f"identyfikator wpłaty w systemie: {payment.pk}.", small))
    doc.build(story)
    return buf.getvalue()
