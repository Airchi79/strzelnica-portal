"""Płatności online przez Stripe (konto Stripe Express stowarzyszenia, platforma GunTracker) w ewidencji.

Eksporty z panelu Stripe (Saldo → Eksportuj, kolumny „Wszystko”):
- historia salda (balance_history.csv): id, Type (payment / payment_refund / adjustment / payout), Amount, Fee, Net,
  Created (UTC), Description… — bez danych klientów,
- wypłaty (payouts.csv): id, Amount, Created (UTC), Arrival Date (UTC), Status, Statement Descriptor…

Ujęcie w zestawieniu przychodów i kosztów:
- przychód dnia = kwoty zapłacone przez klientów − zwroty (dzień wg czasu polskiego); dowód: dzienne zestawienie wpłat
  online (§ 13 rozporządzenia — przychody bez faktur jedną kwotą za dzień),
- prowizje (opłaty z eksportu) dokumentują faktury platformy w KSeF — księguje się je z faktur; z eksportu Stripe
  prowizje są tylko kontrolą (opcjonalnie: koszt miesięczny, gdy faktury nie ma),
- wypłata z salda Stripe na rachunek bankowy to przeniesienie środków — w wyciągu bankowym łączy się z wypłatą
  (kwota i data ±4 dni; bank księguje zwykle dzień przed „Arrival Date”) i nie jest drugi raz przychodem."""

import csv
import io
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_event

from .models import BankOperation, Record, StripePayout, StripePosting, StripeTransaction

MAX_BYTES = 20 * 1024 * 1024
UTC = ZoneInfo("UTC")
WARSAW = ZoneInfo("Europe/Warsaw")
PAYOUT_WINDOW_DAYS = 4
TX_TYPES_REVENUE = {"payment", "charge", "payment_refund", "refund"}
TX_TYPES_FEE_ADJ = {"adjustment"}


def _grosze(value):
    try:
        return int((Decimal(str(value or "0").replace(",", ".").strip() or "0") * 100).quantize(Decimal(1)))
    except InvalidOperation as e:
        raise ValidationError(f"Niepoprawna kwota w eksporcie Stripe: {value}") from e


def _utc(value):
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _rows(uploaded):
    if uploaded.size > MAX_BYTES:
        raise ValidationError("Plik jest za duży.")
    raw = uploaded.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValidationError("Plik nie jest eksportem CSV ze Stripe (kodowanie UTF-8).") from e
    reader = csv.DictReader(io.StringIO(text))
    return reader.fieldnames or [], list(reader)


def import_files(uploads, *, by):
    """Rozpoznaje eksport po kolumnach. Zwraca słownik liczników."""
    result = {"transactions": 0, "payouts": 0, "duplicates": 0, "matched": 0}
    for uploaded in uploads:
        if not (uploaded.name or "").lower().endswith(".csv"):
            raise ValidationError(f"{uploaded.name}: wybierz pliki CSV wyeksportowane ze Stripe.")
        header, rows = _rows(uploaded)
        if {"Type", "Amount", "Fee", "Net", "Created (UTC)"} <= set(header):
            _import_balance(rows, result)
        elif {"Amount", "Arrival Date (UTC)", "Status"} <= set(header):
            _import_payouts(rows, result)
        else:
            raise ValidationError(f"{uploaded.name}: nierozpoznany eksport. Wgraj „Historię salda” i „Wypłaty” ze Stripe (CSV, kolumny „Wszystko”).")
    result["matched"] = match_bank_operations()
    log_event("ksiegowosc.stripe_import", None, f"Stripe: operacji {result['transactions']}, wypłat {result['payouts']}, "
                                                f"połączonych z wyciągiem {result['matched']}", actor=by)
    return result


