"""Wydarzenia: publikacja z blokadą osi, zapisy z limitem i listą rezerwową, wycofanie z awansem z listy, odwołanie."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit.services import log_event
from apps.core.notifications import notify_board, notify_user
from apps.regulations.models import pending_acceptances

from .models import Event, Registration

PERM = "events.manage_events"


def _sync_block(event, by, cancel_conflicts=False):
    """Blokada osi na czas wydarzenia (opublikowanego). Zwraca listę kolidujących rezerwacji."""
    from apps.bookings import services as booking_services
    from apps.bookings.models import Block

    lanes = list(event.lanes.all())
    if event.block_id:
        Block.objects.filter(pk=event.block_id).delete()
        event.block = None
    conflicts = []
    if event.status == Event.Status.PUBLISHED and lanes:
        blocks, conflicts, _ = booking_services.create_blocks(
            f"Wydarzenie: {event.title}"[:120], "Blokada utworzona automatycznie przez wydarzenie.", lanes,
            [(event.starts_at, event.ends_at)], by, cancel_conflicts,
        )
        event.block = blocks[0]
    event.save(update_fields=["block"])
    return conflicts


def conflicts_for(event, lanes, starts_at, ends_at):
    from apps.bookings import services as booking_services

    return list(booking_services.block_conflicts(lanes, [(starts_at, ends_at)])) if lanes else []


@transaction.atomic
def save_event(event, lanes, by, cancel_conflicts=False):
    is_new = event.pk is None
    if is_new:
        event.created_by = by
    event.save()
    event.lanes.set(lanes)
    conflicts = _sync_block(event, by, cancel_conflicts)
    log_event("wydarzenie.zapis", event, str(event), changes={"status": event.status, "nowe": is_new}, actor=by)
    return conflicts


def can_register(user, event):
    """Powody, dla których osoba nie może się zapisać (pusta lista = może)."""
    reasons = []
    if not event.registration_open:
        reasons.append("Zapisy na to wydarzenie są zamknięte.")
    if not user.email_verified:
        reasons.append("Potwierdź adres e-mail.")
    if pending_acceptances(user):
        reasons.append("Zaakceptuj aktualny regulamin.")
    return reasons


@transaction.atomic
def register(user, event, note=""):
    event = Event.objects.select_for_update().get(pk=event.pk)  # blokada: limit miejsc przy równoczesnych zapisach
    reasons = can_register(user, event)
    if reasons:
        raise ValidationError(reasons)
    if Registration.objects.filter(event=event, user=user).exclude(status=Registration.Status.CANCELLED).exists():
        raise ValidationError("Jesteś już zapisany na to wydarzenie.")
    full = event.capacity is not None and event.taken() >= event.capacity
    reg = Registration.objects.create(
        event=event, user=user, note=note.strip()[:120],
        status=Registration.Status.RESERVE if full else Registration.Status.REGISTERED,
        amount=event.fee, payment_status=Registration.Payment.DUE if event.fee else Registration.Payment.FREE,
    )
    log_event("wydarzenie.zapis_uczestnika", reg, str(reg), changes={"status": reg.status}, actor=user)
    if reg.status == Registration.Status.REGISTERED:
        from apps.payments.services import apply_wallet

        if event.fee and _has_overpayment(user, reg):  # nadpłata/saldo pokrywa opłatę od razu (gdy włączone w ustawieniach)
            apply_wallet(user, targets=[reg])
            reg.refresh_from_db()
    notify_user(user, f"Zapis: {event.title}" + (" — lista rezerwowa" if full else ""), "emails/event_registered.txt", reg=reg)
    return reg


def _has_overpayment(user, reg):
    from apps.payments.models import PaymentSettings
    from apps.payments.services import balance_of

    cfg = PaymentSettings.load()
    return (cfg.wallet_enabled or cfg.overpayment_pays_reservations) and balance_of(user) >= reg.amount


def _promote_reserve(event, by):
    """Pierwsza osoba z listy rezerwowej wchodzi na zwolnione miejsce."""
    if event.capacity is not None and event.taken() >= event.capacity:
        return None
    nxt = event.registrations.filter(status=Registration.Status.RESERVE).order_by("created_at", "pk").first()
    if nxt is None:
        return None
    nxt.status = Registration.Status.REGISTERED
    nxt.save(update_fields=["status"])
    log_event("wydarzenie.awans_z_listy", nxt, str(nxt), actor=by)
    notify_user(nxt.user, f"Zwolniło się miejsce: {event.title}", "emails/event_promoted.txt", reg=nxt)
    return nxt


def _refund_paid(reg, paid, by, reason, refund):
    """Wpłata za zapis po wycofaniu/odwołaniu. `refund`: wpłaty online wracają przez Tpay (bez opłaty za płatność
    online); reszta — przy włączonym saldzie zostaje na saldzie, przy wyłączonym zarząd dostaje przypomnienie o zwrocie
    ręcznym. Bez `refund` cała kwota zostaje nadpłatą. Zwraca słownik do e-maila i komunikatu."""
    from apps.payments.models import Payment
    from apps.payments.services import refund_to_payers, remind_manual_refund, surcharge_of, wallet_enabled

    info = {"refunded": 0, "manual": 0, "to_wallet": 0, "overpaid": 0, "fee_kept": 0}
    if not paid:
        return info
    if not refund:
        info["overpaid"] = paid
        return info
    payments = Payment.objects.filter(event_registration=reg)
    info["refunded"], rest = refund_to_payers(payments, paid, by, reason)
    if wallet_enabled():
        info["to_wallet"] = rest
    else:
        info["manual"] = rest
        if rest:
            remind_manual_refund(reg.user, rest, f"{reg.event.title} — {reg.user.full_name}")
    info["fee_kept"] = surcharge_of(payments)
    return info


@transaction.atomic
def cancel_registration(reg, by, by_staff=False, reason="", refund=True):
    """Wycofanie zapisu. Uczestnik sam (do zamknięcia zapisów) — wpłata online wraca automatycznie przez Tpay;
    organizator — zwrot, gdy `refund` (domyślnie tak). Ustawia `reg.refund_info` (kwoty zwrotu)."""
    from apps.payments.services import refund_registration

    reg = Registration.objects.select_for_update().select_related("event", "user").get(pk=reg.pk)
    event = Event.objects.select_for_update().get(pk=reg.event_id)
    if reg.status == Registration.Status.CANCELLED:
        raise ValidationError("Zapis jest już wycofany.")
    if not by_staff:
        if reg.user_id != by.pk:
            raise ValidationError("To nie jest Twój zapis.")
        if timezone.now() >= event.registration_until:
            raise ValidationError("Po zamknięciu zapisów wycofać może tylko organizator — skontaktuj się z zarządem.")
    was_registered = reg.status == Registration.Status.REGISTERED
    reg.status = Registration.Status.CANCELLED
    reg.cancelled_at = timezone.now()
    reg.save(update_fields=["status", "cancelled_at"])
    paid = reg.paid_amount
    refund_registration(reg, by=by)
    info = _refund_paid(reg, paid, by, f"Wycofano zapis: {event.title}", refund or not by_staff)
    log_event("wydarzenie.wycofanie", reg, str(reg), changes={"przez_organizatora": by_staff, "powod": reason, **info}, actor=by)
    by_self = not by_staff or reg.user_id == by.pk
    notify_user(reg.user, f"Wycofano zapis: {event.title}", "emails/event_cancelled_registration.txt", reg=reg, reason=reason,
                by_self=by_self, **info)
    reg.refund_info = info
    if was_registered:
        _promote_reserve(event, by)
    return reg


@transaction.atomic
def cancel_event(event, by, reason, refund=True):
    if not reason.strip():
        raise ValidationError("Podaj powód odwołania — uczestnicy dostaną go e-mailem.")
    event = Event.objects.select_for_update().get(pk=event.pk)
    event.status = Event.Status.CANCELLED
    event.save(update_fields=["status"])
    _sync_block(event, by)
    affected = list(event.registrations.exclude(status=Registration.Status.CANCELLED).select_related("user"))
    from apps.payments.services import refund_registration

    for reg in affected:
        reg.status = Registration.Status.CANCELLED
        reg.cancelled_at = timezone.now()
        reg.save(update_fields=["status", "cancelled_at"])
        paid = reg.paid_amount
        refund_registration(reg, by=by)
        info = _refund_paid(reg, paid, by, f"Odwołano wydarzenie: {event.title}", refund)
        notify_user(reg.user, f"Odwołano: {event.title}", "emails/event_cancelled.txt", reg=reg, reason=reason, **info)
    log_event("wydarzenie.odwolanie", event, str(event), changes={"powod": reason, "uczestnicy": len(affected), "zwrot_tpay": refund}, actor=by)
    url = reverse("panel:event", args=[event.pk])
    notify_board(f"Odwołano wydarzenie: {event.title}", "emails/board_generic.txt", PERM, url=url,
                 body=f"Powód: {reason}. Uczestników: {len(affected)}.",
                 event=f"Odwołano wydarzenie: {event.title}", text=f"Powód: {reason}. Uczestników: {len(affected)}.", url_path=url)
    return event
