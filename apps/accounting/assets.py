"""Wykaz środków trwałych oraz wartości niematerialnych i prawnych (§ 3 rozporządzenia, Dz. U. 2025 poz. 1880)
z planem odpisów amortyzacyjnych (art. 16h ust. 1 pkt 1 i art. 16i ustawy o CIT) oraz ujęciem odpisów w zestawieniu
przychodów i kosztów."""

import calendar
import io
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.audit.services import log_event

from .models import AccountingYear, DepreciationPosting, FixedAsset, Record

ONE_OFF_LIMIT = 1_000_000  # 10 000 zł w groszach (art. 16d ust. 1)
FIELDS = ("kind", "name", "kst", "acquisition", "acquired_on", "in_use_on", "entered_on", "acquisition_doc", "in_use_doc",
          "initial_value", "updated_value", "method", "rate", "prior_depreciation", "prior_until_year", "notes")
DISPOSAL_FIELDS = ("disposed_on", "disposal", "disposal_reason", "disposal_doc", "disposal_ksef")


def _next_month(d):
    return (d.year + (d.month == 12), d.month % 12 + 1)


def monthly_plan(asset, until_year):
    """Odpisy miesięczne {(rok, miesiąc): grosze} do końca `until_year` oraz suma odpisów sprzed planu (prior)."""
    if asset.method != FixedAsset.Method.LINEAR or not asset.rate or asset.base <= 0:
        return {}, 0
    base = asset.base
    monthly = int((Decimal(base) * Decimal(asset.rate) / Decimal(1200)).quantize(Decimal(1), rounding=ROUND_HALF_UP)) or 1
    y, m = _next_month(asset.entered_on)
    accumulated = prior = 0
    if asset.prior_depreciation is not None and asset.prior_until_year:
        accumulated = prior = min(asset.prior_depreciation, base)
        if (y, m) <= (asset.prior_until_year, 12):
            y, m = asset.prior_until_year + 1, 1
    end = (asset.disposed_on.year, asset.disposed_on.month) if asset.disposed_on else None
    plan = {}
    while accumulated < base and y <= until_year and (end is None or (y, m) <= end):
        amount = min(monthly, base - accumulated)
        plan[(y, m)] = amount
        accumulated += amount
        y, m = (y + (m == 12), m % 12 + 1)
    return plan, prior


def year_figures(asset, year):
    plan, prior = monthly_plan(asset, year)
    this_year = sum(v for (y, _), v in plan.items() if y == year)
    accumulated = prior + sum(plan.values())
    return {"year": this_year, "accumulated": accumulated, "remaining": max(asset.base - accumulated, 0),
            "months": {m: plan.get((year, m), 0) for m in range(1, 13)}}


def schedule(asset, years=40):
    """Plan odpisów rok po roku (do pełnego zamortyzowania albo wykreślenia)."""
    first = _next_month(asset.entered_on)[0]
    if asset.prior_until_year:
        first = min(first, asset.prior_until_year + 1)
    plan, prior = monthly_plan(asset, first + years)
    rows, accumulated = [], prior
    for year in sorted({y for y, _ in plan}):
        amount = sum(v for (y, _), v in plan.items() if y == year)
        accumulated += amount
        rows.append({"year": year, "amount": amount, "accumulated": accumulated})
    return rows, prior


def active_in(year):
    end = date(year, 12, 31)
    return [a for a in FixedAsset.objects.filter(entered_on__lte=end).order_by("number")
            if not a.disposed_on or a.disposed_on.year >= year]


def register(year):
    rows = []
    totals = {"initial": 0, "year": 0, "accumulated": 0}
    for a in active_in(year):
        f = year_figures(a, year)
        rows.append({"asset": a, **f})
        totals["initial"] += a.base
        totals["year"] += f["year"]
        totals["accumulated"] += f["accumulated"]
    return {"year": year, "rows": rows, "totals": totals}


def month_total(year, month):
    return sum(year_figures(a, year)["months"][month] for a in active_in(year))


# --- zapisy -----------------------------------------------------------------------------