def _import_balance(rows, result):
    with transaction.atomic():
        for r in rows:
            sid, kind = (r.get("id") or "").strip(), (r.get("Type") or "").strip()
            created = _utc(r.get("Created (UTC)"))
            if not sid or not created:
                continue
            if (r.get("Currency") or "pln").lower() != "pln":
                raise ValidationError("Eksport zawiera operacje w walucie innej niż PLN — przelicz je ręcznie (§ 7 ust. 5).")
            if kind == "payout":
                # wypłata z historii salda (gdy brak eksportu wypłat) — data wpływu nieznana: przyjmujemy dzień utworzenia
                if not StripePayout.objects.filter(stripe_id=r.get("Source") or sid).exists():
                    StripePayout.objects.create(stripe_id=(r.get("Source") or sid)[:64], amount=-_grosze(r["Amount"]),
                                                created_at=created, arrival_date=None, status="paid")
                    result["payouts"] += 1
                continue
            if StripeTransaction.objects.filter(stripe_id=sid).exists():
                result["duplicates"] += 1
                continue
            StripeTransaction.objects.create(
                stripe_id=sid[:64], type=kind[:40], source=(r.get("Source") or "")[:64], amount=_grosze(r["Amount"]),
                fee=_grosze(r.get("Fee")), net=_grosze(r["Net"]), created_at=created, day=created.astimezone(WARSAW).date(),
                description=" ".join((r.get("Description") or "").split())[:255])
            result["transactions"] += 1


def _import_payouts(rows, result):
    with transaction.atomic():
        for r in rows:
            sid = (r.get("id") or "").strip()
            created = _utc(r.get("Created (UTC)"))
            if not sid or not created or (r.get("Status") or "").strip().lower() != "paid":
                continue  # wypłata w drodze / nieudana — nie ma jej na rachunku
            arrival = _utc(r.get("Arrival Date (UTC)"))
            obj = StripePayout.objects.filter(stripe_id=sid).first()
            if obj:
                if obj.arrival_date is None and arrival:  # uzupełnienie danych z historii salda
                    obj.arrival_date, obj.descriptor = arrival.date(), (r.get("Statement Descriptor") or "")[:60]
                    obj.save(update_fields=["arrival_date", "descriptor"])
                else:
                    result["duplicates"] += 1
                continue
            StripePayout.objects.create(stripe_id=sid[:64], amount=_grosze(r["Amount"]), created_at=created,
                                        arrival_date=arrival.date() if arrival else None, status="paid",
                                        descriptor=(r.get("Statement Descriptor") or "")[:60])
            result["payouts"] += 1


def match_bank_operations():
    """Wpływy z wyciągu czekające na decyzję ↔ wypłaty Stripe (ta sama kwota, data ±4 dni, najbliższa)."""
    matched = 0
    payouts = list(StripePayout.objects.filter(bank_operations__isnull=True))
    for op in BankOperation.objects.filter(status=BankOperation.Status.NEW, amount__gt=0).order_by("booked_on", "pk"):
        best = None
        for p in payouts:
            ref = p.arrival_date or timezone.localtime(p.created_at).date() + timedelta(days=1)
            gap = abs((op.booked_on - ref).days)
            if p.amount == op.amount and gap <= PAYOUT_WINDOW_DAYS and (best is None or gap < best[0]):
                best = (gap, p)
        if best:
            payout = best[1]
            op.stripe_payout, op.status, op.status_note = payout, BankOperation.Status.STRIPE, ""
            op.save(update_fields=["stripe_payout", "status", "status_note"])
            payouts.remove(payout)
            matched += 1
    return matched


# --- zestawienia i ujęcie w ewidencji --------------------------------------------------------


def day_totals(day_from=None, day_to=None):
    """{dzień: {gross, refunds, fees, fee_adjustments, count, revenue}}"""
    qs = StripeTransaction.objects.all()
    if day_from:
        qs = qs.filter(day__gte=day_from)
    if day_to:
        qs = qs.filter(day__lte=day_to)
    out = {}
    for t in qs.order_by("day", "created_at"):
        d = out.setdefault(t.day, {"gross": 0, "refunds": 0, "fees": 0, "fee_adjustments": 0, "count": 0})
        if t.type in ("payment", "charge"):
            d["gross"] += t.amount
            d["fees"] += t.fee
            d["count"] += 1
        elif t.type in ("payment_refund", "refund"):
            d["refunds"] += t.amount  # ujemne
            d["fees"] += t.fee
        else:  # adjustment: zwrot opłaty (+) albo dodatkowa opłata (−) — koryguje koszty prowizji
            d["fee_adjustments"] += t.amount
    for d in out.values():
        d["revenue"] = d["gross"] + d["refunds"]
        d["fee_cost"] = d["fees"] - d["fee_adjustments"]
    return out


