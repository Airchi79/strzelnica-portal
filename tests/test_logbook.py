import base64
from datetime import timedelta
from io import BytesIO

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone
from PIL import Image, ImageDraw

from apps.audit.models import SensitiveAccessLog
from apps.bookings import services as booking_services
from apps.bookings.models import Reservation
from apps.facilities.models import Lane
from apps.logbook import services
from apps.logbook.models import Entry, LogbookSettings

from .conftest import add_credential, grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db


def signature_data_url(blank=False):
    img = Image.new("RGBA", (600, 180), (0, 0, 0, 0))
    if not blank:
        d = ImageDraw.Draw(img)
        d.line([(20, 120), (120, 40), (220, 140), (330, 50), (450, 130), (560, 60)], fill=(17, 17, 17, 255), width=4)
    buf = BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()



@pytest.fixture
def sig():
    return signature_data_url()


@pytest.fixture
def ps(db):
    user = make_user("ps@example.com", first_name="Piotr", last_name="Prowadzący")
    add_credential(user)
    return user


@pytest.fixture
def running(ps):
    """Rezerwacja trwająca teraz (bieżąca godzina) na osi 25 m nr 1 dla 2 osób."""
    # Tworzona wprost (z pominięciem godzin otwarcia), aby test działał o każdej porze doby.
    lane = Lane.objects.get(name="Oś 25 m nr 1")
    start = booking_services.hour_floor(timezone.now())
    rule = lane.price_rule_for(start.date())
    return Reservation.objects.create(
        number="SATOK-R-TEST-0001", lane=lane, user=ps, starts_at=start, ends_at=start + timedelta(hours=1), persons=2,
        price_rule=rule, hourly_amount=rule.hourly_amount(2), amount=rule.hourly_amount(2),
    )


def permit_data(**kw):
    data = {"first_name": "Anna", "last_name": "Nowak", "has_permit": "tak", "permit_number": "ab 123", "permit_issuer": "KWP Wrocław",
            "address": "", "statement": "on", "purpose_note": ""}
    data.update(kw)
    return data


# --- formularz z kodu QR rezerwacji ----------------------------------------------


def test_entry_by_reservation_qr_anonymous(client, running, sig):
    token = services.reservation_token(running)
    response = client.post(f"/ksiazka/wpis/r/{token}/", {**permit_data(), "purpose": "strzelanie", "signature": sig})
    assert response.status_code == 302, response.content.decode()[:2000]
    entry = Entry.objects.get()
    assert entry.number.startswith(f"KP-{timezone.localdate().year}-")
    assert entry.reservation == running and entry.range_officer == running.user
    assert entry.permit_number == "AB 123" and entry.address == ""
    assert entry.source == Entry.Source.RESERVATION_QR and entry.user is None
    assert entry.integrity_ok
    page = client.get(response.url)
    assert entry.number in page.content.decode()


def test_no_permit_requires_address(client, running, sig):
    token = services.reservation_token(running)
    data = {**permit_data(has_permit="nie", permit_number="", permit_issuer=""), "purpose": "obserwator", "signature": sig}
    response = client.post(f"/ksiazka/wpis/r/{token}/", data)
    assert "adres" in response.content.decode().lower() and not Entry.objects.exists()
    client.post(f"/ksiazka/wpis/r/{token}/", {**data, "address": "Sątok 1, 56-420 Bierutów"})
    e = Entry.objects.get()
    assert not e.has_permit and e.address and e.permit_number == "" and e.purpose == "obserwator"


def test_blank_or_missing_signature_rejected(client, running):
    token = services.reservation_token(running)
    for sig in ["", signature_data_url(blank=True), "data:image/png;base64,AAAA", "data:text/html;base64,PGI+"]:
        response = client.post(f"/ksiazka/wpis/r/{token}/", {**permit_data(), "purpose": "strzelanie", "signature": sig})
        assert response.status_code == 200
    assert not Entry.objects.exists()


def test_statement_required(client, running, sig):
    token = services.reservation_token(running)
    data = {**permit_data(), "purpose": "strzelanie", "signature": sig}
    data.pop("statement")
    client.post(f"/ksiazka/wpis/r/{token}/", data)
    assert not Entry.objects.exists()


