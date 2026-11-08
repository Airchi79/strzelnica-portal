"""Ogłoszenia: aktywne do wyświetlenia, odbiorcy e-maila, wysyłka w tle (komenda send_announcements z harmonogramu)."""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import get_connection, send_mail
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Announcement

logger = logging.getLogger(__name__)
BATCH = 200  # najwięcej e-maili na jedno uruchomienie (co 5 minut)


def active(now=None):
    now = now or timezone.now()
    return Announcement.objects.filter(visible_from__lte=now).filter(Q(visible_until__isnull=True) | Q(visible_until__gt=now))


def recipients(a):
    from django.contrib.auth import get_user_model

    from apps.bookings.models import Reservation
    from apps.bookings.services import local_dt

    User = get_user_model()
    qs = User.objects.filter(is_active=True, email_verified_at__isnull=False)
    if a.email_audience == Announcement.Audience.BOOKED:
        if not (a.booked_from and a.booked_to):
            return User.objects.none()
        ids = Reservation.objects.filter(
            starts_at__gte=local_dt(a.booked_from, 0), starts_at__lt=local_dt(a.booked_to + timedelta(days=1), 0),
            status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING_APPROVAL, Reservation.Status.HOLD],
        ).values("user_id")
        qs = qs.filter(pk__in=ids)
    elif a.email_audience != Announcement.Audience.ALL:
        return User.objects.none()
    return qs.order_by("pk")


def send_queued():
    """Wysyła e-maile ogłoszeń oczekujących w kolejce (po jednym ogłoszeniu w partii). Zwraca liczbę wysłanych."""
    a = Announcement.objects.filter(email_queued=True).order_by("created_at").first()
    if a is None:
        return 0
    done = set(a.deliveries.values_list("user_id", flat=True))
    todo = [u for u in recipients(a) if u.pk not in done][:BATCH]
    sent = 0
    connection = get_connection()
    for u in todo:
        body = render_to_string("emails/announcement.txt", {"user": u, "a": a, "site_url": settings.SITE_URL})
        try:
            send_mail(f"[Strzelnica Sątok] {a.title}", body, settings.DEFAULT_FROM_EMAIL, [u.email], connection=connection)
        except Exception:  # noqa: BLE001 — jeden zły adres nie zatrzymuje wysyłki
            logger.exception("Ogłoszenie %s: nie wysłano do %s", a.pk, u.email)
        a.deliveries.create(user=u)
        sent += 1
    if len(todo) < BATCH:
        a.email_queued = False
        a.emailed_at = timezone.now()
    a.emailed_count = a.deliveries.count()
    a.save(update_fields=["email_queued", "emailed_at", "emailed_count"])
    return sent
