"""Wydruk zestawienia przychodów i kosztów według wzoru z załącznika nr 1 do rozporządzenia (Dz. U. 2025 poz. 1880):
PDF (A4 poziomo) i XLSX. Kolumny: 1 Lp., 2 data zdarzenia, 3 nr dowodu księgowego, 4 opis zdarzenia, 5 przychody,
6 koszty uzyskania przychodów, 7 koszty niestanowiące kosztów uzyskania przychodów, 8 uwagi."""

import io
from pathlib import Path
from xml.sax.saxutils import escape

from django.utils import timezone

MONTHS = ["", "styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień", "wrzesień", "październik",
          "listopad", "grudzień"]
HEADERS = [
    "Lp.", "Data zdarzenia", "Nr dowodu księgowego", "Opis zdarzenia",
    "Przychody (m.in. składki członkowskie, darowizny, dotacje, dochody z majątku)",
    "Koszty uzyskania przychodów (art. 15 ustawy o CIT)",
    "Koszty niestanowiące kosztów uzyskania przychodów (art. 16 ust. 1 ustawy o CIT)", "Uwagi",
]
TITLE = "Zestawienie przychodów i kosztów"
LEGAL = ("Uproszczona ewidencja przychodów i kosztów — art. 10a ustawy o działalności pożytku publicznego i o wolontariacie; "
         "wzór z załącznika nr 1 do rozporządzenia Ministra Finansów i Gospodarki z dnia 22 grudnia 2025 r. (Dz. U. poz. 1880).")


def period_label(st):
    return f"{MONTHS[st['month']]} {st['year']}" if st["month"] else f"rok {st['year']}"


def filename(st, ext):
    return f"zestawienie-przychodow-i-kosztow-{st['year']}" + (f"-{st['month']:02d}" if st["month"] else "") + f".{ext}"


def _pln(grosze):
    return f"{grosze / 100:,.2f}".replace(",", " ").replace(".", ",")


def _notes(r):
    parts = [r.notes] if r.notes else []
    if r.counterparty:
        parts.append(r.counterparty)
    for fix in r.corrections.all():
        parts.append(f"skorygowany zapisem Lp. {fix.lp}")
    return "; ".join(parts)


def _rows(st):
    return [[r.lp, r.date.strftime("%d.%m.%Y"), r.document, r.description, r.revenue, r.cost_deductible, r.cost_other, _notes(r)]
            for r in st["records"]]