def test_reservation_qr_outside_window(client, ps, sig):
    lane = Lane.objects.get(name="Oś 35 m")
    future = booking_services.create_reservation(ps, lane, timezone.localdate() + timedelta(days=3), 10, 11, 1)
    token = services.reservation_token(future)
    response = client.get(f"/ksiazka/wpis/r/{token}/")
    assert response.status_code == 410
    with pytest.raises(ValidationError):
        services.create_entry(data={**permit_data(), "purpose": "strzelanie", "has_permit": True}, signature_png=b"x", source="qr_rezerwacji", reservation=future)


def test_forged_tokens_rejected(client, running):
    assert client.get("/ksiazka/wpis/r/podrobiony/").status_code == 404
    assert client.get("/ksiazka/wpis/zly-token/").status_code == 404
    assert client.get("/ksiazka/osoba/podrobiony/").status_code in (302, 404)


def test_logged_in_user_prefilled(client, running):
    user = make_user("ania@example.com", first_name="Anna", last_name="Kowal")
    add_credential(user, kind="pozwolenie", number="PB-777")
    client.force_login(user)
    body = client.get(f"/ksiazka/wpis/r/{services.reservation_token(running)}/").content.decode()
    assert 'value="PB-777"' in body and 'value="Kowal"' in body


# --- kod przy wejściu ---------------------------------------------------------------


def test_gate_entry_work_without_reservation(client, sig):
    token = LogbookSettings.load().gate_token
    data = {**permit_data(has_permit="nie", permit_number="", permit_issuer="", address="Bierutów"), "purpose": "prace", "signature": sig}
    response = client.post(f"/ksiazka/wpis/{token}/", data)
    assert "Opisz krótko" in response.content.decode()
    client.post(f"/ksiazka/wpis/{token}/", {**data, "purpose_note": "koszenie trawy"})
    e = Entry.objects.get()
    assert e.purpose == "prace" and e.reservation is None and e.range_officer is None and e.source == Entry.Source.GATE_QR


def test_gate_shooting_requires_open_reservation(client, running, sig):
    token = LogbookSettings.load().gate_token
    data = {**permit_data(), "purpose": "strzelanie", "signature": sig}
    response = client.post(f"/ksiazka/wpis/{token}/", data)
    assert "wybierz rezerwację" in response.content.decode() and not Entry.objects.exists()
    body = client.get(f"/ksiazka/wpis/{token}/").content.decode()
    assert running.user.last_name not in body  # lista rezerwacji bez nazwisk
    client.post(f"/ksiazka/wpis/{token}/", {**data, "reservation": running.pk})
    assert Entry.objects.get().reservation == running


def test_gate_token_rotation(client, owner):
    old = LogbookSettings.load().gate_token
    services.rotate_gate_token(owner)
    assert client.get(f"/ksiazka/wpis/{old}/").status_code == 404
    assert client.get(f"/ksiazka/wpis/{LogbookSettings.load().gate_token}/").status_code == 200


def test_rate_limit(client, sig, settings):
    cfg = LogbookSettings.load()
    cfg.max_entries_per_hour = 2
    cfg.save()
    token = cfg.gate_token
    data = {**permit_data(has_permit="nie", permit_number="", permit_issuer="", address="X"), "purpose": "obserwator", "signature": sig}
    for _ in range(3):
        response = client.post(f"/ksiazka/wpis/{token}/", data)
    assert Entry.objects.count() == 2 and "Zbyt wiele" in response.content.decode()


# --- wypis i niezmienność --------------------------------------------------------------


