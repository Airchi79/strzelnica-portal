"""Ewidencja przychodów i kosztów: numeracja zapisów, zasilanie przychodami z dziennych raportów wpływów, zapisy ręczne,
zmiana treści i usuwanie zapisów z historią wersji, zamknięcie roku i sumy wg wzoru z załącznika nr 1 do rozporządzenia
(Dz. U. 2025 poz. 1880)."""

import calendar
import hashlib
from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit.services import log_event

from .models import CATEGORY_ORDER, AccountingSettings, AccountingYear, Record, RecordSequence, RecordVersion

COLUMNS = ("revenue", "cost_deductible", "cost_other")
# rodzaje zapisów, w których kwota może być ujemna: faktura korygująca z KSeF, przychody dnia pomniejszone o zwroty, storno
SIGNED_KINDS = (Record.Kind.KSEF, Record.Kind.REVENUE_AUTO, Record.Kind.CORRECTION)
COLUMN_LABELS = {"revenue": "kol. 5 — przychody", "cost_deductible": "kol. 6 — koszty uzyskania przychodów",
                 "cost_other": "kol. 7 — koszty niestanowiące kosztów uzyskania przychodów"}


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


def check_open(year):
    if AccountingYear.is_closed(year):
        raise ValidationError(f"Rok {year} jest zamknięty — zapisów z tego roku nie można dodawać ani zmieniać.")


def _sha256(f):
    digest = hashlib.sha256()
    for chunk in f.chunks():
        digest.update(chunk)
    f.seek(0)
    return digest.hexdigest()


def _version(record, action, by, reason=""):
    RecordVersion.objects.create(record=record, version=record.version, action=action, data=record.snapshot(), reason=reason[:255], by=by)


def _create(*, day, by, **fields):
    check_open(day.year)
    with transaction.atomic():
        record = Record(year=day.year, number=_next_number(day.year), date=day, created_by=by, **fields)
        if fields.get("attachment"):
            record.attachment_sha256 = _sha256(fields["attachment"])
        record.save()
        _version(record, RecordVersion.Action.CREATE, by)
    return record


def _amounts(kind, amount, column):
    if kind in (Record.Kind.REVENUE, Record.Kind.REVENUE_AUTO):
        expected = ("revenue",)
    elif kind in (Record.Kind.KSEF, Record.Kind.CORRECTION):
        expected = COLUMNS
    else:
        expected = ("cost_deductible", "cost_other")
    if column not in expected:
        raise ValidationError("Wybierz właściwą kolumnę zestawienia.")
    return {c: (amount if c == column else 0) for c in COLUMNS}


def add_record(*, kind, day, document, description, amount, column, by, counterparty="", notes="", attachment=None,
               ksef_number="", activity="", task=None):
    """Zapis ręczny: przychód (kol. 5) albo koszt (kol. 6 — koszt uzyskania przychodów, kol. 7 — pozostałe koszty).
    `amount` w groszach; dodatni (ujemny tylko dla faktury korygującej z KSeF)."""
    if kind not in (Record.Kind.REVENUE, Record.Kind.COST, Record.Kind.KSEF):
        raise ValidationError("Nieznany rodzaj zapisu.")
    if amount == 0 or (amount < 0 and kind != Record.Kind.KSEF):
        raise ValidationError("Kwota musi być większa od zera.")
    amounts = _amounts(kind, amount, column)
    _check_date(day)
    record = _create(day=day, by=by, kind=kind, document=document.strip(), description=description.strip(),
                     counterparty=counterparty.strip(), notes=notes.strip(), attachment=attachment or "",
                     ksef_number=ksef_number.strip(), activity=_activity(activity), task=task, **amounts)
    log_event("ksiegowosc.zapis", record, f"Lp. {record.lp}: {record.get_kind_display().lower()} {amount / 100:.2f} zł — "
                                          f"{record.description}", actor=by)
    return record


