"""Reguły rezerwacji osi (decyzje właściciela z 30.10.2026, specyfikacja rozdz. 5 i 9)."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.conf import settings as dj_settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.audit.services import log_event
from apps.core.notifications import notify_board, notify_user
from apps.credentials.models import is_range_officer
from apps.regulations.models import pending_acceptances

from apps.payments.services import apply_wallet, reconcile_reservation, refund_reservation, use_wallet_for_reservation

from .models import Block, BookingSettings, GroupRule, NumberSequence, Reservation

PERM = "bookings.manage_reservations"


def local_dt(day: date, hour: int) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour)))


def hour_floor(dt: datetime) -> datetime:
    return timezone.localtime(dt).replace(minute=0, second=0, microsecond=0)


@dataclass
class Slot:
    start: datetime
    end: datetime
    state: str  # wolne | zajete | blokada | minione | za_pozno (za krótko przed terminem na rezerwację online)
    reservation: Reservation | None = None
    block: Block | None = None

    @property
    def hour(self):
        return timezone.localtime(self.start).hour

    @property
    def label(self):
        return f"{timezone.localtime(self.start):%H:%M}–{timezone.localtime(self.end):%H:%M}"


def day_slots(lane, day: date, now=None, lead_hours=0, holds=False, viewer=None):
    """Godzinowe sloty osi w danym dniu, zgodnie z godzinami otwarcia osi.
    `lead_hours` > 0 (widok strzelca): wolne godziny zaczynające się wcześniej niż za tyle godzin mają stan „za_pozno”."""
    now = now or timezone.now()
    lead_limit = now + timedelta(hours=lead_hours) if lead_hours else None
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
    held = []
    if holds:  # widok strzelca: godziny trzymane dla kogoś z listy oczekujących są zajęte
        from .waitlist import held_ranges

        held = held_ranges(lane, day_start, day_end, now, exclude_user=viewer if viewer and viewer.is_authenticated else None)
    current_hour = hour_floor(now)
    slots = []
    for h in range(hours.opens.hour, hours.closes.hour):
        start, end = local_dt(day, h), local_dt(day, h + 1)
        res = next((r for r in reservations if r.starts_at < end and r.ends_at > start), None)
        blk = next((b for b in blocks if b.starts_at < end and b.ends_at > start), None)
        if blk:
            state = "blokada"
        elif res or any(a < end and b > start for a, b in held):
            state = "zajete"
        elif start < current_hour:
            state = "minione"
        elif lead_limit and start < lead_limit:
            state = "za_pozno"
        else:
            state = "wolne"
        slots.append(Slot(start, end, state, res, blk))
    return slots


def check_can_book(user, ps_pending=False):
    """Zwraca listę powodów, dla których użytkownik nie może rezerwować (pusta = może).
    `ps_pending` — rezerwacja na miejscu (kiosk): legitymacja PS dodana i czeka na weryfikację; rezerwacja powstaje jako
    „do zatwierdzenia” i zostanie potwierdzona dopiero po weryfikacji legitymacji."""
    reasons = []
    if not user.is_authenticated or not user.is_active:
        return ["Zaloguj się."]
    if not user.email_verified:
        reasons.append("Potwierdź adres e-mail.")
    if not ps_pending and not is_range_officer(user) and not BookingSettings.load().officer_service:
        reasons.append("Rezerwować może tylko osoba ze zweryfikowanymi uprawnieniami prowadzącego strzelanie.")
    if pending_acceptances(user):
        reasons.append("Zaakceptuj aktualne regulaminy.")
    return reasons


def booking_readiness(user):
    """Lista kontrolna „co jeszcze, żeby zarezerwować” — te same warunki co check_can_book, z opisem i działaniem.
    Stany: done (zrobione), missing (brakuje — blokuje), waiting (czeka na obsługę — blokuje), tip (zalecane, nie blokuje)."""
    from apps.credentials.models import Credential

    steps = []
    steps.append({
        "key": "email", "title": "Potwierdź adres e-mail",
        "state": "done" if user.email_verified else "missing",
        "detail": f"Potwierdzony: {user.email}" if user.email_verified else f"Kliknij link wysłany na {user.email} (sprawdź też SPAM).",
        "url": "" if user.email_verified else reverse("accounts:resend_verification"), "action": "Wyślij link ponownie",
    })
    pending = pending_acceptances(user)
    steps.append({
        "key": "rules", "title": "Zaakceptuj regulaminy",
        "state": "missing" if pending else "done",
        "detail": ("Do przeczytania i akceptacji: " + ", ".join(str(v.document) for v in pending)) if pending else "Zaakceptowane.",
        "url": reverse("regulations:accept") if pending else "", "action": "Przeczytaj i zaakceptuj",
    })
    ps = list(Credential.objects.filter(user=user, kind=Credential.Kind.RANGE_OFFICER))
    statuses = {c.effective_status for c in ps}
    if Credential.Status.VERIFIED in statuses:
        state, detail, url, action = "done", "Zweryfikowane przez obsługę strzelnicy.", "", ""
    elif Credential.Status.PENDING in statuses:
        state, detail, url, action = ("waiting", "Dokument czeka na weryfikację przez obsługę — dostaniesz e-mail po sprawdzeniu.",
                                      reverse("credentials:my_list"), "Moje dokumenty")
    elif statuses:
        state = "missing"
        detail = "Uprawnienia wygasły lub zostały odrzucone — dodaj aktualny dokument." if Credential.Status.EXPIRED in statuses or Credential.Status.REJECTED in statuses else "Dodaj dokument."
        url, action = reverse("credentials:my_add") + "?rodzaj=ps", "Dodaj uprawnienia PS"
    else:
        state, detail = "missing", ("Rezerwować oś może prowadzący strzelanie (PS). Dodaj numer i zdjęcie legitymacji — obsługa je sprawdzi. "
                                    "Nie masz uprawnień PS? Możesz strzelać z prowadzącym, który ma rezerwację — wpiszesz się do książki pobytu kodem QR.")
        url, action = reverse("credentials:my_add") + "?rodzaj=ps", "Dodaj uprawnienia PS"
    cfg = BookingSettings.load()
    if state != "done" and cfg.officer_service:
        # bez PS można zarezerwować z prowadzącym od strzelnicy — krok nie blokuje rezerwacji
        fee = f" (dopłata {cfg.officer_fee_hourly / 100:.2f} zł / h)".replace(".", ",") if cfg.officer_fee_hourly else ""
        state = "tip"
        detail = (f"Bez uprawnień PS zarezerwujesz oś z prowadzącym zapewnionym przez strzelnicę{fee} — rezerwacja czeka na "
                  "zatwierdzenie przez obsługę. " + detail)
    steps.append({"key": "ps", "title": "Uprawnienia prowadzącego strzelanie (PS)", "state": state, "detail": detail, "url": url, "action": action})
    missing_profile = [label for field, label in (("phone", "telefon"), ("street", "ulica"), ("postal_code", "kod pocztowy"), ("city", "miejscowość"))
                       if not getattr(user, field)]
    steps.append({
        "key": "profile", "title": "Uzupełnij dane kontaktowe",
        "state": "tip" if missing_profile else "done",
        "detail": ("Brakuje: " + ", ".join(missing_profile) + ". Telefon pomaga w kontakcie, adres uzupełni się sam w książce pobytu.")
        if missing_profile else "Kompletne.",
        "url": reverse("accounts:profile") if missing_profile else "", "action": "Uzupełnij profil",
    })
    blocking = [s for s in steps if s["state"] in ("missing", "waiting")]
    done = sum(1 for s in steps if s["state"] == "done")
    return {"steps": steps, "ready": not blocking, "done": done, "total": len(steps)}


def validate_request(lane, day: date, start_hour: int, end_hour: int, persons: int, now=None, enforce_lead=False, user=None,
                     respect_holds=None):
    """Sprawdza termin i liczbę osób. Zwraca (starts_at, ends_at, price_rule).
    `enforce_lead` — rezerwacja online przez strzelca: obowiązuje minimalny czas przed terminem z ustawień.
    `respect_holds` — terminy trzymane dla listy oczekujących (domyślnie jak `enforce_lead`; rezerwacja na miejscu: tak,
    bez limitu czasu)."""
    if respect_holds is None:
        respect_holds = enforce_lead
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
    if enforce_lead and cfg.min_lead_hours and starts_at < now + timedelta(hours=cfg.min_lead_hours):
        raise ValidationError(
            f"Online można rezerwować najpóźniej {cfg.min_lead_hours} h przed rozpoczęciem. "
            "Na wcześniejszą godzinę wybierz inny termin albo skontaktuj się z obsługą strzelnicy."
        )
    if day > timezone.localdate(now) + timedelta(days=cfg.max_advance_days):
        raise ValidationError(f"Rezerwować można najwyżej {cfg.max_advance_days} dni naprzód.")
    if Block.objects.filter(lanes=lane, starts_at__lt=ends_at, ends_at__gt=starts_at).exists():
        raise ValidationError("W wybranym czasie oś jest zablokowana przez zarząd.")
    if respect_holds:  # strzelec: termin trzymany dla osoby z listy oczekujących
        from .waitlist import active_holds

        hold = active_holds(lane, starts_at, ends_at, now, exclude_user=user).order_by("hold_until").first()
        if hold:
            raise ValidationError(f"Ten termin jest chwilowo zarezerwowany dla osoby z listy oczekujących "
                                  f"(do {timezone.localtime(hold.hold_until):%H:%M}). Wybierz inny albo zapisz się na listę.")
    rule = lane.price_rule_for(day)
    if rule is None:
        raise ValidationError("Dla tej osi nie ustalono opłat. Skontaktuj się z zarządem.")
    return starts_at, ends_at, rule


def needs_officer(user):
    """Rezerwacja z prowadzącym od strzelnicy: rezerwujący nie ma ważnych uprawnień PS, a usługa jest włączona."""
    return bool(user) and not is_range_officer(user) and BookingSettings.load().officer_service


def quote(lane, day, start_hour, end_hour, persons, now=None, access_code="", user=None, enforce_lead=False, respect_holds=None,
          ps_pending=False):
    starts_at, ends_at, rule = validate_request(lane, day, start_hour, end_hour, persons, now, enforce_lead=enforce_lead, user=user,
                                                respect_holds=respect_holds)
    hours = end_hour - start_hour
    base = {"starts_at": starts_at, "ends_at": ends_at, "rule": rule, "code": None, "client_pass": None,
            "with_officer": False, "officer_hourly": 0, "officer_amount": 0}
    if user is not None and not ps_pending and needs_officer(user):
        if access_code:
            raise ValidationError("Rezerwacja z prowadzącym od strzelnicy jest bez kodu klubu i kodu wejścia — usuń kod.")
        officer_hourly = BookingSettings.load().officer_fee_hourly
        hourly = rule.hourly_amount(persons)
        return {**base, "hourly": hourly, "amount": (hourly + officer_hourly) * hours, "with_officer": True,
                "officer_hourly": officer_hourly, "officer_amount": officer_hourly * hours}
    if access_code:
        # Kod wejścia strzelca (przedpłacony karnet/abonament): rezerwacja bez osobnej należności.
        from apps.clients.services import find_client_pass

        client_pass = find_client_pass(access_code, user, lane, day, hours, persons)
        if client_pass is not None:
            return {**base, "hourly": 0, "amount": 0, "client_pass": client_pass}
        # Kod klubu: rozliczenie wg umowy z klubem, poza płatnościami strzelca. Kwota tylko informacyjnie (stawka umowna).
        from apps.association.services import find_access_code

        code = find_access_code(access_code, lane, day, hours)
        hourly = code.contract_hourly or 0
        return {**base, "hourly": hourly, "amount": hourly * hours, "code": code}
    hourly = rule.hourly_amount(persons)
    return {**base, "hourly": hourly, "amount": hourly * hours}


def _next_number(year):
    NumberSequence.objects.bulk_create([NumberSequence(year=year)], ignore_conflicts=True)
    seq = NumberSequence.objects.select_for_update().get(year=year)
    seq.last += 1
    seq.save(update_fields=["last"])
    return f"SATOK-R-{year}-{seq.last:04d}"


CHANNEL_ONLINE, CHANNEL_ONSITE = "online", "na_miejscu"


def create_reservation(user, lane, day, start_hour, end_hour, persons, notes="", created_by=None, now=None, access_code="",
                       series=None, notify=True, channel=CHANNEL_ONLINE, checkout=False, ps_pending=False):
    """Rezerwacja. `channel`: "online" (strona — limit czasu przed terminem z ustawień) albo "na_miejscu" (panel na
    strzelnicy — bez limitu czasu, po okazaniu dokumentów). Obsługa w panelu (`created_by` ≠ `user`) — bez limitu.

    `checkout` (rezerwacja przez strzelca na stronie): termin jest trzymany („oczekuje na płatność”) przez czas na wybór
    płatności; potwierdzenie dopiero po zapłacie online, wyborze gotówki na miejscu albo zaksięgowaniu przelewu.

    `ps_pending` (tylko na miejscu): legitymacja PS czeka na weryfikację — rezerwacja „do zatwierdzenia”, bez prowadzącego
    od strzelnicy i bez płatności do czasu weryfikacji (decyzja właściciela: najpierw weryfikacja, potem płatność)."""
    if ps_pending and channel != CHANNEL_ONSITE:
        raise ValueError("Rezerwacja z legitymacją PS w weryfikacji jest możliwa tylko na miejscu.")
    reasons = check_can_book(user, ps_pending=ps_pending)
    if reasons:
        raise ValidationError(reasons)
    by_staff = created_by is not None and created_by != user
    if channel not in (CHANNEL_ONLINE, CHANNEL_ONSITE):
        raise ValueError(f"Nieznany kanał rezerwacji: {channel}")
    q = quote(lane, day, start_hour, end_hour, persons, now, access_code=access_code, user=user,
              enforce_lead=not by_staff and channel == CHANNEL_ONLINE, respect_holds=not by_staff, ps_pending=ps_pending)
    code, client_pass = q["code"], q["client_pass"]
    now = now or timezone.now()
    group = rule = None
    if not by_staff:
        # Zasady grupy strzelca (decyzje G2–G4) obowiązują przy rezerwacji przez strzelca; obsługa rezerwuje bez ograniczeń.
        from apps.clients.services import client_type

        group = client_type(user, now)
        rule = GroupRule.for_group(group)
        if client_pass is not None and not rule.pay_pass:
            raise ValidationError("Kody wejścia nie są dostępne dla Twojej grupy strzelców. Zarezerwuj bez kodu.")
    # prowadzący od strzelnicy: zawsze do zatwierdzenia (obsługa przydziela prowadzącego)
    pending = bool(ps_pending or q["with_officer"] or (rule and rule.requires_approval and code is None and client_pass is None))
    cfg = BookingSettings.load()
    hold = bool(checkout and not by_staff and code is None and client_pass is None and q["amount"] > 0
                and (not pending or cfg.prepay_before_approval))
    if hold:
        if cfg.max_unpaid_holds and Reservation.objects.filter(user=user, status=Reservation.Status.HOLD,
                                                               ends_at__gt=now).count() >= cfg.max_unpaid_holds:
            raise ValidationError(f"Masz już {cfg.max_unpaid_holds} nieopłacone rezerwacje — opłać je (albo anuluj niepotrzebne) "
                                  "przed kolejną rezerwacją.")
        status = Reservation.Status.HOLD
    else:
        status = Reservation.Status.PENDING_APPROVAL if pending else Reservation.Status.CONFIRMED
    release_expired_holds(lane, q["starts_at"], q["ends_at"], now)  # wygasłe blokady innych strzelców nie trzymają terminu
    try:
        with transaction.atomic():
            if client_pass is not None:
                # Blokada wiersza kodu: równoległe rezerwacje nie przekroczą limitu wejść.
                from apps.clients.services import find_client_pass

                client_pass = find_client_pass(access_code, user, lane, day, end_hour - start_hour, persons, lock=True)
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
                with_officer=q["with_officer"],
                officer_hourly=q["officer_hourly"],
                created_by=created_by or user,
                access_code=code,
                client_pass=client_pass,
                payment_status=(
                    Reservation.Payment.CONTRACT if code else Reservation.Payment.PASS if client_pass else Reservation.Payment.DUE
                ),
                status=status,
                needs_approval=pending,
                pay_by=min(now + timedelta(minutes=cfg.hold_minutes or 5), hold_limit(q["starts_at"], q["ends_at"], now)) if hold else None,
                client_group=group or "",
                series=series,
            )
            if not pending and not hold:
                reservation.pay_by = payment_deadline(reservation, now)
                if reservation.pay_by:
                    reservation.save(update_fields=["pay_by"])
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
    use_wallet_for_reservation(reservation, by=created_by)  # saldo najpierw — także częściowo (gdy saldo włączone)
    reservation.refresh_from_db()
    from .waitlist import mark_booked

    mark_booked(reservation)
    if reservation.status == Reservation.Status.HOLD:
        return reservation  # potwierdzenie i powiadomienia — po zapłacie / wyborze gotówki / zaksięgowaniu przelewu
    if not notify:  # rezerwacje cykliczne: jedno zbiorcze powiadomienie dla całej serii
        return reservation
    _notify_created(reservation, pending)
    return reservation


def _notify_created(reservation, pending):
    """E-mail do strzelca i powiadomienie zarządu o nowej (potwierdzonej albo czekającej na zatwierdzenie) rezerwacji."""
    user, persons = reservation.user, reservation.persons
    if pending:
        notify_user(user, f"Rezerwacja {reservation.number} czeka na zatwierdzenie", "emails/reservation_pending.txt", reservation=reservation,
                    options=payment_options(reservation), tpay=dj_settings.TPAY_ENABLED and BookingSettings.load().prepay_before_approval)
    else:
        notify_user(user, f"Rezerwacja {reservation.number} potwierdzona", "emails/reservation_created.txt", reservation=reservation,
                    options=payment_options(reservation))
    notify_board(
        ("Do zatwierdzenia: " if pending else "Nowa rezerwacja: ") + f"{reservation.lane}, {timezone.localtime(reservation.starts_at):%d.%m %H:%M}",
        "emails/reservation_board.txt",
        PERM,
        url=reverse("panel:reservation", args=[reservation.pk]),
        body=f"{user.full_name}, {persons} os." + (" Nowy strzelec — rezerwacja czeka na zatwierdzenie." if pending else ""),
        reservation=reservation,
        event="Rezerwacja do zatwierdzenia" if pending else "Nowa rezerwacja",
    )


# --- rezerwacje cykliczne (tworzy obsługa w panelu) -------------------------------------------

SERIES_MAX = 53  # najwyżej rok co tydzień


def weekly_days(first_day, until):
    """Ten sam dzień tygodnia co `first_day`, co 7 dni, do `until` włącznie."""
    days, d = [], first_day
    while d <= until and len(days) < SERIES_MAX:
        days.append(d)
        d += timedelta(days=7)
    return days


def preview_series(user, lane, days, start_hour, end_hour, persons, access_code="", now=None):
    """Sprawdza każdy termin serii bez zapisu. Zwraca [(dzień, None | powód, że termin odpada)]."""
    rows = []
    for d in days:
        try:
            q = quote(lane, d, start_hour, end_hour, persons, now, access_code=access_code, user=user)
        except ValidationError as e:
            rows.append((d, "; ".join(e.messages)))
            continue
        clash = (
            Reservation.objects.filter(lane=lane, starts_at__lt=q["ends_at"], ends_at__gt=q["starts_at"])
            .exclude(status__in=[Reservation.Status.CANCELLED, Reservation.Status.NO_SHOW])
            .select_related("user").first()
        )
        rows.append((d, f"zajęte: {clash.number} ({clash.user.last_name})" if clash else None))
    return rows


def create_series(user, lane, days, start_hour, end_hour, persons, notes, by, access_code=""):
    """Tworzy rezerwacje cykliczne jako zwykłe rezerwacje ze wspólnym `series`. Terminy kolidujące są pomijane.
    Zwraca (utworzone, pominięte [(dzień, powód)]). Strzelec i zarząd dostają po jednym zbiorczym powiadomieniu."""
    series = uuid.uuid4()
    created, skipped = [], []
    for d in days:
        try:
            created.append(create_reservation(user, lane, d, start_hour, end_hour, persons, notes, created_by=by,
                                              access_code=access_code, series=series, notify=False))
        except ValidationError as e:
            skipped.append((d, "; ".join(e.messages)))
    if not created:
        return created, skipped
    log_event(
        "rezerwacja.seria",
        created[0],
        f"Seria {len(created)} rezerwacji: {lane}, {timezone.localtime(created[0].starts_at):%H:%M}, co tydzień",
        changes={"seria": str(series), "numery": [r.number for r in created], "pominiete": [str(d) for d, _ in skipped]},
        actor=by,
    )
    notify_user(user, f"Rezerwacje cykliczne: {lane}, {len(created)} terminów", "emails/reservation_series_created.txt",
                reservations=created, skipped=skipped, total=sum(r.amount for r in created))
    notify_board(
        f"Nowa seria rezerwacji: {lane}, {len(created)} terminów",
        "emails/reservation_board.txt",
        PERM,
        url=reverse("panel:reservation", args=[created[0].pk]),
        body=f"{user.full_name}, co tydzień od {timezone.localtime(created[0].starts_at):%d.%m %H:%M}",
        reservation=created[0],
        event="Nowa seria rezerwacji",
    )
    return created, skipped


def cancel_series(reservation, by, reason, now=None):
    """Anuluje wszystkie przyszłe, aktywne terminy serii (bez opłaty). Zwraca listę anulowanych."""
    now = now or timezone.now()
    if not reservation.series:
        raise ValidationError("Ta rezerwacja nie należy do serii.")
    future = Reservation.objects.filter(
        series=reservation.series, starts_at__gt=now,
        status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING_APPROVAL],
    ).order_by("starts_at")
    cancelled = []
    with transaction.atomic():
        for r in future:
            cancelled.append(cancel_reservation(r, by, reason, as_board=True, now=now, notify=False))
    if cancelled:
        log_event("rezerwacja.seria_anulowanie", cancelled[0], f"Anulowano {len(cancelled)} terminów serii",
                  changes={"seria": str(reservation.series), "numery": [r.number for r in cancelled], "powod": reason}, actor=by)
        notify_user(cancelled[0].user, f"Anulowano rezerwacje cykliczne ({len(cancelled)})", "emails/reservation_series_cancelled.txt",
                    reservations=cancelled, reason=reason)
    return cancelled


# --- zasady grup strzelców: płatności, zatwierdzanie, wygasanie ------------------------------


def group_rule(reservation):
    """Zasady grupy zapisane przy rezerwacji; rezerwacja utworzona przez obsługę — wszystkie metody dozwolone."""
    if not reservation.client_group:
        return None
    return GroupRule.for_group(reservation.client_group)


def payment_options(reservation, now=None):
    """Dostępne sposoby płatności (wg zasad grupy strzelca; rezerwacja obsługi — wszystkie)."""
    rule = group_rule(reservation)
    transfer = rule is None or rule.pay_transfer
    cash = (rule is None or rule.pay_cash) and not reservation.needs_approval
    return {
        "online": rule is None or rule.pay_online,
        "transfer": transfer,
        "cash": cash,
        "offline": transfer or cash,
        "transfer_deadline": transfer_deadline(reservation, now) if transfer else None,
        "instructions": BookingSettings.load().offline_instructions,
    }


def payment_deadline(reservation, start, after_approval=False):
    """Termin opłacenia dla grup „tylko online”: krótki po rezerwacji, dłuższy po zatwierdzeniu przez obsługę."""
    from django.conf import settings as dj_settings

    rule = group_rule(reservation)
    if rule is None or not rule.online_only or reservation.payment_status != Reservation.Payment.DUE:
        return None
    if not dj_settings.TPAY_ENABLED:
        return None  # bez działających płatności online nie zwalniamy rezerwacji automatycznie
    cfg = BookingSettings.load()
    delta = timedelta(hours=cfg.approval_payment_hours) if after_approval else timedelta(minutes=cfg.online_payment_minutes)
    if not delta:
        return None
    return min(start + delta, reservation.starts_at)


@transaction.atomic
def assign_officer(reservation, officer, by):
    """Przydział prowadzącego od strzelnicy (osoba ze zweryfikowanym PS) do rezerwacji."""
    r = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if not r.with_officer:
        raise ValidationError("Ta rezerwacja nie wymaga prowadzącego od strzelnicy.")
    if r.status not in (Reservation.Status.PENDING_APPROVAL, Reservation.Status.CONFIRMED):
        raise ValidationError("Nie można zmienić prowadzącego w anulowanej rezerwacji.")
    if officer is None or not is_range_officer(officer):
        raise ValidationError("Prowadzący musi mieć zweryfikowane, ważne uprawnienia PS.")
    before = r.officer_id
    r.officer = officer
    r.save(update_fields=["officer", "updated_at"])
    log_event("rezerwacja.prowadzacy", r, f"{r.number}: {officer.full_name}", changes={"bylo": before, "jest": officer.pk}, actor=by)
    if r.status == Reservation.Status.CONFIRMED and before != officer.pk:
        notify_user(r.user, f"Prowadzący dla rezerwacji {r.number}", "emails/reservation_officer.txt", reservation=r)
    return r


@transaction.atomic
def approve_reservation(reservation, by, now=None):
    now = now or timezone.now()
    from apps.kiosk.models import OnsiteBooking

    if OnsiteBooking.objects.filter(reservation_id=reservation.pk, status=OnsiteBooking.Status.AWAITING).exists():
        # rezerwacja na miejscu (osoba czeka przy kiosku): także w trwającej godzinie, potem płatność kodem QR
        from apps.kiosk.onsite import approve_for_payment

        return approve_for_payment(reservation, by, now=now)
    r = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if r.status != Reservation.Status.PENDING_APPROVAL:
        raise ValidationError("Ta rezerwacja nie czeka na zatwierdzenie.")
    if r.starts_at <= now:
        raise ValidationError("Termin rezerwacji już minął — nie można jej zatwierdzić.")
    if r.with_officer and not r.officer_id:
        raise ValidationError("Najpierw przydziel prowadzącego od strzelnicy.")
    r.status = Reservation.Status.CONFIRMED
    r.approved_at, r.approved_by = now, by
    r.pay_by = payment_deadline(r, now, after_approval=True)
    r.save()
    log_event("rezerwacja.zatwierdzenie", r, str(r), actor=by)
    paid = r.payment_status == Reservation.Payment.PAID
    notify_user(r.user, f"Rezerwacja {r.number} potwierdzona" + (" i opłacona" if paid else ""), "emails/reservation_created.txt",
                reservation=r, options=payment_options(r), approved=True)
    return r


def refund_paid_to_payer(r, amount, by, reason, remind_manual=True):
    """Zwraca `amount` z wpłat za rezerwację na rachunek/kartę płacącego (Tpay). Kwota, której nie da się zwrócić
    automatycznie (wpłata poza Tpay, błąd Tpay), zostaje nadpłatą — zarząd dostaje przypomnienie o zwrocie ręcznym.
    Zwraca (zwrócono_automatycznie, do_zwrotu_ręcznie)."""
    from apps.payments.models import Payment
    from apps.payments.services import refund_to_payers, remind_manual_refund

    refunded, manual = refund_to_payers(Payment.objects.filter(reservation=r), amount, by, reason)
    if manual and remind_manual:
        remind_manual_refund(r.user, manual, r.number)
    return refunded, manual


def reject_reservation(reservation, by, reason=""):
    """Odrzucenie rezerwacji czekającej na zatwierdzenie. Wpłata online (Tpay) wraca automatycznie na rachunek/kartę
    płacącego; inne wpłaty (gotówka, przelew) zostają jako nadpłata, a zarząd dostaje informację o zwrocie ręcznym."""
    r = Reservation.objects.get(pk=reservation.pk)
    if r.status != Reservation.Status.PENDING_APPROVAL:
        raise ValidationError("Ta rezerwacja nie czeka na zatwierdzenie.")
    paid = r.paid_amount
    r = cancel_reservation(r, by, reason or "Rezerwacja nie została zatwierdzona", as_board=True, notify=False)
    refunded, manual = refund_paid_to_payer(r, paid, by, f"Rezerwacja {r.number} nie została zatwierdzona") if paid else (0, 0)
    notify_user(r.user, f"Rezerwacja {r.number} nie została zatwierdzona", "emails/reservation_rejected.txt", reservation=r,
                refunded=refunded, manual=manual, fee_kept=_online_fee_kept(r) if (refunded or manual) else 0)
    return r


def cancel_by_board(reservation, by, reason, keep_fee=False, refund=False):
    """Anulowanie przez obsługę. `refund` (bez zachowania należności): wpłata wraca na rachunek/kartę płacącego (Tpay);
    bez zaznaczenia zostaje nadpłatą strzelca do decyzji zarządu (jak dotąd)."""
    r = Reservation.objects.get(pk=reservation.pk)
    paid = r.paid_amount
    r = cancel_reservation(r, by, reason, as_board=True, keep_fee=keep_fee, notify=False)
    refunded = manual = 0
    if refund and not r.late_cancellation and paid:
        refunded, manual = refund_paid_to_payer(r, paid, by, f"Anulowano rezerwację {r.number}")
    _notify_cancelled(r, refunded=refunded, manual=manual, overpaid=0 if refund or r.late_cancellation else paid)
    return r


def cancel_by_client(reservation, by, reason="", now=None):
    """Anulowanie przez strzelca w terminie bezpłatnym. Wpłata online (Tpay) za rezerwację wraca zawsze automatycznie
    na rachunek/kartę płacącego. Reszta (gotówka, przelew, błąd Tpay): przy włączonym saldzie zostaje na saldzie
    strzelca; przy wyłączonym — zarząd dostaje przypomnienie o zwrocie ręcznym.
    Zwraca (rezerwacja, zwrócono_tpay, do_zwrotu_ręcznie, na_saldo)."""
    from apps.payments.services import wallet_enabled

    paid = Reservation.objects.get(pk=reservation.pk).paid_amount
    r = cancel_reservation(reservation, by, reason, now=now, notify=False)
    refunded = manual = to_wallet = 0
    if paid:
        wallet = wallet_enabled()
        refunded, rest = refund_paid_to_payer(r, paid, by, f"Anulowano rezerwację {r.number}", remind_manual=not wallet)
        if wallet:
            to_wallet = rest
        else:
            manual = rest
    _notify_cancelled(r, refunded=refunded, manual=manual, to_wallet=to_wallet)
    return r, refunded, manual, to_wallet


# --- rezerwacja z blokadą terminu: wybór płatności, potwierdzenie, zwolnienie ---------------------------------------


def transfer_deadline(reservation, now=None):
    """Termin zaksięgowania przelewu tradycyjnego albo None, gdy na przelew jest za późno."""
    now = now or timezone.now()
    cfg = BookingSettings.load()
    days, cutoff = timedelta(days=cfg.transfer_days), timedelta(hours=cfg.transfer_cutoff_hours)
    if not cfg.transfer_days or reservation.starts_at - now < days + cutoff:
        return None
    return min(now + days, reservation.starts_at - cutoff)


@transaction.atomic
def choose_payment(reservation, user, method, now=None):
    """Strzelec wybiera sposób płatności dla rezerwacji „oczekuje na płatność”: gotówka na miejscu (potwierdzona od razu)
    albo przelew tradycyjny (oczekuje na przelew do terminu zaksięgowania)."""
    now = now or timezone.now()
    r = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if r.user_id != user.pk:
        raise ValidationError("To nie jest Twoja rezerwacja.")
    if r.status != Reservation.Status.HOLD or r.payment_status != Reservation.Payment.DUE:
        raise ValidationError("Ta rezerwacja nie czeka na wybór płatności.")
    if r.pay_by and r.pay_by < now and r.pay_method != Reservation.PayMethod.TRANSFER:
        raise ValidationError("Czas na wybór płatności minął — termin wrócił do puli. Zarezerwuj ponownie.")
    options = payment_options(r, now)
    if method == Reservation.PayMethod.CASH:
        if not options["cash"]:
            raise ValidationError("Gotówka na miejscu nie jest dostępna dla tej rezerwacji.")
        r.pay_method, r.status, r.pay_by = method, Reservation.Status.CONFIRMED, None
        r.save(update_fields=["pay_method", "status", "pay_by", "updated_at"])
        log_event("rezerwacja.gotowka", r, f"{r.number}: płatność gotówką na miejscu", actor=user)
        _notify_created(r, False)
        return r
    if method == Reservation.PayMethod.TRANSFER:
        if not options["transfer"]:
            raise ValidationError("Przelew tradycyjny nie jest dostępny dla tej rezerwacji.")
        if not options["transfer_deadline"]:
            raise ValidationError("Na przelew tradycyjny jest za późno — zapłać online" + (" albo gotówką na miejscu." if options["cash"] else "."))
        r.pay_method, r.pay_by = method, options["transfer_deadline"]
        # Przypomnienie przed terminem tylko wtedy, gdy do terminu zostało więcej niż doba (inaczej wystarczy ten e-mail).
        r.pay_reminder_sent_at = now if r.pay_by - now <= TRANSFER_REMINDER_BEFORE else None
        r.overdue_notified_at = None
        r.save(update_fields=["pay_method", "pay_by", "pay_reminder_sent_at", "overdue_notified_at", "updated_at"])
        log_event("rezerwacja.przelew", r, f"{r.number}: przelew do {timezone.localtime(r.pay_by):%d.%m %H:%M}", actor=user)
        from apps.core.models import AssociationSettings

        notify_user(r.user, f"Rezerwacja {r.number} oczekuje na przelew", "emails/reservation_transfer.txt", reservation=r,
                    options=options, association=AssociationSettings.load())
        return r
    raise ValidationError("Nieznany sposób płatności.")


def hold_limit(starts_at, ends_at, now):
    """Do kiedy najdłużej trzymać termin do płatności: do rozpoczęcia rezerwacji, a dla już trwającej (rezerwacja
    na miejscu w bieżącej godzinie) — do jej końca."""
    return starts_at if starts_at > now else ends_at


def start_online_payment(reservation, now=None):
    """Wejście do płatności online: termin trzymany dodatkowe minuty (BLIK, logowanie do banku)."""
    now = now or timezone.now()
    r = Reservation.objects.get(pk=reservation.pk)
    if r.status != Reservation.Status.HOLD:
        return r
    cfg = BookingSettings.load()
    until = min(now + timedelta(minutes=cfg.online_payment_minutes or 15), hold_limit(r.starts_at, r.ends_at, now))
    Reservation.objects.filter(pk=r.pk).update(pay_method=Reservation.PayMethod.ONLINE,
                                               pay_by=max(r.pay_by, until) if r.pay_by else until)
    r.refresh_from_db()
    return r


@transaction.atomic
def confirm_after_payment(reservation, by=None):
    """Rezerwacja „oczekuje na płatność” opłacona w całości (online, z salda, zaksięgowany przelew) → potwierdzona
    (albo czeka na zatwierdzenie, gdy grupa tego wymaga) + powiadomienia."""
    r = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if r.status != Reservation.Status.HOLD or r.payment_status != Reservation.Payment.PAID:
        return r
    r.status = Reservation.Status.PENDING_APPROVAL if r.needs_approval else Reservation.Status.CONFIRMED
    r.pay_by = None
    r.save(update_fields=["status", "pay_by", "updated_at"])
    log_event("rezerwacja.potwierdzenie_po_platnosci", r, str(r), actor=by)
    _notify_created(r, r.needs_approval)
    return r


TRANSFER_REMINDER_BEFORE = timedelta(hours=24)


def _expire_hold(r, now):
    """Wygasła blokada terminu. Zwraca True, gdy rezerwację zwolniono.

    - Trwająca płatność Tpay (ostatnie 30 min) — czekamy.
    - Przelew tradycyjny po terminie — NIE zwalniamy automatycznie: przelew mógł zostać wysłany, a księguje go dopiero
      import wyciągu przez zarząd. Zarząd dostaje powiadomienie (raz) i decyduje; automatycznie zwalnia się dopiero
      w chwili rozpoczęcia, jeśli nadal nie jest opłacona.
    - Pozostałe (porzucony wybór płatności, nieopłacona płatność online) — zwolnienie z rozliczeniem wpłat."""
    from apps.payments.models import Payment

    if Payment.objects.filter(reservation=r, status=Payment.Status.PENDING, created_at__gte=now - timedelta(minutes=30)).exists():
        return False
    if r.pay_method == Reservation.PayMethod.TRANSFER and r.starts_at > now:
        _notify_transfer_overdue(r, now)
        return False
    _release_hold(r, now)
    return True


def _release_hold(r, now):
    """Zwolnienie wygasłej blokady terminu. Wpłaty za rezerwację (z salda, przelew częściowy, Tpay) są rozliczane jak przy
    anulowaniu bez opłat: Tpay wraca na kartę / rachunek, reszta na saldo (gdy włączone) albo zarząd dostaje przypomnienie
    o zwrocie ręcznym. Porzucony wybór płatności bez wpłat — bez e-maili."""
    from apps.payments.services import wallet_enabled

    if r.pay_method == Reservation.PayMethod.TRANSFER:
        reason = "Przelew nie został zaksięgowany przed rozpoczęciem"
    elif r.pay_method:
        reason = "Nieopłacona w wyznaczonym terminie"
    else:
        reason = "Czas na wybór płatności minął"
    paid = r.paid_amount
    r = cancel_reservation(r, None, reason, as_board=True, now=now, expired=True, notify=False)
    refunded = manual = to_wallet = 0
    if paid:
        wallet = wallet_enabled()
        refunded, rest = refund_paid_to_payer(r, paid, None, f"Zwolniono rezerwację {r.number}", remind_manual=not wallet)
        to_wallet, manual = (rest, 0) if wallet else (0, rest)
    if r.pay_method or paid:
        _notify_cancelled(r, expired=True, refunded=refunded, manual=manual, to_wallet=to_wallet)
    return r


def _notify_transfer_overdue(r, now):
    """Termin przelewu minął: zarząd decyduje (import wyciągu albo anulowanie); strzelec dostaje informację. Raz."""
    updated = Reservation.objects.filter(pk=r.pk, overdue_notified_at__isnull=True).update(overdue_notified_at=now)
    if not updated:
        return
    r.refresh_from_db()
    remaining = max(r.amount - r.paid_amount, 0)
    paid_note = f" Zaksięgowano {r.paid_amount / 100:.2f} zł z {r.amount / 100:.2f} zł." if r.paid_amount else ""
    url = reverse("panel:reservation", args=[r.pk])
    title = f"Przelew po terminie: {r.number} ({r.lane}, {timezone.localtime(r.starts_at):%d.%m %H:%M})"
    notify_board(
        title, "emails/board_generic.txt", PERM, url=url,
        body=f"{r.user.full_name}: do zapłaty {remaining / 100:.2f} zł.{paid_note} Zaimportuj wyciąg — po zaksięgowaniu rezerwacja "
             "potwierdzi się sama. Jeśli wpłaty nie ma, anuluj rezerwację (bez decyzji zwolni się w chwili rozpoczęcia).",
        event=title,
        text=f"{r.user.full_name} wybrał przelew tradycyjny z terminem {timezone.localtime(r.pay_by):%d.%m.%Y %H:%M}. Do zapłaty "
             f"{remaining / 100:.2f} zł.{paid_note}\nZaimportuj wyciąg bankowy (Płatności → Import wyciągu) — po zaksięgowaniu "
             "rezerwacja potwierdzi się sama. Jeśli wpłaty nie ma, anuluj rezerwację w panelu, żeby zwolnić termin. "
             "Bez decyzji rezerwacja zwolni się automatycznie w chwili rozpoczęcia.",
        url_path=url,
    )
    notify_user(r.user, f"Rezerwacja {r.number}: termin przelewu minął", "emails/reservation_transfer_overdue.txt",
                reservation=r, remaining=remaining)


def send_transfer_reminders(now=None):
    """Przypomnienie strzelcowi o przelewie na dobę przed terminem zaksięgowania (raz)."""
    from apps.core.models import AssociationSettings

    now = now or timezone.now()
    qs = Reservation.objects.filter(status=Reservation.Status.HOLD, pay_method=Reservation.PayMethod.TRANSFER,
                                    pay_reminder_sent_at__isnull=True, pay_by__gt=now,
                                    pay_by__lte=now + TRANSFER_REMINDER_BEFORE).select_related("user", "lane")
    count = 0
    for r in qs:
        with transaction.atomic():
            if not Reservation.objects.filter(pk=r.pk, pay_reminder_sent_at__isnull=True).update(pay_reminder_sent_at=now):
                continue
            notify_user(r.user, f"Przypomnienie: przelew za rezerwację {r.number}", "emails/reservation_transfer_reminder.txt",
                        reservation=r, options=payment_options(r, now), association=AssociationSettings.load())
        count += 1
    return count


def release_expired_holds(lane, starts_at, ends_at, now=None):
    """Zwalnia wygasłe blokady nakładające się na podany termin (przed zapisem nowej rezerwacji)."""
    now = now or timezone.now()
    count = 0
    for r in Reservation.objects.filter(lane=lane, status=Reservation.Status.HOLD, pay_by__lt=now,
                                        starts_at__lt=ends_at, ends_at__gt=starts_at):
        count += _expire_hold(r, now)
    return count


def expire_unpaid(now=None):
    """Zwalnia rezerwacje nieopłacone w terminie (grupy „tylko online”). Pomija trwające płatności Tpay."""
    from apps.payments.models import Payment

    now = now or timezone.now()
    count = 0
    qs = Reservation.objects.filter(
        status=Reservation.Status.CONFIRMED, payment_status=Reservation.Payment.DUE, pay_by__isnull=False, pay_by__lt=now
    )
    for r in qs:
        if Payment.objects.filter(reservation=r, status=Payment.Status.PENDING, created_at__gte=now - timedelta(minutes=30)).exists():
            continue
        if r.paid_amount:
            continue  # częściowo opłacona — decyzja obsługi
        cancel_reservation(r, None, "Nieopłacona w wyznaczonym terminie", as_board=True, now=now, expired=True)
        count += 1
    for r in Reservation.objects.filter(status=Reservation.Status.HOLD, pay_by__isnull=False, pay_by__lt=now):
        count += _expire_hold(r, now)
    return count


@transaction.atomic
def cancel_reservation(reservation, by, reason="", as_board=False, now=None, keep_fee=False, expired=False, notify=True):
    """Anulowanie. Strzelec: sam do terminu z ustawień (domyślnie 24 h przed), bez opłaty; później tylko obsługa.
    Obsługa: bez opłaty albo — przy późnym odwołaniu — z zachowaniem należności (`keep_fee`)."""
    now = now or timezone.now()
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status not in (Reservation.Status.CONFIRMED, Reservation.Status.PENDING_APPROVAL, Reservation.Status.HOLD):
        raise ValidationError("Tej rezerwacji nie można już anulować.")
    if not as_board:
        if reservation.user_id != by.pk:
            raise ValidationError("To nie jest Twoja rezerwacja.")
        if not reservation.can_user_cancel(now):
            hours = BookingSettings.load().free_cancel_hours
            raise ValidationError(f"Na mniej niż {hours} h przed terminem anulować może tylko obsługa strzelnicy — skontaktuj się z nami.")
    late = bool(as_board and keep_fee and reservation.status == Reservation.Status.CONFIRMED)
    reservation.status = Reservation.Status.CANCELLED
    reservation.cancelled_at = now
    reservation.cancelled_by = by
    reservation.cancel_reason = reason.strip()[:255]
    reservation.late_cancellation = late
    if not late:
        reservation.payment_status = Reservation.Payment.NONE
    reservation.save()  # anulowanie po terminie: należność (lub rozliczenie wg umowy klubu) pozostaje
    lane, freed_from, freed_to = reservation.lane, reservation.starts_at, reservation.ends_at

    def _offer():
        from .waitlist import offer_freed

        offer_freed(lane, freed_from, freed_to)

    transaction.on_commit(_offer)  # zwolniony termin — oferta dla pierwszej osoby z listy oczekujących
    if not late:
        reservation = refund_reservation(reservation, by=by)
    log_event(
        "rezerwacja.anulowanie",
        reservation,
        str(reservation),
        changes={"z_oplata": late, "przez_obsluge": as_board, "wygasla": expired, "powod": reservation.cancel_reason},
        actor=by,
    )
    if not notify:
        return reservation
    _notify_cancelled(reservation, expired=expired)
    return reservation


def _online_fee_kept(r):
    """Opłaty za płatność online zapłacone za rezerwację — nie wracają przy zwrocie."""
    from apps.payments.models import Payment
    from apps.payments.services import surcharge_of

    return surcharge_of(Payment.objects.filter(reservation=r))


def _notify_cancelled(reservation, expired=False, refunded=0, manual=0, overpaid=0, to_wallet=0):
    late = reservation.late_cancellation
    kept = _online_fee_kept(reservation) if (refunded or manual or to_wallet) else 0
    notify_user(reservation.user, f"Rezerwacja {reservation.number} anulowana", "emails/reservation_cancelled.txt", reservation=reservation,
                refunded=refunded, manual=manual, overpaid=overpaid, to_wallet=to_wallet, fee_kept=kept)
    notify_board(
        f"Anulowano rezerwację: {reservation.lane}, {timezone.localtime(reservation.starts_at):%d.%m %H:%M}",
        "emails/reservation_board.txt",
        PERM,
        url=reverse("panel:reservation", args=[reservation.pk]),
        body=f"{reservation.user.full_name}" + (" — z zachowaniem należności" if late else "") + (" — nieopłacona w terminie" if expired else "")
        + (f" — zwrot {refunded / 100:.2f} zł przez Tpay" if refunded else ""),
        reservation=reservation,
        event="Anulowanie rezerwacji",
    )


@transaction.atomic
def change_persons(reservation, persons, by):
    """Zmiana liczby osób przez zarząd — należność liczona według cennika z chwili rezerwacji."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status != Reservation.Status.CONFIRMED:
        raise ValidationError("Można zmieniać tylko aktywne rezerwacje.")
    if persons < 1 or persons > BookingSettings.load().max_persons:
        raise ValidationError("Nieprawidłowa liczba osób.")
    before = (reservation.persons, reservation.amount)
    if reservation.client_pass_id and persons > reservation.client_pass.max_persons:
        raise ValidationError(f"Kod wejścia obejmuje najwyżej {reservation.client_pass.max_persons} os. — anuluj i zarezerwuj bez kodu.")
    reservation.persons = persons
    if reservation.access_code_id is None and reservation.client_pass_id is None:
        reservation.hourly_amount = reservation.price_rule.hourly_amount(persons)
        reservation.recompute_amount()
    reservation.save()
    if reservation.access_code_id is None and reservation.client_pass_id is None:
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