def test_exit_once_and_immutability(client, running, sig):
    token = services.reservation_token(running)
    response = client.post(f"/ksiazka/wpis/r/{token}/", {**permit_data(), "purpose": "strzelanie", "signature": sig})
    entry = Entry.objects.get()
    exit_url = f"/ksiazka/wyjscie/{services.exit_token(entry)}/"
    client.post(exit_url)
    entry.refresh_from_db()
    assert entry.exited_at is not None and entry.integrity_ok
    with pytest.raises(DatabaseError), transaction.atomic():
        Entry.objects.filter(pk=entry.pk).update(last_name="Zmieniony")
    with pytest.raises(DatabaseError), transaction.atomic():
        Entry.objects.filter(pk=entry.pk).update(exited_at=timezone.now())  # drugi wypis
    with pytest.raises(DatabaseError), transaction.atomic():
        Entry.objects.filter(pk=entry.pk).delete()
    assert Entry.objects.get(pk=entry.pk).last_name == "Nowak"


def test_done_page_only_for_author(client, running, sig):
    client.post(f"/ksiazka/wpis/r/{services.reservation_token(running)}/", {**permit_data(), "purpose": "strzelanie", "signature": sig})
    entry = Entry.objects.get()
    from django.test import Client

    assert Client().get(f"/ksiazka/wpisano/{entry.pk}/").status_code == 404


def test_purge_after_retention(running, sig):
    raw = services.decode_signature(sig)
    data = {**permit_data(), "purpose": "obserwator", "has_permit": True}
    old = services.create_entry(data=data, signature_png=raw, source="qr_wejscia", now=timezone.now() - timedelta(days=6 * 365))
    fresh = services.create_entry(data=data, signature_png=raw, source="qr_wejscia")
    assert services.purge_expired() == 1
    assert list(Entry.objects.values_list("pk", flat=True)) == [fresh.pk]
    assert old.pk != fresh.pk


# --- kod osobisty strzelca ------------------------------------------------------------


def test_personal_qr_added_by_range_officer(client, running, sig):
    person = make_user("osoba@example.com", first_name="Ewa", last_name="Strzelec")
    add_credential(person, kind="pozwolenie", number="PB-1")
    token = services.personal_token(person)
    client.force_login(running.user)
    body = client.get(f"/ksiazka/osoba/{token}/").content.decode()
    assert "Ewa" in body and 'value="PB-1"' in body
    client.post(f"/ksiazka/osoba/{token}/", {**permit_data(first_name="Ewa", last_name="Strzelec", permit_number="PB-1"), "purpose": "strzelanie", "reservation": running.pk, "signature": sig})
    e = Entry.objects.get()
    assert e.user == person and e.created_by == running.user and e.source == Entry.Source.PERSONAL_QR and e.reservation == running


def test_personal_qr_requires_own_running_reservation(client, running):
    person = make_user("osoba@example.com")
    other = make_user("inny-ps@example.com")
    add_credential(other, number="PS-9")
    client.force_login(other)
    assert client.get(f"/ksiazka/osoba/{services.personal_token(person)}/").status_code == 403


def test_personal_qr_regeneration_revokes_old(client, user):
    old = services.personal_token(user)
    assert services.user_from_personal_token(old) == user
    client.force_login(user)
    client.post("/ksiazka/moj-kod/")
    user.refresh_from_db()
    assert services.user_from_personal_token(old) is None
    assert services.user_from_personal_token(services.personal_token(user)) == user


# --- rozliczenie rezerwacji wg książki ------------------------------------------------


def _shooters(reservation, n, sig):
    raw = services.decode_signature(sig)
    for i in range(n):
        services.create_entry(
            data={**permit_data(last_name=f"S{i}"), "purpose": "strzelanie", "has_permit": True},
            signature_png=raw, source="qr_rezerwacji", reservation=reservation, now=reservation.starts_at + timedelta(minutes=5),
        )


def _reservation(ps, lane_name, persons, hours=2):
    lane = Lane.objects.get(name=lane_name)
    day = timezone.localdate() + timedelta(days=2)
    return booking_services.create_reservation(ps, lane, day, 10, 10 + hours, persons)


def test_settlement_raises_amount_up_to_flat_rate(ps, sig):
    r = _reservation(ps, "Oś 25 m nr 2", 2)  # 2 × 35 = 70 zł/h
    _shooters(r, 3, sig)  # 3 × 35 = 105 → ryczałt 100 zł/h
    assert services.settle_finished_reservations(now=r.ends_at + timedelta(minutes=1)) == 1
    r.refresh_from_db()
    assert (r.persons, r.declared_persons, r.hourly_amount, r.amount) == (3, 2, 10000, 20000)
    assert services.settle_finished_reservations(now=r.ends_at + timedelta(hours=2)) == 0  # tylko raz