def _validate(data):
    if data["in_use_on"] < data["acquired_on"]:
        raise ValidationError("Data przyjęcia do używania nie może być wcześniejsza niż data nabycia.")
    if data["entered_on"] < data["in_use_on"].replace(day=1):
        raise ValidationError("Do wykazu wprowadza się najwcześniej w miesiącu przyjęcia do używania.")
    if data["entered_on"] > timezone.localdate():
        raise ValidationError("Data wprowadzenia do wykazu nie może być z przyszłości.")
    if data["initial_value"] <= 0:
        raise ValidationError("Wartość początkowa musi być większa od zera.")
    if data["method"] == FixedAsset.Method.LINEAR and not data.get("rate"):
        raise ValidationError("Podaj stawkę amortyzacyjną z Wykazu rocznych stawek amortyzacyjnych (załącznik nr 1 do ustawy o CIT).")
    if data["method"] == FixedAsset.Method.ONE_OFF and data["initial_value"] > ONE_OFF_LIMIT:
        raise ValidationError("Bez odpisów można ująć tylko składnik o wartości początkowej do 10 000 zł (art. 16d ust. 1).")
    if data["method"] != FixedAsset.Method.LINEAR:
        data["rate"] = Decimal(0)
    if (data.get("prior_depreciation") is None) != (not data.get("prior_until_year")):
        raise ValidationError("Odpisy sprzed prowadzenia wykazu w systemie podaj razem z rokiem, do którego ich dokonano.")
    if data.get("prior_depreciation") and data["prior_depreciation"] > (data.get("updated_value") or data["initial_value"]):
        raise ValidationError("Dotychczasowe odpisy nie mogą przekraczać wartości początkowej.")


def late_entry(asset):
    """Wprowadzenie po miesiącu przyjęcia do używania = ujawnienie (§ 3 ust. 3 rozporządzenia, art. 16h ust. 1 pkt 4)."""
    return (asset.entered_on.year, asset.entered_on.month) > (asset.in_use_on.year, asset.in_use_on.month)


def _check_open(asset_or_year):
    year = asset_or_year if isinstance(asset_or_year, int) else asset_or_year.entered_on.year
    if AccountingYear.is_closed(year):
        raise ValidationError(f"Rok {year} jest zamknięty — tej pozycji wykazu nie można zmienić.")


def _snap(asset, fields=FIELDS + DISPOSAL_FIELDS):
    return {f: str(getattr(asset, f)) if getattr(asset, f) is not None else None for f in fields}


def create(data, *, by):
    _validate(data)
    _check_open(data["entered_on"].year)
    for _ in range(3):
        try:
            with transaction.atomic():
                number = (FixedAsset.objects.aggregate(m=Max("number"))["m"] or 0) + 1
                asset = FixedAsset.objects.create(number=number, created_by=by, **{f: data[f] for f in FIELDS if f in data})
            break
        except IntegrityError:
            continue
    else:
        raise ValidationError("Nie udało się nadać numeru — spróbuj ponownie.")
    log_event("ksiegowosc.srodek_trwaly", asset, f"Wykaz poz. {asset.number}: {asset.name}, {asset.initial_value / 100:.2f} zł",
              changes=_snap(asset), actor=by)
    return asset


def update(asset, data, *, by, reason):
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError("Podaj powód zmiany.")
    _validate(data)
    _check_open(asset)
    _check_open(data["entered_on"].year)
    before = _snap(asset)
    for f in FIELDS:
        if f in data:
            setattr(asset, f, data[f])
    after = _snap(asset)
    if before == after:
        raise ValidationError("Nic się nie zmieniło.")
    asset.save()
    changes = {f: [before[f], after[f]] for f in FIELDS if before[f] != after[f]}
    log_event("ksiegowosc.srodek_trwaly_zmiana", asset, f"Wykaz poz. {asset.number}: {reason}", changes=changes, actor=by)
    return asset


def dispose(asset, *, by, day, kind, reason="", doc="", ksef_number=""):
    if asset.disposed_on:
        raise ValidationError("Pozycja jest już wykreślona.")
    if kind not in FixedAsset.Disposal.values:
        raise ValidationError("Wybierz rodzaj wykreślenia.")
    if day < asset.in_use_on or day > timezone.localdate():
        raise ValidationError("Niepoprawna data wykreślenia.")
    if kind == FixedAsset.Disposal.LIQUIDATION and not reason.strip():
        raise ValidationError("Podaj przyczynę likwidacji (§ 3 ust. 1 pkt 11).")
    _check_open(day.year)
    asset.disposed_on, asset.disposal, asset.disposal_reason = day, kind, reason.strip()[:255]
    asset.disposal_doc, asset.disposal_ksef = doc.strip()[:64], ksef_number.strip()[:40]
    asset.save(update_fields=list(DISPOSAL_FIELDS))
    log_event("ksiegowosc.srodek_trwaly_wykreslenie", asset,
              f"Wykaz poz. {asset.number}: {asset.get_disposal_display().lower()} {day:%d.%m.%Y}", changes=_snap(asset, DISPOSAL_FIELDS), actor=by)
    return asset


