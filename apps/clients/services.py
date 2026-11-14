"""Kartoteka strzelców: typ strzelca, wizyty, zatwierdzanie; kody wejścia: wydawanie, sprawdzanie, wykorzystanie."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.services import log_event

from .models import ClientPass, ClientProfile, ClientTag

TYPE_MEMBER, TYPE_RETURNING, TYPE_NEW = "czlonek", "powracajacy", "nowy"
TYPE_LABELS = {TYPE_MEMBER: "Członek", TYPE_RETURNING: "Powracający", TYPE_NEW: "Nowy"}


def profile_for(user):
    profile, _ = ClientProfile.objects.get_or_create(user=user)
    return profile


# --- typ strzelca i wizyty -------------------------------------------------------------


def _member_user_ids(user_ids):
    from apps.association.models import Member

    return set(Member.objects.filter(user_id__in=user_ids, status=Member.Status.ACTIVE).values_list("user_id", flat=True))


def visit_days(user_ids, now=None):
    """{user_id: [daty wizyt rosnąco]} — dni z wpisem w książce pobytu na konto strzelca albo z odbytą rezerwacją."""
    from apps.bookings.models import Reservation
    from apps.logbook.models import Entry

    now = now or timezone.now()
    days = {uid: set() for uid in user_ids}
    for uid, at in Entry.objects.filter(user_id__in=user_ids).values_list("user_id", "entered_at"):
        days[uid].add(timezone.localdate(at))
    for uid, at in Reservation.objects.filter(
        user_id__in=user_ids, status=Reservation.Status.CONFIRMED, starts_at__lte=now
    ).values_list("user_id", "starts_at"):
        days[uid].add(timezone.localdate(at))
    return {uid: sorted(d) for uid, d in days.items()}


def _vetted_user_ids(user_ids):
    """Strzelcy, którym obsługa zatwierdziła już rezerwację (nadal potwierdzoną) — zostali sprawdzeni, nawet jeśli
    termin jeszcze się nie odbył."""
    from apps.bookings.models import Reservation

    return set(Reservation.objects.filter(user_id__in=user_ids, status=Reservation.Status.CONFIRMED, approved_at__isnull=False)
               .values_list("user_id", flat=True))


def annotate_clients(users, now=None):
    """Uzupełnia obiekty User o: client_type, client_type_label, visits, last_visit, active_passes, profile."""
    users = list(users)
    ids = [u.pk for u in users]
    members = _member_user_ids(ids)
    vetted = _vetted_user_ids(ids)
    visits = visit_days(ids, now)
    profiles = {p.user_id: p for p in ClientProfile.objects.filter(user_id__in=ids).prefetch_related("tags")}
    today = timezone.localdate()
    passes = {}
    for p in ClientPass.objects.filter(user_id__in=ids, is_active=True, valid_until__gte=today):
        passes.setdefault(p.user_id, []).append(p)
    ps = ps_status(ids)
    for u in users:
        u.ps_status, u.ps_label = ps.get(u.pk, ("brak", "brak"))
        u.profile = profiles.get(u.pk)
        u.visits = len(visits[u.pk])
        u.last_visit = visits[u.pk][-1] if visits[u.pk] else None
        if u.pk in members:
            u.client_type = TYPE_MEMBER
        elif u.visits or u.pk in vetted or (u.profile and u.profile.approved_at):
            u.client_type = TYPE_RETURNING
        else:
            u.client_type = TYPE_NEW
        u.client_type_label = TYPE_LABELS[u.client_type]
        u.active_passes = passes.get(u.pk, [])
    return users


PS_RANK = {"wazne": 3, "do_weryfikacji": 2, "wygasle": 1}
PS_LABELS = {"wazne": "ważne", "do_weryfikacji": "do weryfikacji", "wygasle": "wygasłe"}


def ps_status(user_ids):
    """{user_id: (kod, etykieta)} — najlepszy status uprawnień prowadzącego strzelanie."""
    from apps.credentials.models import Credential

    out = {}
    for c in Credential.objects.filter(user_id__in=user_ids, kind=Credential.Kind.RANGE_OFFICER):
        st = c.effective_status
        code = {Credential.Status.VERIFIED: "wazne", Credential.Status.PENDING: "do_weryfikacji", Credential.Status.EXPIRED: "wygasle"}.get(st)
        if code and PS_RANK[code] > PS_RANK.get(out.get(c.user_id, ("",))[0], 0):
            out[c.user_id] = (code, PS_LABELS[code])
    return out


def client_type(user, now=None):
    return annotate_clients([user], now)[0].client_type


# --- notatki, tagi, zatwierdzenie -----------------------------------------------------------


@transaction.atomic
def update_profile(user, *, notes, tag_names, by):
    profile = profile_for(user)
    before = (profile.notes, sorted(t.name for t in profile.tags.all()))
    profile.notes = notes.strip()
    profile.save()
    names = sorted({n.strip()[:40] for n in tag_names if n.strip()})
    tags = [ClientTag.objects.get_or_create(name=n)[0] for n in names]
    profile.tags.set(tags)
    changes = {}
    if before[0] != profile.notes:
        changes["notatki"] = "zmienione"
    if before[1] != names:
        changes["tagi"] = [before[1], names]
    if changes:
        log_event("klient.kartoteka", user, user.email, changes=changes, actor=by)
    return profile


@transaction.atomic
def set_approved(user, approved, by):
    profile = profile_for(user)
    if approved and not profile.approved_at:
        profile.approved_at, profile.approved_by = timezone.now(), by
    elif not approved:
        profile.approved_at, profile.approved_by = None, None
    profile.save()
    log_event("klient.zatwierdzenie" if approved else "klient.cofniecie_zatwierdzenia", user, user.email, actor=by)
    return profile


# --- kody wejścia -------------------------------------------------------------------


def _unique_code():
    from apps.association.models import AccessCode

    from .models import new_pass_code

    for _ in range(20):
        code = new_pass_code()
        if not ClientPass.objects.filter(code=code).exists() and not AccessCode.objects.filter(code=code).exists():
            return code
    raise RuntimeError("Nie udało się wygenerować unikalnego kodu")


@transaction.atomic
def issue_pass(user, *, kind, valid_from, valid_until, by, entries_limit=None, max_hours=2, max_persons=1, lanes=(),
               price=0, paid_with="", note=""):
    if valid_until < valid_from:
        raise ValidationError("Data końca ważności nie może być wcześniejsza niż początku.")
    if kind == ClientPass.Kind.SINGLE:
        entries_limit = 1
    elif kind == ClientPass.Kind.SUBSCRIPTION:
        entries_limit = None
    elif not entries_limit or entries_limit < 1:
        raise ValidationError("Podaj liczbę wejść karnetu.")
    if paid_with and paid_with not in ClientPass.PaidWith.values:
        raise ValidationError("Nieprawidłowy sposób zapłaty.")
    p = ClientPass.objects.create(
        user=user, code=_unique_code(), kind=kind, entries_limit=entries_limit, valid_from=valid_from, valid_until=valid_until,
        max_hours=max_hours, max_persons=max_persons, price=price, paid_with=paid_with,
        paid_at=timezone.now() if paid_with else None, note=note.strip()[:255], created_by=by,
    )
    if lanes:
        p.lanes.set(lanes)
    log_event("kod_wejscia.wydanie", p, f"{p.code} dla {user.full_name}: {p.get_kind_display()}",
              changes={"wejscia": entries_limit, "cena_gr": price, "zaplata": paid_with or "nieopłacony"}, actor=by)
    return p


@transaction.atomic
def mark_pass_paid(p, paid_with, by):
    p = ClientPass.objects.select_for_update().get(pk=p.pk)
    if p.paid_with:
        raise ValidationError("Kod jest już opłacony.")
    if paid_with not in ClientPass.PaidWith.values or not paid_with:
        raise ValidationError("Wybierz sposób zapłaty.")
    p.paid_with, p.paid_at = paid_with, timezone.now()
    p.save(update_fields=["paid_with", "paid_at"])
    log_event("kod_wejscia.zaplata", p, f"{p.code}: {p.get_paid_with_display()}", actor=by)
    return p


@transaction.atomic
def deactivate_pass(p, by):
    p = ClientPass.objects.select_for_update().get(pk=p.pk)
    p.is_active = False
    p.save(update_fields=["is_active"])
    log_event("kod_wejscia.dezaktywacja", p, p.code, actor=by)
    return p


def pass_reservations(p, exclude_reservation=None):
    from apps.bookings.models import Reservation

    qs = p.reservations.exclude(status=Reservation.Status.CANCELLED, late_cancellation=False)
    if exclude_reservation is not None:
        qs = qs.exclude(pk=exclude_reservation.pk)
    return qs


def pass_uses(p, exclude_reservation=None):
    return pass_reservations(p, exclude_reservation).count()


def pass_remaining(p):
    return None if p.entries_limit is None else max(0, p.entries_limit - pass_uses(p))


def pass_extra_hourly(rule, persons, client_pass):
    """Dopłata za godzinę za osoby ponad limit kodu wejścia: cena osi za wszystkie osoby minus cena za osoby objęte
    kodem (decyzja właściciela, listopad 2026). Z kodem nigdy nie płaci się więcej niż bez kodu; oś rozliczana tylko
    ryczałtem (cena niezależna od liczby osób) — bez dopłaty."""
    covered = min(persons, client_pass.max_persons)
    return max(rule.hourly_amount(persons) - rule.hourly_amount(covered), 0)


def usable_passes(user, day=None):
    """Aktywne, opłacone kody wejścia strzelca ważne w danym dniu (domyślnie od dziś) z wolnymi wejściami —
    do podpowiedzi w formularzu rezerwacji."""
    from django.utils import timezone

    day = day or timezone.localdate()
    out = []
    for p in ClientPass.objects.filter(user=user, is_active=True, valid_until__gte=day).order_by("valid_until", "pk"):
        remaining = pass_remaining(p)
        if p.is_paid and remaining != 0:
            p.remaining = remaining
            out.append(p)
    return out


def find_client_pass(raw, user, lane, day, hours, persons, lock=False, exclude_reservation=None):
    """Zwraca ClientPass dla kodu albo None, gdy to nie jest kod wejścia. Zgłasza ValidationError, gdy kod nie pasuje.
    Osoby ponad limit kodu nie blokują rezerwacji — płaci się za nie dopłatę (`pass_extra_hourly`)."""
    raw = (raw or "").strip().upper()
    qs = ClientPass.objects.filter(code=raw)
    if lock:
        qs = qs.select_for_update()
    p = qs.first()
    if p is None:
        return None
    if user is None or p.user_id != user.pk:
        raise ValidationError("Ten kod wejścia należy do innej osoby.")
    if not p.is_active:
        raise ValidationError("Kod wejścia jest nieaktywny.")
    if not p.is_paid:
        raise ValidationError("Kod wejścia nie został jeszcze opłacony.")
    if not (p.valid_from <= day <= p.valid_until):
        raise ValidationError(f"Kod wejścia jest ważny od {p.valid_from:%d.%m.%Y} do {p.valid_until:%d.%m.%Y}.")
    if p.lanes.exists() and not p.lanes.filter(pk=lane.pk).exists():
        raise ValidationError("Ten kod wejścia nie obejmuje wybranej osi.")
    if hours > p.max_hours:
        raise ValidationError(f"Jedno wejście z tego kodu obejmuje najwyżej {p.max_hours} h.")
    if p.entries_limit is not None and pass_uses(p, exclude_reservation) >= p.entries_limit:
        raise ValidationError("Wykorzystano już wszystkie wejścia z tego kodu.")
    return p


def search_users(qs, q):
    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(phone__icontains=q))
    return qs