def test_settlement_no_change_when_flat_or_fewer(ps, sig):
    flat = _reservation(ps, "Oś 100 m", 4)  # ryczałt 200 zł/h
    _shooters(flat, 6, sig)
    fewer = _reservation(ps, "Oś 35 m", 3)
    _shooters(fewer, 1, sig)
    services.settle_finished_reservations(now=flat.ends_at + timedelta(minutes=1))
    flat.refresh_from_db()
    fewer.refresh_from_db()
    assert (flat.persons, flat.amount) == (4, 40000)
    assert (fewer.persons, fewer.amount) == (3, 20000)
    assert flat.settled_at and fewer.settled_at


def test_settlement_of_paid_reservation_only_flags(ps, owner, sig):
    r = _reservation(ps, "Oś 25 m nr 1", 1)
    from apps.payments.services import register_manual_payment

    register_manual_payment(ps, r.amount, "gotowka", owner, reservation=r)
    _shooters(r, 2, sig)
    services.settle_finished_reservations(now=r.ends_at + timedelta(minutes=1))
    r.refresh_from_db()
    assert r.persons == 1 and r.amount == 2 * 3500 and r.payment_status == Reservation.Payment.PAID
    from apps.core.models import Notification

    assert Notification.objects.filter(recipient=owner, title__contains="Rozbieżność").exists()


def test_observers_do_not_count(ps, sig):
    r = _reservation(ps, "Oś 25 m nr 1", 1)
    raw = services.decode_signature(sig)
    services.create_entry(
        data={**permit_data(), "purpose": "obserwator", "has_permit": True}, signature_png=raw, source="qr_rezerwacji",
        reservation=r, now=r.starts_at,
    )
    services.settle_finished_reservations(now=r.ends_at + timedelta(minutes=1))
    r.refresh_from_db()
    assert r.persons == 1


# --- panel ---------------------------------------------------------------------------


def test_panel_logbook_permissions_and_access_log(client, owner_client, running, sig):
    client.post(f"/ksiazka/wpis/r/{services.reservation_token(running)}/", {**permit_data(), "purpose": "strzelanie", "signature": sig})
    entry = Entry.objects.get()
    body = owner_client.get("/panel/ksiazka/").content.decode()
    assert entry.number in body
    detail = owner_client.get(f"/panel/ksiazka/{entry.pk}/").content.decode()
    assert "dane i podpis bez zmian" in detail and "data:image/png;base64," in detail
    printed = owner_client.get("/panel/ksiazka/druk/").content.decode()
    assert "Książka rejestru pobytu" in printed and "AB 123" in printed
    assert SensitiveAccessLog.objects.filter(resource__startswith="ksiazka:").count() >= 3

    from django.test import Client

    staff = grant(make_user("z@example.com"), "credentials.review_credential")
    c = Client()
    login_with_otp(c, staff)
    assert c.get("/panel/ksiazka/").status_code == 403
    assert c.get("/panel/ksiazka/druk/").status_code == 403


def test_panel_settings_and_poster(owner_client):
    response = owner_client.post("/panel/ksiazka/ustawienia/", {"retention_years": 7, "window_before_minutes": 45, "max_entries_per_hour": 40})
    assert response.status_code == 302
    assert LogbookSettings.load().retention_years == 7
    old = LogbookSettings.load().gate_token
    owner_client.post("/panel/ksiazka/ustawienia/nowy-kod/")
    assert LogbookSettings.load().gate_token != old
    assert "data:image/svg+xml" in owner_client.get("/panel/ksiazka/plakat/").content.decode()


def test_reservation_detail_shows_qr_to_owner(client, running):
    client.force_login(running.user)
    body = client.get(f"/rezerwacje/{running.pk}/").content.decode()
    assert "Kod QR do książki pobytu" in body