def cancel_disposal(asset, *, by, reason):
    reason = " ".join((reason or "").split())
    if not asset.disposed_on:
        raise ValidationError("Pozycja nie jest wykreślona.")
    if not reason:
        raise ValidationError("Podaj powód.")
    _check_open(asset.disposed_on.year)
    before = _snap(asset, DISPOSAL_FIELDS)
    asset.disposed_on, asset.disposal, asset.disposal_reason, asset.disposal_doc, asset.disposal_ksef = None, "", "", "", ""
    asset.save(update_fields=list(DISPOSAL_FIELDS))
    log_event("ksiegowosc.srodek_trwaly_wykreslenie_cofniete", asset, f"Wykaz poz. {asset.number}: cofnięto wykreślenie — {reason}",
              changes=before, actor=by)


def delete(asset, *, by, reason):
    """Usunięcie pozycji wprowadzonej omyłkowo (nie dotyczy likwidacji i zbycia — te są wykreśleniem)."""
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError("Podaj przyczynę usunięcia.")
    _check_open(asset)
    if posted_months(asset):
        raise ValidationError("Odpisy od tej pozycji są już ujęte w zestawieniu przychodów i kosztów — zamiast usuwać, "
                              "popraw dane albo wykreśl pozycję.")
    log_event("ksiegowosc.srodek_trwaly_usuniecie", None, f"Usunięto z wykazu poz. {asset.number} ({asset.name}): {reason}",
              changes=_snap(asset), actor=by)
    asset.delete()


def posted_months(asset):
    """Miesiące, w których odpisy od pozycji są już ujęte w zestawieniu przychodów i kosztów."""
    plan, _ = monthly_plan(asset, timezone.localdate().year)
    out = []
    for p in DepreciationPosting.objects.filter(record__removed_at__isnull=True):
        out += [k for k in plan if k[0] == p.year and (p.month is None or k[1] == p.month)]
    return out


def post_depreciation(year, month, *, by, column="cost_deductible"):
    """Ujmuje odpisy z wykazu w zestawieniu (jeden zapis za miesiąc albo za cały rok — month=None)."""
    from . import services

    if column not in ("cost_deductible", "cost_other"):
        raise ValidationError("Wybierz kolumnę 6 albo 7.")
    existing = DepreciationPosting.objects.filter(year=year, record__removed_at__isnull=True)
    if month is None and existing.exists():
        raise ValidationError(f"Odpisy za część roku {year} są już ujęte — ujmuj kolejne miesiące osobno.")
    if month is not None and existing.filter(month__isnull=True).exists():
        raise ValidationError(f"Odpisy za cały rok {year} są już ujęte.")
    if month is not None and existing.filter(month=month).exists():
        raise ValidationError("Odpisy za ten miesiąc są już ujęte.")
    last = date(year, 12, 31) if month is None else date(year, month, calendar.monthrange(year, month)[1])
    if last > timezone.localdate():
        raise ValidationError("Odpisy ujmuje się po zakończeniu okresu.")
    amount = (sum(year_figures(a, year)["year"] for a in active_in(year)) if month is None else month_total(year, month))
    if not amount:
        raise ValidationError("Brak odpisów amortyzacyjnych w tym okresie.")
    label = f"{year}" if month is None else f"{month:02d}/{year}"
    with transaction.atomic():
        record = services.add_record(kind=Record.Kind.COST, day=last, document=f"Wykaz ŚT — odpisy {label}",
                                     description=f"Odpisy amortyzacyjne od środków trwałych i WNiP za {label}",
                                     amount=amount, column=column, by=by, notes="Zestawienie odpisów z wykazu środków trwałych")
        DepreciationPosting.objects.create(year=year, month=month, amount=amount, record=record)
    return record


# --- wydruk wykazu ------------------------------------------------------------------------


def _pln(grosze):
    return "" if grosze is None else f"{grosze / 100:,.2f}".replace(",", " ").replace(".", ",")


HEADERS = ["Lp.", "Data nabycia / wytworzenia", "Data przyjęcia do używania",
           "Dowód: a) nr KSeF faktury zbycia, b) nr dowodu przyjęcia do używania / wykreślenia, c) rodzaj dowodu nabycia / wytworzenia",
           "Określenie środka trwałego / WNiP", "Symbol KŚT", "Wartość początkowa", "Stawka amortyzacyjna",
           "Odpis amortyzacyjny za rok / narastająco", "Zaktualizowana wartość początkowa",
           "Data likwidacji i przyczyna albo data zbycia"]


