"""Książka rejestru pobytu: tokeny kodów QR, zapis wpisów, wypis, rozliczenie rezerwacji, czyszczenie."""

import base64
import binascii
import hashlib
from datetime import timedelta
from io import BytesIO

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from apps.audit.services import log_event
from apps.bookings.models import Reservation
from apps.core.notifications import notify_board, notify_user
from apps.credentials.models import Credential, valid_credentials
from apps.payments.services import apply_wallet
from apps.regulations.models import RegulationDocument

from .models import STATEMENT_TEXT, Entry, EntryCorrection, EntrySequence, LogbookSettings

RESERVATION_SALT = "satok.logbook.reservation-qr"
PERSONAL_SALT = "satok.logbook.personal-qr"
EXIT_SALT = "satok.logbook.exit"
COMPANION_SALT = "satok.logbook.companion"
MAX_SIGNATURE_BYTES = 300 * 1024


# --- tokeny kodów QR ----------------------------------------------------------


def reservation_token(reservation):
    return signing.dumps({"r": reservation.pk}, salt=RESERVATION_SALT, compress=True)


def reservation_from_token(token):
    try:
        data = signing.loads(token, salt=RESERVATION_SALT)
        return Reservation.objects.select_related("lane", "user").get(pk=data["r"])
    except (signing.BadSignature, KeyError, TypeError, Reservation.DoesNotExist):
        return None


def personal_token(user):
    return signing.dumps({"u": user.pk, "s": user.qr_secret[:12]}, salt=PERSONAL_SALT, compress=True)


def user_from_personal_token(token):
    from apps.accounts.models import User

    try:
        data = signing.loads(token, salt=PERSONAL_SALT)
        user = User.objects.get(pk=data["u"], is_active=True)
    except (signing.BadSignature, KeyError, TypeError, User.DoesNotExist):
        return None
    return user if user.qr_secret[:12] == data.get("s") else None


def exit_token(entry):
    return signing.dumps({"e": entry.pk}, salt=EXIT_SALT, compress=True)


def entry_from_exit_token(token, max_age_hours=36):
    try:
        data = signing.loads(token, salt=EXIT_SALT, max_age=max_age_hours * 3600)
        return Entry.objects.get(pk=data["e"])
    except (signing.BadSignature, KeyError, TypeError, Entry.DoesNotExist):
        return None


def gate_token_valid(token):
    expected = LogbookSettings.load().gate_token
    return bool(token) and secrets_compare(token, expected)


def secrets_compare(a, b):
    import hmac

    return hmac.compare_digest(str(a).encode(), str(b).encode())


@transaction.atomic
def rotate_gate_token(by):
    from .models import new_gate_token

    cfg = LogbookSettings.load()
    cfg.gate_token = new_gate_token()
    cfg.gate_token_rotated_at = timezone.now()
    cfg.save()
    log_event("ksiazka.nowy_kod_wejscia", cfg, "Wymieniono kod QR przy wejściu — stary kod przestał działać", actor=by)
    return cfg


# --- rezerwacje dostępne do wpisu -----------------------------------------------


def reservation_open_for_entry(reservation, now=None):
    now = now or timezone.now()
    before = timedelta(minutes=LogbookSettings.load().window_before_minutes)
    return reservation.status == Reservation.Status.CONFIRMED and reservation.starts_at - before <= now <= reservation.ends_at


def open_reservations(now=None):
    now = now or timezone.now()
    before = timedelta(minutes=LogbookSettings.load().window_before_minutes)
    return (
        Reservation.objects.filter(status=Reservation.Status.CONFIRMED, starts_at__lte=now + before, ends_at__gte=now)
        .select_related("lane", "user")
        .order_by("starts_at", "lane__order")
    )


# --- dane podpowiadane z konta ------------------------------------------------------


def prefill_for(user):
    data = {"first_name": user.first_name, "last_name": user.last_name, "address": user.address}
    permit = valid_credentials(user, Credential.Kind.FIREARM_PERMIT).order_by("-issued_on").first()
    if permit:
        data.update(has_permit="tak", permit_number=permit.number, permit_issuer=permit.issuer)
    else:
        data["has_permit"] = "nie"
    return data


# --- podpis -------------------------------------------------------------------------


