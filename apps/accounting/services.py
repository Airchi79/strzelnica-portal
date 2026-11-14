"""Ewidencja przychodów i kosztów: numeracja zapisów, zasilanie przychodami z dziennych raportów wpływów, zapisy ręczne,
korekty (storno) i sumy wg wzoru z załącznika nr 1 do rozporządzenia (Dz. U. 2025 poz. 1880)."""

import calendar
import hashlib
from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit.services import log_event

from .models import CATEGORY_ORDER, AccountingSettings, Record, RecordSequence

COLUMNS = ("revenue", "cost_deductible", "cost_other")


def _next_number(year):
    RecordSequence.objects.bulk_create([RecordSequence(year=year)], ignore_conflicts=True)
    seq = RecordSequence.objects.select_for_update().get(year=year)
    seq.last += 1
    seq.save(update_fields=["last"])
    return seq.last


def _check_date(day, today=None):
    today = today or timezone.localdate()
    if day > today:
        raise ValidationError("Data zdarzenia nie może być z przyszłości.")
    if day.year < 2000:
        raise ValidationError("Niepoprawna data zdarzenia.")


def _create(*, day, by, **fields):
    with transaction.atomic():
        record = Record(year=day.year, number=_next_number(day.year), date=day, created_by=by, **fields)
        attachment = fields.get("attachment")
        if attachment:
            digest = hashlib.sha256()
            for chunk in attachment.chunks():
                digest.update(chunk)
            attachment.seek(0)
            record.attachment_sha256 = digest.hexdigest()
        record.save()
    return record


def add_record(*, kind, day, document, description, amount, column, by, counterparty="", notes="", attachment=None):
    """Zapis ręczny: przychód (kol. 5) albo koszt (kol. 6 — koszt uzyskania przychodów, kol. 7 — pozostałe koszty).
    `amount` w groszach, dodatni."""
    if kind not in (Record.Kind.REVENUE, Record.Kind.COST):
        raise ValidationError("Nieznany rodzaj zapisu.")
    if amount <= 0:
        raise ValidationError("Kwota musi być większa od zera.")
    expected = ("revenue",) if kind == Record.Kind.REVENUE else ("cost_deductible", "cost_other")
    if column not in expected:
        raise ValidationError("Wybierz właściwą kolumnę zestawienia.")
    _check_date(day)
    record = _create(day=day, by=by, kind=kind, document=document.strip(), description=description.strip(),
                     counterparty=counterparty.strip(), notes=notes.strip(), attachment=attachment or "", **{column: amount})
    log_event("ksiegowosc.zapis", record, f"Lp. {record.lp}: {record.get_kind_display().lower()} {amount / 100:.2f} zł — "
                                          f"{record.description}", actor=by)
    return record


def correct(record, *, reason, by, day=None):
    """Zapis korygujący (storno): nowy zapis z przeciwnymi kwotami, wskazujący korygowany zapis (§ 7 ust. 2 pkt 2).
    Korekty nie można skorygować; każdy zapis można skorygować tylko raz — poprawną treść wpisuje się nowym zapisem."""
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError("Podaj przyczynę korekty.")
    if record.kind == Record.Kind.CORRECTION:
        raise ValidationError("Zapisu korygującego nie koryguje się — w razie potrzeby wprowadź nowy zapis.")
    day = day or timezone.localdate()
    _check_date(day)
    if day < record.date:
        raise ValidationError("Data korekty nie może być wcześniejsza niż data korygowanego zapisu.")
    if day.year != record.year:
        raise ValidationError("Korekta musi mieć datę z roku korygowanego zapisu.")
    try:
        with transaction.atomic():
            fix = _create(
                day=day, by=by, kind=Record.Kind.CORRECTION, document=record.document, counterparty=record.counterparty,
                description=f"Korekta zapisu Lp. {record.lp}: {record.description}"[:255], notes=reason[:255],
                corrects=record, category=record.category, **{c: -getattr(record, c) for c in COLUMNS})
    except IntegrityError as e:
        raise ValidationError("Ten zapis został już skorygowany.") from e
    log_event("ksiegowosc.korekta", fix, f"Lp. {fix.lp} koryguje Lp. {record.lp}: {reason}", actor=by)
    return fix


def sync_from_sales_reports(now=None):
    """Przychody z dziennych raportów wpływów (od dnia ustawionego w ustawieniach): jeden zapis na kategorię dnia,
    numer dowodu = numer raportu (dowód zbiorczy z wykazem operacji — § 7 ust. 4 i § 13 rozporządzenia).
    Idempotentne — raport i kategoria trafiają do ewidencji raz."""
    from apps.reports.models import SalesReport
    from apps.reports.services import generate_missing

    settings = AccountingSettings.load()
    if not settings.auto_from:
        return []
    generate_missing(now=now)
    created = []
    reports = (SalesReport.objects.filter(kind=SalesReport.Kind.DAILY, period_start__gte=settings.auto_from)
               .exclude(accounting_records__isnull=False).order_by("period_start", "pk"))
    for report in reports:
        by_category = report.data["summary"]["by_category"]
        counts = {}
        for row in report.data["rows"]:
            counts[row["category"]] = counts.get(row["category"], 0) + 1
        for category in sorted(by_category, key=lambda c: (CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99, c)):
            amount = by_category[category]
            if not amount:
                continue
            try:
                with transaction.atomic():
                    record = _create(
                        day=report.period_start, by=None, kind=Record.Kind.REVENUE_AUTO, document=report.number,
                        description=settings.label(category)[:255], counterparty="Wpłaty strzelców — wykaz w raporcie",
                        notes=f"Dowód zbiorczy — liczba operacji: {counts.get(category, 0)}", category=category, sales_report=report,
                        revenue=amount)
            except IntegrityError:  # równoległe uzupełnianie
                continue
            created.append(record)
    if created:
        log_event("ksiegowosc.przychody_z_systemu", None,
                  f"Dopisano {len(created)} zapisów przychodów z raportów wpływów (Lp. {created[0].lp} – {created[-1].lp})")
    return created


# --- zestawienie ----------------------------------------------------------------------


def period_bounds(year, month=None):
    if month:
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    return date(year, 1, 1), date(year, 12, 31)


def _sums(qs):
    agg = qs.aggregate(**{c: Sum(c) for c in COLUMNS})
    return {c: agg[c] or 0 for c in COLUMNS}


def statement(year, month=None):
    """Zestawienie za miesiąc (albo cały rok): zapisy w kolejności Lp., „Razem w okresie sprawozdawczym”
    i „Razem od początku roku” (narastająco do końca okresu)."""
    start, end = period_bounds(year, month)
    in_year = Record.objects.filter(year=year)
    records = list(in_year.filter(date__gte=start, date__lte=end).select_related("corrects", "created_by")
                   .prefetch_related("corrections").order_by("number"))
    period = {c: sum(getattr(r, c) for r in records) for c in COLUMNS}
    ytd = _sums(in_year.filter(date__lte=end))
    return {"year": year, "month": month, "start": start, "end": end, "records": records, "period": period, "ytd": ytd}


def years():
    found = set(Record.objects.values_list("year", flat=True).distinct())
    found.add(timezone.localdate().year)
    return sorted(found, reverse=True)