def to_pdf(st, association):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    fonts = Path(__file__).resolve().parent.parent / "reports" / "fonts"
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", str(fonts / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(fonts / "DejaVuSans-Bold.ttf")))
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=7.5, leading=9.5)
    bold = ParagraphStyle("bb", parent=base, fontName="DejaVu-Bold")
    head = ParagraphStyle("h", parent=base, fontName="DejaVu-Bold", fontSize=12, leading=16)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm,
                            bottomMargin=12 * mm, title=f"{TITLE} — {period_label(st)}", author=association.name or "")
    story = [
        Paragraph(f"{TITLE} — {escape(period_label(st))}", head),
        Paragraph(f"Nazwa podatnika: <b>{escape(association.name or '')}</b>"
                  + (f" · NIP {escape(association.nip)}" if getattr(association, "nip", "") else ""), base),
        Paragraph(f"Adres: {escape(association.address or '')}", base),
        Paragraph(f"Okres sprawozdawczy: {st['start']:%d.%m.%Y} – {st['end']:%d.%m.%Y}", base),
        Spacer(1, 4 * mm),
    ]
    data = [[Paragraph(h, bold) for h in HEADERS], [str(i) for i in range(1, 9)]]
    for lp, day, docno, desc, rev, kup, other, notes in _rows(st):
        data.append([lp, day, Paragraph(escape(docno), base), Paragraph(escape(desc), base),
                     _pln(rev) if rev else "", _pln(kup) if kup else "", _pln(other) if other else "", Paragraph(escape(notes), base)])
    if not st["records"]:
        data.append(["", "", "", Paragraph("Brak zdarzeń w okresie", base), "", "", "", ""])
    p, y = st["period"], st["ytd"]
    data.append(["", "", "", Paragraph("Razem w okresie sprawozdawczym", bold), _pln(p["revenue"]), _pln(p["cost_deductible"]),
                 _pln(p["cost_other"]), ""])
    data.append(["", "", "", Paragraph("Razem od początku roku", bold), _pln(y["revenue"]), _pln(y["cost_deductible"]),
                 _pln(y["cost_other"]), ""])
    widths = [16 * mm, 19 * mm, 30 * mm, 74 * mm, 30 * mm, 30 * mm, 30 * mm, 48 * mm]
    t = Table(data, colWidths=widths, repeatRows=2)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "DejaVu", 7.5), ("FONT", (0, -2), (-1, -1), "DejaVu-Bold", 7.5),
        ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#e8efe9")), ("BACKGROUND", (0, -2), (-1, -1), colors.HexColor("#f3f3f3")),
        ("ALIGN", (0, 1), (-1, 1), "CENTER"), ("ALIGN", (4, 2), (6, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#888888")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story += [t, Spacer(1, 4 * mm),
              Paragraph(escape(LEGAL) + f" Wydruk z systemu: {timezone.localtime():%d.%m.%Y %H:%M}. Zapisy są trwałe — "
                        "błędy poprawia się zapisem korygującym.", base),
              Spacer(1, 10 * mm),
              Paragraph("Sporządził(a): ............................................ &nbsp;&nbsp;&nbsp;&nbsp; "
                        "Podpisy członków zarządu: ............................................", base)]

    def footer(canvas, doc_):
        canvas.setFont("DejaVu", 7)
        canvas.drawRightString(doc_.pagesize[0] - 10 * mm, 6 * mm, f"Strona {doc_.page}")

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _safe(v):
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v


def to_xlsx(st, association):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = (f"{st['year']}-{st['month']:02d}" if st["month"] else str(st["year"]))
    thin = Side(style="thin", color="888888")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    ws.append([f"{TITLE} — {period_label(st)}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([f"Nazwa podatnika: {association.name or ''}"])
    ws.append([f"Adres: {association.address or ''}"])
    ws.append([f"Okres sprawozdawczy: {st['start']:%d.%m.%Y} – {st['end']:%d.%m.%Y}"])
    ws.append([])
    ws.append(HEADERS)
    ws.append(list(range(1, 9)))
    first = ws.max_row - 1
    for row in ws.iter_rows(min_row=first, max_row=first + 1):
        for c in row:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="E8EFE9")
            c.alignment = Alignment(wrap_text=True, vertical="top", horizontal="center")
    for lp, day, docno, desc, rev, kup, other, notes in _rows(st):
        ws.append([lp, day, _safe(docno), _safe(desc), rev / 100 if rev else None, kup / 100 if kup else None,
                   other / 100 if other else None, _safe(notes)])
    p, y = st["period"], st["ytd"]
    ws.append([None, None, None, "Razem w okresie sprawozdawczym", p["revenue"] / 100, p["cost_deductible"] / 100, p["cost_other"] / 100])
    ws.append([None, None, None, "Razem od początku roku", y["revenue"] / 100, y["cost_deductible"] / 100, y["cost_other"] / 100])
    for row in ws.iter_rows(min_row=first, max_row=ws.max_row, max_col=8):
        for c in row:
            c.border = border
            if c.column in (5, 6, 7) and c.row > first + 1:
                c.number_format = "#,##0.00"
            elif c.row > first + 1:
                c.alignment = Alignment(wrap_text=True, vertical="top")
    for c in ws[ws.max_row - 1] + ws[ws.max_row]:
        c.font = Font(bold=True)
    for col, width in zip("ABCDEFGH", (10, 12, 18, 50, 18, 18, 18, 36)):
        ws.column_dimensions[col].width = width
    ws.row_dimensions[first].height = 60
    ws.append([])
    ws.append([LEGAL])
    ws.freeze_panes = ws.cell(row=first + 2, column=1)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
