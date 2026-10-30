"""Reguły rezerwacji osi (decyzje właściciela z 30.10.2026, specyfikacja rozdz. 5 i 9)."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.audit.services import log_event
from apps.core.notifications import notify_board, notify_user
from apps.credentials.models import is_range_officer
from apps.regulations.models import pending_acceptances

from apps.payments.services import apply_wallet, reconcile_reservation, refund_reservation

from .models import Block, BookingSettings, NumberSequence, Reservation

PERM = "bookings.manage_reservations"


def local_dt(day: date, hour: int) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour)))


def hour_floor(dt: datetime) -> datetime:
    return timezone.localtime(dt).replace(minute=0, second=0, microsecond=0)


@dataclass
class Slot:
    start: datetime
    end: datetime
    state: str  # wolne | zajete | blokada | minione
    reservation: Reservation | None = None
    block: Block | None = None

    @property
    def hour(self):
        return timezone.localtime(self.start).hour

    @property
    def label(self):
        return f"{timezone.localtime(self.start):%H:%M}–{timezone.localtime(self.end):%H:%M}"


def day_slots(lane, day: date, now=None):
    """Godzinowe sloty osi w danym dniu, zgodnie z godzinami otwarcia osi."""
    now = now or timezone.now()
    hours = lane.hours_for(day)
    if hours is None or not lane.is_active:
        return []
    day_start, day_end = local_dt(day, hours.opens.hour), local_dt(day, hours.closes.hour)
    reservations = list(
        Reservation.objects.filter(lane=lane, starts_at__lt=day_end, ends_at__gt=day_start)
        .exclude(status=Reservation.Status.CANCELLED)
        .select_related("user")
    )
    blocks = list(Block.objects.filter(lanes=lane, starts_at__lt=day_end, ends_at__gt=day_start))
    current_hour = hour_floor(now)
    slots = []
    for h in range(hours.opens.hour, hours.closes.hour):
        start, end = local_dt(day, h), local_dt(day, h + 1)
        res = next((r for r in reservations if r.starts_at < end and r.ends_at > start), None)
        blk = next((b for b in blocks if b.starts_at < end and b.ends_at > start), None)
        if blk:
            state = "blokada"
        elif res:
            state = "zajete"
        elif start < current_hour:
            state = "minione"
        else:
            state = "wolne"
        slots.append(Slot(start, end, state, res, blk))
    return slots


def check_can_book(user):
    """Zwraca listę powodów, dla których użytkownik nie może rezerwować (pusta = może)."""
    reasons = []
    if not user.is_authenticated or not user.is_active:
        return ["Zaloguj się."]
    if not user.email_verified:
        reasons.append("Potwierdź adres e-mail.")
    if not is_range_officer(user):
        reasons.append("Rezerwować może tylko osoba ze zweryfikowanymi uprawnieniami prowadzącego strzelanie.")
    if pending_acceptances(user):
        reasons.append("Zaakceptuj aktualne regulaminy.")
    return reasons


def validate_request(lane, day: date, start_hour: int, end_hour: int, persons: int, now=None):
    """Sprawdza termin i liczbę osób. Zwraca (starts_at, ends_at, price_rule)."""
    now = now or timezone.now()
    cfg = BookingSettings.load()
    if not lane.is_active or not lane.facility.is_active:
        raise ValidationError("Ta oś jest obecnie niedostępna.")
    if persons < 1 or persons > cfg.max_persons:
        raise ValidationError(f"Liczba osób musi wynosić od 1 do {cfg.max_persons}.")
    if end_hour <= start_hour:
        raise ValidationError("Koniec rezerwacji musi być później niż początek.")
    hours = lane.hours_for(day)
    if hours is None:
        raise ValidationError("Oś jest tego dnia zamknięta.")
    if start_hour < hours.opens.hour or end_hour > hours.closes.hour:
        raise ValidationError(f"Oś jest czynna w godzinach {hours.opens:%H:%M}–{hours.closes:%H:%M}.")
    starts_at, ends_at = local_dt(day, start_hour), local_dt(day, end_hour)
    # Rezerwować można do chwili rozpoczęcia — także bieżącą, już rozpoczętą godzinę (rezerwacja na miejscu).
    if starts_at < hour_floor(now):
        raise ValidationError("Nie można rezerwować terminu, który już minął.")
    if day > timezone.localdate(now) + timedelta(days=cfg.max_advance_days):
        raise ValidationError(f"Rezerwować można najwyżej {cfg.max_advance_days} dni naprzód.")
    if Block.objects.filter(lanes=lane, starts_at__lt=ends_at, ends_at__gt=starts_at).exists():
        raise ValidationError("W wybranym czasie oś jest zablokowana przez zarząd.")
    rule = lane.price_rule_for(day)
    if rule is None:
        raise ValidationError("Dla tej osi nie ustalono cennika. Skontaktuj się z zarządem.")
    return starts_at, ends_at, rule


def quote(lane, day, start_hour, end_hour, persons, now=None):
    starts_at, ends_at, rule = validate_request(lane, day, start_hour, end_hour, persons, now)
    hourly = rule.hourly_amount(persons)
    return {"starts_at": starts_at, "ends_at": ends_at, "rule": rule, "hourly": hourly, "amount": hourly * (end_hour - start_hour)}


def _next_number(year):
    NumberSequence.objects.bulk_create([NumberSequence(year=year)], ignore_conflicts=True)
    seq = NumberSequence.objects.select_for_update().get(year=year)
    seq.last += 1
    seq.save(update_fields=["last"])
    return f"SATOK-R-{year}-{seq.last:04d}"


def create_reservation(user, lane, day, start_hour, end_hour, persons, notes="", created_by=None, now=None):
    reasons = check_can_book(user)
    if reasons:
        raise ValidationError(reasons)
    q = quote(lane, day, start_hour, end_hour, persons, now)
    try:
        with transaction.atomic():
            reservation = Reservation.objects.create(
                number=_next_number(day.year),
                lane=lane,
                user=user,
                starts_at=q["starts_at"],
                ends_at=q["ends_at"],
                persons=persons,
                notes=notes.strip()[:500],
                price_rule=q["rule"],
                hourly_amount=q["hourly"],
                amount=q["amount"],
                created_by=created_by or user,
            )
    except IntegrityError as e:
        if "reservation_no_overlap" in str(e):
            raise ValidationError("Ten termin został właśnie zajęty. Wybierz inną godzinę.") from e
        raise
    log_event(
        "rezerwacja.utworzenie",
        reservation,
        str(reservation),
        changes={"osoby": persons, "kwota_gr": reservation.amount, "dla": user.email},
        actor=created_by or user,
    )
    # Opłata pobierana od razu z salda, jeśli wystarcza środków (w przeciwnym razie należność „do zapłaty”).
    apply_wallet(user, by=created_by)
    reservation.refresh_from_db()
    notify_user(user, f"Rezerwacja {reservation.number} potwierdzona", "emails/reservation_created.txt", reservation=reservation)
    notify_board(
        f"Nowa rezerwacja: {reservation.lane}, {timezone.localtime(reservation.starts_at):%d.%m %H:%M}",
        "emails/reservation_board.txt",
        PERM,
        url=reverse("panel:reservation", args=[reservation.pk]),
        body=f"{user.full_name}, {persons} os.",
        reservation=reservation,
        event="Nowa rezerwacja",
    )
    return reservation


@transaction.atomic
def cancel_reservation(reservation, by, reason="", as_board=False, now=None):
    """Anulowanie. Strzelec: bezpłatnie do terminu z ustawień (48 h), później należność pozostaje do zapłaty.
    Zarząd: anulowanie zawsze bez opłaty."""
    now = now or timezone.now()
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status != Reservation.Status.CONFIRMED:
        raise ValidationError("Tej rezerwacji nie można już anulować.")
    if not as_board:
        if reservation.user_id != by.pk:
            raise ValidationError("To nie jest Twoja rezerwacja.")
        if not reservation.can_user_cancel(now):
            raise ValidationError("Rezerwacja już się rozpoczęła — anulować może tylko zarząd.")
    late = not as_board and now > reservation.free_cancel_deadline
    reservation.status = Reservation.Status.CANCELLED
    reservation.cancelled_at = now
    reservation.cancelled_by = by
    reservation.cancel_reason = reason.strip()[:255]
    reservation.late_cancellation = late
    if not late:
        reservation.payment_status = Reservation.Payment.NONE
    reservation.save()
    if not late:
        reservation = refund_reservation(reservation, by=by)
    log_event(
        "rezerwacja.anulowanie",
        reservation,
        str(reservation),
        changes={"po_terminie": late, "przez_zarzad": as_board, "powod": reservation.cancel_reason},
        actor=by,
    )
    notify_user(reservation.user, f"Rezerwacja {reservation.number} anulowana", "emails/reservation_cancelled.txt", reservation=reservation)
    notify_board(
        f"Anulowano rezerwację: {reservation.lane}, {timezone.localtime(reservation.starts_at):%d.%m %H:%M}",
        "emails/reservation_board.txt",
        PERM,
        url=reverse("panel:reservation", args=[reservation.pk]),
        body=f"{reservation.user.full_name}" + (" — po terminie bezpłatnego anulowania" if late else ""),
        reservation=reservation,
        event="Anulowanie rezerwacji",
    )
    return reservation


@transaction.atomic
def change_persons(reservation, persons, by):
    """Zmiana liczby osób przez zarząd — należność liczona według cennika z chwili rezerwacji."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status != Reservation.Status.CONFIRMED:
        raise ValidationError("Można zmieniać tylko aktywne rezerwacje.")
    if persons < 1 or persons > BookingSettings.load().max_persons:
        raise ValidationError("Nieprawidłowa liczba osób.")
    before = (reservation.persons, reservation.amount)
    reservation.persons = persons
    reservation.hourly_amount = reservation.price_rule.hourly_amount(persons)
    reservation.amount = reservation.hourly_amount * reservation.hours
    reservation.save()
    reservation = reconcile_reservation(reservation, by=by)
    log_event(
        "rezerwacja.zmiana_osob",
        reservation,
        str(reservation),
        changes={"osoby": [before[0], persons], "kwota_gr": [before[1], reservation.amount]},
        actor=by,
    )
    notify_user(reservation.user, f"Zmiana rezerwacji {reservation.number}", "emails/reservation_changed.txt", reservation=reservation)
    return reservation


