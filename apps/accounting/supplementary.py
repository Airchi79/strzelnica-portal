"""Zestawienie uzupełniające (§ 4 rozporządzenia, Dz. U. 2025 poz. 1880):
1) dane do rachunkowego wyodrębnienia form działalności (art. 10 ust. 1 pkt 1 i 2 ustawy o działalności pożytku
   publicznego) i środków z umów o realizację zadania publicznego (art. 16 ust. 5),
2) łączna wartość darowizn rzeczowych — do limitu przychodów z art. 10a ust. 1 pkt 5 ustawy (1 000 000 zł).
Bez danych osobowych (§ 4 ust. 2)."""

import io
import re
from pathlib import Path
from xml.sax.saxutils import escape

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.audit.services import log_event

from .models import Activity, InKindDonation, PublicTask, Record

REVENUE_LIMIT = 100_000_000  # 1 000 000 zł w groszach


def _sums(qs):
    agg = qs.aggregate(r=Sum("revenue"), k=Sum("cost_deductible"), o=Sum("cost_other"))
    r, k, o = agg["r"] or 0, agg["k"] or 0, agg["o"] or 0
    return {"revenue": r, "cost_deductible": k, "cost_other": o, "costs": k + o, "result": r - k - o}


def donations_total(year, statutory_only=True):
    qs = InKindDonation.objects.filter(date__year=year)
    if statutory_only:
        qs = qs.filter(statutory=True)
    return qs.aggregate(s=Sum("value"))["s"] or 0


def revenue_for_limit(year):
    """Przychody roku bez darowizn rzeczowych przeznaczonych na działalność statutową (art. 10a ust. 1 pkt 5)."""
    revenue = Record.objects.filter(year=year, removed_at__isnull=True).aggregate(s=Sum("revenue"))["s"] or 0
    return revenue - donations_total(year)


def build(year):
    records = Record.objects.filter(year=year, removed_at__isnull=True)
    activities = [{"key": key, "label": label, **_sums(records.filter(activity=key))} for key, label in Activity.choices]
    unassigned = _sums(records.filter(activity=""))
    total = _sums(records)
    tasks = []
    for task in PublicTask.objects.filter(Q(records__year=year) | Q(starts_on__year__lte=year, ends_on__year__gte=year)).distinct():
        in_year = _sums(records.filter(task=task))
        all_time = _sums(Record.objects.filter(task=task, removed_at__isnull=True))
        tasks.append({"task": task, "year": in_year, "all": all_time, "left": task.grant - all_time["costs"]})
    donations = list(InKindDonation.objects.filter(date__year=year).order_by("date", "pk"))
    statutory = sum(d.value for d in donations if d.statutory)
    limit_base = total["revenue"] - statutory
    return {
        "year": year, "activities": activities, "unassigned": unassigned, "unassigned_count": records.filter(activity="").count(),
        "total": total, "tasks": tasks, "donations": donations, "donations_total": sum(d.value for d in donations),
        "donations_statutory": statutory, "limit_base": limit_base, "limit": REVENUE_LIMIT, "limit_ok": limit_base <= REVENUE_LIMIT,
    }


# --- umowy i darowizny ----------------------------------------------------------------------


def save_task(data, *, by, task=None):
    if data["ends_on"] < data["starts_on"]:
        raise ValidationError("Koniec realizacji nie może być wcześniejszy niż początek.")
    if data["grant"] <= 0:
        raise ValidationError("Kwota dotacji musi być większa od zera.")
    created = task is None
    task = task or PublicTask(created_by=by)
    for f in ("name", "agreement", "grantor", "signed_on", "starts_on", "ends_on", "grant", "notes"):
        setattr(task, f, data.get(f, getattr(task, f, "")))
    task.save()
    log_event("ksiegowosc.zadanie_publiczne", task, ("Dodano" if created else "Zmieniono") + f" umowę: {task}", actor=by)
    return task


def add_donation(data, *, by, add_revenue=False):
    from . import services

    if data["value"] <= 0:
        raise ValidationError("Wartość darowizny musi być większa od zera.")
    if data["date"] > timezone.localdate():
        raise ValidationError("Data nie może być z przyszłości.")
    with transaction.atomic():
        record = None
        if add_revenue:
            record = services.add_record(kind=Record.Kind.REVENUE, day=data["date"], document=data.get("document") or "darowizna rzeczowa",
                                         description=f"Darowizna rzeczowa: {data['description']}"[:255], amount=data["value"],
                                         column="revenue", by=by, activity=data.get("activity", ""))
        donation = InKindDonation.objects.create(date=data["date"], description=data["description"], value=data["value"],
                                                 document=data.get("document", ""), statutory=data.get("statutory", True),
                                                 record=record, created_by=by)
    log_event("ksiegowosc.darowizna_rzeczowa", donation, f"Darowizna rzeczowa {donation.value / 100:.2f} zł: {donation.description}",
              actor=by)
    return donation


def delete_donation(donation, *, by, reason):
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError("Podaj przyczynę usunięcia.")
    log_event("ksiegowosc.darowizna_rzeczowa_usuniecie", None,
              f"Usunięto darowiznę rzeczową {donation.value / 100:.2f} zł z {donation.date:%d.%m.%Y} ({donation.description}): {reason}",
              actor=by)
    donation.delete()


# --- wydruk -------------------------------------------------------------------------------


