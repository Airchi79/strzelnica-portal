"""Lista kontrolna miesiąca: czy ewidencja za miesiąc jest kompletna (faktury KSeF, faktura prowizyjna, Stripe, wyciąg,
operacje do decyzji, zwroty, uzgodnienie salda). Każdy krok ma stan i link do miejsca, w którym się go załatwia."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Max, Sum
from django.urls import reverse
from django.utils import timezone

from apps.facilities.models import format_pln

from .models import PAYMENT_OFFSET, BankOperation, KsefInvoice, KsefSync, StripePayout, StripePosting, StripeTransaction

OK, TODO, WARN, LATER = "ok", "todo", "warn", "later"
LABELS = {OK: "gotowe", TODO: "do zrobienia", WARN: "sprawdź", LATER: "jeszcze nie teraz"}
BADGES = {OK: "ok", TODO: "bad", WARN: "warn", LATER: ""}


@dataclass
class Step:
    title: str
    state: str
    detail: str
    url: str = ""
    action: str = ""

    @property
    def label(self):
        return LABELS[self.state]

    @property
    def badge(self):
        return BADGES[self.state]


def _next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def month_steps(year, month, today=None):
    today = today or timezone.localdate()
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    finished = today > end
    last_day = min(end, today - timedelta(days=1))  # ostatni zakończony dzień okresu
    steps = []

    # 1. faktury KSeF do decyzji
    sync = KsefSync.load()
    pending = KsefInvoice.objects.filter(status=KsefInvoice.Status.NEW, issue_date__lte=end)
    n = pending.count()
    sync_note = (f"Automatyczne pobieranie: ostatni udany przebieg {timezone.localtime(sync.last_success_at):%d.%m.%Y %H:%M}."
                 if sync.enabled and sync.last_success_at else "Automatyczne pobieranie z KSeF jest wyłączone — wgraj faktury ręcznie.")
    steps.append(Step(
        "Faktury KSeF przyjęte lub odrzucone", TODO if n else (OK if sync.enabled else WARN),
        (f"Do decyzji: {n} (wystawione do {end:%d.%m.%Y}). " if n else "Brak faktur czekających na decyzję. ") + sync_note,
        reverse("panel:accounting_ksef"), "Przyjmij faktury" if n else "Faktury KSeF"))

    # 2. faktura prowizyjna platformy za miesiąc (wystawiana w następnym miesiącu)
    fee = 0
    for t in StripeTransaction.objects.filter(day__gte=start, day__lte=end).exclude(type="payout"):
        fee += t.fee if t.type in ("payment", "charge", "payment_refund", "refund") else -t.amount
    nxt = _next_month(start)
    commission = list(KsefInvoice.objects.filter(payment_method=PAYMENT_OFFSET, issue_date__gte=nxt,
                                                 issue_date__lt=_next_month(nxt)).exclude(status=KsefInvoice.Status.REJECTED))
    fees_posted = StripePosting.objects.filter(kind=StripePosting.Kind.FEES, period_start=start, record__removed_at__isnull=True).exists()
    invoiced = sum(i.gross for i in commission)
    if not fee and not commission:
        steps.append(Step("Faktura prowizyjna za płatności online", OK, "W tym miesiącu Stripe nie pobrał prowizji."))
    elif fees_posted:
        steps.append(Step("Faktura prowizyjna za płatności online", OK,
                          f"Prowizje {format_pln(fee)} ujęte z eksportu Stripe (bez faktury).", reverse("panel:accounting_stripe"), "Stripe"))
    elif not commission:
        state = WARN if today >= nxt + timedelta(days=14) else LATER
        steps.append(Step("Faktura prowizyjna za płatności online", state,
                          f"Prowizje Stripe za miesiąc: {format_pln(fee)}. Faktura platformy przychodzi zwykle na początku "
                          f"{nxt:%m.%Y} przez KSeF" + (" — jeszcze jej nie ma, sprawdź z wystawcą." if state == WARN else "."),
                          reverse("panel:accounting_ksef"), "Faktury KSeF"))
    else:
        not_accepted = [i for i in commission if i.status != KsefInvoice.Status.ACCEPTED]
        numbers = ", ".join(i.invoice_number for i in commission)
        if not_accepted:
            steps.append(Step("Faktura prowizyjna za płatności online", TODO, f"Faktura {numbers} czeka na przyjęcie do ewidencji.",
                              reverse("panel:accounting_ksef_invoice", args=[not_accepted[0].pk]), "Otwórz fakturę"))
        elif invoiced != fee:
            steps.append(Step("Faktura prowizyjna za płatności online", WARN,
                              f"Faktura {numbers}: {format_pln(invoiced)}, a prowizje w eksporcie Stripe: {format_pln(fee)} — "
                              "sprawdź, czy eksport Stripe obejmuje cały miesiąc i czy faktura dotyczy tego okresu.",
                              reverse("panel:accounting_stripe"), "Stripe"))
        else:
            steps.append(Step("Faktura prowizyjna za płatności online", OK,
                              f"Faktura {numbers} przyjęta, kwota zgodna z prowizjami Stripe ({format_pln(fee)})."))

    # 3. Stripe: eksport obejmuje miesiąc, dni ujęte w ewidencji
    last_tx = StripeTransaction.objects.aggregate(m=Max("day"))["m"]
    days = set(StripeTransaction.objects.filter(day__gte=start, day__lte=last_day)
               .exclude(type="payout").values_list("day", flat=True))
    posted = set(StripePosting.objects.filter(kind=StripePosting.Kind.REVENUE, period_start__gte=start, period_start__lte=end,
                                              record__removed_at__isnull=True).values_list("period_start", flat=True))
    missing = sorted(days - posted)
    if last_tx is None:
        steps.append(Step("Płatności online (Stripe) wgrane i ujęte", WARN, "Nie wgrano jeszcze eksportu ze Stripe.",
                          reverse("panel:accounting_stripe"), "Wgraj eksport"))
    elif last_tx < last_day:
        steps.append(Step("Płatności online (Stripe) wgrane i ujęte", TODO,
                          f"Eksport kończy się na {last_tx:%d.%m.%Y} — wgraj świeży eksport (historia salda i wypłaty).",
                          reverse("panel:accounting_stripe"), "Wgraj eksport"))
    elif missing:
        steps.append(Step("Płatności online (Stripe) wgrane i ujęte", TODO,
                          f"Dni z płatnościami jeszcze nieujęte w ewidencji: {len(missing)} (od {missing[0]:%d.%m}).",
                          reverse("panel:accounting_stripe"), "Ujmij w ewidencji"))
    else:
        steps.append(Step("Płatności online (Stripe) wgrane i ujęte", OK if finished else LATER,
                          f"Ujęte dni z płatnościami: {len(posted)}." + ("" if finished else " Miesiąc jeszcze trwa.")))

    # 4. wyciąg bankowy obejmuje miesiąc (kontrola: wypłaty Stripe z miesiąca mają swoje wpływy w wyciągu)
    last_op = BankOperation.objects.aggregate(m=Max("booked_on"))["m"]
    unmatched = StripePayout.objects.filter(bank_operations__isnull=True, arrival_date__gte=start, arrival_date__lte=min(end, today - timedelta(days=4)))
    if last_op is None:
        steps.append(Step("Wyciąg bankowy za cały miesiąc", WARN, "Nie wgrano jeszcze wyciągu z BNP Paribas.",
                          reverse("panel:accounting_bank"), "Wgraj wyciąg"))
    elif unmatched.exists() or (finished and last_op < end):
        reasons = []
        if last_op < end:
            reasons.append(f"ostatnia operacja w systemie: {last_op:%d.%m.%Y}")
        if unmatched.exists():
            reasons.append(f"wypłaty ze Stripe bez wpływu w wyciągu: {unmatched.count()}")
        steps.append(Step("Wyciąg bankowy za cały miesiąc", TODO,
                          "Wgraj zestawienie operacji za cały miesiąc (" + "; ".join(reasons) + "). Jeśli w końcówce miesiąca "
                          "nie było operacji, a wypłaty się zgadzają — wszystko w porządku.", reverse("panel:accounting_bank"), "Wgraj wyciąg"))
    else:
        steps.append(Step("Wyciąg bankowy za cały miesiąc", OK if finished else LATER,
                          f"Ostatnia operacja: {last_op:%d.%m.%Y}." + ("" if finished else " Miesiąc jeszcze trwa.")))

    # 5. operacje z wyciągu do decyzji
    n = BankOperation.objects.filter(status=BankOperation.Status.NEW, booked_on__gte=start, booked_on__lte=end).count()
    steps.append(Step("Operacje z wyciągu rozstrzygnięte", TODO if n else OK,
                      f"Do decyzji: {n} — zaksięguj, połącz z fakturą lub zwrotem albo pomiń." if n else "Nic nie czeka na decyzję.",
                      reverse("panel:accounting_bank"), "Wyciąg bankowy" if n else ""))

    # 6. zwroty za faktury zapłacone prywatnie (niezależnie od miesiąca — przypomnienie)
    owed = KsefInvoice.objects.filter(payment_method="prywatnie", reimbursement="do_zwrotu").exclude(status=KsefInvoice.Status.REJECTED)
    total = owed.aggregate(s=Sum("gross"))["s"] or 0
    steps.append(Step("Zwroty za faktury zapłacone prywatnie", WARN if total else OK,
                      f"Do zwrotu: {format_pln(total)} ({owed.count()} faktur). Po przelewie połącz go w wyciągu jako zwrot."
                      if total else "Nic nie czeka na zwrot.", reverse("panel:accounting_reconciliation") + f"?rok={year}#g-private"
                      if total else "", "Szczegóły" if total else ""))

    # 7. uzgodnienie salda bez niewyjaśnionej różnicy i bez faktur o nieznanej zapłacie
    from . import reconciliation

    r = reconciliation.reconcile(year, end)
    unpaid = next((g for g in r["groups"] if g.key == "unpaid"), None)
    url = reverse("panel:accounting_reconciliation") + f"?rok={year}&do={end:%Y-%m-%d}"
    if not r["ops_count"]:
        steps.append(Step("Saldo rachunku uzgodnione z ewidencją", LATER, "Brak operacji z wyciągu w tym roku.", url, "Uzgodnienie salda"))
    elif r["unexplained"]:
        steps.append(Step("Saldo rachunku uzgodnione z ewidencją", WARN, f"Różnica niewyjaśniona: {format_pln(r['unexplained'])}.",
                          url, "Uzgodnienie salda"))
    elif unpaid:
        steps.append(Step("Saldo rachunku uzgodnione z ewidencją", WARN,
                          f"Faktury bez zapłaty z rachunku: {unpaid.count} ({format_pln(unpaid.total)}) — zapłacone później, "
                          "inaczej (wskaż sposób na karcie faktury) albo jeszcze niezapłacone.", url + "#g-unpaid", "Uzgodnienie salda"))
    else:
        steps.append(Step("Saldo rachunku uzgodnione z ewidencją", OK,
                          f"Saldo na {end:%d.%m.%Y}: {format_pln(r['closing'])} — wszystkie różnice wyjaśnione.", url, "Uzgodnienie salda"))
    return steps