PAYMENT_TRANSITIONS = {
    "waive": (Reservation.Payment.WAIVED, {Reservation.Payment.DUE}),
    "unwaive": (Reservation.Payment.DUE, {Reservation.Payment.WAIVED}),
}


@transaction.atomic
def set_payment(reservation, action, by):
    """Zwolnienie z (pozostałej) opłaty lub jego cofnięcie. Wpłaty przyjmuje się w module płatności."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    target, allowed_from = PAYMENT_TRANSITIONS[action]
    if reservation.payment_status not in allowed_from:
        raise ValidationError("Tej zmiany płatności nie można wykonać w obecnym stanie.")
    before = reservation.payment_status
    reservation.payment_status = target
    reservation.payment_marked_by = by
    reservation.payment_marked_at = timezone.now()
    reservation.save()
    log_event("rezerwacja.platnosc", reservation, str(reservation), changes={"platnosc": [before, target]}, actor=by)
    if target == Reservation.Payment.DUE:
        apply_wallet(reservation.user, by=by)
        reservation.refresh_from_db()
    return reservation


@transaction.atomic
def mark_no_show(reservation, by):
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status != Reservation.Status.CONFIRMED or reservation.starts_at > timezone.now():
        raise ValidationError("Nieodbytą można oznaczyć tylko rozpoczętą, aktywną rezerwację.")
    reservation.status = Reservation.Status.NO_SHOW
    reservation.save()
    log_event("rezerwacja.nieodbyta", reservation, str(reservation), actor=by)
    return reservation


# --- blokady ----------------------------------------------------------------


def block_occurrences(day_from: date, day_to: date, start_hour: int, end_hour: int, weekly: bool):
    """Przedziały czasu blokady. Jednorazowa: od day_from start_hour do day_to end_hour.
    Cykliczna (co tydzień): w dniu tygodnia day_from, od day_from do day_to, w godzinach start–end."""
    if not weekly:
        return [(local_dt(day_from, start_hour), local_dt(day_to, end_hour) if end_hour < 24 else local_dt(day_to + timedelta(days=1), 0))]
    out, d = [], day_from
    while d <= day_to:
        out.append((local_dt(d, start_hour), local_dt(d, end_hour) if end_hour < 24 else local_dt(d + timedelta(days=1), 0)))
        d += timedelta(days=7)
    return out


def block_conflicts(lanes, occurrences):
    q = Q()
    for start, end in occurrences:
        q |= Q(starts_at__lt=end, ends_at__gt=start)
    return (
        Reservation.objects.filter(lane__in=lanes, status=Reservation.Status.CONFIRMED)
        .filter(q)
        .select_related("lane", "user")
        .order_by("starts_at")
    )


@transaction.atomic
def create_blocks(title, reason, lanes, occurrences, by, cancel_conflicts):
    conflicts = list(block_conflicts(lanes, occurrences))
    blocks = []
    series = None
    for start, end in occurrences:
        block = Block.objects.create(title=title, reason=reason, starts_at=start, ends_at=end, created_by=by, **({"series": series} if series else {}))
        series = block.series
        block.lanes.set(lanes)
        blocks.append(block)
    log_event(
        "blokada.utworzenie",
        blocks[0],
        f"{title} ({len(blocks)} termin(y))",
        changes={"osie": [str(lane) for lane in lanes], "kolizje": [r.number for r in conflicts], "anulowano_kolizje": cancel_conflicts},
        actor=by,
    )
    cancelled = []
    if cancel_conflicts:
        for r in conflicts:
            cancelled.append(cancel_reservation(r, by, reason=f"Blokada osi: {title}", as_board=True))
    else:
        for r in conflicts:
            notify_user(r.user, f"Blokada osi w terminie rezerwacji {r.number}", "emails/reservation_block_conflict.txt", reservation=r, title=title)
    return blocks, conflicts, cancelled


@transaction.atomic
def delete_block_series(block, by, whole_series=False):
    qs = Block.objects.filter(series=block.series) if whole_series else Block.objects.filter(pk=block.pk)
    count = qs.count()
    log_event("blokada.usuniecie", summary=f"{block.title} ({count} termin(y))", actor=by)
    qs.delete()
    return count


def send_reminders(now=None):
    """Przypomnienia e-mail dla rezerwacji zaczynających się w ciągu najbliższych 24 h."""
    now = now or timezone.now()
    qs = Reservation.objects.filter(
        status=Reservation.Status.CONFIRMED, reminder_sent_at__isnull=True, starts_at__gt=now, starts_at__lte=now + timedelta(hours=24)
    ).select_related("user", "lane")
    count = 0
    for r in qs:
        with transaction.atomic():
            notify_user(r.user, f"Przypomnienie: {r.lane}, {timezone.localtime(r.starts_at):%d.%m %H:%M}", "emails/reservation_reminder.txt", reservation=r)
            Reservation.objects.filter(pk=r.pk).update(reminder_sent_at=now)
        count += 1
    return count
