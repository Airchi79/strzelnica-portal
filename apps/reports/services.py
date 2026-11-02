"""Generowanie numerowanych raportów sprzedaży i ich eksport (CSV, PDF)."""

import calendar
import csv
import io
from datetime import date, datetime, time, timedelta

from xml.sax.saxutils import escape

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import log_event

from .models import ReportSequence, SalesReport

CATEGORY_LABELS = {
    "rezerwacja": "Rezerwacje",
    "skladka": "Składki i wpisowe",
    "kod_wejscia": "Kody wejścia (karnety, abonamenty)",
    "inne": "Inne wpłaty",
    "zwrot": "Zwroty do wpłacających",
}
METHOD_LABELS = {
    "tpay": "Online (Tpay)", "przelew": "Przelew", "gotowka": "Gotówka", "blik": "BLIK na telefon", "karta": "Karta",
}


def _bounds(start, end):
    return (timezone.make_aware(datetime.combine(start, time.min)),
            timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min)))


def month_bounds(year, month):
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def collect(start, end):
    """Zbiera operacje z okresu: zaksięgowane wpłaty, sprzedaż kodów wejścia, zwroty do wpłacających."""
    from apps.clients.models import ClientPass
    from apps.payments.models import LedgerEntry, Payment

    lo, hi = _bounds(start, end)
    rows = []
    payments = Payment.objects.filter(status=Payment.Status.COMPLETED, completed_at__gte=lo, completed_at__lt=hi).select_related(
        "user", "reservation", "fee"
    )
    for p in payments.order_by("completed_at", "id"):
        category = "rezerwacja" if p.reservation_id else "skladka" if p.fee_id else "inne"
        doc = p.reservation.number if p.reservation_id else (p.fee.label if p.fee_id else (p.title or ""))
        rows.append({
            "at": timezone.localtime(p.completed_at).isoformat(timespec="minutes"), "category": category, "method": p.method,
            "doc": doc[:80], "client": (p.user.full_name if p.user else p.payer_name)[:80], "amount": p.amount, "ref": str(p.pk),
        })
    passes = ClientPass.objects.filter(paid_at__gte=lo, paid_at__lt=hi, price__gt=0).exclude(paid_with__in=["", "bezplatny"])
    for cp in passes.select_related("user").order_by("paid_at", "id"):
        rows.append({
            "at": timezone.localtime(cp.paid_at).isoformat(timespec="minutes"), "category": "kod_wejscia", "method": cp.paid_with,
            "doc": f"{cp.code} · {cp.get_kind_display()}", "client": cp.user.full_name[:80], "amount": cp.price, "ref": f"kod:{cp.pk}",
        })
    for le in LedgerEntry.objects.filter(kind=LedgerEntry.Kind.CHARGEBACK, created_at__gte=lo, created_at__lt=hi).select_related("user"):
        rows.append({
            "at": timezone.localtime(le.created_at).isoformat(timespec="minutes"), "category": "zwrot", "method": "zwrot",
            "doc": le.description[:80], "client": le.user.full_name[:80], "amount": le.amount, "ref": f"saldo:{le.pk}",
        })
    rows.sort(key=lambda r: (r["at"], r["ref"]))
    return rows


def summarize(rows):
    by_category, by_method = {}, {}
    inflow = refunds = 0
    for r in rows:
        by_category[r["category"]] = by_category.get(r["category"], 0) + r["amount"]
        if r["category"] == "zwrot":
            refunds += r["amount"]
        else:
            inflow += r["amount"]
            by_method[r["method"]] = by_method.get(r["method"], 0) + r["amount"]
    return {"count": len(rows), "inflow": inflow, "refunds": refunds, "net": inflow + refunds,
            "by_category": by_category, "by_method": by_method}


def _next_number(kind, year):
    ReportSequence.objects.bulk_create([ReportSequence(kind=kind, year=year)], ignore_conflicts=True)
    seq = ReportSequence.objects.select_for_update().get(kind=kind, year=year)
    seq.last += 1
    seq.save(update_fields=["last"])
    return f"RS/{kind}/{year}/{seq.last:04d}"