def decode_signature(data_url):
    """Przyjmuje podpis z formularza (data:image/png;base64,...) i zwraca bajty PNG. Odrzuca pusty podpis."""
    prefix = "data:image/png;base64,"
    if not data_url or not data_url.startswith(prefix):
        raise ValidationError("Złóż podpis w polu podpisu.")
    try:
        raw = base64.b64decode(data_url[len(prefix):], validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValidationError("Nieprawidłowy podpis.") from e
    if len(raw) > MAX_SIGNATURE_BYTES:
        raise ValidationError("Podpis jest za duży.")
    try:
        img = Image.open(BytesIO(raw))
        if img.format != "PNG" or img.width > 2000 or img.height > 1000:  # wymiary z nagłówka — przed rozpakowaniem
            raise ValidationError("Nieprawidłowy podpis.")
        img.verify()
        img = Image.open(BytesIO(raw)).convert("LA")
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as e:
        raise ValidationError("Nieprawidłowy podpis.") from e
    # Podpis musi zawierać ślad: odpowiednio dużo nieprzezroczystych, ciemnych pikseli.
    pixels = img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata()
    inked = sum(1 for lum, alpha in pixels if alpha > 64 and lum < 160)
    if inked < 150:
        raise ValidationError("Podpis jest pusty lub za krótki — podpisz się palcem w ramce.")
    return raw


# --- zapis wpisu --------------------------------------------------------------------


def _next_number(year):
    EntrySequence.objects.bulk_create([EntrySequence(year=year)], ignore_conflicts=True)
    seq = EntrySequence.objects.select_for_update().get(year=year)
    seq.last += 1
    seq.save(update_fields=["last"])
    return f"KP-{year}-{seq.last:06d}"


def check_rate_limit(ip):
    if not ip:
        return
    from apps.accounts.ratelimit import allow

    if not allow("logbook", ip, LogbookSettings.load().max_entries_per_hour, 3600):
        raise ValidationError("Zbyt wiele wpisów z tego urządzenia. Spróbuj później lub poproś o pomoc prowadzącego strzelanie.")


STAFF_BACKDATE_HOURS = 24
DEVICE_BACKDATE_HOURS = 72  # wpisy z kolejki offline kiosku


@transaction.atomic
def create_entry(
    *, data, signature_png, source, reservation=None, user=None, created_by=None, ip=None, user_agent="", now=None, entered_at=None,
    device=None, client_ref=None,
):
    """Zapisuje wpis. `entered_at` (godzina wejścia wcześniejsza niż chwila zapisu) przyjmuje się tylko przy wpisie
    przez obsługę (najwyżej 24 h wstecz) i z kiosku, który zapisał wpis bez połączenia (najwyżej 72 h wstecz)."""
    now = now or timezone.now()
    if entered_at is not None:
        limits = {Entry.Source.STAFF: STAFF_BACKDATE_HOURS, Entry.Source.DEVICE: DEVICE_BACKDATE_HOURS}
        if source not in limits:
            raise ValidationError("Godzinę wejścia może podać tylko obsługa.")
        if entered_at > now + timedelta(minutes=2) or entered_at < now - timedelta(hours=limits[source]):
            raise ValidationError(f"Godzina wejścia musi mieścić się w ostatnich {limits[source]} godzinach.")
        entered_at = min(entered_at, now)
    else:
        entered_at = now
    purpose = data["purpose"]
    if purpose == Entry.Purpose.SHOOTING and reservation is None:
        raise ValidationError("Przy strzelaniu wybierz rezerwację, w ramach której strzelasz.")
    if reservation is not None:
        reservation = Reservation.objects.select_related("user").get(pk=reservation.pk)
        if not reservation_open_for_entry(reservation, entered_at):
            raise ValidationError("Ta rezerwacja nie trwa — wpis do niej nie jest możliwy." if entered_at == now
                                  else "Godzina wejścia nie mieści się w czasie wybranej rezerwacji.")
    has_permit = data["has_permit"]
    entry = Entry(
        number=_next_number(timezone.localdate(entered_at).year),
        entered_at=entered_at,
        purpose=purpose,
        purpose_note=data.get("purpose_note", "").strip()[:160],
        reservation=reservation,
        range_officer=reservation.range_officer_user if reservation else None,
        user=user,
        created_by=created_by,
        first_name=data["first_name"].strip(),
        last_name=data["last_name"].strip(),
        has_permit=has_permit,
        permit_number=data.get("permit_number", "").strip().upper() if has_permit else "",
        permit_issuer=data.get("permit_issuer", "").strip() if has_permit else "",
        address="" if has_permit else data.get("address", "").strip(),
        statement=STATEMENT_TEXT,
        regulation_version=_current_range_rules(),
        signature=signature_png,
        signature_sha256=hashlib.sha256(signature_png).hexdigest(),
        source=source,
        ip=ip,
        user_agent=(user_agent or "")[:255],
        device=device,
        client_ref=client_ref,
        received_at=now if device is not None else None,
    )
    entry.content_sha256 = entry.compute_content_hash()
    entry.save()
    log_event("ksiazka.wpis", entry, f"{entry.number} ({entry.get_purpose_display()})", actor=created_by or user)
    return entry


def _current_range_rules():
    doc = RegulationDocument.objects.filter(slug="regulamin-strzelnicy").first()
    return doc.current_version() if doc else None


# --- korekty wpisów (wpis pozostaje niezmieniony) ------------------------------------------------------------------

CORRECTABLE = dict(EntryCorrection.FIELDS)


def _correction_value(field, raw):
    if field == "exited_at":
        from datetime import datetime

        return datetime.fromisoformat(raw) if raw else None
    return raw


def apply_corrections(entries):
    """Nakłada korekty na wpisy (tylko w pamięci, do wyświetlenia — wpisów nie da się zapisać). Każdy wpis dostaje
    `corrected_fields` (zbiór pól) i `integrity_original` (suma kontrolna oryginału, liczona przed nałożeniem)."""
    entries = list(entries)
    by_id = {e.pk: e for e in entries}
    for e in entries:
        e.corrected_fields = set()
    if not by_id:
        return entries
    for c in EntryCorrection.objects.filter(entry_id__in=by_id).order_by("created_at", "id"):
        e = by_id[c.entry_id]
        if not e.corrected_fields:
            e.integrity_original = e.integrity_ok
        setattr(e, c.field, _correction_value(c.field, c.new_value))
        e.corrected_fields.add(c.field)
    return entries


@transaction.atomic
def correct_entry(entry, values, reason, by, now=None):
    """Korekta wpisu przez obsługę: tylko zmienione pola, z powodem. Wyjście dla otwartego pobytu zapisuje się
    zwykłym trybem (raz), późniejsze zmiany godziny wyjścia — jako korekta. Zwraca listę skorygowanych pól."""
    now = now or timezone.now()
    reason = " ".join(str(reason or "").split())[:255]
    if len(reason) < 5:
        raise ValidationError("Podaj powód korekty (np. „literówka w nazwisku”, „osoba wyszła o 21:30”).")
    entry = Entry.objects.select_for_update().get(pk=entry.pk)
    current = apply_corrections([Entry.objects.get(pk=entry.pk)])[0]
    changed = []
    for field, raw in values.items():
        if field not in CORRECTABLE:
            raise ValidationError("Tego pola nie można korygować.")
        if field == "exited_at":
            new = raw
            if new is None or new == current.exited_at:
                continue
            if new < current.entered_at or new > now + timedelta(minutes=1):
                raise ValidationError("Godzina wyjścia musi być po wejściu i nie później niż teraz.")
            if entry.exited_at is None:  # otwarty pobyt — zwykłe zapisanie wyjścia (dozwolone raz)
                register_exit(entry, now=new)
                changed.append(field)
                continue
            old_s, new_s = current.exited_at.isoformat() if current.exited_at else "", new.isoformat()
        else:
            new_s = " ".join(str(raw or "").split())
            if field == "permit_number":
                new_s = new_s.upper()
            old_s = getattr(current, field) or ""
            if new_s == old_s:
                continue
            if field in ("first_name", "last_name") and not new_s:
                raise ValidationError("Imię i nazwisko nie mogą być puste.")
            if field in ("permit_number", "permit_issuer") and (not current.has_permit or not new_s):
                raise ValidationError("Dane pozwolenia: wpis bez pozwolenia albo puste pole.")
            if field == "address" and (current.has_permit or not new_s):
                raise ValidationError("Adres korygujemy tylko we wpisie bez pozwolenia na broń (i nie może być pusty).")
            if field == "purpose_note" and not new_s and (current.purpose != Entry.Purpose.SHOOTING or current.reservation_id is None):
                raise ValidationError("Opis celu nie może być pusty.")
        EntryCorrection.objects.create(entry=entry, field=field, old_value=old_s[:255], new_value=new_s[:255], reason=reason,
                                       created_by=by)
        changed.append(field)
    if not changed:
        raise ValidationError("Nic nie zmieniono.")
    log_event("ksiazka.korekta", entry, f"{entry.number}: {', '.join(CORRECTABLE[f] for f in changed)}",
              changes={"pola": changed, "powod": reason}, actor=by)
    return changed


def register_exit(entry, now=None):
    now = now or timezone.now()
    if entry.exited_at is not None:
        raise ValidationError("Wyjście zostało już zapisane.")
    Entry.objects.filter(pk=entry.pk, exited_at__isnull=True).update(exited_at=now)
    entry.exited_at = now
    log_event("ksiazka.wypis", entry, entry.number)
    return entry


MY_EXIT_HOURS = 36  # jak ważność linku wyjścia z ekranu po wpisie


def my_open_stays(user, now=None):
    """Otwarte pobyty powiązane z kontem (do zakończenia z panelu klienta)."""
    now = now or timezone.now()
    return (Entry.objects.filter(user=user, exited_at__isnull=True, entered_at__gte=now - timedelta(hours=MY_EXIT_HOURS))
            .select_related("reservation__lane").order_by("-entered_at"))


def my_reservations_for_entry(user, now=None):
    """Trwające rezerwacje, w których osoba jest rezerwującym lub przydzielonym prowadzącym, a jeszcze nie ma w nich
    swojego otwartego wpisu — z linkiem do wpisu bez skanowania kodu (to ten sam link co w kodzie QR rezerwacji)."""
    from django.db.models import Q

    now = now or timezone.now()
    entered = Entry.objects.filter(user=user, exited_at__isnull=True).values("reservation_id")
    qs = open_reservations(now).filter(Q(user=user) | Q(officer=user)).exclude(pk__in=entered)
    return [(r, reservation_token(r)) for r in qs]


def auto_close_at(entry):
    """Kiedy pobyt zamknie się sam (ta sama zasada co close_stale_entries): koniec rezerwacji albo N godzin od wejścia."""
    hours = LogbookSettings.load().auto_exit_hours
    if not hours or entry.exited_at:
        return None
    at = entry.entered_at + timedelta(hours=hours)
    if entry.reservation_id and entry.entered_at <= entry.reservation.ends_at < at:
        at = entry.reservation.ends_at
    return at


def companion_token(entry):
    return signing.dumps({"e": entry.pk}, salt=COMPANION_SALT, compress=True)


def entry_from_companion_token(token):
    """Wpis osoby, do której dopisują się towarzysze — tylko gdy jej pobyt trwa."""
    try:
        data = signing.loads(token, salt=COMPANION_SALT, max_age=36 * 3600)
        entry = Entry.objects.select_related("reservation__lane").get(pk=data["e"])
    except (signing.BadSignature, KeyError, TypeError, Entry.DoesNotExist):
        return None
    if entry.exited_at:
        return None
    close = auto_close_at(entry)
    if close and close <= timezone.now():
        return None
    return entry


def close_stale_entries(now=None):
    """Zamyka pobyty bez zapisanego wyjścia: o końcu rezerwacji (strzelanie) albo po N godzinach od wejścia."""
    now = now or timezone.now()
    hours = LogbookSettings.load().auto_exit_hours
    if not hours:
        return 0
    limit = timedelta(hours=hours)
    closed = 0
    stale = Entry.objects.filter(exited_at__isnull=True).filter(
        Q(entered_at__lte=now - limit) | Q(reservation__isnull=False, reservation__ends_at__lte=now)
    ).select_related("reservation")
    for e in stale:
        exit_at = e.entered_at + limit
        if e.reservation and e.entered_at <= e.reservation.ends_at < exit_at:
            exit_at = e.reservation.ends_at
        if exit_at > now:
            continue
        closed += Entry.objects.filter(pk=e.pk, exited_at__isnull=True).update(exited_at=exit_at, exit_auto=True)
    if closed:
        log_event("ksiazka.wypis_auto", summary=f"Automatycznie zamknięto pobytów: {closed}")
    return closed


# --- maskowanie danych na listach ---------------------------------------------------


def mask(value, keep_start=2, keep_end=2):
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= keep_start + keep_end:
        return value[:1] + "•" * (len(value) - 1)
    tail = value[len(value) - keep_end:] if keep_end else ""
    return value[:keep_start] + "•" * min(6, len(value) - keep_start - keep_end) + tail


def masked_identity(entry):
    if entry.has_permit:
        return f"pozwolenie {mask(entry.permit_number)}"
    return f"adres {mask(entry.address, 3, 0)}"


# --- rozliczenie rezerwacji wg książki pobytu -------------------------------------


def settle_finished_reservations(now=None):
    """Po zakończeniu rezerwacji: jeśli strzelających wpisów jest więcej niż zadeklarowanych osób,
    należność rośnie wg cennika z chwili rezerwacji, ale nie ponad ryczałt (reguła „mniejsza z kwot”).
    Mniej wpisów nie obniża opłaty. Rezerwacje opłacone lub zwolnione są tylko oznaczane do decyzji zarządu."""
    now = now or timezone.now()
    count = 0
    for r in Reservation.objects.filter(status=Reservation.Status.CONFIRMED, ends_at__lte=now, settled_at__isnull=True):
        with transaction.atomic():
            r = Reservation.objects.select_for_update().select_related("price_rule", "user", "lane").get(pk=r.pk)
            shooters = r.log_entries.filter(purpose=Entry.Purpose.SHOOTING).count()
            r.settled_at = now
            if r.access_code_id:  # kod klubu — rozliczenie wg umowy, bez dopłat
                r.save(update_fields=["settled_at"])
                continue
            if shooters > r.persons:
                if r.client_pass_id:  # kod wejścia: dopłata tylko za osoby ponad limit kodu
                    from apps.clients.services import pass_extra_hourly

                    new_hourly = pass_extra_hourly(r.price_rule, shooters, r.client_pass)
                else:
                    new_hourly = r.price_rule.hourly_amount(shooters)
                if new_hourly > r.hourly_amount:
                    if r.payment_status in (Reservation.Payment.DUE, Reservation.Payment.PASS):
                        r.payment_status = Reservation.Payment.DUE
                        before = (r.persons, r.amount)
                        r.declared_persons = r.persons
                        r.persons = shooters
                        r.hourly_amount = new_hourly
                        r.recompute_amount()
                        r.save()
                        log_event(
                            "rezerwacja.rozliczenie_ksiazka",
                            r,
                            str(r),
                            changes={"osoby": [before[0], shooters], "kwota_gr": [before[1], r.amount]},
                        )
                        apply_wallet(r.user, targets=[r])
                        r.refresh_from_db()
                        notify_user(r.user, f"Rozliczenie rezerwacji {r.number}", "emails/reservation_settled.txt", reservation=r)
                    else:
                        r.save(update_fields=["settled_at"])
                        notify_board(
                            f"Rozbieżność w rozliczeniu {r.number}",
                            "emails/reservation_board.txt",
                            "bookings.manage_reservations",
                            url=reverse("panel:reservation", args=[r.pk]),
                            body=f"W książce {shooters} strzelających, zadeklarowano {r.persons}; rezerwacja jest już rozliczona.",
                            reservation=r,
                            event=f"Rozbieżność: w książce pobytu {shooters} strzelających, zadeklarowano {r.persons}",
                        )
                    count += 1
                    continue
            r.save(update_fields=["settled_at"])
    return count


# --- czyszczenie po okresie przechowywania -----------------------------------------


def purge_expired(now=None):
    now = now or timezone.now()
    years = LogbookSettings.load().retention_years
    cutoff = now.replace(year=now.year - years) if not (now.month == 2 and now.day == 29) else now.replace(year=now.year - years, day=28)
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT set_config('satok.logbook_purge', 'on', true)")
        EntryCorrection.objects.filter(entry__entered_at__lt=cutoff).delete()  # korekty razem z wpisami
        deleted, _ = Entry.objects.filter(entered_at__lt=cutoff).delete()
        from .models import ArchivedEntry

        archived, _ = ArchivedEntry.objects.filter(started_at__lt=cutoff).delete()
        deleted += archived
        from .daily_print import purge_before

        prints = purge_before(timezone.localdate(cutoff))
    # Kartoteka osób bez konta (np. z GunTrackera) — ten sam okres przechowywania, liczony od wprowadzenia do kartoteki.
    from apps.clients.models import Person

    persons, _ = Person.objects.filter(user__isnull=True, created_at__lt=cutoff).delete()
    if persons:
        log_event("klient.czyszczenie_kartoteki", summary=f"Usunięto {persons} osób z kartoteki wprowadzonych ponad {years} lat temu")
    if prints:
        log_event("ksiazka.czyszczenie_wydrukow", summary=f"Usunięto {prints} dziennych wydruków starszych niż {years} lat")
    if deleted:
        log_event("ksiazka.czyszczenie", summary=f"Usunięto {deleted} wpisów starszych niż {years} lat")
    return deleted
