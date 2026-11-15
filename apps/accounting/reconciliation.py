"""Uzgodnienie salda rachunku bankowego z ewidencją: dlaczego stan konta różni się od wyniku (przychody − koszty).

Ewidencja ujmuje przychody i koszty (memoriałowo wg dowodów), wyciąg — przepływ pieniędzy. Każda operacja z wyciągu
i każdy zapis z ewidencji okresu trafia do dokładnie jednej grupy; wartość grupy = wpływ na rachunek − wpływ na wynik.
Dlatego zawsze: saldo początkowe + wynik + suma grup = saldo końcowe (wyliczone z operacji) — co do grosza.

Grupy: zgodne pary (zapis z wyciągu, zapłata faktury), Stripe (środki w saldzie Stripe, prowizje bez faktury,
płatności jeszcze nieujęte), wpłaty z systemu rezerwacji, koszty zapłacone prywatnie (i ich zwroty), gotówka,
amortyzacja, faktury bez zapłaty z rachunku, operacje do decyzji / pominięte, inne zapisy."""

from dataclasses import dataclass, field
from datetime import date

from .models import (PAYMENT_CASH, PAYMENT_OFFSET, PAYMENT_PRIVATE, REIMBURSE_CASH, REIMBURSE_PENDING, REIMBURSE_TRANSFER,
                     REIMBURSE_WAIVED, BankOperation, KsefInvoice, Record, StripePosting, StripeTransaction)

MAX_ITEMS = 300


@dataclass
class Item:
    day: date
    text: str
    value: int  # wpływ na rachunek − wpływ na wynik [gr]
    op: BankOperation = None
    record: Record = None
    invoice: KsefInvoice = None


@dataclass
class Group:
    key: str
    label: str
    help: str
    items: list = field(default_factory=list)
    lines: list = field(default_factory=list)  # rozbicie grupy: (opis, kwota)

    @property
    def value(self):
        return sum(i.value for i in self.items)


GROUPS = [
    ("stripe", "Płatności online (Stripe)",
     "Przychód ujmuje się w dniu płatności klienta, a pieniądze przychodzą na rachunek z opóźnieniem, pomniejszone o prowizje."),
    ("system", "Wpłaty strzelców rozliczane w systemie",
     "Przychód z raportów wpływów a wpłaty, które faktycznie przyszły na rachunek (np. gotówka na strzelnicy nie trafia na konto)."),
    ("private", "Koszty zapłacone prywatnie",
     "Koszt jest w ewidencji, ale zapłaciła go osoba prywatnie — z rachunku wychodzi dopiero zwrot."),
    ("cash", "Koszty zapłacone gotówką z kasy", "Koszt w ewidencji bez wydatku z rachunku."),
    ("depreciation", "Amortyzacja", "Odpisy amortyzacyjne są kosztem bez wydatku pieniędzy."),
    ("unpaid", "Faktury bez zapłaty z rachunku w tym okresie",
     "Faktura jest w ewidencji, ale w wyciągu nie ma jej zapłaty: niezapłacona, zapłacona w innym okresie albo inaczej — "
     "wskaż sposób zapłaty na karcie faktury."),
    ("invoice_unbooked", "Zapłaty faktur nieprzyjętych do ewidencji w tym okresie",
     "Wydatek z rachunku połączony z fakturą, która nie jest (jeszcze) w ewidencji tego roku."),
    ("pending", "Operacje z wyciągu czekające na decyzję", "Zaksięguj je, połącz z fakturą albo pomiń."),
    ("skipped", "Operacje pominięte (nie dotyczą ewidencji)", "Np. przelewy między własnymi rachunkami, zwroty pomyłkowych wpłat."),
    ("other_year", "Zapisy z wyciągu ujęte w innym roku", "Operacja z tego okresu zaksięgowana z datą innego roku albo zapis usunięty."),
    ("other", "Inne zapisy bez powiązania z wyciągiem",
     "Wpisy ręczne i korekty, których nie połączono z operacją z wyciągu (np. przychód gotówkowy, wpis sprzed importu wyciągu)."),
    ("matched", "Operacje powiązane z zapisem lub fakturą",
     "Operacja z wyciągu i jej zapis w ewidencji powinny mieć tę samą kwotę — różnica oznacza błąd w zapisie, sprawdź go."),
]


def _effect(record):
    return record.revenue - record.cost_deductible - record.cost_other


