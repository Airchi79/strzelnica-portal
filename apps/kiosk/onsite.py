"""Rezerwacja na miejscu w kiosku (bez wcześniejszego konta).

Przebieg: dane osoby → 6-cyfrowy kod na e-mail (potwierdza adres; konto zakładane w tle, bez hasła) → regulaminy
i oświadczenie o prawdziwości danych → legitymacja PS (zdjęcie do weryfikacji), o ile osoba nie ma już zweryfikowanej →
rezerwacja na dziś. Decyzje właściciela: tylko osoby z uprawnieniami PS; najpierw weryfikacja, potem płatność; bez
weryfikacji w ciągu 15 minut rezerwacja anuluje się sama bez opłat; zdjęcie legitymacji usuwane po weryfikacji."""

import base64
import binascii
import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta
from io import BytesIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.validators import validate_email
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts import ratelimit
from apps.audit.services import log_event
from apps.bookings import services as bookings
from apps.bookings.models import Reservation
from apps.core.notifications import _send, notify_board, notify_user
from apps.core.qr import qr_svg_data_uri
from apps.credentials.models import Credential, is_range_officer
from apps.facilities.models import Lane
from apps.regulations.models import Acceptance, pending_acceptances

from .models import OnsiteBooking, token_hash

VERIFY_MINUTES = 15  # decyzja właściciela: tyle czeka rezerwacja na weryfikację legitymacji
AUTO_OK = "zgodne"  # = ocr.RESULT_OK
AUTO_NOTE = ("Zweryfikowano automatycznie: dane wpisane w kiosku zgodne ze zdjęciem legitymacji "
             "(imię i nazwisko, numer, rodzaj dokumentu, wystawca).")
CODE_MINUTES = 10
CODE_ATTEMPTS = 5
FLOW_MINUTES = 60  # na przejście kroków w kiosku
STARTS_PER_DEVICE_PER_HOUR = 20
STARTS_PER_EMAIL_PER_HOUR = 3
RETENTION_DAYS = 7
MAX_PHOTO_BYTES = 4 * 1024 * 1024
MIN_PHOTO_SIDE = 480
PAY_SALT = "kiosk-onsite-pay"
PAY_LINK_MINUTES = 120  # link do płatności z kodu QR w kiosku (na telefonie osoby)

# Oświadczenie przed rezerwacją. Celowo bez art. 233 k.k. (fałszywe zeznania) — dotyczy on oświadczeń składanych tylko
# tam, gdzie ustawa przewiduje odebranie oświadczenia pod rygorem odpowiedzialności karnej, a rezerwacja na strzelnicy
# stowarzyszenia do nich nie należy. Treść do akceptacji przez właściciela (ewentualnie po konsultacji z prawnikiem).
STATEMENT_TEXT = (
    "Oświadczam, że podane dane są prawdziwe, a okazana legitymacja prowadzącego strzelanie jest moja, autentyczna i ważna. "
    "Wiem, że używanie dokumentu stwierdzającego uprawnienia innej osoby (art. 275 § 1 Kodeksu karnego) oraz dokumentu "
    "podrobionego lub przerobionego (art. 270 § 1 Kodeksu karnego) jest przestępstwem, a za szkodę wyrządzoną podaniem "
    "nieprawdziwych danych odpowiadam na zasadach ogólnych (art. 415 Kodeksu cywilnego). Podanie nieprawdziwych danych "
    "oznacza anulowanie rezerwacji bez zwrotu kosztów zgodnie z regulaminem strzelnicy i zgłoszenie sprawy odpowiednim organom."
)


def _code_hash(ref, code):
    return hmac.new(settings.SECRET_KEY.encode(), f"kiosk-onsite:{ref}:{code}".encode(), hashlib.sha256).hexdigest()


def _clean_text(value, limit):
    return " ".join(str(value or "").split())[:limit]


# --- terminy -----------------------------------------------------------------------------------------------------