def change_term(reservation, lane, day, start_hour, end_hour, by, refund_difference=False, now=None):
    """Zmiana osi i/lub terminu przez obsługę. Należność wg cennika nowej osi i dnia: wyższa — dopłata (do zapłaty),
    niższa — różnica wraca (na rachunek/kartę przez Tpay przy `refund_difference`, inaczej jako nadpłata).
    Zwolniony dotychczasowy termin trafia do listy oczekujących."""
    now = now or timezone.now()
    with transaction.atomic():
        r = Reservation.objects.select_for_update(of=("self",)).select_related("lane", "client_pass").get(pk=reservation.pk)
        if r.status not in (Reservation.Status.CONFIRMED, Reservation.Status.PENDING_APPROVAL):
            raise ValidationError("Można zmieniać tylko aktywne rezerwacje.")
        starts_at, ends_at, rule = validate_request(lane, day, start_hour, end_hour, r.persons, now)
        clash = (Reservation.objects.filter(lane=lane, starts_at__lt=ends_at, ends_at__gt=starts_at)
                 .exclude(pk=r.pk).exclude(status__in=[Reservation.Status.CANCELLED, Reservation.Status.NO_SHOW]).first())
        if clash:
            raise ValidationError(f"Nowy termin jest zajęty (rezerwacja {clash.number}).")
        hours = end_hour - start_hour
        if r.client_pass_id:
            p = r.client_pass
            if (p.lanes.exists() and not p.lanes.filter(pk=lane.pk).exists()) or (p.max_hours and hours > p.max_hours):
                raise ValidationError("Kod wejścia nie obejmuje nowej osi lub długości — anuluj i zarezerwuj ponownie.")
        old = {"oś": str(r.lane), "od": timezone.localtime(r.starts_at).strftime("%d.%m.%Y %H:%M"),
               "do": timezone.localtime(r.ends_at).strftime("%H:%M"), "kwota_gr": r.amount}
        freed = (r.lane, r.starts_at, r.ends_at)
        paid_before = r.paid_amount
        r.lane, r.starts_at, r.ends_at = lane, starts_at, ends_at
        if r.access_code_id is None and r.client_pass_id is None:
            r.price_rule = rule
            r.hourly_amount = rule.hourly_amount(r.persons)
            r.amount = (r.hourly_amount + r.officer_hourly) * hours
        elif r.access_code_id:
            r.hourly_amount = r.access_code.contract_hourly or 0
            r.amount = r.hourly_amount * hours
        r.reminder_sent_at = None
        try:
            with transaction.atomic():
                r.save()
        except IntegrityError as e:
            raise ValidationError("Nowy termin został właśnie zajęty. Wybierz inny.") from e
        if r.access_code_id is None and r.client_pass_id is None:
            r = reconcile_reservation(r, by=by)
        log_event("rezerwacja.zmiana_terminu", r, str(r), actor=by,
                  changes={"przed": old, "po": {"oś": str(lane), "od": timezone.localtime(starts_at).strftime("%d.%m.%Y %H:%M"),
                                                "do": timezone.localtime(ends_at).strftime("%H:%M"), "kwota_gr": r.amount}})

        def _offer():
            from .waitlist import offer_freed

            offer_freed(*freed)

        transaction.on_commit(_offer)
    excess = max(paid_before - r.amount, 0) if r.access_code_id is None and r.client_pass_id is None else 0
    refunded = manual = 0
    if excess and refund_difference:
        refunded, manual = refund_paid_to_payer(r, excess, by, f"Zmiana rezerwacji {r.number} — zwrot różnicy")
    r.refresh_from_db()
    notify_user(r.user, f"Zmiana rezerwacji {r.number}", "emails/reservation_changed.txt", reservation=r, old=old,
                surcharge=max(r.amount - r.paid_amount, 0) if r.payment_status == Reservation.Payment.DUE else 0,
                refunded=refunded, manual=manual, overpaid=0 if refund_difference else excess, options=payment_options(r))
    return r


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
        apply_wallet(reservation.user, by=by, targets=[reservation])
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
        # także rezerwacje czekające na płatność (przelew trzyma termin do kilku dni) — po zapłacie byłyby potwierdzone w blokadzie
        Reservation.objects.filter(lane__in=lanes, status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING_APPROVAL,
                                                               Reservation.Status.HOLD])
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
            # strzelec nie zawinił — wpłata online wraca automatycznie przez Tpay (bez opłaty za płatność online)
            cancelled.append(cancel_by_board(r, by, f"Blokada osi: {title}", refund=True))
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