def months():
    """Podsumowanie miesięcy: sumy, wypłaty, stan ujęcia w ewidencji."""
    totals = day_totals()
    posted_days = set(StripePosting.objects.filter(kind=StripePosting.Kind.REVENUE, record__removed_at__isnull=True)
                      .values_list("period_start", flat=True))
    posted_months = set(StripePosting.objects.filter(kind=StripePosting.Kind.FEES, record__removed_at__isnull=True)
                        .values_list("period_start", flat=True))
    out = {}
    for day, t in totals.items():
        m = out.setdefault(day.replace(day=1), {"month": day.replace(day=1), "days": 0, "gross": 0, "refunds": 0, "revenue": 0,
                                                 "fee_cost": 0, "count": 0, "posted_days": 0, "payouts": 0})
        m["days"] += 1
        for k in ("gross", "refunds", "revenue", "fee_cost", "count"):
            m[k] += t[k]
        m["posted_days"] += day in posted_days
    for p in StripePayout.objects.all():
        key = (p.arrival_date or timezone.localtime(p.created_at).date()).replace(day=1)
        if key in out:
            out[key]["payouts"] += p.amount
    for m in out.values():
        m["fees_posted"] = m["month"] in posted_months
    return sorted(out.values(), key=lambda m: m["month"], reverse=True)


def post(*, by, column="cost_deductible", activity="", until=None, include_fees=False):
    """Ujmuje w ewidencji zakończone dni (przychód) i — gdy include_fees — zakończone miesiące prowizji, jeszcze nieujęte.
    Prowizje domyślnie pomija: dowodem kosztu są faktury platformy w KSeF. Zwraca (dni, miesiące)."""
    from . import services
    from .models import AccountingSettings, AccountingYear

    if column not in ("cost_deductible", "cost_other"):
        raise ValidationError("Wybierz kolumnę dla prowizji (6 albo 7).")
    today = until or timezone.localdate()
    settings = AccountingSettings.load()
    activity = activity or settings.auto_activity
    closed_years = set(AccountingYear.objects.filter(closed_at__isnull=False).values_list("year", flat=True))
    totals = day_totals(day_to=today - timedelta(days=1))
    done_days = set(StripePosting.objects.filter(kind=StripePosting.Kind.REVENUE, record__removed_at__isnull=True)
                    .values_list("period_start", flat=True))
    days = months_done = 0
    with transaction.atomic():
        for day, t in sorted(totals.items()):
            if day in done_days or day.year in closed_years or not t["revenue"]:
                continue
            record = services._create(
                day=day, by=by, kind=Record.Kind.REVENUE_AUTO, document=f"Stripe {day:%d.%m.%Y}",
                description="Dochody z majątku stowarzyszenia — udostępnianie strzelnicy (płatności online, Stripe)",
                counterparty="Klienci — płatności online (Stripe)", category="stripe", activity=activity,
                notes=f"Dzienne zestawienie wpłat online: {t['count']} płatności, brutto {t['gross'] / 100:.2f} zł"
                      + (f", zwroty {-t['refunds'] / 100:.2f} zł" if t["refunds"] else ""),
                revenue=t["revenue"])
            StripePosting.objects.create(kind=StripePosting.Kind.REVENUE, period_start=day, amount=t["revenue"], record=record)
            days += 1
        if include_fees:
            months_done = _post_fees(by=by, column=column, activity=activity, today=today, closed_years=closed_years)
    if days or months_done:
        log_event("ksiegowosc.stripe_ujecie", None, f"Stripe w ewidencji: dni {days}, miesiące prowizji {months_done}", actor=by)
    return days, months_done


def _post_fees(*, by, column, activity, today, closed_years):
    """Prowizje zakończonych miesięcy jako koszt (tylko gdy nie ma faktur prowizyjnych w KSeF)."""
    from . import services

    done = set(StripePosting.objects.filter(kind=StripePosting.Kind.FEES, record__removed_at__isnull=True)
               .values_list("period_start", flat=True))
    by_month = {}
    for day, t in day_totals(day_to=today.replace(day=1) - timedelta(days=1)).items():
        by_month[day.replace(day=1)] = by_month.get(day.replace(day=1), 0) + t["fee_cost"]
    count = 0
    for month, cost in sorted(by_month.items()):
        if month in done or month.year in closed_years or cost <= 0:
            continue
        last = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        record = services.add_record(
            kind=Record.Kind.COST, day=last, document=f"Stripe prowizje {month:%m/%Y}",
            description="Prowizje za obsługę płatności online (Stripe / platforma GunTracker)", amount=cost, column=column,
            by=by, counterparty="Stripe Technology Europe Ltd", activity=activity,
            notes="Suma opłat z historii salda Stripe pomniejszona o zwroty opłat")
        StripePosting.objects.create(kind=StripePosting.Kind.FEES, period_start=month, amount=cost, record=record)
        count += 1
    return count