def price(lane_id, start_hour, end_hour, persons, now=None):
    """Wycena przed rezerwacją (do pokazania w kiosku przed podaniem danych). Bez prowadzącego od strzelnicy."""
    now = now or timezone.now()
    try:
        lane = Lane.objects.get(pk=int(lane_id), is_active=True)
        start_hour, end_hour, persons = int(start_hour), int(end_hour), int(persons)
    except (Lane.DoesNotExist, TypeError, ValueError):
        raise ValidationError("Wybierz oś i godziny.") from None
    q = bookings.quote(lane, timezone.localdate(now), start_hour, end_hour, persons, now=now, respect_holds=True)
    return {"amount": q["amount"], "lane": str(lane), "label": f"{lane} · {start_hour:02d}:00–{end_hour:02d}:00"}


def free_slots(now=None):
    """Wolne godziny dziś (od bieżącej godziny) na czynnych osiach — do wyboru w kiosku."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    out = []
    for lane in Lane.objects.filter(is_active=True, facility__is_active=True).select_related("facility").order_by("order"):
        hours = [s.start for s in bookings.day_slots(lane, today, now=now, holds=True) if s.state == "wolne"]
        if hours:
            out.append({"lane": lane.pk, "name": str(lane), "hours": [timezone.localtime(h).hour for h in hours]})
    return out


# --- kroki przebiegu -----------------------------------------------------------------------------------------------


@transaction.atomic
def start(device, email, first_name, last_name, phone):
    """Krok 1: dane osoby i wysłanie kodu na e-mail. Zwraca (przebieg, sekret) — sekret zna tylko kiosk."""
    email = _clean_text(email, 254).lower()
    first_name, last_name, phone = _clean_text(first_name, 80), _clean_text(last_name, 120), _clean_text(phone, 32)
    try:
        validate_email(email)
    except ValidationError:
        raise ValidationError("Podaj prawidłowy adres e-mail.") from None
    if not first_name or not last_name:
        raise ValidationError("Podaj imię i nazwisko.")
    if len("".join(ch for ch in phone if ch.isdigit())) < 9:
        raise ValidationError("Podaj numer telefonu (min. 9 cyfr).")
    if not ratelimit.allow("kiosk-onsite-device", str(device.pk), STARTS_PER_DEVICE_PER_HOUR):
        raise ValidationError("Zbyt wiele rozpoczętych rezerwacji w tym kiosku. Spróbuj za godzinę albo zarezerwuj przez stronę.")
    if not ratelimit.allow("kiosk-onsite-email", email, STARTS_PER_EMAIL_PER_HOUR):
        raise ValidationError("Na ten adres wysłano już kilka kodów. Sprawdź skrzynkę (także SPAM) albo spróbuj za godzinę.")
    secret = secrets.token_urlsafe(24)
    code = f"{secrets.randbelow(10**6):06d}"
    ref = uuid.uuid4()
    flow = OnsiteBooking.objects.create(
        ref=ref, secret_hash=token_hash(secret), device=device, email=email, first_name=first_name, last_name=last_name,
        phone=phone, code_hash=_code_hash(ref, code), code_expires_at=timezone.now() + timedelta(minutes=CODE_MINUTES),
    )
    _send("Kod do rezerwacji na strzelnicy", "emails/onsite_code.txt", {"first_name": first_name, "code": code,
                                                                       "minutes": CODE_MINUTES}, [email])
    log_event("kiosk.na_miejscu_start", device, device.name)
    return flow, secret


def get_flow(device, ref, secret, now=None):
    """Przebieg tego kiosku po identyfikatorze i sekrecie (inny kiosk ani obca strona go nie użyje)."""
    now = now or timezone.now()
    try:
        ref = uuid.UUID(str(ref))
    except (ValueError, TypeError):
        return None
    flow = OnsiteBooking.objects.filter(ref=ref, device=device).select_related("user", "credential", "reservation__lane").first()
    if flow is None or not secret or not hmac.compare_digest(flow.secret_hash, token_hash(str(secret))):
        return None
    if flow.status in (OnsiteBooking.Status.CODE_SENT, OnsiteBooking.Status.VERIFIED) and \
            flow.created_at < now - timedelta(minutes=FLOW_MINUTES):
        return None
    return flow


def verify_code(flow, code, now=None):
    """Krok 2: kod z e-maila. Adres potwierdzony → konto istniejące albo nowe (bez hasła).
    Licznik błędnych prób zapisywany jest poza transakcją potwierdzenia — błąd nie może go cofnąć."""
    now = now or timezone.now()
    with transaction.atomic():
        flow = OnsiteBooking.objects.select_for_update().get(pk=flow.pk)
        if flow.status != OnsiteBooking.Status.CODE_SENT:
            return flow
        if flow.code_attempts >= CODE_ATTEMPTS:
            raise ValidationError("Zbyt wiele błędnych prób. Zacznij od nowa.")
        if flow.code_expires_at is None or flow.code_expires_at < now:
            raise ValidationError("Kod wygasł. Zacznij od nowa — wyślemy nowy kod.")
        code = "".join(ch for ch in str(code or "") if ch.isdigit())
        wrong = not hmac.compare_digest(flow.code_hash, _code_hash(flow.ref, code))
        if wrong:
            flow.code_attempts += 1
            flow.save(update_fields=["code_attempts", "updated_at"])
        else:
            return _confirm(flow, now)
    left = CODE_ATTEMPTS - flow.code_attempts
    raise ValidationError(f"Nieprawidłowy kod. Pozostało prób: {left}." if left else "Nieprawidłowy kod. Zacznij od nowa.")


def _confirm(flow, now):
    User = get_user_model()
    user = User.objects.filter(email__iexact=flow.email).first()
    if user is None:
        user = User.objects.create_user(flow.email, None, first_name=flow.first_name, last_name=flow.last_name, phone=flow.phone,
                                        email_verified_at=now)
        log_event("konto.utworzenie", user, f"{user.full_name} (kiosk: {flow.device.name})", actor=user)
    elif not user.is_active or user.anonymized_at:
        raise ValidationError("Tego adresu nie można użyć do rezerwacji. Skontaktuj się z obsługą strzelnicy.")
    else:
        changed = []
        if user.email_verified_at is None:
            user.email_verified_at = now
            changed.append("email_verified_at")
        if not user.phone:
            user.phone = flow.phone
            changed.append("phone")
        if changed:
            user.save(update_fields=changed)
    flow.user = user
    flow.status = OnsiteBooking.Status.VERIFIED
    flow.code_hash = ""
    flow.save(update_fields=["user", "status", "code_hash", "updated_at"])
    log_event("kiosk.na_miejscu_email", flow.device, f"{flow.device.name}: {user.email}", actor=user)
    return flow


def _require_verified(flow):
    if flow.status != OnsiteBooking.Status.VERIFIED or flow.user_id is None:
        raise ValidationError("Najpierw potwierdź adres e-mail kodem.")


def accept_regulations(flow, ip=None):
    _require_verified(flow)
    for version in pending_acceptances(flow.user):
        Acceptance.objects.get_or_create(user=flow.user, version=version, defaults={"ip": ip})
        log_event("regulamin.akceptacja", version, f"{version} (kiosk)", actor=flow.user)


def accept_statement(flow, now=None):
    _require_verified(flow)
    flow.statement_at = now or timezone.now()
    flow.save(update_fields=["statement_at", "updated_at"])
    log_event("kiosk.na_miejscu_oswiadczenie", flow.device, f"{flow.device.name}: {flow.user.email}", actor=flow.user)


def _photo_file(data_url):
    """Zdjęcie z kamery kiosku (data URL JPEG/PNG) → JPEG bez metadanych, najwyżej 2000 px."""
    from PIL import Image, UnidentifiedImageError

    raw = str(data_url or "")
    if "," not in raw or not raw.startswith(("data:image/jpeg;base64,", "data:image/png;base64,")):
        raise ValidationError("Brak zdjęcia legitymacji.")
    try:
        data = base64.b64decode(raw.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("Nieprawidłowe zdjęcie. Zrób je ponownie.") from None
    if len(data) > MAX_PHOTO_BYTES:
        raise ValidationError("Zdjęcie jest za duże.")
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValidationError("Nieprawidłowe zdjęcie. Zrób je ponownie.") from None
    if min(img.size) < MIN_PHOTO_SIDE:
        raise ValidationError("Zdjęcie ma za małą rozdzielczość. Zrób je ponownie, bliżej kamery.")
    img = img.convert("RGB")
    img.thumbnail((2000, 2000))
    out = BytesIO()
    img.save(out, "JPEG", quality=88)  # nowy plik — bez EXIF (lokalizacja, model urządzenia)
    return SimpleUploadedFile("skan.jpg", out.getvalue(), content_type="image/jpeg")


def submit_ps(flow, number, issuer, issued_on, valid_until, photo):
    """Krok 4: legitymacja PS ze zdjęciem — trafia do weryfikacji (moduł dokumentów, powiadomienie obsługi).

    Zapis legitymacji w krótkiej transakcji; automatyczne porównanie (odczyt tekstu, do kilkunastu sekund) PO niej —
    bez trzymania transakcji i blokad przez czas odczytu. Wynik zapisywany osobno; nieudany odczyt = weryfikacja przez obsługę."""
    credential, scan_bytes = _save_ps(flow, number, issuer, issued_on, valid_until, photo)
    if scan_bytes is not None:
        _compare_ps(flow, credential, scan_bytes)
    return credential


def _compare_ps(flow, credential, scan_bytes):
    from . import ocr

    result = ocr.check(scan_bytes, flow.user, credential)[:255]
    with transaction.atomic():
        OnsiteBooking.objects.filter(pk=flow.pk, credential=credential).update(ocr_result=result, updated_at=timezone.now())
        log_event("kiosk.na_miejscu_porownanie", credential, f"{credential}: {result}", actor=flow.user)
    flow.ocr_result = result


@transaction.atomic
def _save_ps(flow, number, issuer, issued_on, valid_until, photo):
    """Zapis legitymacji ze zdjęciem. Zwraca (legitymacja, bajty zdjęcia do porównania albo None — porównanie wyłączone)."""
    from apps.credentials.forms import CredentialForm
    from apps.credentials.services import submit

    _require_verified(flow)
    if is_range_officer(flow.user):
        raise ValidationError("Masz już zweryfikowane uprawnienia prowadzącego strzelanie — przejdź do rezerwacji.")
    scan = _photo_file(photo)
    number_norm = " ".join(str(number or "").split()).upper()
    existing = Credential.objects.filter(user=flow.user, kind=Credential.Kind.RANGE_OFFICER, number=number_norm).first()
    old_scan = existing.scan.name if existing and existing.scan else None
    form = CredentialForm(
        {"kind": Credential.Kind.RANGE_OFFICER, "number": number, "issuer": issuer, "issued_on": issued_on,
         "valid_until": valid_until or "", "details": ""},
        {"scan": scan}, instance=existing or Credential(user=flow.user), user=flow.user,
    )
    if not form.is_valid():
        raise ValidationError([m for msgs in form.errors.values() for m in msgs])
    credential = form.save(commit=False)
    credential.user = flow.user
    submit(credential, changed_fields=form.changed_data + ["scan"], notify=False)  # pilne powiadomienie przy rezerwacji
    if old_scan and old_scan != credential.scan.name:
        transaction.on_commit(lambda: Credential.scan.field.storage.delete(old_scan))
    flow.credential = credential
    flow.ocr_result = ""
    flow.save(update_fields=["credential", "ocr_result", "updated_at"])
    if not bookings.BookingSettings.load().onsite_auto_verify:
        return credential, None
    scan.seek(0)
    return credential, scan.read()


@transaction.atomic
def book(flow, lane_id, start_hour, end_hour, persons, now=None):
    """Krok 5: rezerwacja na dziś. Zweryfikowane PS → termin trzymany do płatności; legitymacja w weryfikacji →
    rezerwacja „do zatwierdzenia” na VERIFY_MINUTES minut."""
    now = now or timezone.now()
    flow = OnsiteBooking.objects.select_for_update(of=("self",)).select_related("user", "credential", "device").get(pk=flow.pk)
    if flow.reservation_id:
        raise ValidationError("Ta rezerwacja została już złożona.")
    _require_verified(flow)
    if pending_acceptances(flow.user):
        raise ValidationError("Zaakceptuj regulamin.")
    if flow.statement_at is None:
        raise ValidationError("Potwierdź oświadczenie o prawdziwości danych.")
    try:
        lane = Lane.objects.get(pk=int(lane_id), is_active=True)
        start_hour, end_hour, persons = int(start_hour), int(end_hour), int(persons)
    except (Lane.DoesNotExist, TypeError, ValueError):
        raise ValidationError("Wybierz oś i godziny.") from None
    verified = is_range_officer(flow.user)
    pending_ps = bool(flow.credential_id and flow.credential.status == Credential.Status.PENDING)
    if not verified and not pending_ps:
        raise ValidationError("Dodaj zdjęcie legitymacji prowadzącego strzelanie.")
    day = timezone.localdate(now)
    reservation = bookings.create_reservation(
        flow.user, lane, day, start_hour, end_hour, persons, notes=f"Rezerwacja na miejscu (kiosk: {flow.device.name})",
        now=now, channel=bookings.CHANNEL_ONSITE, checkout=verified, ps_pending=not verified,
        notify=False,  # zamiast ogólnych powiadomień: jedno pilne (poniżej), potwierdzenie e-mail po płatności
    )
    flow.reservation = reservation
    if reservation.status == Reservation.Status.PENDING_APPROVAL:
        flow.status = OnsiteBooking.Status.AWAITING
        flow.awaiting_until = now + timedelta(minutes=VERIFY_MINUTES)
        flow.save(update_fields=["reservation", "status", "awaiting_until", "updated_at"])
        if pending_ps and flow.ocr_result == AUTO_OK:
            # Dane zgodne ze zdjęciem legitymacji — zatwierdzenie od razu (zdjęcie usuwane, jak przy decyzji obsługi);
            # rezerwacja przechodzi do płatności przez credential_reviewed.
            from apps.credentials.services import review

            review(flow.credential, None, True, AUTO_NOTE)
            flow.refresh_from_db()
            reservation.refresh_from_db()
            log_event("kiosk.na_miejscu_rezerwacja", reservation, f"{reservation} (kiosk: {flow.device.name}, PS automatycznie)",
                      actor=flow.user)
            return reservation
        url = reverse("panel:credential", args=[flow.credential_id]) if pending_ps else reverse("panel:reservation", args=[reservation.pk])
        notify_board(
            f"PILNE: rezerwacja na miejscu czeka na weryfikację ({VERIFY_MINUTES} min)", "emails/board_generic.txt",
            "bookings.manage_reservations", url=url, event="Rezerwacja na miejscu — weryfikacja",
            body=(f"{flow.user.full_name}, {reservation.lane}, {timezone.localtime(reservation.starts_at):%H:%M}–"
                  f"{timezone.localtime(reservation.ends_at):%H:%M}. Bez decyzji do "
                  f"{timezone.localtime(flow.awaiting_until):%H:%M} rezerwacja anuluje się sama."),
            url_path=url, text="Sprawdź legitymację PS i zatwierdź albo odrzuć.",
        )
    else:
        flow.status = OnsiteBooking.Status.BOOKED
    flow.save(update_fields=["reservation", "status", "awaiting_until", "updated_at"])
    log_event("kiosk.na_miejscu_rezerwacja", reservation, f"{reservation} (kiosk: {flow.device.name})", actor=flow.user)
    return reservation


# --- stan dla kiosku ---------------------------------------------------------------------------------------------


def _sync_status(flow):
    """Stan przebiegu wynikający z rezerwacji (obsługa mogła ją zatwierdzić lub anulować w panelu)."""
    r = flow.reservation
    if flow.status == OnsiteBooking.Status.AWAITING and r is not None:
        if r.status in (Reservation.Status.CONFIRMED, Reservation.Status.HOLD):
            flow.status = OnsiteBooking.Status.BOOKED
        elif r.status == Reservation.Status.CANCELLED:
            flow.status = OnsiteBooking.Status.REJECTED
        if flow.status != OnsiteBooking.Status.AWAITING:
            flow.save(update_fields=["status", "updated_at"])
    return flow


def state(flow, now=None):
    now = now or timezone.now()
    flow = _sync_status(flow)
    user = flow.user
    out = {"ref": str(flow.ref), "status": flow.status, "name": f"{flow.first_name} {flow.last_name}"}
    if user is None:
        return out
    out["name"] = user.full_name
    out["first_name"], out["last_name"] = user.first_name, user.last_name
    out["regulations"] = [{"id": v.pk, "title": str(v), "url": reverse("regulations:version", args=[v.pk])}
                          for v in pending_acceptances(user)]
    out["statement"] = flow.statement_at is not None
    out["statement_text"] = STATEMENT_TEXT
    if is_range_officer(user):
        out["ps"] = "zweryfikowana"
    elif flow.credential_id and flow.credential.status == Credential.Status.PENDING:
        out["ps"] = "weryfikacja"
    else:
        out["ps"] = "brak"
    r = flow.reservation
    if r is not None:
        out["reservation"] = {
            "id": r.pk, "number": r.number, "status": r.status, "status_label": r.get_status_display(), "amount": r.amount,
            "label": f"{r.lane} · {timezone.localtime(r.starts_at):%H:%M}–{timezone.localtime(r.ends_at):%H:%M}",
        }
        out["reservation"]["paid"] = r.payment_status != Reservation.Payment.DUE or r.paid_amount >= r.amount
        out.update(payment_info(r, now))
    if flow.status == OnsiteBooking.Status.AWAITING and flow.awaiting_until:
        out["seconds_left"] = max(int((flow.awaiting_until - now).total_seconds()), 0)
    return out


# --- płatność kodem QR na telefonie osoby -----------------------------------------------------------------------------


def pay_token(reservation):
    return signing.dumps({"r": reservation.pk}, salt=PAY_SALT, compress=True)


def reservation_from_pay_token(token):
    try:
        data = signing.loads(token, salt=PAY_SALT, max_age=PAY_LINK_MINUTES * 60)
        return Reservation.objects.select_related("lane", "user").get(pk=data["r"])
    except (signing.BadSignature, KeyError, TypeError, Reservation.DoesNotExist):
        return None


def payable(reservation):
    return (reservation.payment_status == Reservation.Payment.DUE and reservation.amount > reservation.paid_amount
            and reservation.status in (Reservation.Status.HOLD, Reservation.Status.CONFIRMED))


def payment_info(reservation, now=None):
    """Dla kiosku: kod QR z linkiem do płatności na własnym telefonie (BLIK, przelew online) albo informacja,
    jak zapłacić, gdy płatność online nie jest dostępna dla tej rezerwacji."""
    if not payable(reservation):
        return {}
    due = reservation.amount - reservation.paid_amount
    until = timezone.localtime(reservation.pay_by).strftime("%H:%M") if reservation.status == Reservation.Status.HOLD and reservation.pay_by else ""
    if settings.TPAY_ENABLED and bookings.payment_options(reservation, now)["online"]:
        url = settings.SITE_URL + reverse("payments:guest_pay", args=[pay_token(reservation)])
        return {"pay": {"url": url, "qr": qr_svg_data_uri(url, box_size=6), "amount_due": due, "until": until}}
    return {"pay_info": {"amount_due": due, "until": until,
                         "text": bookings.payment_options(reservation, now)["instructions"] or
                         "Płatność online jest niedostępna — skontaktuj się z obsługą strzelnicy."}}


@transaction.atomic
def approve_for_payment(reservation, by, now=None):
    """Zatwierdzenie rezerwacji na miejscu po weryfikacji legitymacji: także w trakcie trwającej godziny (inaczej niż
    zwykłe zatwierdzenie). Nieopłacona — termin trzymany do płatności online (czas z ustawień); opłacona — potwierdzona."""
    now = now or timezone.now()
    cfg = bookings.BookingSettings.load()
    r = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if r.status != Reservation.Status.PENDING_APPROVAL:
        raise ValidationError("Ta rezerwacja nie czeka na zatwierdzenie.")
    if r.ends_at <= now:
        raise ValidationError("Rezerwacja już się zakończyła — nie można jej zatwierdzić.")
    r.approved_at, r.approved_by, r.needs_approval = now, by, False
    if r.payment_status == Reservation.Payment.DUE and r.amount > r.paid_amount:
        r.status = Reservation.Status.HOLD
        r.pay_by = min(now + timedelta(minutes=cfg.online_payment_minutes or 15), bookings.hold_limit(r.starts_at, r.ends_at, now))
    else:
        r.status, r.pay_by = Reservation.Status.CONFIRMED, None
    r.save(update_fields=["approved_at", "approved_by", "needs_approval", "status", "pay_by", "updated_at"])
    log_event("rezerwacja.zatwierdzenie", r, f"{r} (na miejscu, po weryfikacji legitymacji)", actor=by)
    if r.status == Reservation.Status.HOLD:
        info = payment_info(r, now)
        notify_user(r.user, f"Legitymacja zweryfikowana — opłać rezerwację {r.number}", "emails/onsite_approved.txt",
                    reservation=r, pay_url=info.get("pay", {}).get("url", ""), pay_until=info.get("pay", {}).get("until", ""))
    else:
        bookings._notify_created(r, False)
    return r


# --- po weryfikacji legitymacji i zadania okresowe -----------------------------------------------------------------


def credential_reviewed(credential, reviewer):
    """Wywoływane po decyzji obsługi o dokumencie: zatwierdzona legitymacja potwierdza czekającą rezerwację na miejscu,
    odrzucona — anuluje ją (bez opłat, bo płatność następuje dopiero po weryfikacji)."""
    flows = OnsiteBooking.objects.filter(credential=credential, status=OnsiteBooking.Status.AWAITING).select_related("reservation")
    for flow in flows:
        r = flow.reservation
        if r is None or r.status != Reservation.Status.PENDING_APPROVAL:
            _sync_status(flow)
            continue
        if credential.status == Credential.Status.VERIFIED:
            try:
                approve_for_payment(r, by=reviewer)
            except ValidationError as e:  # np. termin już minął — obsługa zobaczy rezerwację w panelu
                log_event("kiosk.na_miejscu_blad", r, "; ".join(e.messages), actor=reviewer)
                continue
            flow.status = OnsiteBooking.Status.BOOKED
        else:
            bookings.cancel_reservation(r, by=reviewer, as_board=True,
                                        reason="Legitymacja prowadzącego strzelanie nie została zweryfikowana.")
            flow.status = OnsiteBooking.Status.REJECTED
        flow.save(update_fields=["status", "updated_at"])


def expire_pending(now=None):
    """Rezerwacje na miejscu bez weryfikacji w ciągu VERIFY_MINUTES — anulowane bez opłat. Uruchamiać co minutę."""
    now = now or timezone.now()
    count = 0
    for flow in OnsiteBooking.objects.filter(status=OnsiteBooking.Status.AWAITING, awaiting_until__lt=now).select_related("reservation"):
        with transaction.atomic():
            r = flow.reservation
            if r is not None and r.status == Reservation.Status.PENDING_APPROVAL:
                bookings.cancel_reservation(
                    r, by=None, as_board=True, now=now,
                    reason=f"Brak weryfikacji legitymacji PS w ciągu {VERIFY_MINUTES} minut (rezerwacja na miejscu). "
                           "Zarezerwuj przez stronę — legitymacja pozostaje w weryfikacji.",
                )
                flow.status = OnsiteBooking.Status.EXPIRED
                flow.save(update_fields=["status", "updated_at"])
                count += 1
            else:
                _sync_status(flow)
                if flow.status == OnsiteBooking.Status.AWAITING:
                    flow.status = OnsiteBooking.Status.EXPIRED
                    flow.save(update_fields=["status", "updated_at"])
    return count


def purge(now=None):
    """Usuwa dane przebiegów: niedokończone po dobie, pozostałe po RETENTION_DAYS (konto, dokument i rezerwacja zostają)."""
    now = now or timezone.now()
    unfinished = OnsiteBooking.objects.filter(status__in=[OnsiteBooking.Status.CODE_SENT, OnsiteBooking.Status.VERIFIED],
                                              created_at__lt=now - timedelta(days=1))
    old = OnsiteBooking.objects.filter(created_at__lt=now - timedelta(days=RETENTION_DAYS)).exclude(status=OnsiteBooking.Status.AWAITING)
    return unfinished.delete()[0] + old.delete()[0]
