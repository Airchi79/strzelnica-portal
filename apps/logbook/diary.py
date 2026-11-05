"""Mój dziennik strzelca: wizyty z książki pobytu (także archiwum GunTracker — tylko przy zgodnym numerze pozwolenia),
rezerwacje jako prowadzący, udział w wydarzeniach i wydatki w danym roku."""

from datetime import datetime

from django.db.models import Q, Sum
from django.utils import timezone

MONTHS = ["sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz", "paź", "lis", "gru"]


def _norm(v):
    return "".join(ch for ch in (v or "").upper() if ch.isalnum())


def year_bounds(year):
    tz = timezone.get_current_timezone()
    return datetime(year, 1, 1, tzinfo=tz), datetime(year + 1, 1, 1, tzinfo=tz)


def visits(user, year):
    from apps.credentials.models import Credential

    from .models import ArchivedEntry, Entry

    start, end = year_bounds(year)
    rows = []
    for e in Entry.objects.filter(user=user, entered_at__gte=start, entered_at__lt=end).select_related("reservation__lane", "range_officer"):
        minutes = int((e.exited_at - e.entered_at).total_seconds() // 60) if e.exited_at else None
        rows.append({"at": e.entered_at, "end": e.exited_at, "minutes": minutes, "purpose": e.get_purpose_display(),
                     "shooting": e.purpose == Entry.Purpose.SHOOTING, "lane": str(e.reservation.lane) if e.reservation_id else "",
                     "officer": e.range_officer.full_name if e.range_officer_id else "", "source": "książka"})
    numbers = {_norm(n) for n in Credential.objects.filter(user=user).values_list("number", flat=True)} - {""}
    if numbers:  # archiwum: tylko wpisy z tym samym imieniem, nazwiskiem i numerem pozwolenia
        arch = ArchivedEntry.objects.filter(first_name__iexact=user.first_name, last_name__iexact=user.last_name,
                                            started_at__gte=start, started_at__lt=end)
        for a in arch:
            if _norm(a.permit_number) not in numbers:
                continue
            rows.append({"at": a.started_at, "end": a.ended_at, "minutes": a.duration_minutes, "purpose": a.entry_type or "strzelanie",
                         "shooting": True, "lane": a.lane, "officer": "", "source": "archiwum"})
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows


def summary(user, year):
    from apps.bookings.models import Reservation
    from apps.events.models import Registration
    from apps.payments.models import LedgerEntry

    start, end = year_bounds(year)
    v = visits(user, year)
    days = {timezone.localtime(r["at"]).date() for r in v}
    by_month = [0] * 12
    for d in days:
        by_month[d.month - 1] += 1
    res = list(Reservation.objects.filter(user=user, starts_at__gte=start, starts_at__lt=end)
               .exclude(status__in=[Reservation.Status.CANCELLED, Reservation.Status.NO_SHOW]).select_related("lane"))
    lanes = {}
    for r in res:
        lanes[str(r.lane)] = lanes.get(str(r.lane), 0) + r.hours
    spent = -(LedgerEntry.objects.filter(user=user, created_at__gte=start, created_at__lt=end, amount__lt=0)
              .exclude(kind=LedgerEntry.Kind.CHARGEBACK).aggregate(s=Sum("amount"))["s"] or 0)
    refunds = LedgerEntry.objects.filter(user=user, created_at__gte=start, created_at__lt=end, kind=LedgerEntry.Kind.REFUND).aggregate(s=Sum("amount"))["s"] or 0
    events = list(Registration.objects.filter(user=user, status=Registration.Status.REGISTERED, event__starts_at__gte=start,
                                              event__starts_at__lt=end).select_related("event").order_by("event__starts_at"))
    peak = max(by_month) or 1
    return {
        "year": year, "visits": v, "days": len(days), "shooting_days": len({timezone.localtime(r["at"]).date() for r in v if r["shooting"]}),
        "minutes": sum(r["minutes"] or 0 for r in v),
        "months": [(MONTHS[i], n, round(100 * n / peak)) for i, n in enumerate(by_month)],
        "reservations": len(res), "res_hours": sum(r.hours for r in res), "lanes": sorted(lanes.items(), key=lambda x: -x[1]),
        "spent": max(spent - refunds, 0), "events": events,
        "has_archive": any(r["source"] == "archiwum" for r in v),
    }
