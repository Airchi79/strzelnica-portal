"""Wizualizacja faktury ustrukturyzowanej KSeF (FA(2) / FA(3)) — czytelna postać pliku XML w panelu i w PDF.

Odczyt jest niezależny od wersji schematu (elementy po nazwie lokalnej). Kod QR „KOD I” prowadzi do weryfikacji faktury
w KSeF: https://qr.ksef.mf.gov.pl/invoice/{NIP sprzedawcy}/{data wystawienia DD-MM-RRRR}/{SHA-256 pliku w Base64URL}
(dokumentacja MF „Kody weryfikujące QR”)."""

import base64
import io
from pathlib import Path
from xml.sax.saxutils import escape

from .ksef import _child, _date, _grosze, _local, _text

QR_BASES = {"produkcja": "https://qr.ksef.mf.gov.pl", "test": "https://qr-test.ksef.mf.gov.pl"}

# Pola sum wg stawek (schemat FA): netto P_13_x, VAT P_14_x
RATE_SUMS = [
    ("P_13_1", "P_14_1", "stawka podstawowa"), ("P_13_2", "P_14_2", "pierwsza stawka obniżona"),
    ("P_13_3", "P_14_3", "druga stawka obniżona"), ("P_13_4", "P_14_4", "ryczałt dla taksówek"),
    ("P_13_5", "P_14_5", "procedura szczególna (OSS)"), ("P_13_6_1", None, "0% — dostawa krajowa"),
    ("P_13_6_2", None, "0% — wewnątrzwspólnotowa dostawa towarów"), ("P_13_6_3", None, "0% — eksport"),
    ("P_13_7", None, "zwolnione z podatku"), ("P_13_8", None, "niepodlegające opodatkowaniu (poza krajem)"),
    ("P_13_9", None, "niepodlegające opodatkowaniu (art. 100 ust. 1 pkt 4)"), ("P_13_10", None, "odwrotne obciążenie"),
    ("P_13_11", None, "procedura marży"),
]
PAYMENT_FORMS = {"1": "gotówka", "2": "karta", "3": "bon", "4": "czek", "5": "kredyt", "6": "przelew", "7": "mobilna"}
INVOICE_KINDS = {"VAT": "Faktura podstawowa", "KOR": "Faktura korygująca", "ZAL": "Faktura zaliczkowa", "ROZ": "Faktura rozliczeniowa",
                 "UPR": "Faktura uproszczona", "KOR_ZAL": "Korekta faktury zaliczkowej", "KOR_ROZ": "Korekta faktury rozliczeniowej"}


def _all(el, name):
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _party(el):
    if el is None:
        return None
    ident, addr, contact = _child(el, "DaneIdentyfikacyjne"), _child(el, "Adres"), _child(el, "DaneKontaktowe")
    nip = _text(ident, "NIP")
    other = _text(ident, "NrID") or _text(ident, "NrVatUE")
    return {
        "name": _text(ident, "Nazwa"), "nip": nip, "other_id": other,
        "address": [x for x in (_text(addr, "AdresL1"), _text(addr, "AdresL2")) if x],
        "country": _text(addr, "KodKraju"),
        "email": _text(contact, "Email"), "phone": _text(contact, "Telefon"),
        "customer_no": _text(el, "NrKlienta"),
    }