def record_column(record):
    for c in COLUMNS:
        if getattr(record, c):
            return c
    return "revenue" if record.kind in (Record.Kind.REVENUE, Record.Kind.REVENUE_AUTO) else "cost_deductible"


def record_amount(record):
    return getattr(record, record_column(record))


KEEP = object()


def _activity(value):
    from .models import Activity

    if value and value not in Activity.values:
        raise ValidationError("Nieznana forma działalności.")
    return value or ""


def update_record(record, *, by, reason, day, document, description, amount, column, counterparty="", notes="",
                  ksef_number="", attachment=None, activity=KEEP, task=KEEP):
    """Wpisanie nowej treści zapisu (§ 7 ust. 2 pkt 1). Poprzednia treść zostaje w historii wersji; numer (Lp.) bez zmian."""
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError("Podaj powód zmiany.")
    if record.is_removed:
        raise ValidationError("Zapis jest usunięty — najpierw go przywróć.")
    check_open(record.year)
    _check_date(day)
    if day.year != record.year:
        raise ValidationError(f"Data musi pozostać w roku {record.year} (numeracja zapisów jest roczna). "
                              "Zapis z innego roku usuń i wprowadź od nowa.")
    if amount == 0 or (amount < 0 and record.kind not in SIGNED_KINDS):
        raise ValidationError("Kwota musi być większa od zera.")
    amounts = _amounts(record.kind, amount, column)
    original = record
    with transaction.atomic():
        record = Record.objects.select_for_update().get(pk=record.pk)
        if record.is_removed:
            raise ValidationError("Zapis jest usunięty — najpierw go przywróć.")
        before = record.snapshot()
        record.date, record.document, record.description = day, document.strip(), description.strip()
        record.counterparty, record.notes, record.ksef_number = counterparty.strip(), notes.strip(), ksef_number.strip()
        if activity is not KEEP:
            record.activity = _activity(activity)
        if task is not KEEP:
            record.task = task
        for c, v in amounts.items():
            setattr(record, c, v)
        if attachment:
            record.attachment = attachment
            record.attachment_sha256 = _sha256(attachment)
        if record.snapshot() == before:
            raise ValidationError("Nic się nie zmieniło.")
        record.version += 1
        record.updated_at, record.updated_by = timezone.now(), by
        record.save()
        _version(record, RecordVersion.Action.UPDATE, by, reason)
    changed = [f for f in Record.CONTENT_FIELDS if before[f] != record.snapshot()[f]]
    log_event("ksiegowosc.zmiana", record, f"Lp. {record.lp}, zmiana: {', '.join(changed)} — {reason}", actor=by)
    original.refresh_from_db()
    return original


def remove_record(record, *, by, reason):
    """Usunięcie z ewidencji: zapis zachowuje numer i treść w historii, nie wchodzi do sum, na wydruku jest oznaczony."""
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError("Podaj przyczynę usunięcia.")
    if record.is_removed:
        raise ValidationError("Zapis jest już usunięty.")
    check_open(record.year)
    original = record
    with transaction.atomic():
        record = Record.objects.select_for_update().get(pk=record.pk)
        if record.is_removed:  # równoległe usunięcie
            raise ValidationError("Zapis jest już usunięty.")
        record.removed_at, record.removed_by, record.removed_reason = timezone.now(), by, reason[:255]
        record.version += 1
        record.save()
        _version(record, RecordVersion.Action.REMOVE, by, reason)
        # operacja z wyciągu wraca do decyzji (powiązanie zostaje — przywrócenie zapisu przywróci też „zaksięgowana”)
        record.bank_operations.update(status="nowa", status_note=f"zapis usunięty z ewidencji: {reason}"[:255])
        for invoice in record.ksef_invoices.all():  # faktura wraca do decyzji
            invoice.status, invoice.record, invoice.status_note = invoice.Status.NEW, None, f"usunięta z ewidencji: {reason}"[:255]
            invoice.save(update_fields=["status", "record", "status_note"])
    log_event("ksiegowosc.usuniecie", record, f"Lp. {record.lp} usunięty z ewidencji — {reason}", actor=by)
    original.refresh_from_db()
    return original