def _balances(year_start, end):
    """Saldo początkowe i końcowe (suma po rachunkach): z „Saldo po transakcji” pierwszej operacji okresu; końcowe
    wyliczone z operacji i porównane z saldem ostatniej operacji (kontrola kompletności importu)."""
    opening = closing_statement = 0
    known = True
    warnings = []
    accounts = BankOperation.objects.filter(currency="PLN").order_by().values_list("account", flat=True).distinct()
    for account in accounts:
        qs = BankOperation.objects.filter(currency="PLN", account=account).order_by("booked_on", "transaction_at", "pk")
        in_period = qs.filter(booked_on__gte=year_start, booked_on__lte=end)
        first, last = in_period.first(), in_period.last()
        if first is None:
            before = qs.filter(booked_on__lt=year_start).last()
            if before is not None and before.balance_after is not None:
                opening += before.balance_after
                closing_statement += before.balance_after
            elif before is not None:
                known = False
            continue
        if first.balance_after is None:
            known = False
        else:
            opening += first.balance_after - first.amount
        flows = sum(in_period.values_list("amount", flat=True))
        if last.balance_after is not None:
            closing_statement += last.balance_after
            if first.balance_after is not None and first.balance_after - first.amount + flows != last.balance_after:
                warnings.append(f"Rachunek …{account[-4:] or '?'}: saldo z ostatniej operacji nie zgadza się z sumą operacji — "
                                "możliwe, że brakuje części wyciągu (wgraj pełny okres).")
        else:
            known = False
    return opening, closing_statement, known, warnings


