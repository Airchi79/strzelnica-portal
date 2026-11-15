"""Wyciąg bankowy w księgowości: import „Zestawienia operacji” z bankowości BNP Paribas (eksport XLSX), dopasowanie
do wpłat strzelców i faktur KSeF, księgowanie pozostałych operacji jako przychód albo koszt.

Format BNP (arkusz „ExcelExportFile”): Data transakcji, Data zaksięgowania, Data odrzucenia, Kwota (wpływ +, wydatek −),
Waluta, Nadawca, Odbiorca (rachunek, nazwa i adres w kolejnych wierszach komórki), Opis, Produkt (nazwa i rachunek),
Typ transakcji, Kwota zlecenia, Waluta zlecenia, Status, Saldo po transakcji.

Zasady (bez podwójnego ujęcia):
- wpływ, którego tytuł wskazuje rezerwację / doładowanie salda, rejestruje się jako wpłata strzelca (moduł Płatności);
  przychód trafia do ewidencji z dziennego raportu wpływów,
- wydatek równy należności z faktury KSeF to zapłata tej faktury; koszt ujmuje się z faktury,
- pozostałe operacje księguje zarząd (przychód / koszt z formą działalności) albo pomija z podaniem przyczyny."""

import hashlib
import io
import itertools
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_event

from .models import (PAYMENT_BANK, PAYMENT_METHODS, PAYMENT_PRIVATE, REIMBURSE_PENDING, REIMBURSE_TRANSFER, REIMBURSEMENTS,
                     BankOperation, KsefInvoice, Record)

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 20000
REQUIRED = ["Data zaksięgowania", "Kwota", "Opis", "Typ transakcji", "Nadawca", "Odbiorca"]
DONE_STATUSES = {"zrealizowana", "zaksięgowana", "wykonana", ""}
INVOICE_WINDOW_DAYS = 180
PREPAYMENT_DAYS = 30
MAX_REIMBURSE_CANDIDATES = 15


def _grosze(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int((Decimal(str(value)) * 100).quantize(Decimal(1)))
    text = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
    return int((Decimal(text) * 100).quantize(Decimal(1)))


def _aware(value):
    """Daty z banku są w czasie polskim, bez strefy — zapisujemy je ze strefą (Europe/Warsaw)."""
    if value is None or timezone.is_aware(value):
        return value
    return timezone.make_aware(value)


def _when(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _party(cell):
    """„rachunek\\nnazwa\\nadres…” → (rachunek, nazwa) — adres pomijamy (minimalizacja danych)."""
    lines = [line.strip().strip('"') for line in str(cell or "").splitlines() if line.strip()]
    account = ""
    if lines and re.fullmatch(r"[A-Z]{0,2}\d{10,32}", lines[0].replace(" ", "")):
        account = lines.pop(0).replace(" ", "")
    return account[:34], (lines[0] if lines else "")[:160]


def parse_xlsx(uploaded):
    from openpyxl import load_workbook

    if not (uploaded.name or "").lower().endswith(".xlsx"):
        raise ValidationError("Wybierz plik XLSX z bankowości („Zestawienie operacji” → eksport do Excela).")
    if uploaded.size > MAX_BYTES:
        raise ValidationError("Plik jest za duży.")
    try:
        wb = load_workbook(io.BytesIO(uploaded.read()), read_only=True, data_only=True)
    except Exception as e:  # uszkodzony / nie-XLSX — openpyxl zgłasza różne wyjątki
        raise ValidationError("To nie jest poprawny plik Excel (XLSX).") from e
    rows = wb.worksheets[0].iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows, [])]
    missing = [h for h in REQUIRED if h not in header]
    if missing:
        raise ValidationError(f"Brak kolumn: {', '.join(missing)}. Użyj eksportu „Zestawienie operacji” z bankowości BNP Paribas bez zmian.")
    out = []
    for n, values in enumerate(rows, start=2):
        if n > MAX_ROWS:
            raise ValidationError("Arkusz ma zbyt wiele wierszy.")
        row = dict(zip(header, values))
        if not any(v not in (None, "") for v in values):
            continue
        if row.get("Data odrzucenia") or str(row.get("Status") or "").strip().lower() not in DONE_STATUSES:
            continue  # operacja odrzucona / w toku — nie ma jej na rachunku
        booked = _when(row.get("Data zaksięgowania")) or _when(row.get("Data transakcji"))
        amount = _grosze(row.get("Kwota"))
        if booked is None or not amount:
            continue
        inflow = amount > 0
        account, name = _party(row.get("Nadawca") if inflow else row.get("Odbiorca"))
        product = [line.strip() for line in str(row.get("Produkt") or "").splitlines() if line.strip()]
        own = product[-1].replace(" ", "") if product and re.fullmatch(r"\d{10,32}", product[-1].replace(" ", "")) else ""
        out.append({
            "account": own[:34], "transaction_at": _aware(_when(row.get("Data transakcji"))), "booked_on": booked.date(), "amount": amount,
            "currency": str(row.get("Waluta") or "PLN")[:3], "counterparty_account": account, "counterparty": name,
            "title": " ".join(str(row.get("Opis") or "").split())[:255], "kind": str(row.get("Typ transakcji") or "")[:60],
            "balance_after": _grosze(row.get("Saldo po transakcji")),
        })
    return out