def restore_record(record, *, by, reason=""):
    if not record.is_removed:
        raise ValidationError("Zapis nie jest usunięty.")
    check_open(record.year)
    if record.ksef_number and Record.objects.filter(ksef_number=record.ksef_number, removed_at__isnull=True).exclude(pk=record.pk).exists():
        raise ValidationError("Ta faktura KSeF jest już w ewidencji pod innym numerem.")
    original = record
    with transaction.atomic():
        record = Record.objects.select_for_update().get(pk=record.pk)
        if not record.is_removed:
            raise ValidationError("Zapis nie jest usunięty.")
        record.removed_at, record.removed_by, record.removed_reason = None, None, ""
        record.version += 1
        record.save()
        _version(record, RecordVersion.Action.RESTORE, by, reason or "przywrócenie zapisu")
        record.bank_operations.filter(status="nowa").update(status="zaksiegowana", status_note="")
        if record.ksef_number:
            from .models import KsefInvoice

            KsefInvoice.objects.filter(ksef_number=record.ksef_number, record__isnull=True).update(
                status=KsefInvoice.Status.ACCEPTED, record=record, status_note="")
    log_event("ksiegowosc.przywrocenie", record, f"Lp. {record.lp} przywrócony do ewidencji", actor=by)
    original.refresh_from_db()
    return original


def history(record):
    """Wersje zapisu z listą zmienionych pól względem poprzedniej wersji."""
    rows, prev = [], None
    for v in record.versions.select_related("by").order_by("pk"):
        changes = []
        if prev is not None and v.action == RecordVersion.Action.UPDATE:
            changes = [(f, prev.get(f), v.data.get(f)) for f in Record.CONTENT_FIELDS if prev.get(f) != v.data.get(f)]
        rows.append({"v": v, "changes": changes})
        prev = v.data
    return rows


def close_year(year, *, by):
    obj = AccountingYear.get(year)
    if obj.closed_at:
        raise ValidationError(f"Rok {year} jest już zamknięty.")
    if year >= timezone.localdate().year:
        raise ValidationError("Zamknąć można tylko zakończony rok podatkowy.")
    obj.closed_at, obj.closed_by = timezone.now(), by
    obj.save(update_fields=["closed_at", "closed_by"])
    log_event("ksiegowosc.zamkniecie_roku", None, f"Zamknięto rok {year} w ewidencji", actor=by)
    return obj


def reopen_year(year, *, by, reason):
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError("Podaj powód otwarcia roku.")
    obj = AccountingYear.get(year)
    if not obj.closed_at:
        raise ValidationError(f"Rok {year} nie jest zamknięty.")
    obj.closed_at, obj.closed_by = None, None
    obj.save(update_fields=["closed_at", "closed_by"])
    log_event("ksiegowosc.otwarcie_roku", None, f"Otwarto ponownie rok {year}: {reason}", actor=by)
    return obj


def sync_from_sales_reports(now=None):
    """Przychody z dziennych raportów wpływów (od dnia ustawionego w ustawieniach): jeden zapis na kategorię dnia,
    numer dowodu = numer raportu (dzienne zestawienie sprzedaży — § 8 pkt 1 i § 13 rozporządzenia).
    Idempotentne — raport i kategoria trafiają do ewidencji raz (także gdy zapis później usunięto)."""
    from apps.reports.models import SalesReport
    from apps.reports.services import generate_missing

    settings = AccountingSettings.load()
    if not settings.auto_from:
        return []
    generate_missing(now=now)
    created = []
    closed = set(AccountingYear.objects.filter(closed_at__isnull=False).values_list("year", flat=True))
    reports = (SalesReport.objects.filter(kind=SalesReport.Kind.DAILY, period_start__gte=settings.auto_from)
               .exclude(accounting_records__isnull=False).order_by("period_start", "pk"))
    for report in reports:
        if report.period_start.year in closed:
            continue
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
                        notes=f"Dowód zbiorczy — liczba operacji: {counts.get(category, 0)}", category=category,
                        sales_report=report, revenue=amount, activity=settings.auto_activity)
            except IntegrityError:  # równoległe uzupełnianie
                continue
            created.append(record)
    if created:
        log_event("ksiegowosc.przychody_z_systemu", None,
                  f"Dopisano {len(created)} zapisów przychodów z raportów wpływów (Lp. {created[0].lp} – {created[-1].lp})")
    return created