def _pln(grosze):
    return f"{grosze / 100:,.2f}".replace(",", " ").replace(".", ",")


def sections(st):
    """Tabele wydruku: (tytuł, nagłówek, wiersze)."""
    act = [[a["label"], _pln(a["revenue"]), _pln(a["cost_deductible"]), _pln(a["cost_other"]), _pln(a["result"])]
           for a in st["activities"]]
    u = st["unassigned"]
    if st["unassigned_count"]:
        act.append(["Nieprzypisane do formy działalności", _pln(u["revenue"]), _pln(u["cost_deductible"]), _pln(u["cost_other"]),
                    _pln(u["result"])])
    t = st["total"]
    act.append(["Razem", _pln(t["revenue"]), _pln(t["cost_deductible"]), _pln(t["cost_other"]), _pln(t["result"])])
    tasks = [[f"{x['task'].name}" + (f" — umowa {x['task'].agreement}" if x["task"].agreement else "") + f" ({x['task'].grantor})",
              _pln(x["task"].grant), _pln(x["year"]["revenue"]), _pln(x["year"]["costs"]), _pln(x["all"]["costs"]), _pln(x["left"])]
             for x in st["tasks"]] or [["Brak umów o realizację zadania publicznego", "", "", "", "", ""]]
    dons = [[d.date.strftime("%d.%m.%Y"), d.description, d.document, "tak" if d.statutory else "nie", _pln(d.value)] for d in st["donations"]]
    dons.append(["", "Razem darowizny rzeczowe", "", "", _pln(st["donations_total"])])
    limit = [["Przychody roku (kol. 5 zestawienia przychodów i kosztów)", _pln(t["revenue"])],
             ["minus darowizny rzeczowe przeznaczone na działalność statutową", _pln(st["donations_statutory"])],
             ["Przychody do limitu z art. 10a ust. 1 pkt 5 ustawy", _pln(st["limit_base"])],
             ["Limit", _pln(st["limit"])],
             ["Warunek limitu", "spełniony" if st["limit_ok"] else "PRZEKROCZONY — zawiadom urząd skarbowy (art. 10a ust. 5 pkt 2)"]]
    return [
        ("1. Formy działalności (art. 10 ust. 1 pkt 1 i 2 ustawy o działalności pożytku publicznego)",
         ["Forma działalności", "Przychody", "Koszty uzyskania przychodów", "Pozostałe koszty", "Wynik"], act),
        ("2. Środki z umów o realizację zadania publicznego (art. 16 ust. 5 ustawy)",
         ["Zadanie / umowa", "Dotacja z umowy", "Przychody w roku", "Koszty w roku", "Koszty od początku umowy", "Pozostało"], tasks),
        ("3. Darowizny rzeczowe (§ 4 ust. 1 pkt 2 rozporządzenia)", ["Data", "Przedmiot", "Dowód", "Na działalność statutową", "Wartość"], dons),
        ("4. Limit przychodów uproszczonej ewidencji (art. 10a ust. 1 pkt 5 ustawy)", ["Pozycja", "Kwota"], limit),
    ]


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
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=8, leading=10)
    bold = ParagraphStyle("bb", parent=base, fontName="DejaVu-Bold")
    head = ParagraphStyle("h", parent=base, fontName="DejaVu-Bold", fontSize=12, leading=16)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"Zestawienie uzupełniające {st['year']}")
    story = [Paragraph(f"Zestawienie uzupełniające — rok {st['year']}", head),
             Paragraph(escape(f"Nazwa podatnika: {association.name} · Adres: {association.address}"), base), Spacer(1, 4 * mm)]
    for title, header, rows in sections(st):
        story.append(Paragraph(escape(title), bold))
        story.append(Spacer(1, 2 * mm))
        data = [[Paragraph(escape(h), bold) for h in header]] + [[Paragraph(escape(str(c)), base) for c in r] for r in rows]
        first = doc.width * (0.45 if len(header) > 2 else 0.7)
        widths = [first] + [(doc.width - first) / (len(header) - 1)] * (len(header) - 1)
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#888888")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8efe9"))]))
        story += [t, Spacer(1, 5 * mm)]
    story.append(Paragraph("Zestawienie uzupełniające wg § 4 rozporządzenia Ministra Finansów i Gospodarki z dnia 22 grudnia 2025 r. "
                           "(Dz. U. poz. 1880); nie zawiera danych osobowych. Wydruk z systemu: "
                           f"{timezone.localtime():%d.%m.%Y %H:%M}.", base))
    doc.build(story)
    return buf.getvalue()


def _cell(v):
    """Tekst z danych nie może zostać formułą arkusza; kwoty ujemne („-1 234,00”) zostają bez zmian."""
    if isinstance(v, str) and v[:1] in "=+-@" and not re.fullmatch(r"-[\d \u00a0]+,\d{2}", v):
        return "'" + v
    return v


def to_xlsx(st, association):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = f"Uzupełniające {st['year']}"
    ws.append([f"Zestawienie uzupełniające — rok {st['year']}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([f"Nazwa podatnika: {association.name}"])
    ws.append([f"Adres: {association.address}"])
    for title, header, rows in sections(st):
        ws.append([])
        ws.append([title])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        ws.append(header)
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append([_cell(v) for v in r])
    ws.column_dimensions["A"].width = 60
    for col in "BCDEF":
        ws.column_dimensions[col].width = 20
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