def _key(op):
    base = "|".join(str(op[k]) for k in ("account", "transaction_at", "booked_on", "amount", "counterparty_account", "title",
                                         "balance_after"))
    return hashlib.sha256(base.encode()).hexdigest()


def _norm(text):
    return re.sub(r"[^0-9A-Z]", "", (text or "").upper())


def _invoice_window(booked_on):
    """Faktura wystawiona do 180 dni przed zapłatą albo do 30 dni po niej (przedpłata przy zamówieniu)."""
    return booked_on - timedelta(days=INVOICE_WINDOW_DAYS), booked_on + timedelta(days=PREPAYMENT_DAYS)


def candidate_invoices(op, *, include_linked=False):
    """Faktury KSeF (zakup) z należnością równą wydatkowi, z okna dat; najpierw te z numerem faktury w tytule przelewu."""
    start, end = _invoice_window(op.booked_on)
    qs = KsefInvoice.objects.filter(direction=KsefInvoice.Direction.PURCHASE, gross=-op.amount, currency="PLN",
                                    issue_date__gte=start, issue_date__lte=end, payment_method=PAYMENT_BANK
                                    ).exclude(status=KsefInvoice.Status.REJECTED)
    if not include_linked:
        qs = qs.exclude(bank_operations__isnull=False)
    title = _norm(op.title)
    found = list(qs.order_by("issue_date", "pk"))
    return sorted(found, key=lambda i: (not (_norm(i.invoice_number) and _norm(i.invoice_number) in title),
                                        abs((i.issue_date - op.booked_on).days)))


def match_invoice(op):
    """Jednoznaczna faktura do zapłaty: po numerze w tytule albo jedyna o tej kwocie w oknie dat."""
    candidates = candidate_invoices(op)
    title = _norm(op.title)
    by_number = [i for i in candidates if _norm(i.invoice_number) and _norm(i.invoice_number) in title]
    if len(by_number) == 1:
        return by_number[0]
    return candidates[0] if len(candidates) == 1 else None


def match_pending_invoices():
    """Wydatki „Do decyzji” ↔ faktury KSeF — także po imporcie faktur, które przyszły później niż wyciąg."""
    matched = 0
    for op in BankOperation.objects.filter(status=BankOperation.Status.NEW, amount__lt=0, currency="PLN").order_by("booked_on", "pk"):
        invoice = match_invoice(op)
        if invoice:
            op.invoice, op.status, op.status_note = invoice, BankOperation.Status.INVOICE, ""
            op.save(update_fields=["invoice", "status", "status_note"])
            matched += 1
    return matched


def booked_payment_for(invoice):
    """Wydatek z wyciągu już zaksięgowany jako koszt (bez połączenia z fakturą), pasujący kwotą i datą do faktury —
    przyjęcie faktury podwoiłoby koszt."""
    if invoice.direction != KsefInvoice.Direction.PURCHASE:
        return None
    for op in (BankOperation.objects.filter(status=BankOperation.Status.BOOKED, amount=-invoice.gross, invoice__isnull=True,
                                            record__removed_at__isnull=True).select_related("record")):
        start, end = _invoice_window(op.booked_on)
        if start <= invoice.issue_date <= end:
            return op
    return None