def statement_pdf(month, association):
    """Zestawienie wpłat online za miesiąc: dzień po dniu z listą płatności (identyfikatory Stripe) — dowód do zapisów."""
    from pathlib import Path
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
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
    head = ParagraphStyle("h", parent=base, fontName="DejaVu-Bold", fontSize=12, leading=16)
    last = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    totals = day_totals(month, last)
    postings = {p.period_start: p.record for p in StripePosting.objects.filter(kind=StripePosting.Kind.REVENUE, period_start__gte=month,
                                                                             period_start__lte=last, record__removed_at__isnull=True)
                .select_related("record")}

    def pl(g):
        return f"{g / 100:,.2f}".replace(",", " ").replace(".", ",")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"Zestawienie wpłat online {month:%m/%Y}")
    story = [Paragraph(f"Zestawienie wpłat online (Stripe) — {month:%m.%Y}", head),
             Paragraph(escape(f"{association.name} · {association.address}"), base), Spacer(1, 4 * mm)]
    rows = [["Dzień", "Płatności", "Wpłaty brutto", "Zwroty", "Przychód dnia", "Opłaty", "Zapis w ewidencji"]]
    for day, t in sorted(totals.items()):
        rec = postings.get(day)
        rows.append([day.strftime("%d.%m.%Y"), str(t["count"]), pl(t["gross"]), pl(t["refunds"]), pl(t["revenue"]), pl(t["fee_cost"]),
                     f"Lp. {rec.lp}" if rec else "—"])
    rows.append(["Razem", str(sum(t["count"] for t in totals.values())), pl(sum(t["gross"] for t in totals.values())),
                 pl(sum(t["refunds"] for t in totals.values())), pl(sum(t["revenue"] for t in totals.values())),
                 pl(sum(t["fee_cost"] for t in totals.values())), ""])
    t = Table(rows, repeatRows=1)
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "DejaVu", 7.5), ("FONT", (0, 0), (-1, 0), "DejaVu-Bold", 7.5),
                           ("FONT", (0, -1), (-1, -1), "DejaVu-Bold", 7.5), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                           ("ALIGN", (1, 1), (-2, -1), "RIGHT"), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee"))]))
    story += [t, Spacer(1, 5 * mm), Paragraph("Wykaz operacji (identyfikatory transakcji Stripe):", ParagraphStyle("x", parent=base,
                                                                                                                 fontName="DejaVu-Bold"))]
    ops = [["Data i godzina", "Typ", "Identyfikator Stripe", "Kwota", "Opłata"]]
    names = {"payment": "płatność", "charge": "płatność", "payment_refund": "zwrot", "refund": "zwrot", "adjustment": "korekta opłaty"}
    for tx in StripeTransaction.objects.filter(day__gte=month, day__lte=last).order_by("created_at"):
        ops.append([timezone.localtime(tx.created_at, WARSAW).strftime("%d.%m.%Y %H:%M"), names.get(tx.type, tx.type), tx.stripe_id,
                    pl(tx.amount), pl(tx.fee)])
    ot = Table(ops, repeatRows=1, colWidths=[30 * mm, 24 * mm, 70 * mm, 25 * mm, 20 * mm])
    ot.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "DejaVu", 7), ("FONT", (0, 0), (-1, 0), "DejaVu-Bold", 7),
                            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#bbbbbb")), ("ALIGN", (3, 1), (-1, -1), "RIGHT")]))
    story += [Spacer(1, 2 * mm), ot, Spacer(1, 4 * mm),
              Paragraph("Dowód zbiorczy przychodów z płatności online za dany dzień (§ 13 rozporządzenia, Dz. U. 2025 poz. 1880). "
                        f"Źródło: eksport historii salda Stripe. Sporządzono: {timezone.localtime():%d.%m.%Y %H:%M}.", base)]
    doc.build(story)
    return buf.getvalue()