def read(content):
    """Treść faktury do wizualizacji (słownik). Kwoty w groszach."""
    from defusedxml import ElementTree

    root = ElementTree.fromstring(content)
    header, fa = _child(root, "Naglowek"), _child(root, "Fa")
    form = _child(header, "KodFormularza")
    lines = []
    for row in _all(fa, "FaWiersz"):
        lines.append({
            "no": _text(row, "NrWierszaFa"), "name": _text(row, "P_7"), "unit": _text(row, "P_8A"), "qty": _text(row, "P_8B"),
            "unit_net": _grosze(_text(row, "P_9A")) if _text(row, "P_9A") else None,
            "unit_gross": _grosze(_text(row, "P_9B")) if _text(row, "P_9B") else None,
            "net": _grosze(_text(row, "P_11")) if _text(row, "P_11") else None,
            "gross": _grosze(_text(row, "P_11A")) if _text(row, "P_11A") else None,
            "rate": _text(row, "P_12"), "discount": _text(row, "P_10"),
        })
    sums = []
    for net_f, vat_f, label in RATE_SUMS:
        net = _text(fa, net_f)
        if net:
            sums.append({"label": label, "net": _grosze(net), "vat": _grosze(_text(fa, vat_f)) if vat_f and _text(fa, vat_f) else None})
    notes = _child(fa, "Adnotacje")
    flags = []
    if _text(notes, "P_16") == "1":
        flags.append("metoda kasowa")
    if _text(notes, "P_17") == "1":
        flags.append("samofakturowanie")
    if _text(notes, "P_18") == "1":
        flags.append("odwrotne obciążenie")
    if _text(notes, "P_18A") == "1":
        flags.append("mechanizm podzielonej płatności")
    exemption = _child(notes, "Zwolnienie")
    if exemption is not None and _text(exemption, "P_19") == "1":
        basis = _text(exemption, "P_19A") or _text(exemption, "P_19B") or _text(exemption, "P_19C")
        flags.append("zwolnienie z VAT" + (f": {basis}" if basis else ""))
    payment = _child(fa, "Platnosc")
    accounts = [{"number": _text(a, "NrRB"), "bank": _text(a, "NazwaBanku"), "description": _text(a, "OpisRachunku")}
                for a in _all(payment, "RachunekBankowy")]
    terms = [_text(t, "Termin") for t in _all(payment, "TerminPlatnosci") if _text(t, "Termin")]
    period = _child(fa, "OkresFa")
    corrected = [{"date": _text(k, "DataWystFaKorygowanej"), "number": _text(k, "NrFaKorygowanej"), "ksef": _text(k, "NrKSeFFaKorygowanej")}
                 for k in _all(fa, "DaneFaKorygowanej")]
    footer = _child(root, "Stopka")
    registers = _child(footer, "Rejestry")
    kind = _text(fa, "RodzajFaktury")
    return {
        "form": f"{form.get('kodSystemowy', '')}" if form is not None else "",
        "created": _text(header, "DataWytworzeniaFa"), "system": _text(header, "SystemInfo"),
        "seller": _party(_child(root, "Podmiot1")), "buyer": _party(_child(root, "Podmiot2")),
        "third": [p for p in (_party(x) for x in _all(root, "Podmiot3")) if p],
        "currency": _text(fa, "KodWaluty") or "PLN", "issue_date": _date(_text(fa, "P_1")), "place": _text(fa, "P_1M"),
        "number": _text(fa, "P_2"), "sale_date": _date(_text(fa, "P_6")),
        "period": (_text(period, "P_6_Od"), _text(period, "P_6_Do")) if period is not None else None,
        "kind": kind, "kind_label": INVOICE_KINDS.get(kind, kind or "Faktura"), "correction_reason": _text(fa, "PrzyczynaKorekty"),
        "corrected": corrected, "lines": lines, "sums": sums, "total": _grosze(_text(fa, "P_15")),
        "flags": flags, "extra": [(_text(d, "Klucz"), _text(d, "Wartosc")) for d in _all(fa, "DodatkowyOpis")],
        "payment": {
            "paid": _text(payment, "Zaplacono") == "1", "paid_on": _text(payment, "DataZaplaty"), "terms": terms,
            "form": PAYMENT_FORMS.get(_text(payment, "FormaPlatnosci"), _text(payment, "FormaPlatnosci")), "accounts": accounts,
        } if payment is not None else None,
        "to_pay": _grosze(_text(_child(fa, "Rozliczenie"), "DoZaplaty")) if _text(_child(fa, "Rozliczenie"), "DoZaplaty") else None,
        "footer": [_text(i, "StopkaFaktury") for i in _all(footer, "Informacje") if _text(i, "StopkaFaktury")],
        "registers": [(k, _text(registers, k)) for k in ("KRS", "REGON", "BDO") if _text(registers, k)],
    }