def import_xlsx(uploaded, *, by):
    """Zwraca (nowe operacje, pominięte duplikaty, rozpoznane wpłaty strzelców, połączone faktury)."""
    from apps.payments import services as payments
    from apps.payments.models import Payment

    rows = parse_xlsx(uploaded)
    created, duplicates, system, invoices = [], 0, 0, 0
    inflows = [r for r in rows if r["amount"] > 0]
    ext_ids = dict(zip((id(r) for r in inflows), payments.bank_external_ids(
        [{"booked_on": r["booked_on"], "amount": r["amount"], "title": r["title"], "payer_name": r["counterparty"]} for r in inflows])))
    with transaction.atomic():
        for row in rows:
            key = _key(row)
            if BankOperation.objects.filter(key=key).exists():
                duplicates += 1
                continue
            op = BankOperation(key=key, source_file=(uploaded.name or "")[:160], imported_by=by, **row)
            if row["amount"] > 0:
                payment, _ = payments.register_bank_transfer(
                    {"booked_on": row["booked_on"], "amount": row["amount"], "title": row["title"], "payer_name": row["counterparty"]},
                    ext_ids[id(row)], by, raw={"plik": op.source_file[:120], "ksiegowosc": True}, unmatched=False)
                if payment is not None and payment.status == Payment.Status.COMPLETED:
                    op.payment, op.status = payment, BankOperation.Status.SYSTEM
                    system += 1
            op.save()
            if op.amount < 0 and op.currency == "PLN":
                invoice = match_invoice(op)
                if invoice:
                    op.invoice, op.status = invoice, BankOperation.Status.INVOICE
                    op.save(update_fields=["invoice", "status"])
                    invoices += 1
            created.append(op)
    from .stripe_import import match_bank_operations

    stripe = match_bank_operations()  # wypłaty ze Stripe — przychód ujmuje zestawienie Stripe
    if created:
        log_event("ksiegowosc.wyciag_import", None, f"Wyciąg {uploaded.name}: nowych operacji {len(created)}, wpłat strzelców {system}, "
                                                    f"zapłat faktur {invoices}, wypłat Stripe {stripe}", actor=by)
    return created, duplicates, system, invoices


def book(op, *, by, description, column, activity="", task=None, day=None, document="", notes=""):
    """Zapis w zestawieniu przychodów i kosztów na podstawie wyciągu (dowód: wyciąg bankowy)."""
    from . import services

    if op.status != BankOperation.Status.NEW:
        raise ValidationError("Ta operacja nie czeka na decyzję.")
    if op.currency != "PLN":
        raise ValidationError("Operacja w walucie obcej — przelicz kwotę na złote i wprowadź zapis ręcznie (§ 7 ust. 5).")
    if op.is_inflow and column != "revenue":
        raise ValidationError("Wpływ księguje się jako przychód (kol. 5).")
    if not op.is_inflow and column not in ("cost_deductible", "cost_other"):
        raise ValidationError("Wydatek księguje się jako koszt (kol. 6 albo 7).")
    with transaction.atomic():
        op = BankOperation.objects.select_for_update().get(pk=op.pk)
        if op.status != BankOperation.Status.NEW:
            raise ValidationError("Ta operacja nie czeka na decyzję.")
        record = services.add_record(
            kind=Record.Kind.REVENUE if op.is_inflow else Record.Kind.COST, day=day or op.booked_on,
            document=(document or f"Wyciąg bankowy {op.booked_on:%d.%m.%Y}")[:64], description=description, amount=abs(op.amount),
            column=column, by=by, counterparty=op.counterparty, notes=(notes or op.title)[:255], activity=activity, task=task)
        op.record, op.status, op.status_note = record, BankOperation.Status.BOOKED, ""
        op.save(update_fields=["record", "status", "status_note"])
    return record


def link_invoice(op, invoice, *, by):
    if op.is_inflow or op.status != BankOperation.Status.NEW:
        raise ValidationError("Z fakturą łączy się wydatek czekający na decyzję.")
    if invoice.direction != KsefInvoice.Direction.PURCHASE:
        raise ValidationError("Wybierz fakturę zakupu.")
    op.invoice, op.status, op.status_note = invoice, BankOperation.Status.INVOICE, ""
    op.save(update_fields=["invoice", "status", "status_note"])
    log_event("ksiegowosc.wyciag_faktura", None, f"Wydatek {abs(op.amount) / 100:.2f} zł z {op.booked_on} — zapłata faktury "
                                                 f"{invoice.invoice_number}", actor=by)


def skip(op, *, by, reason):
    reason = " ".join((reason or "").split())
    if not reason:
        raise ValidationError("Podaj przyczynę (np. przelew między własnymi rachunkami, zwrot pomyłkowej wpłaty).")
    if op.status != BankOperation.Status.NEW:
        raise ValidationError("Ta operacja nie czeka na decyzję.")
    op.status, op.status_note = BankOperation.Status.SKIPPED, reason[:255]
    op.save(update_fields=["status", "status_note"])
    log_event("ksiegowosc.wyciag_pominiecie", None, f"Operacja {op.amount / 100:.2f} zł z {op.booked_on} pominięta: {reason}", actor=by)