def _row(r):
    a = r["asset"]
    proof = []
    if a.disposal_ksef:
        proof.append(f"a) {a.disposal_ksef}")
    b = " / ".join(x for x in (a.in_use_doc, a.disposal_doc) if x)
    if b:
        proof.append(f"b) {b}")
    if a.acquisition_doc:
        proof.append(f"c) {a.acquisition_doc}")
    disposal = ""
    if a.disposed_on:
        disposal = f"{a.get_disposal_display()} {a.disposed_on:%d.%m.%Y}" + (f": {a.disposal_reason}" if a.disposal_reason else "")
    rate = f"{a.rate:.2f}".replace(".", ",") + " %" if a.method == FixedAsset.Method.LINEAR else a.get_method_display().split(" — ")[0].split(" (")[0]
    name = a.name + (" (WNiP)" if a.kind == FixedAsset.Kind.INTANGIBLE else "")
    return [str(a.number), a.acquired_on.strftime("%d.%m.%Y"), a.in_use_on.strftime("%d.%m.%Y"), "\n".join(proof), name, a.kst,
            _pln(a.initial_value), rate, f"{_pln(r['year'])} / {_pln(r['accumulated'])}", _pln(a.updated_value), disposal]


def to_pdf(reg, association):
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
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=7, leading=9)
    bold = ParagraphStyle("bb", parent=base, fontName="DejaVu-Bold")
    head = ParagraphStyle("h", parent=base, fontName="DejaVu-Bold", fontSize=12, leading=16)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=8 * mm, rightMargin=8 * mm, topMargin=10 * mm, bottomMargin=10 * mm,
                            title=f"Wykaz środków trwałych {reg['year']}")
    data = [[Paragraph(h, bold) for h in HEADERS], [str(i) for i in range(1, 12)]]
    for r in reg["rows"]:
        data.append([Paragraph(escape(c).replace("\n", "<br/>"), base) for c in _row(r)])
    if not reg["rows"]:
        data.append(["", "", "", "", Paragraph("Brak pozycji", base)] + [""] * 6)
    t = reg["totals"]
    data.append(["", "", "", "", Paragraph("Razem", bold), "", _pln(t["initial"]), "", f"{_pln(t['year'])} / {_pln(t['accumulated'])}", "", ""])
    widths = [10, 18, 18, 46, 44, 14, 22, 16, 32, 20, 32]
    table = Table(data, colWidths=[w * mm for w in widths], repeatRows=2)
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "DejaVu", 7), ("FONT", (0, -1), (-1, -1), "DejaVu-Bold", 7),
        ("BACKGROUND", (0, 0), (-1, 1), colors.HexColor("#e8efe9")), ("ALIGN", (0, 1), (-1, 1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#888888")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story = [Paragraph(f"Wykaz środków trwałych oraz wartości niematerialnych i prawnych — rok {reg['year']}", head),
             Paragraph(escape(f"Nazwa podatnika: {association.name} · Adres: {association.address}"), base), Spacer(1, 4 * mm), table,
             Spacer(1, 3 * mm),
             Paragraph("Wykaz prowadzony zgodnie z § 3 rozporządzenia Ministra Finansów i Gospodarki z dnia 22 grudnia 2025 r. "
                       "(Dz. U. poz. 1880). Odpisy liniowe od miesiąca następującego po wprowadzeniu do wykazu (art. 16h ust. 1 "
                       f"pkt 1 ustawy o CIT). Wydruk z systemu: {timezone.localtime():%d.%m.%Y %H:%M}.", base)]
    doc.build(story)
    return buf.getvalue()


def to_xlsx(reg, association):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    ws = wb.active
    ws.title = f"Wykaz {reg['year']}"
    ws.append([f"Wykaz środków trwałych oraz wartości niematerialnych i prawnych — rok {reg['year']}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([f"Nazwa podatnika: {association.name}"])
    ws.append([f"Adres: {association.address}"])
    ws.append([])
    ws.append(HEADERS + ["Odpis za rok [zł]", "Odpisy narastająco [zł]"])
    ws.append(list(range(1, 12)))
    for c in ws[5] + ws[6]:
        c.font = Font(bold=True)
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in reg["rows"]:
        row = _row(r)
        ws.append([("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for v in row] + [r["year"] / 100, r["accumulated"] / 100])
    for col, width in zip("ABCDEFGHIJKLM", (6, 12, 12, 34, 34, 9, 14, 12, 22, 14, 26, 14, 14)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
