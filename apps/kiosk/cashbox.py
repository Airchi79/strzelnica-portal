"""Gotówka do skarbonki przy wejściu (kiosk) i rozliczenie skarbonki przez obsługę.

Osoba wrzuca gotówkę i potwierdza oświadczenie w kiosku — rezerwacja jest potwierdzona, a należność czeka na
rozliczenie. Obsługa po opróżnieniu skarbonki wpisuje wyjętą kwotę i potwierdza wpłaty (rejestrowane jak zwykła wpłata
gotówkowa); niepotwierdzone zostają „do zapłaty”, a przy różnicy panel pokazuje osoby z danego dnia do kontaktu."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_event
from apps.bookings import services as bookings
from apps.bookings.models import BookingSettings, Reservation
from apps.core.notifications import notify_board
from apps.facilities.models import format_pln

from .models import CashBoxSettlement, CashDeclaration, OnsiteBooking

STATEMENT = ("Oświadczam, że wrzuciłem(-am) do skarbonki przy wejściu gotówkę w kwocie {amount} za rezerwację {number}. "
             "Wiem, że wpłaty w skarbonce są rozliczane na podstawie oświadczeń, w razie niezgodności obsługa skontaktuje się "
             "ze mną, a brak wpłaty oznacza nieopłacony pobyt i może skutkować zakazem korzystania ze strzelnicy.")


def cash_option(reservation, now=None):
    """Czy kiosk może zaproponować gotówkę do skarbonki (ustawienie i zasady grupy strzelca) — kwota i treść oświadczenia."""
    from .onsite import payable

    if not BookingSettings.load().kiosk_cash_box or not payable(reservation):
        return None
    if CashDeclaration.objects.filter(reservation=reservation).exclude(status=CashDeclaration.Status.MISSING).exists():
        return None
    if not bookings.payment_options(reservation, now)["cash"]:
        return None
    amount = reservation.amount - reservation.paid_amount
    return {"amount": amount, "statement": STATEMENT.format(amount=format_pln(amount), number=reservation.number)}


def declared(reservation):
    return CashDeclaration.objects.filter(reservation=reservation, status=CashDeclaration.Status.DECLARED).exists()


@transaction.atomic
def declare(flow, accept, now=None):
    """Krok kiosku: oświadczenie o wrzuceniu gotówki do skarbonki → rezerwacja potwierdzona (gotówka do rozliczenia)."""
    now = now or timezone.now()
    flow = OnsiteBooking.objects.select_for_update(of=("self",)).select_related("reservation", "user", "device").get(pk=flow.pk)
    r = flow.reservation
    if r is None:
        raise ValidationError("Najpierw zarezerwuj termin.")
    option = cash_option(r, now)
    if option is None:
        raise ValidationError("Gotówka do skarbonki nie jest dostępna dla tej rezerwacji — zapłać online.")
    if not accept:
        raise ValidationError("Zaznacz oświadczenie o wpłacie gotówki do skarbonki.")
    if r.status == Reservation.Status.HOLD:
        r = bookings.choose_payment(r, flow.user, Reservation.PayMethod.CASH, now=now)  # potwierdzona, gotówka do zapłaty
    else:
        Reservation.objects.filter(pk=r.pk).update(pay_method=Reservation.PayMethod.CASH, pay_by=None)
    d = CashDeclaration.objects.create(reservation=r, user=flow.user, device=flow.device, amount=option["amount"],
                                       statement=option["statement"])
    log_event("kiosk.gotowka_skarbonka", r, f"{r.number}: {format_pln(d.amount)} do skarbonki (oświadczenie)", actor=flow.user)
    return d


@transaction.atomic
def settle(counted_amount, confirmed_ids, by, note=""):
    """Rozliczenie skarbonki: potwierdzone oświadczenia → wpłata gotówkowa za rezerwację; pozostałe → „brak wpłaty”."""
    from apps.payments.services import register_manual_payment

    pending = list(CashDeclaration.objects.select_for_update().filter(status=CashDeclaration.Status.DECLARED)
                   .select_related("reservation", "user"))
    if not pending:
        raise ValidationError("Brak oświadczeń do rozliczenia.")
    if counted_amount is None or counted_amount < 0:
        raise ValidationError("Podaj kwotę wyjętą ze skarbonki.")
    confirmed_ids = {int(i) for i in confirmed_ids}
    settlement = CashBoxSettlement.objects.create(counted_amount=counted_amount, expected_amount=sum(d.amount for d in pending),
                                                  confirmed_amount=0, note=note.strip()[:255], created_by=by)
    confirmed = 0
    day = timezone.localdate()
    for d in pending:
        d.settlement = settlement
        if d.pk in confirmed_ids:
            r = Reservation.objects.get(pk=d.reservation_id)
            if r.payment_status == Reservation.Payment.DUE and r.status != Reservation.Status.CANCELLED:
                register_manual_payment(d.user, d.amount, "gotowka", by, reservation=r, note=f"Skarbonka (kiosk) — rozliczenie {day:%d.%m.%Y}")
            d.status = CashDeclaration.Status.CONFIRMED
            confirmed += d.amount
        else:
            d.status = CashDeclaration.Status.MISSING
        d.save(update_fields=["settlement", "status"])
    settlement.confirmed_amount = confirmed
    settlement.save(update_fields=["confirmed_amount"])
    log_event("skarbonka.rozliczenie", settlement,
              f"wyjęto {format_pln(counted_amount)}, wg oświadczeń {format_pln(settlement.expected_amount)}, "
              f"potwierdzono {format_pln(confirmed)}", actor=by)
    missing = [d for d in pending if d.status == CashDeclaration.Status.MISSING]
    if missing or settlement.difference:
        from django.urls import reverse

        url = reverse("panel:cashbox")
        body = (f"Wyjęto {format_pln(counted_amount)}, potwierdzono {format_pln(confirmed)} (różnica {format_pln(settlement.difference)}); "
                f"oświadczeń bez wpłaty: {len(missing)}. Lista osób do kontaktu w panelu.")
        notify_board("Skarbonka: niezgodność rozliczenia", "emails/board_generic.txt", "payments.manage_payments",
                     url=url, body=body, event="Rozliczenie skarbonki — niezgodność", url_path=url, text=body)
    return settlement