def reopen(op, *, by):
    """Cofnięcie decyzji: pominięcie / połączenie z fakturą. Zaksięgowaną operację cofa usunięcie zapisu w ewidencji."""
    if op.status in (BankOperation.Status.NEW, BankOperation.Status.SYSTEM):
        raise ValidationError("Tej operacji nie można przywrócić do decyzji.")
    if op.status == BankOperation.Status.BOOKED and op.record_id and not op.record.is_removed:
        raise ValidationError("Operacja jest zaksięgowana — usuń zapis w ewidencji (karta zapisu), operacja wróci do decyzji.")
    with transaction.atomic():
        if op.status == BankOperation.Status.REIMBURSEMENT:
            KsefInvoice.objects.filter(reimbursement_op=op).update(reimbursement=REIMBURSE_PENDING, reimbursement_op=None,
                                                                    reimbursed_on=None)
        op.status, op.status_note, op.invoice, op.record, op.stripe_payout = BankOperation.Status.NEW, "", None, None, None
        op.save(update_fields=["status", "status_note", "invoice", "record", "stripe_payout"])
    log_event("ksiegowosc.wyciag_przywrocenie", None, f"Operacja {op.amount / 100:.2f} zł z {op.booked_on} wraca do decyzji", actor=by)


def suggestion(op):
    """Podpowiedź opisu zdarzenia do zaksięgowania."""
    title = (op.title or "").lower()
    who = (op.counterparty or "").lower()
    if op.is_inflow:
        if "stripe" in who or "tpay" in who or "przelewy24" in who or "payu" in who or "axepta" in who:
            return "Dochody z majątku stowarzyszenia — udostępnianie strzelnicy (wypłata od operatora płatności online)"
        if "składk" in title or "skladk" in title:
            return "Składki członkowskie"
        if "darowizn" in title:
            return "Darowizna"
        if "odsetk" in title or "kapitalizac" in title:
            return "Odsetki bankowe"
        return "Dochody z majątku stowarzyszenia — udostępnianie strzelnicy"
    if "prowizj" in title or "opłat" in title or "oplat" in title or op.kind.lower().startswith(("opłata", "prowizja")):
        return "Opłaty i prowizje bankowe"
    return op.title or op.counterparty


# --- faktury zapłacone prywatnie i ich zwrot z rachunku ------------------------------------------


def set_payment(invoice, *, by, method, paid_by="", paid_on=None, reimbursement="", reimbursed_on=None, note="",
                same_seller=False):
    """Sposób zapłaty faktury zakupu, gdy nie przelewem z rachunku: prywatnie (z rozliczeniem zwrotu), potrącenie przez
    operatora płatności (Stripe), gotówka. Zwraca liczbę faktur tego sprzedawcy, którym ustawiono ten sam sposób."""
    methods = {m for m, _ in PAYMENT_METHODS}
    if method not in methods:
        raise ValidationError("Wybierz sposób zapłaty.")
    if invoice.direction != KsefInvoice.Direction.PURCHASE:
        raise ValidationError("Sposób zapłaty dotyczy faktur zakupu.")
    paid_by = " ".join((paid_by or "").split())[:120]
    note = " ".join((note or "").split())[:255]
    with transaction.atomic():
        invoice = KsefInvoice.objects.select_for_update().get(pk=invoice.pk)
        if method != PAYMENT_BANK and invoice.bank_operations.exists():
            raise ValidationError("Faktura jest połączona z zapłatą z wyciągu bankowego — najpierw przywróć tamtą operację do decyzji.")
        if invoice.reimbursement_op_id and (method != PAYMENT_PRIVATE or reimbursement != REIMBURSE_TRANSFER):
            raise ValidationError("Zwrot jest połączony z przelewem z wyciągu — najpierw przywróć tamtą operację do decyzji.")
        if method == PAYMENT_PRIVATE:
            if not paid_by:
                raise ValidationError("Podaj, kto zapłacił fakturę prywatnie.")
            reimbursement = reimbursement or REIMBURSE_PENDING
            if reimbursement not in {r for r, _ in REIMBURSEMENTS}:
                raise ValidationError("Wybierz stan zwrotu.")
            if reimbursement == REIMBURSE_TRANSFER and not invoice.reimbursement_op_id:
                raise ValidationError("Zwrot przelewem łączy się z operacją na karcie przelewu w wyciągu bankowym.")
            if reimbursement == REIMBURSE_PENDING:
                reimbursed_on = None
        else:
            paid_by, reimbursement, reimbursed_on = "", "", None
        invoice.payment_method, invoice.paid_by, invoice.paid_on = method, paid_by, paid_on
        invoice.reimbursement, invoice.reimbursed_on, invoice.payment_note = reimbursement, reimbursed_on, note
        invoice.save(update_fields=["payment_method", "paid_by", "paid_on", "reimbursement", "reimbursed_on", "payment_note"])
        others = 0
        if same_seller and method not in (PAYMENT_BANK, PAYMENT_PRIVATE) and invoice.seller_nip:
            others = (KsefInvoice.objects.filter(direction=KsefInvoice.Direction.PURCHASE, seller_nip=invoice.seller_nip,
                                                 payment_method=PAYMENT_BANK, bank_operations__isnull=True)
                      .exclude(pk=invoice.pk).update(payment_method=method))
    log_event("ksiegowosc.ksef_zaplata", None, f"Faktura {invoice.invoice_number}: sposób zapłaty — "
                                               f"{invoice.get_payment_method_display()}"
                                               + (f" ({invoice.get_reimbursement_display().lower()})" if reimbursement else "")
                                               + (f"; to samo dla innych faktur sprzedawcy: {others}" if others else ""), actor=by)
    return others


