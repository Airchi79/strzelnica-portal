"""„Teraz na strzelnicy”: stan osi w tej chwili (publicznie, bez danych osobowych) i — dla zarządu — kto jest na terenie
według książki pobytu oraz czy ma do tego rezerwację."""

from datetime import timedelta

from django.utils import timezone

ENTRY_MARGIN = timedelta(minutes=30)  # wpis do rezerwacji możliwy 30 min przed jej początkiem


def lanes_now(now=None):
    """Lista osi: stan (zajeta / wolna / blokada / nieczynna), do kiedy, następna rezerwacja dziś."""
    from apps.bookings.models import Block, Reservation
    from apps.bookings.services import local_dt
    from apps.facilities.models import Lane

    now = now or timezone.now()
    today = timezone.localdate(now)
    day_end = local_dt(today + timedelta(days=1), 0)
    active = Reservation.objects.exclude(status__in=[Reservation.Status.CANCELLED, Reservation.Status.NO_SHOW])
    rows = []
    for lane in Lane.objects.filter(is_active=True, facility__is_active=True).select_related("facility").order_by("facility__order", "order"):
        hours = lane.hours_for(today)
        current = active.filter(lane=lane, starts_at__lte=now, ends_at__gt=now).select_related("user").first()
        block = Block.objects.filter(lanes=lane, starts_at__lte=now, ends_at__gt=now).first()
        later = list(active.filter(lane=lane, starts_at__gt=now, starts_at__lt=day_end).order_by("starts_at")[:4])
        opens = local_dt(today, hours.opens.hour) if hours else None
        closes = local_dt(today, hours.closes.hour) if hours else None
        if block:
            state, until = "blokada", block.ends_at
        elif current:
            state, until = "zajeta", current.ends_at
        elif hours is None or not (opens <= now < closes):
            state, until = "nieczynna", None
        else:
            state = "wolna"
            until = min([r.starts_at for r in later] + [closes])
        rows.append({"lane": lane, "state": state, "until": until, "current": current, "block": block, "later": later,
                     "hours": hours, "opens": opens, "closes": closes})
    return rows


def present(now=None):
    """Osoby na terenie (wpis bez wyjścia, pobyt jeszcze się nie zamknął) z oceną: ok / po_czasie / bez_rezerwacji / prace / info."""
    from apps.bookings.models import Reservation

    from .models import Entry
    from .services import auto_close_at

    now = now or timezone.now()
    active_res = list(Reservation.objects.filter(status=Reservation.Status.CONFIRMED, starts_at__lte=now + ENTRY_MARGIN, ends_at__gt=now)
                      .select_related("lane", "user"))
    out = []
    for e in Entry.objects.filter(exited_at__isnull=True).select_related("reservation__lane", "reservation__user", "reservation__officer", "range_officer").order_by("entered_at"):
        close = auto_close_at(e)
        if close and close <= now:
            continue  # zamknie go najbliższe automatyczne wypisanie
        r = e.reservation
        if r and r.starts_at - ENTRY_MARGIN <= now < r.ends_at and r.status == Reservation.Status.CONFIRMED:
            verdict, note = "ok", f"rezerwacja {r.lane} do {timezone.localtime(r.ends_at):%H:%M}"
        elif r and now >= r.ends_at:
            verdict, note = "po_czasie", f"rezerwacja {r.lane} skończyła się o {timezone.localtime(r.ends_at):%H:%M}"
        elif r:
            verdict, note = "po_czasie", f"rezerwacja {r.number}: {r.get_status_display().lower()}"
        elif e.purpose == Entry.Purpose.WORK:
            verdict, note = "prace", e.purpose_note or "prace / obsługa strzelnicy"
        elif active_res:
            verdict, note = "info", (e.purpose_note or "bez rezerwacji") + " · trwają rezerwacje: " + ", ".join(str(x.lane) for x in active_res)
        else:
            verdict, note = "bez_rezerwacji", "na terenie, a nikt nie ma teraz rezerwacji"
        out.append({"entry": e, "verdict": verdict, "note": note, "close_at": close})
    return out


def summary(now=None):
    now = now or timezone.now()
    people = present(now)
    lanes = lanes_now(now)
    from .models import Entry

    for row in lanes:
        r = row["current"]
        if r:
            row["shooters"] = sum(1 for p in people if p["entry"].reservation_id == r.pk and p["entry"].purpose == Entry.Purpose.SHOOTING)
            row["nobody"] = row["shooters"] == 0 and now - r.starts_at > timedelta(minutes=15)
    return {
        "now": now, "lanes": lanes, "people": people,
        "alarms": sum(1 for p in people if p["verdict"] == "bez_rezerwacji"),
        "warnings": sum(1 for p in people if p["verdict"] == "po_czasie") + sum(1 for row in lanes if row.get("nobody")),
    }