def generate(kind, period_start, by=None, now=None):
    """Tworzy raport za zamknięty okres (idempotentnie — istniejący raport jest zwracany bez zmian)."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    if kind == SalesReport.Kind.DAILY:
        start = end = period_start
    elif kind == SalesReport.Kind.MONTHLY:
        start, end = month_bounds(period_start.year, period_start.month)
    else:
        raise ValidationError("Nieznany rodzaj raportu.")
    if end >= today:
        raise ValidationError("Raport można wygenerować dopiero po zakończeniu okresu.")
    existing = SalesReport.objects.filter(kind=kind, period_start=start).first()
    if existing:
        return existing, False
    rows = collect(start, end)
    data = {"rows": rows, "summary": summarize(rows), "generated": timezone.localtime(now).isoformat(timespec="seconds")}
    if kind == SalesReport.Kind.MONTHLY:
        # Najpierw brakujące raporty dzienne z tego miesiąca, by miesięczny wskazywał komplet.
        day = start
        while day <= end:
            if not SalesReport.objects.filter(kind=SalesReport.Kind.DAILY, period_start=day).exists() and collect(day, day):
                generate(SalesReport.Kind.DAILY, day, by=by, now=now)
            day += timedelta(days=1)
        data["daily"] = [
            {"number": r.number, "day": r.period_start.isoformat(), "net": r.data["summary"]["net"]}
            for r in SalesReport.objects.filter(kind=SalesReport.Kind.DAILY, period_start__gte=start, period_start__lte=end).order_by("period_start")
        ]
    try:
        with transaction.atomic():
            report = SalesReport.objects.create(
                kind=kind, number=_next_number(kind, start.year), period_start=start, period_end=end, data=data,
                sha256=SalesReport.hash_data(data), generated_by=by,
            )
    except IntegrityError:  # równoległe wygenerowanie tego samego okresu
        return SalesReport.objects.get(kind=kind, period_start=start), False
    log_event("raport.sprzedaz", report, f"{report.number}: {start}–{end}, netto {data['summary']['net'] / 100:.2f} zł", actor=by)
    return report, True


def first_activity_day():
    from apps.clients.models import ClientPass
    from apps.payments.models import Payment

    candidates = [
        Payment.objects.filter(status=Payment.Status.COMPLETED).order_by("completed_at").values_list("completed_at", flat=True).first(),
        ClientPass.objects.exclude(paid_at__isnull=True).order_by("paid_at").values_list("paid_at", flat=True).first(),
    ]
    candidates = [c for c in candidates if c]
    return timezone.localdate(min(candidates)) if candidates else None


def generate_missing(now=None):
    """Dzienne raporty za dni z operacjami i miesięczne za zakończone miesiące — od pierwszej operacji w systemie."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    first = first_activity_day()
    created = []
    if first is None:
        return created
    day = first
    while day < today:
        if not SalesReport.objects.filter(kind=SalesReport.Kind.DAILY, period_start=day).exists() and collect(day, day):
            created.append(generate(SalesReport.Kind.DAILY, day, now=now)[0])
        day += timedelta(days=1)
    month = date(first.year, first.month, 1)
    while month_bounds(month.year, month.month)[1] < today:
        report, new = generate(SalesReport.Kind.MONTHLY, month, now=now)
        if new:
            created.append(report)
        month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return created


# --- eksport --------------------------------------------------------------------------


def _pln(grosze):
    return f"{grosze / 100:.2f}".replace(".", ",")


def _safe(v):
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v


def to_csv(report):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Raport", report.number, "Okres", report.period_start.isoformat(), report.period_end.isoformat()])
    w.writerow(["Data i godzina", "Kategoria", "Sposób", "Dokument", "Klient", "Kwota [zł]"])
    for r in report.data["rows"]:
        w.writerow([r["at"], CATEGORY_LABELS.get(r["category"], r["category"]), METHOD_LABELS.get(r["method"], r["method"]),
                    _safe(r["doc"]), _safe(r["client"]), _pln(r["amount"])])
    s = report.data["summary"]
    w.writerow([])
    w.writerow(["Wpływy", _pln(s["inflow"])])
    w.writerow(["Zwroty", _pln(s["refunds"])])
    w.writerow(["Razem netto", _pln(s["net"])])
    w.writerow(["Suma kontrolna SHA-256", report.sha256])
    return "﻿" + buf.getvalue()  # BOM — poprawne polskie znaki w Excelu