def qr_url(invoice, environment="produkcja"):
    """KOD I — link weryfikacyjny KSeF (tylko gdy znany jest numer KSeF i NIP sprzedawcy)."""
    if not invoice.ksef_number or not invoice.seller_nip:
        return ""
    digest = base64.urlsafe_b64encode(bytes.fromhex(invoice.sha256)).decode().rstrip("=")
    return f"{QR_BASES.get(environment, QR_BASES['produkcja'])}/invoice/{invoice.seller_nip}/{invoice.issue_date:%d-%m-%Y}/{digest}"


def _pln(grosze, currency="PLN"):
    if grosze is None:
        return ""
    return f"{grosze / 100:,.2f}".replace(",", " ").replace(".", ",") + ("" if currency == "PLN" else f" {currency}")


def to_pdf(invoice, data, qr=""):
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    fonts = Path(__file__).resolve().parent.parent / "reports" / "fonts"
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", str(fonts / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(fonts / "DejaVuSans-Bold.ttf")))
    from reportlab.pdfbase.pdfmetrics import registerFontFamily

    registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu", boldItalic="DejaVu-Bold")  # <b> w akapitach
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=8.5, leading=11)
    small = ParagraphStyle("s", parent=base, fontSize=7, leading=9)
    bold = ParagraphStyle("bb", parent=base, fontName="DejaVu-Bold")
    head = ParagraphStyle("h", parent=base, fontName="DejaVu-Bold", fontSize=14, leading=18)
    cur = data["currency"]

    def p(text, style=base):
        return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)

    def party(title, x):
        if not x:
            return p("")
        rows = [f"<b>{escape(title)}</b>", escape(x["name"])] + [escape(a) for a in x["address"]]
        if x["nip"]:
            rows.append(f"NIP: {escape(x['nip'])}")
        elif x["other_id"]:
            rows.append(f"Identyfikator: {escape(x['other_id'])}")
        if x["email"] or x["phone"]:
            rows.append(escape(" · ".join(v for v in (x["email"], x["phone"]) if v)))
        return Paragraph("<br/>".join(rows), base)

    class Qr(Flowable):
        def __init__(self, url, size=28 * mm):
            super().__init__()
            self.url, self.size = url, size
            self.width = self.height = size

        def draw(self):
            widget = QrCodeWidget(self.url, barLevel="M")
            x0, y0, x1, y1 = widget.getBounds()
            d = Drawing(self.size, self.size, transform=[self.size / (x1 - x0), 0, 0, self.size / (y1 - y0), 0, 0])
            d.add(widget)
            renderPDF.draw(d, self.canv, 0, 0)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"Faktura {data['number']}")
    dates = [f"Data wystawienia: {data['issue_date']:%d.%m.%Y}" + (f", {data['place']}" if data["place"] else "")]
    if data["sale_date"]:
        dates.append(f"Data dostawy / wykonania usługi: {data['sale_date']:%d.%m.%Y}")
    if data["period"]:
        dates.append(f"Okres: {data['period'][0]} – {data['period'][1]}")
    title = [p(f"{data['kind_label']} nr {data['number']}", head), p("\n".join(dates))]
    if invoice.ksef_number:
        title.append(p(f"Numer KSeF: {invoice.ksef_number}", small))
    top = Table([[title, Qr(qr) if qr else ""], ["", p(invoice.ksef_number, small) if qr else ""]],
                colWidths=[doc.width - 32 * mm, 32 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, -1), "CENTER"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story = [top, Spacer(1, 4 * mm)]
    parties = Table([[party("Sprzedawca", data["seller"]), party("Nabywca", data["buyer"])]], colWidths=[doc.width / 2] * 2)
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (0, 0), 0.4, colors.grey),
                                 ("BOX", (1, 0), (1, 0), 0.4, colors.grey)]))
    story += [parties, Spacer(1, 4 * mm)]
    for x in data["third"]:
        story += [party("Podmiot trzeci", x), Spacer(1, 2 * mm)]
    if data["corrected"]:
        for k in data["corrected"]:
            story.append(p(f"Korygowana faktura: {k['number']} z {k['date']}" + (f" (KSeF {k['ksef']})" if k["ksef"] else ""), bold))
        if data["correction_reason"]:
            story.append(p(f"Przyczyna korekty: {data['correction_reason']}"))
        story.append(Spacer(1, 2 * mm))
    rows = [[p(h, bold) for h in ("Lp.", "Nazwa", "J.m.", "Ilość", "Cena netto", "Wartość netto", "Stawka", "Wartość brutto")]]
    for line in data["lines"]:
        rows.append([p(line["no"]), p(line["name"]), p(line["unit"]), p(line["qty"]), p(_pln(line["unit_net"] or line["unit_gross"], cur)),
                     p(_pln(line["net"], cur)), p(line["rate"]), p(_pln(line["gross"], cur))])
    t = Table(rows, colWidths=[11 * mm, None, 12 * mm, 14 * mm, 22 * mm, 24 * mm, 17 * mm, 24 * mm], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t, Spacer(1, 3 * mm)]
    if data["sums"]:
        srows = [[p("Stawka", bold), p("Netto", bold), p("VAT", bold)]] + [[p(s["label"]), p(_pln(s["net"], cur)), p(_pln(s["vat"], cur))]
                                                                       for s in data["sums"]]
        st = Table(srows, colWidths=[60 * mm, 30 * mm, 30 * mm], hAlign="RIGHT")
        st.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999"))]))
        story += [st, Spacer(1, 2 * mm)]
    story.append(Paragraph(f"<b>Należność ogółem: {escape(_pln(data['total'], cur))}{' zł' if cur == 'PLN' else ''}</b>", ParagraphStyle("t", parent=base, fontSize=11,
                                                                                                         leading=14, alignment=2)))
    if data["to_pay"] is not None and data["to_pay"] != data["total"]:
        story.append(Paragraph(f"Do zapłaty: {escape(_pln(data['to_pay'], cur))}{' zł' if cur == 'PLN' else ''}", ParagraphStyle("tp", parent=base, alignment=2)))
    story.append(Spacer(1, 3 * mm))
    if data["payment"]:
        pay = data["payment"]
        bits = []
        if pay["paid"]:
            bits.append("Zapłacono" + (f" {pay['paid_on']}" if pay["paid_on"] else ""))
        if pay["form"]:
            bits.append(f"Forma płatności: {pay['form']}")
        if pay["terms"]:
            bits.append(f"Termin płatności: {', '.join(pay['terms'])}")
        for a in pay["accounts"]:
            bits.append(f"Rachunek: {a['number']}" + (f" ({a['bank']})" if a["bank"] else ""))
        story.append(p("\n".join(bits)))
    if data["flags"]:
        story.append(p("Adnotacje: " + "; ".join(data["flags"])))
    for k, v in data["extra"]:
        story.append(p(f"{k}: {v}", small))
    for line in data["footer"]:
        story.append(p(line, small))
    if data["registers"]:
        story.append(p(" · ".join(f"{k}: {v}" for k, v in data["registers"]), small))
    story += [Spacer(1, 4 * mm), p("Wizualizacja faktury ustrukturyzowanej pobranej z KSeF. Dokumentem jest plik XML w KSeF"
                                   + (" — kod QR prowadzi do jego weryfikacji." if qr else "."), small)]
    doc.build(story)
    return buf.getvalue()