def reconcile(year, until=None):
    year_start, year_end = date(year, 1, 1), date(year, 12, 31)
    end = min(until or year_end, year_end)
    groups = {key: Group(key, label, help) for key, label, help in GROUPS}

    ops = list(BankOperation.objects.filter(booked_on__gte=year_start, booked_on__lte=end, currency="PLN")
               .select_related("record", "invoice__record").order_by("booked_on", "pk"))
    foreign = BankOperation.objects.filter(booked_on__gte=year_start, booked_on__lte=end).exclude(currency="PLN").count()
    records = {r.pk: r for r in Record.objects.filter(year=year, date__lte=end, removed_at__isnull=True)
               .prefetch_related("ksef_invoices", "stripe_postings", "depreciation_postings")}
    used = set()

    def take(group, record, text=None, invoice=None):
        used.add(record.pk)
        groups[group].items.append(Item(record.date, text or f"Lp. {record.lp} — {record.description}", -_effect(record),
                                        record=record, invoice=invoice))

    for op in ops:
        text = f"{op.counterparty or op.title or 'operacja'}"
        S = BankOperation.Status
        if op.status == S.BOOKED:
            rec = op.record
            if rec and rec.pk in records and rec.pk not in used:
                used.add(rec.pk)
                groups["matched"].items.append(Item(op.booked_on, f"{text} — Lp. {rec.lp}", op.amount - _effect(rec), op=op, record=rec))
            else:
                groups["other_year"].items.append(Item(op.booked_on, text, op.amount, op=op, record=rec))
        elif op.status == S.INVOICE:
            inv = op.invoice
            rec = inv.record if inv and inv.status == KsefInvoice.Status.ACCEPTED else None
            if rec and rec.pk in records and rec.pk not in used:
                used.add(rec.pk)
                groups["matched"].items.append(Item(op.booked_on, f"{text} — faktura {inv.invoice_number}", op.amount - _effect(rec),
                                                    op=op, record=rec, invoice=inv))
            else:
                groups["invoice_unbooked"].items.append(Item(op.booked_on, f"{text} — faktura {inv.invoice_number if inv else '?'}",
                                                             op.amount, op=op, invoice=inv))
        elif op.status == S.REIMBURSEMENT:
            groups["private"].items.append(Item(op.booked_on, f"Zwrot przelewem: {op.status_note or text}", op.amount, op=op))
        elif op.status == S.STRIPE:
            groups["stripe"].items.append(Item(op.booked_on, "Wypłata ze Stripe na rachunek", op.amount, op=op))
        elif op.status == S.SYSTEM:
            groups["system"].items.append(Item(op.booked_on, f"Wpłata na rachunek: {text}", op.amount, op=op))
        elif op.status == S.SKIPPED:
            groups["skipped"].items.append(Item(op.booked_on, f"{text} — {op.status_note}", op.amount, op=op))
        else:
            groups["pending"].items.append(Item(op.booked_on, f"{text} — {op.title}", op.amount, op=op))

    stripe_fees_ew = 0
    for rec in sorted(records.values(), key=lambda r: (r.date, r.number)):
        if rec.pk in used:
            continue
        invoices = list(rec.ksef_invoices.all())
        inv = invoices[0] if invoices else None
        postings = list(rec.stripe_postings.all())
        if rec.kind == Record.Kind.REVENUE_AUTO and rec.category == "stripe":
            take("stripe", rec, f"Przychód dnia — płatności online (Lp. {rec.lp})")
        elif any(p.kind == StripePosting.Kind.FEES for p in postings) or (inv and inv.payment_method == PAYMENT_OFFSET):
            stripe_fees_ew += -_effect(rec)
            take("stripe", rec, f"Prowizje{' — faktura ' + inv.invoice_number if inv else ''} (Lp. {rec.lp})", inv)
        elif rec.kind == Record.Kind.REVENUE_AUTO:
            take("system", rec)
        elif inv and inv.payment_method == PAYMENT_PRIVATE:
            state = inv.get_reimbursement_display().lower() if inv.reimbursement else "do zwrotu"
            take("private", rec, f"Faktura {inv.invoice_number} — zapłacił(a) {inv.paid_by}, {state} (Lp. {rec.lp})", inv)
        elif inv and inv.payment_method == PAYMENT_CASH:
            take("cash", rec, f"Faktura {inv.invoice_number} (Lp. {rec.lp})", inv)
        elif rec.depreciation_postings.all():
            take("depreciation", rec)
        elif inv:
            take("unpaid", rec, f"Faktura {inv.invoice_number} — {inv.seller_name} (Lp. {rec.lp})", inv)
        else:
            take("other", rec)

    # rozbicie Stripe: saldo Stripe / prowizje bez faktury / płatności nieujęte
    stripe = groups["stripe"]
    if stripe.items:
        txs = StripeTransaction.objects.filter(day__gte=year_start, day__lte=end).exclude(type="payout")
        net = gross_rev = fee_cost = 0
        for t in txs:
            net += t.net
            if t.type in ("payment", "charge", "payment_refund", "refund"):
                gross_rev += t.amount
                fee_cost += t.fee
            else:
                fee_cost -= t.amount  # adjustment: zwrot opłaty (+) zmniejsza koszt prowizji
        payouts = sum(i.value for i in stripe.items if i.op is not None)
        revenue_posted = sum(-i.value for i in stripe.items if i.record is not None and i.record.revenue)
        stripe.lines = [
            ("Środki w saldzie Stripe albo w drodze na rachunek", payouts - net),
            ("Prowizje potrącone przez Stripe, jeszcze bez faktury / zapisu w ewidencji", -(fee_cost - stripe_fees_ew)),
            ("Płatności online jeszcze nieujęte w ewidencji", gross_rev - revenue_posted),
        ]

    private = groups["private"]
    if private.items:
        sums = {REIMBURSE_PENDING: 0, REIMBURSE_CASH: 0, REIMBURSE_WAIVED: 0, REIMBURSE_TRANSFER: 0}
        for i in private.items:
            if i.invoice is not None:
                state = i.invoice.reimbursement or REIMBURSE_PENDING
                sums[state if state in sums else REIMBURSE_PENDING] += i.value
            else:
                sums[REIMBURSE_TRANSFER] += i.value
        private.lines = [(label, value) for label, value in [
            ("Do zwrotu — pieniądze jeszcze na rachunku", sums[REIMBURSE_PENDING]),
            ("Zwrócone gotówką", sums[REIMBURSE_CASH]),
            ("Bez zwrotu (osoba zrzekła się zwrotu)", sums[REIMBURSE_WAIVED]),
            ("Faktury z innego okresu a zwroty przelewem w tym okresie", sums[REIMBURSE_TRANSFER]),
        ] if value]

    revenue = sum(r.revenue for r in records.values())
    cost6 = sum(r.cost_deductible for r in records.values())
    cost7 = sum(r.cost_other for r in records.values())
    result = revenue - cost6 - cost7
    flows = sum(op.amount for op in ops)
    opening, closing_statement, known, warnings = _balances(year_start, end)
    if foreign:
        warnings.append(f"Operacje w walucie obcej ({foreign}) nie są uwzględnione w uzgodnieniu.")
    explained = sum(g.value for g in groups.values())
    shown = [g for g in groups.values() if g.items]
    for g in shown:  # suma liczona przed skróceniem listy do wyświetlenia
        g.total, g.count, g.more = g.value, len(g.items), max(0, len(g.items) - MAX_ITEMS)
        g.items = g.items[:MAX_ITEMS]
    return {
        "year": year, "end": end, "ops_count": len(ops), "revenue": revenue, "cost_deductible": cost6, "cost_other": cost7,
        "income": revenue - cost6, "result": result, "flows": flows, "opening": opening, "opening_known": known,
        "closing": opening + flows, "closing_statement": closing_statement, "groups": [g for g in shown if g.total],
        "zero_groups": [g for g in shown if not g.total], "explained": explained,
        "unexplained": flows - result - explained, "warnings": warnings,
    }