def reimbursement_candidates(op):
    """Faktury zapłacone prywatnie, czekające na zwrot, wystawione najpóźniej 30 dni po przelewie."""
    if op.is_inflow or op.currency != "PLN":
        return []
    return list(KsefInvoice.objects.filter(direction=KsefInvoice.Direction.PURCHASE, payment_method=PAYMENT_PRIVATE,
                                           reimbursement=REIMBURSE_PENDING, currency="PLN",
                                           issue_date__lte=op.booked_on + timedelta(days=PREPAYMENT_DAYS))
                .exclude(status=KsefInvoice.Status.REJECTED).order_by("issue_date", "pk"))


def reimbursement_match(op, candidates=None):
    """Zestaw faktur do zwrotu, których suma równa się kwocie przelewu (najmniej faktur; przy remisie — najstarsze)."""
    candidates = reimbursement_candidates(op) if candidates is None else candidates
    target = -op.amount
    pool = candidates[:MAX_REIMBURSE_CANDIDATES]
    for size in range(1, len(pool) + 1):
        for combo in itertools.combinations(pool, size):
            if sum(i.gross for i in combo) == target:
                return list(combo)
    return []


def link_reimbursement(op, invoices, *, by):
    """Przelew z rachunku = zwrot osobie, która zapłaciła faktury prywatnie. Koszt ujmują faktury — przelew nie jest
    drugim kosztem."""
    if op.is_inflow or op.status != BankOperation.Status.NEW:
        raise ValidationError("Jako zwrot łączy się wydatek czekający na decyzję.")
    if not invoices:
        raise ValidationError("Zaznacz faktury, za które jest ten zwrot.")
    with transaction.atomic():
        op = BankOperation.objects.select_for_update().get(pk=op.pk)
        if op.status != BankOperation.Status.NEW:
            raise ValidationError("Ta operacja nie czeka na decyzję.")
        locked = list(KsefInvoice.objects.select_for_update().filter(pk__in=[i.pk for i in invoices]))
        if any(not i.awaiting_reimbursement for i in locked) or len(locked) != len(invoices):
            raise ValidationError("Zaznaczone faktury muszą być zapłacone prywatnie i czekać na zwrot.")
        total = sum(i.gross for i in locked)
        if total != -op.amount:
            from apps.facilities.models import format_pln

            raise ValidationError(f"Suma zaznaczonych faktur ({format_pln(total)}) różni się od kwoty przelewu ({format_pln(-op.amount)}).")
        for i in locked:
            i.reimbursement, i.reimbursement_op, i.reimbursed_on = REIMBURSE_TRANSFER, op, op.booked_on
            i.save(update_fields=["reimbursement", "reimbursement_op", "reimbursed_on"])
        names = ", ".join(sorted({i.invoice_number for i in locked}))
        op.status, op.status_note = BankOperation.Status.REIMBURSEMENT, f"zwrot za faktury: {names}"[:255]
        op.save(update_fields=["status", "status_note"])
    log_event("ksiegowosc.wyciag_zwrot", None, f"Wydatek {-op.amount / 100:.2f} zł z {op.booked_on} — zwrot za faktury "
                                               f"zapłacone prywatnie: {names}", actor=by)
    return op
