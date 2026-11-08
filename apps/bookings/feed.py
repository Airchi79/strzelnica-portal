"""Kalendarz do subskrypcji (iCalendar) dla zarządu: rezerwacje, blokady i wydarzenia — bez danych osobowych."""

import hashlib
import secrets
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .models import Block, CalendarFeed, Reservation

PERM = "bookings.manage_reservations"
PAST_DAYS, FUTURE_DAYS = 30, 180


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(user):
    """Tworzy nowy link (poprzedni przestaje działać). Zwraca token — pokazywany tylko raz."""
    token = secrets.token_urlsafe(32)
    CalendarFeed.objects.update_or_create(user=user, defaults={"token_hash": _hash(token), "last_used_at": None})
    return token


def user_for_token(token):
    feed = CalendarFeed.objects.select_related("user").filter(token_hash=_hash(token or "")).first()
    if feed is None or not feed.user.is_active or not feed.user.is_staff or not feed.user.has_perm(PERM):
        return None
    feed.last_used_at = timezone.now()
    feed.save(update_fields=["last_used_at"])
    return feed.user


def _esc(text):
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def build(now=None):
    now = now or timezone.now()
    lo, hi = now - timedelta(days=PAST_DAYS), now + timedelta(days=FUTURE_DAYS)
    fmt = "%Y%m%dT%H%M%SZ"
    utc = lambda dt: dt.astimezone(dt_timezone.utc).strftime(fmt)  # noqa: E731
    host = settings.SITE_URL.split("//")[-1].split("/")[0] or "strzelnica"
    base = settings.SITE_URL
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Strzelnica Satok//Panel//PL", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:Strzelnica Sątok — zajętość", "X-WR-TIMEZONE:Europe/Warsaw", "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
             "X-PUBLISHED-TTL:PT1H"]

    def event(uid, start, end, summary, description, url, status="CONFIRMED"):
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}@{host}", f"DTSTAMP:{utc(now)}", f"DTSTART:{utc(start)}", f"DTEND:{utc(end)}",
                      f"SUMMARY:{_esc(summary)}", f"DESCRIPTION:{_esc(description)}", f"URL:{url}", f"STATUS:{status}", "END:VEVENT"])

    reservations = (Reservation.objects.filter(starts_at__lt=hi, ends_at__gt=lo)
                    .exclude(status__in=[Reservation.Status.CANCELLED, Reservation.Status.NO_SHOW]).select_related("lane"))
    for r in reservations:
        pending = r.status == Reservation.Status.PENDING_APPROVAL
        paid = "" if r.payment_status != Reservation.Payment.DUE else " · do zapłaty"
        event(f"rez-{r.pk}", r.starts_at, r.ends_at, f"{r.lane} — {r.persons} os.{' (do zatwierdzenia)' if pending else ''}",
              f"Rezerwacja {r.number}{paid}. Szczegóły w panelu (wymaga logowania).", base + reverse("panel:reservation", args=[r.pk]),
              "TENTATIVE" if pending else "CONFIRMED")
    for b in Block.objects.filter(starts_at__lt=hi, ends_at__gt=lo).prefetch_related("lanes"):
        lanes = ", ".join(lane.name for lane in b.lanes.all())
        event(f"blok-{b.pk}", b.starts_at, b.ends_at, f"Blokada: {b.title}", f"Osie: {lanes}", base + reverse("panel:blocks"))
    from apps.events.models import Event

    for e in Event.objects.filter(status=Event.Status.PUBLISHED, starts_at__lt=hi, ends_at__gt=lo):
        event(f"wyd-{e.pk}", e.starts_at, e.ends_at, f"Wydarzenie: {e.title}", f"Zapisani: {e.taken()}"
              + (f" / {e.capacity}" if e.capacity else ""), base + reverse("panel:event", args=[e.pk]))
    lines.append("END:VCALENDAR")
    # zawijanie linii > 75 znaków (RFC 5545)
    out = []
    for line in lines:
        while len(line.encode()) > 74:
            cut = 74
            while len(line[:cut].encode()) > 74:
                cut -= 1
            out.append(line[:cut])
            line = " " + line[cut:]
        out.append(line)
    return "\r\n".join(out) + "\r\n"