def to_pdf(report, association):
    from pathlib import Path

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    fonts = Path(__file__).parent / "fonts"
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", str(fonts / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(fonts / "DejaVuSans-Bold.ttf")))
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=8.5, leading=11)
    head = ParagraphStyle("h", parent=base, fontName="DejaVu-Bold", fontSize=13, leading=17)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Raport sprzedaży {report.number}", author=association.name or "Strzelnica Sątok")
    s = report.data["summary"]
    period = report.period_start.strftime("%d.%m.%Y") + ("" if report.period_start == report.period_end else "–" + report.period_end.strftime("%d.%m.%Y"))
    story = [
        Paragraph(f"Raport sprzedaży {report.get_kind_display().lower()} nr {report.number}", head),
        Paragraph(escape(f"{association.name}{', ' + association.address if association.address else ''}"
                         f"{' · NIP ' + association.nip if association.nip else ''}"), base),
        Paragraph(f"Okres: {period} · wygenerowano: {timezone.localtime(report.generated_at):%d.%m.%Y %H:%M}", base),
        Spacer(1, 6 * mm),
    ]

    def table(data, widths, bold_last=False):
        t = Table(data, colWidths=widths, repeatRows=1)
        style = [("FONT", (0, 0), (-1, -1), "DejaVu", 8.5), ("FONT", (0, 0), (-1, 0), "DejaVu-Bold", 8.5),
                 ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8efe9")), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                 ("ALIGN", (-1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP")]
        if bold_last:
            style.append(("FONT", (0, -1), (-1, -1), "DejaVu-Bold", 8.5))
        t.setStyle(TableStyle(style))
        return t

    summary = [["Podsumowanie", "Kwota [zł]"]]
    summary += [[CATEGORY_LABELS.get(k, k), _pln(v)] for k, v in sorted(s["by_category"].items())]
    summary += [["Razem netto (wpływy − zwroty)", _pln(s["net"])]]
    story += [table(summary, [120 * mm, 40 * mm], bold_last=True), Spacer(1, 4 * mm)]
    methods = [["Wpływy według sposobu zapłaty", "Kwota [zł]"]] + [[METHOD_LABELS.get(k, k), _pln(v)] for k, v in sorted(s["by_method"].items())]
    if len(methods) > 1:
        story += [table(methods, [120 * mm, 40 * mm]), Spacer(1, 6 * mm)]
    if report.data.get("daily"):
        daily = [["Raport dzienny", "Dzień", "Netto [zł]"]] + [[d["number"], d["day"], _pln(d["net"])] for d in report.data["daily"]]
        story += [table(daily, [60 * mm, 50 * mm, 50 * mm]), Spacer(1, 6 * mm)]
    rows = [["Data", "Kategoria", "Sposób", "Dokument", "Klient", "Kwota [zł]"]]
    for r in report.data["rows"]:
        day_part, time_part = r["at"][:10], r["at"][11:16]
        rows.append([Paragraph(f"{day_part[8:10]}.{day_part[5:7]}.{day_part[:4]}<br/>{time_part}", base), Paragraph(escape(CATEGORY_LABELS.get(r["category"], r["category"])), base),
                     METHOD_LABELS.get(r["method"], r["method"]), Paragraph(escape(r["doc"]), base), Paragraph(escape(r["client"]), base), _pln(r["amount"])])
    if len(rows) == 1:
        rows.append(["—", "Brak operacji w okresie", "", "", "", "0,00"])
    story += [table(rows, [22 * mm, 32 * mm, 24 * mm, 44 * mm, 36 * mm, 24 * mm]), Spacer(1, 6 * mm),
              Paragraph(f"Liczba operacji: {s['count']}. Suma kontrolna danych raportu (SHA-256): {report.sha256}. "
                        "Raport jest niezmienny — korekty ujmuje się w raporcie za okres, w którym je zaksięgowano.", base)]
    doc.build(story)
    return buf.getvalue()
