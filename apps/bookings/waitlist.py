"""Lista oczekujących na zajęte terminy osi."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.audit.services import log_event
from apps.core.notifications import notify_user

from .models import Block, BookingSettings, Reservation, WaitlistEntry

MAX_ACTIVE_PER_USER = 5


def active_holds(lane, starts_at, ends_at, now=None, exclude_user=None):
    now = now or timezone.now()
    qs = WaitlistEntry.objects.filter(lane=lane, status=WaitlistEntry.Status.OFFERED, hold_until__gt=now,
                                      starts_at__lt=ends_at, ends_at__gt=starts_at)
    if exclude_user is not None:
        qs = qs.exclude(user=exclude_user)
    return qs


def _range_free(lane, starts_at, ends_at, now, for_user=None):
    busy = Reservation.objects.filter(lane=lane, starts_at__lt=ends_at, ends_at__gt=starts_at).exclude(
        status__in=[Reservation.Status.CANCELLED, Reservation.Status.NO_SHOW]).exists()
    blocked = Block.objects.filter(lanes=lane, starts_at__lt=ends_at, ends_at__gt=starts_at).exists()
    held = active_holds(lane, starts_at, ends_at, now, exclude_user=for_user).exists()
    return not (busy or blocked or held)


def join(user, lane, day, start_hour, end_hour, persons=1, now=None):
    from .services import local_dt

    now = now or timezone.now()
    starts_at, ends_at = local_dt(day, start_hour), local_dt(day, end_hour)
    hours = lane.hours_for(day)
    if hours is None or start_hour < hours.opens.hour or end_hour > hours.closes.hour or end_hour <= start_hour:
        raise ValidationError("Oś nie jest czynna w wybranych godzinach.")
    if starts_at <= now:
        raise ValidationError("Ten termin już minął.")
    if _range_free(lane, starts_at, ends_at, now, for_user=user):
        raise ValidationError("Ten termin jest wolny — możesz go od razu zarezerwować.")
    mine = WaitlistEntry.objects.filter(user=user, status__in=WaitlistEntry.ACTIVE)
    if mine.filter(lane=lane, starts_at=starts_at, ends_at=ends_at).exists():
        raise ValidationError("Już czekasz na ten termin.")
    if mine.filter(starts_at__gt=now).count() >= MAX_ACTIVE_PER_USER:
        raise ValidationError(f"Możesz czekać najwyżej na {MAX_ACTIVE_PER_USER} terminów naraz.")
    entry = WaitlistEntry.objects.create(user=user, lane=lane, starts_at=starts_at, ends_at=ends_at, persons=persons)
    log_event("lista_oczekujacych.zapis", entry, str(entry), actor=user)
    return entry


@transaction.atomic
def offer_freed(lane, starts_at, ends_at, now=None):
    """Po zwolnieniu fragmentu osi: oferta dla pierwszej osoby, której cały oczekiwany zakres jest teraz wolny."""
    now = now or timezone.now()
    candidates = (WaitlistEntry.objects.select_for_update().select_related("user", "lane")
                  .filter(lane=lane, status=WaitlistEntry.Status.WAITING, starts_at__lt=ends_at, ends_at__gt=starts_at, starts_at__gt=now)
                  .order_by("created_at", "pk"))
    hold = BookingSettings.load().waitlist_hold_minutes
    offered = []
    for entry in candidates:
        if not _range_free(entry.lane, entry.starts_at, entry.ends_at, now):
            continue
        entry.status = WaitlistEntry.Status.OFFERED
        entry.offered_at = now
        entry.hold_until = min(now + timedelta(minutes=hold), entry.starts_at)
        entry.save(update_fields=["status", "offered_at", "hold_until"])
        log_event("lista_oczekujacych.oferta", entry, str(entry))
        local = timezone.localtime(entry.starts_at)
        link = reverse("bookings:new") + f"?os={entry.lane_id}&dzien={local:%Y-%m-%d}&od={local.hour}&do={timezone.localtime(entry.ends_at).hour}"
        notify_user(entry.user, f"Zwolnił się termin: {entry.lane}, {local:%d.%m %H:%M}", "emails/waitlist_offer.txt",
                    entry=entry, link=link)
        offered.append(entry)
        # kolejne osoby z kolizyjnymi zakresami poczekają — termin jest już trzymany
    return offered


def mark_booked(reservation):
    """Rezerwacja osoby z listy realizuje jej oczekiwanie na ten termin."""
    WaitlistEntry.objects.filter(
        user=reservation.user, lane=reservation.lane, status__in=WaitlistEntry.ACTIVE,
        starts_at__lt=reservation.ends_at, ends_at__gt=reservation.starts_at,
    ).update(status=WaitlistEntry.Status.DONE, hold_until=None)


def process(now=None):
    """Harmonogram: wygasza nieodebrane oferty i proponuje termin kolejnym osobom; czyści minione wpisy."""
    now = now or timezone.now()
    expired = list(WaitlistEntry.objects.filter(status=WaitlistEntry.Status.OFFERED, hold_until__lte=now))
    for entry in expired:
        entry.status = WaitlistEntry.Status.EXPIRED
        entry.save(update_fields=["status"])
        offer_freed(entry.lane, entry.starts_at, entry.ends_at, now)
    WaitlistEntry.objects.filter(status=WaitlistEntry.Status.WAITING, starts_at__lte=now).update(status=WaitlistEntry.Status.EXPIRED)
    return len(expired)


def held_ranges(lane, day_start, day_end, now=None, exclude_user=None):
    return list(active_holds(lane, day_start, day_end, now, exclude_user).values_list("starts_at", "ends_at"))