# --- zestawienia ----------------------------------------------------------------------


def period_bounds(year, month=None):
    if month:
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    return date(year, 1, 1), date(year, 12, 31)


def _sums(qs):
    agg = qs.aggregate(**{c: Sum(c) for c in COLUMNS})
    return {c: agg[c] or 0 for c in COLUMNS}


def statement(year, month=None):
    """Zestawienie za miesiąc (albo cały rok): zapisy w kolejności Lp. (także usunięte — oznaczone, bez kwot w sumach),
    „Razem w okresie sprawozdawczym” i „Razem od początku roku” (narastająco do końca okresu)."""
    start, end = period_bounds(year, month)
    in_year = Record.objects.filter(year=year)
    records = list(in_year.filter(date__gte=start, date__lte=end).select_related("created_by", "removed_by")
                   .order_by("number"))
    period = {c: sum(getattr(r, c) for r in records if not r.is_removed) for c in COLUMNS}
    ytd = _sums(in_year.filter(date__lte=end, removed_at__isnull=True))
    return {"year": year, "month": month, "start": start, "end": end, "records": records, "period": period, "ytd": ytd,
            "closed": AccountingYear.is_closed(year)}


def year_totals(year):
    return _sums(Record.objects.filter(year=year, removed_at__isnull=True))


def flows(year):
    """Zestawienie przepływów finansowych (załącznik nr 2): kol. 1–2 z ewidencji, kol. 3 = 1 − 2, kol. 4–6 wpisuje
    zarząd, kol. 5 domyślnie = kol. 7 z roku poprzedniego, kol. 7 = 4 + 5 − 6."""
    totals = year_totals(year)
    obj = AccountingYear.get(year)
    prev = AccountingYear.objects.filter(year=year - 1).first()
    prev_col7 = flows(year - 1)["col7"] if prev and prev.free_income is not None else None
    col4 = obj.free_income
    col5 = obj.free_prior if obj.free_prior is not None else prev_col7
    col6 = obj.free_spent
    col7 = (col4 or 0) + (col5 or 0) - (col6 or 0) if col4 is not None or col5 is not None or col6 is not None else None
    return {"year": year, "col1": totals["revenue"], "col2": totals["cost_deductible"],
            "col3": totals["revenue"] - totals["cost_deductible"], "col4": col4, "col5": col5, "col6": col6, "col7": col7,
            "col5_from_prev": obj.free_prior is None and prev_col7 is not None, "cost_other": totals["cost_other"]}


def years():
    found = set(Record.objects.values_list("year", flat=True).distinct())
    found.add(timezone.localdate().year)
    return sorted(found, reverse=True)


def classify(records, *, by, activity=KEEP, task=KEEP):
    """Przypisanie formy działalności / umowy wielu zapisom naraz (nowa wersja każdego zapisu z powodem)."""
    changed = 0
    for record in records:
        if record.is_removed or AccountingYear.is_closed(record.year):
            continue
        if (activity is KEEP or record.activity == activity) and (task is KEEP or record.task_id == (task.pk if task else None)):
            continue
        update_record(record, by=by, reason="przypisanie do zestawienia uzupełniającego", day=record.date, document=record.document,
                      description=record.description, amount=record_amount(record), column=record_column(record),
                      counterparty=record.counterparty, notes=record.notes, ksef_number=record.ksef_number,
                      activity=activity, task=task)
        changed += 1
    return changed
