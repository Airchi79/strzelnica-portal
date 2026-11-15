import base64
import re
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

from .conftest import approve, add_credential, grant, login_with_otp, make_user

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
    approve(user)
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


def test_settlement_of_paid_reservation_charges_surcharge(ps, owner, sig):
    from apps.core.models import OutgoingEmail

    r = _reservation(ps, "Oś 25 m nr 1", 1)
    from apps.payments.services import register_manual_payment

    register_manual_payment(ps, r.amount, "gotowka", owner, reservation=r)
    _shooters(r, 2, sig)
    OutgoingEmail.objects.all().delete()
    services.settle_finished_reservations(now=r.ends_at + timedelta(minutes=1))
    r.refresh_from_db()
    # opłacone 1 os. × 2 h; strzelało 2 → dopłata 2 × 35 zł „do zapłaty” (decyzja właściciela: automatycznie)
    assert (r.persons, r.declared_persons, r.amount, r.paid_amount) == (2, 1, 4 * 3500, 2 * 3500)
    assert r.payment_status == Reservation.Payment.DUE
    mail = OutgoingEmail.objects.get(recipients=[ps.email])
    assert "Rozliczenie rezerwacji" in mail.subject and "Dopłata: 70" in mail.body and "Do zapłaty: 70" in mail.body


def test_settlement_of_waived_reservation_only_flags(ps, owner, sig):
    r = _reservation(ps, "Oś 25 m nr 1", 1)
    Reservation.objects.filter(pk=r.pk).update(payment_status=Reservation.Payment.WAIVED)
    _shooters(r, 2, sig)
    services.settle_finished_reservations(now=r.ends_at + timedelta(minutes=1))
    r.refresh_from_db()
    assert r.persons == 1 and r.amount == 2 * 3500 and r.payment_status == Reservation.Payment.WAIVED
    from apps.core.models import Notification

    assert Notification.objects.filter(recipient=owner, title__contains="Rozbieżność").exists()


def test_holder_notified_during_reservation_once_per_new_count(ps, sig, mailoutbox, django_capture_on_commit_callbacks):
    r = _reservation(ps, "Oś 25 m nr 2", 1)  # 1 × 35 zł/h, 2 h
    subject = "więcej strzelających niż zadeklarowano"
    with django_capture_on_commit_callbacks(execute=True):
        _shooters(r, 1, sig)  # zgodnie z deklaracją — bez wiadomości
    assert not [m for m in mailoutbox if subject in m.subject]
    with django_capture_on_commit_callbacks(execute=True):
        _shooters(r, 1, sig)  # 2 strzelających
    mails = [m for m in mailoutbox if subject in m.subject]
    assert len(mails) == 1 and mails[0].to == [ps.email]
    assert "wpisało się 2 strzelających" in mails[0].body and "dopłata przy obecnej liczbie strzelających: 70" in mails[0].body
    r.refresh_from_db()
    assert r.overflow_notified == 2 and r.persons == 1  # w trakcie tylko informacja — należność przeliczy rozliczenie
    with django_capture_on_commit_callbacks(execute=True):
        _shooters(r, 1, sig)  # 3 strzelających: 3 × 35 = 105 → ryczałt 100 zł/h, dopłata rośnie
    assert len([m for m in mailoutbox if subject in m.subject]) == 2
    with django_capture_on_commit_callbacks(execute=True):
        _shooters(r, 1, sig)  # 4 — ryczałt osiągnięty, dopłata już nie rośnie → bez kolejnej wiadomości
    assert len([m for m in mailoutbox if subject in m.subject]) == 2


def test_no_overflow_notice_for_flat_rate_lane(ps, sig, mailoutbox, django_capture_on_commit_callbacks):
    r = _reservation(ps, "Oś 100 m", 4)  # 4 × 60 = 240 → ryczałt 200 zł/h
    with django_capture_on_commit_callbacks(execute=True):
        _shooters(r, 6, sig)
    assert not [m for m in mailoutbox if "więcej strzelających" in m.subject]


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


def test_panel_logbook_permissions_and_access_log(owner_client, running, sig):
    from django.test import Client

    client = Client()  # osoba wpisująca się (bez konta) — osobna przeglądarka niż panel
    client.post(f"/ksiazka/wpis/r/{services.reservation_token(running)}/", {**permit_data(), "purpose": "strzelanie", "signature": sig})
    entry = Entry.objects.get()
    body = owner_client.get("/panel/ksiazka/").content.decode()
    assert entry.number in body
    detail = owner_client.get(f"/panel/ksiazka/{entry.pk}/").content.decode()
    assert "dane i podpis bez zmian" in detail and "data:image/png;base64," not in detail and "AB 123" not in detail
    detail = owner_client.post(f"/panel/ksiazka/{entry.pk}/", {"pokaz": "1"}).content.decode()
    assert "data:image/png;base64," in detail and "AB 123" in detail
    printed = owner_client.get("/panel/ksiazka/druk/").content.decode()
    assert "Książka rejestru pobytu" in printed and "AB 123" in printed
    assert SensitiveAccessLog.objects.filter(resource__startswith="ksiazka:").count() >= 3

    staff = grant(make_user("z@example.com"), "credentials.review_credential")
    c = Client()
    login_with_otp(c, staff)
    assert c.get("/panel/ksiazka/").status_code == 403
    assert c.get("/panel/ksiazka/druk/").status_code == 403


def test_panel_settings_and_poster(owner_client):
    response = owner_client.post("/panel/ksiazka/ustawienia/", {"retention_years": 7, "window_before_minutes": 45, "max_entries_per_hour": 40, "auto_exit_hours": 10})
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


# --- punkt 2: lista z grupowaniem i maskowaniem, eksport, wpis obsługi, auto-wyjście ----------


def _entry(running, sig, **kw):
    data = {**permit_data(**kw), "purpose": kw.pop("purpose", "strzelanie"), "has_permit": True}
    return services.create_entry(data=data, signature_png=services.decode_signature(sig), source="qr_rezerwacji", reservation=running)


def test_panel_list_masks_groups_and_reveals_with_log(owner_client, running, sig):
    e = _entry(running, sig, permit_number="XY 987654")
    body = owner_client.get("/panel/ksiazka/").content.decode()
    assert running.number in body and e.full_name in body
    assert "XY 987654" not in body and "XY•••••54" in body
    before = SensitiveAccessLog.objects.count()
    body = owner_client.post("/panel/ksiazka/", {"pokaz": "1", "od": "", "do": ""}).content.decode()
    assert "XY 987654" in body
    assert SensitiveAccessLog.objects.filter(resource=f"ksiazka:wpis:{e.number}").count() == 1
    assert SensitiveAccessLog.objects.count() == before + 1  # odkrycie: wpis za każdą osobę na stronie
    flat = owner_client.get("/panel/ksiazka/?grupuj=0").content.decode()
    assert "prowadzący:" not in flat and e.number in flat


def test_panel_list_only_open(owner_client, running, sig):
    a = _entry(running, sig, last_name="Otwarty")
    b = _entry(running, sig, last_name="Zamknięty")
    services.register_exit(b)
    body = owner_client.get("/panel/ksiazka/?otwarte=1").content.decode()
    assert a.number in body and b.number not in body


def test_export_xlsx(owner_client, running, sig):
    from io import BytesIO

    from openpyxl import load_workbook

    _entry(running, sig, last_name="=HYPERLINK(1)")
    response = owner_client.get("/panel/ksiazka/eksport.xlsx")
    assert response.status_code == 200 and response["Content-Type"].startswith("application/vnd.openxml")
    ws = load_workbook(BytesIO(response.content)).active
    rows = list(ws.values)
    assert rows[0][0] == "Nr wpisu" and len(rows) == 2
    assert rows[1][5] == "'=HYPERLINK(1)"  # zabezpieczenie przed formułą
    assert rows[1][6] == "AB 123" and rows[1][18] == "zgodna"
    assert SensitiveAccessLog.objects.filter(resource__startswith="ksiazka:eksport:").exists()


def test_export_requires_permission(client):
    staff = grant(make_user("z2@example.com"), "credentials.review_credential")
    login_with_otp(client, staff)
    assert client.get("/panel/ksiazka/eksport.xlsx").status_code == 403
    assert client.get("/panel/ksiazka/nowy-wpis/").status_code == 403


def test_staff_entry_with_earlier_time(owner_client, owner, running, sig):
    start = timezone.localtime(running.starts_at)
    data = {**permit_data(), "purpose": "strzelanie", "reservation": running.pk, "signature": sig,
            "entered_at": start.strftime("%Y-%m-%dT%H:%M")}
    response = owner_client.post("/panel/ksiazka/nowy-wpis/", data)
    assert response.status_code == 302, response.content.decode()[:2000]
    e = Entry.objects.get()
    assert e.source == Entry.Source.STAFF and e.created_by == owner and e.user is None
    assert e.entered_at == running.starts_at and e.range_officer == running.user and e.integrity_ok


def test_staff_entry_rejects_time_outside_window_and_without_signature(owner_client, running, sig):
    too_old = timezone.localtime(timezone.now() - timedelta(hours=30))
    data = {**permit_data(), "purpose": "obserwator", "signature": sig, "entered_at": too_old.strftime("%Y-%m-%dT%H:%M")}
    body = owner_client.post("/panel/ksiazka/nowy-wpis/", data).content.decode()
    assert "ostatnich 24 godzinach" in body
    data["entered_at"] = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
    data["signature"] = ""
    assert owner_client.post("/panel/ksiazka/nowy-wpis/", data).status_code == 200
    assert not Entry.objects.exists()


def test_entered_at_only_for_staff(running, sig):
    with pytest.raises(ValidationError):
        services.create_entry(data={**permit_data(), "purpose": "obserwator", "has_permit": True}, signature_png=services.decode_signature(sig),
                              source="qr_wejscia", entered_at=timezone.now() - timedelta(hours=1))


def test_auto_exit_closes_stale_entries(running, sig):
    raw = services.decode_signature(sig)
    data = {**permit_data(), "purpose": "obserwator", "has_permit": True}
    now = timezone.now()
    old = services.create_entry(data=data, signature_png=raw, source="qr_wejscia", now=now - timedelta(hours=13))
    fresh = services.create_entry(data=data, signature_png=raw, source="qr_wejscia", now=now - timedelta(hours=1))
    shooter = _entry(running, sig)
    assert services.close_stale_entries(now=now) == 1
    old.refresh_from_db(); fresh.refresh_from_db(); shooter.refresh_from_db()
    assert old.exit_auto and old.exited_at == old.entered_at + timedelta(hours=12)
    assert fresh.exited_at is None and shooter.exited_at is None
    # Po końcu rezerwacji pobyt strzelca zamyka się o jej końcu.
    assert services.close_stale_entries(now=running.ends_at + timedelta(minutes=5)) == 1
    shooter.refresh_from_db()
    assert shooter.exited_at == running.ends_at and shooter.exit_auto and shooter.integrity_ok
    # Zamkniętego automatycznie wpisu nie da się już zmienić.
    with pytest.raises(DatabaseError), transaction.atomic():
        Entry.objects.filter(pk=old.pk).update(exited_at=now, exit_auto=False)
    LogbookSettings.objects.filter(pk=1).update(auto_exit_hours=0)
    assert services.close_stale_entries(now=now + timedelta(days=2)) == 0


def test_mask_helpers():
    assert services.mask("AB 123456") == "AB•••••56"
    assert services.mask("Sątok 5, 56-420 Bierutów", 3, 0) == "Sąt••••••"
    assert services.mask("ab") == "a•" and services.mask("") == ""


def test_my_diary_shows_own_visits_and_matched_archive_only(client):
    import datetime as dt

    from django.utils import timezone

    from apps.logbook.models import ArchivedEntry

    from .conftest import add_credential, make_user

    u = make_user("diary@example.com", first_name="Anna", last_name="Kowalska")
    add_credential(u, kind="pozwolenie", number="AB 123456")
    now = timezone.now()
    ArchivedEntry.objects.create(row_hash="h1", started_at=now - dt.timedelta(days=2), first_name="Anna", last_name="Kowalska",
                                 permit_number="ab123456", lane="Oś 25 m", entry_type="strzelanie")
    ArchivedEntry.objects.create(row_hash="h2", started_at=now - dt.timedelta(days=3), first_name="Anna", last_name="Kowalska",
                                 permit_number="ZZ999", lane="Oś 100 m", entry_type="strzelanie")  # imienniczka
    client.force_login(u)
    response = client.get("/ksiazka/dziennik/")
    assert response.status_code == 200
    visits = response.context["visits"]
    assert len(visits) == 1 and visits[0]["lane"] == "Oś 25 m"
    assert response.context["days"] == 1
    csv = client.get(f"/ksiazka/dziennik/?rok={now.year}&format=csv").content.decode("utf-8-sig")
    assert "Oś 25 m" in csv and "Oś 100 m" not in csv


def test_logbook_list_reveal_single_row(owner_client, running, sig):
    a = _entry(running, sig, permit_number="AA 111111")
    b = _entry(running, sig, permit_number="BB 222222", first_name="Ewa")
    before = SensitiveAccessLog.objects.count()
    body = owner_client.post(f"/panel/ksiazka/?grupuj=0", {"pokaz": f"w{a.pk}"}).content.decode()
    assert "AA 111111" in body and "BB 222222" not in body  # odkryty tylko jeden wiersz
    assert SensitiveAccessLog.objects.count() == before + 1
    assert SensitiveAccessLog.objects.filter(resource=f"ksiazka:wpis:{a.number}").exists()


def test_done_page_share_link_and_companion_entry(client, running, sig):
    from apps.logbook.models import Entry

    # wpis bez rezerwacji (z plakatu): link dla towarzyszy prowadzi do wpisu „osoba towarzysząca”
    data = {**permit_data(), "purpose": "obserwator", "has_permit": True}
    host = services.create_entry(data=data, signature_png=services.decode_signature(sig), source="qr_wejscia")
    session = client.session
    session["logbook_recent_entries"] = [host.pk]
    session.save()
    page = client.get(f"/ksiazka/wpisano/{host.pk}/")
    assert page.status_code == 200
    url = page.context["share_url"]
    assert "/ksiazka/dolacz/" in url and page.context["close_at"]
    path = url.split("testserver")[-1] if "testserver" in url else "/" + url.split("/", 3)[3]
    form = client.get(path)
    assert form.status_code == 200 and "osoba towarzysząca" in form.content.decode()
    post = {**permit_data(first_name="Ola"), "purpose": "obserwator", "signature": sig, "statement": "on"}
    client.post(path, post)
    comp = Entry.objects.filter(first_name="Ola").first()
    assert comp and host.number in comp.purpose_note
    # po wyjściu gospodarza link przestaje działać
    services.register_exit(host)
    assert client.get(path).status_code == 410


def test_done_page_with_reservation_shares_reservation_link(client, running, sig):
    e = _entry(running, sig)
    session = client.session
    session["logbook_recent_entries"] = [e.pk]
    session.save()
    url = client.get(f"/ksiazka/wpisano/{e.pk}/").context["share_url"]
    assert "/ksiazka/wpis/r/" in url


# --- panel klienta: wpis i zakończenie pobytu bez kodu QR -------------------------------


def test_my_page_offers_entry_for_own_running_reservation_and_manual_exit(client, running, ps, sig):
    client.force_login(ps)
    token = services.reservation_token(running)
    body = client.get("/ksiazka/moj-kod/").content.decode()
    # token zawiera znacznik czasu (sekunda podpisu) — porównujemy rezerwację, do której prowadzi link, nie sam tekst
    links = re.findall(r"/ksiazka/wpis/r/([^/\"]+)/", body)
    assert [services.reservation_from_token(t) for t in links] == [running]
    client.post(f"/ksiazka/wpis/r/{token}/", {**permit_data(first_name="Piotr", last_name="Prowadzący"), "purpose": "strzelanie",
                                             "signature": sig})
    entry = Entry.objects.get()
    assert entry.user == ps and entry.exited_at is None
    body = client.get("/ksiazka/moj-kod/").content.decode()
    assert "Zakończ pobyt" in body and "/ksiazka/wpis/r/" not in body  # już wpisany — bez ponownego wpisu
    client.post(f"/ksiazka/moje-wyjscie/{entry.pk}/")
    entry.refresh_from_db()
    assert entry.exited_at is not None
    assert "Zakończ pobyt" not in client.get("/ksiazka/moj-kod/").content.decode()


def test_my_exit_only_own_open_entry(client, running, ps, sig):
    token = services.reservation_token(running)
    client.post(f"/ksiazka/wpis/r/{token}/", {**permit_data(), "purpose": "strzelanie", "signature": sig})
    entry = Entry.objects.get()  # wpis anonimowy, bez konta
    other = make_user("inny@example.com")
    client.force_login(other)
    client.post(f"/ksiazka/moje-wyjscie/{entry.pk}/")
    entry.refresh_from_db()
    assert entry.exited_at is None
    assert client.get(f"/ksiazka/moje-wyjscie/{entry.pk}/").status_code == 405


def test_my_page_without_reservation_explains_qr(client, user):
    client.force_login(user)
    body = client.get("/ksiazka/moj-kod/").content.decode()
    assert "nie trwa żadna Twoja rezerwacja" in body


# --- podpowiedź osi po imieniu i nazwisku rezerwującego ---------------------------------


def test_suggest_reservation_exact_name_only(running, ps):
    assert services.suggest_reservation("piotr", "  PROWADZĄCY ") == running  # wielkość liter i spacje bez znaczenia
    assert services.suggest_reservation("Piotr", "Prowadząc") is None  # bez dopasowań częściowych
    assert services.suggest_reservation("Piotr", "") is None
    # dwie trwające rezerwacje tej samej osoby — bez podpowiedzi (nie wiadomo, którą wybrać)
    other = Lane.objects.get(name="Oś 25 m nr 2")
    Reservation.objects.create(number="SATOK-R-TEST-0002", lane=other, user=ps, starts_at=running.starts_at, ends_at=running.ends_at,
                               persons=1, price_rule=running.price_rule, hourly_amount=100, amount=100)
    assert services.suggest_reservation("Piotr", "Prowadzący") is None


def test_suggest_ignores_reservations_not_open(running):
    Reservation.objects.filter(pk=running.pk).update(status=Reservation.Status.CANCELLED)
    assert services.suggest_reservation("Piotr", "Prowadzący") is None


def test_gate_suggest_endpoint(client, running):
    token = LogbookSettings.load().gate_token
    page = client.get(f"/ksiazka/wpis/{token}/").content.decode()
    assert f'data-suggest-url="/ksiazka/wpis/{token}/podpowiedz/"' in page and "logbook-suggest.js" in page
    r = client.get(f"/ksiazka/wpis/{token}/podpowiedz/", {"imie": "Piotr", "nazwisko": "Prowadzący"})
    assert r.status_code == 200 and r.json() == {"reservation": running.pk}  # sam identyfikator, bez danych osobowych
    assert client.get(f"/ksiazka/wpis/{token}/podpowiedz/", {"imie": "Jan", "nazwisko": "Obcy"}).json() == {"reservation": None}
    assert client.get("/ksiazka/wpis/zly-token/podpowiedz/", {"imie": "Piotr", "nazwisko": "Prowadzący"}).status_code == 404


def test_gate_suggest_rate_limited(client, running, monkeypatch):
    monkeypatch.setattr(services, "SUGGEST_PER_HOUR", 2)
    token = LogbookSettings.load().gate_token
    url = f"/ksiazka/wpis/{token}/podpowiedz/"
    for _ in range(2):
        assert client.get(url, {"imie": "Jan", "nazwisko": "Kowalski"}).status_code == 200
    assert client.get(url, {"imie": "Piotr", "nazwisko": "Prowadzący"}).status_code == 429


# --- szybkie akcje w pulpicie i „+ Dodaj wpis” rezerwującego w panelu ------------------------


def test_dashboard_quick_logbook_actions(client, ps, running, sig):
    client.force_login(ps)
    page = client.get("/pulpit/").content.decode()
    # token zawiera znacznik czasu podpisu (sekundy) — porównujemy rezerwację, na którą wskazuje, a nie sam tekst tokenu
    links = re.findall(r'href="/ksiazka/wpis/r/([^"/]+)/"', page)
    assert "Zarezerwuj oś" in page and [services.reservation_from_token(t) for t in links] == [running]  # od razu wpis
    assert 'href="/ksiazka/moj-kod/">Zakończ pobyt' in page  # bez otwartego pobytu → strona książki z informacją
    assert "Nie masz teraz otwartego pobytu" in client.get("/ksiazka/moj-kod/").content.decode()
    entry = services.create_entry(data={**permit_data(), "purpose": "strzelanie", "has_permit": True},
                                  signature_png=services.decode_signature(sig), source="qr_rezerwacji", reservation=running, user=ps)
    page = client.get("/pulpit/").content.decode()
    assert f'action="/ksiazka/moje-wyjscie/{entry.pk}/"' in page  # jeden otwarty pobyt → przycisk od razu zapisuje wyjście
    client.post(f"/ksiazka/moje-wyjscie/{entry.pk}/")
    entry.refresh_from_db()
    assert entry.exited_at is not None


def test_panel_add_entry_prefilled_with_holder(owner_client, ps, running):
    add_credential(ps, kind="pozwolenie", number="AB 777")
    page = owner_client.get(f"/panel/ksiazka/nowy-wpis/?rezerwacja={running.pk}&rezerwujacy=1").content.decode()
    assert 'value="Piotr"' in page and 'value="Prowadzący"' in page and 'value="AB 777"' in page
    assert SensitiveAccessLog.objects.filter(subject=ps, reason__contains="wpisu rezerwującego").exists()
    # przycisk przy rezerwacji bez wpisu (lista dniami i szczegóły rezerwacji)
    days = owner_client.get(f"/panel/rezerwacje/dniami/?od={timezone.localdate():%Y-%m-%d}").content.decode()
    assert f"?rezerwacja={running.pk}&amp;rezerwujacy=1" in days
    detail = owner_client.get(f"/panel/rezerwacje/{running.pk}/").content.decode()
    assert "+ Dodaj wpis rezerwującego" in detail


# --- wpis obsługi podpisany zdalnie przez rezerwującego --------------------------------------


def _send_for_signature(owner_client, running, **extra):
    entered = max(running.starts_at, timezone.now() - timedelta(minutes=1))  # w czasie rezerwacji, nie w przyszłości
    data = {"entered_at": timezone.localtime(entered).strftime("%Y-%m-%dT%H:%M"),
            "purpose": "strzelanie", "reservation": running.pk, "first_name": "Piotr", "last_name": "Prowadzący",
            "has_permit": "tak", "permit_number": "AB 777", "permit_issuer": "KWP Wrocław", "do_podpisu": "1", **extra}
    return owner_client.post(f"/panel/ksiazka/nowy-wpis/?rezerwacja={running.pk}&rezerwujacy=1", data)


def test_remote_signature_flow(owner_client, client, ps, running, sig):
    from apps.core.models import OutgoingEmail
    from apps.logbook.models import EntrySignatureRequest

    r = _send_for_signature(owner_client, running)
    assert r.status_code == 302 and r.url == f"/panel/rezerwacje/{running.pk}/"
    req = EntrySignatureRequest.objects.get()
    assert req.user == ps and req.status == "oczekuje" and not Entry.objects.exists()  # w książce dopiero po podpisie
    mail = OutgoingEmail.objects.get(recipients=[ps.email])
    assert f"/ksiazka/podpis/{req.pk}/" in mail.body
    assert "czeka na podpis" in owner_client.get(f"/panel/rezerwacje/{running.pk}/").content.decode()
    # inna osoba nie zobaczy wpisu; rezerwujący — tak (z listą na stronie książki)
    other = make_user("obcy@example.com")
    client.force_login(other)
    assert client.get(f"/ksiazka/podpis/{req.pk}/").status_code == 404
    client.force_login(ps)
    assert "Sprawdź i podpisz" in client.get("/ksiazka/moj-kod/").content.decode()
    page = client.get(f"/ksiazka/podpis/{req.pk}/").content.decode()
    assert "AB 777" in page and "Podpisuję wpis" in page
    assert client.post(f"/ksiazka/podpis/{req.pk}/", {"signature": sig}).status_code == 400  # bez oświadczenia
    r = client.post(f"/ksiazka/podpis/{req.pk}/", {"signature": sig, "statement": "on"})
    assert r.status_code == 302
    e = Entry.objects.get()
    assert (e.source, e.user, e.reservation, e.permit_number) == (Entry.Source.REMOTE, ps, running, "AB 777")
    assert e.entered_at == req.entered_at and e.created_by is not None
    req.refresh_from_db()
    assert req.status == "podpisany" and req.entry == e
    assert client.post(f"/ksiazka/podpis/{req.pk}/", {"signature": sig, "statement": "on"}).status_code == 400  # tylko raz
    assert Entry.objects.count() == 1


def test_remote_signature_only_for_holder_and_expires(owner_client, client, ps, running, sig):
    from apps.logbook.models import EntrySignatureRequest

    page = _send_for_signature(owner_client, running, first_name="Anna", last_name="Nowak").content.decode()
    assert "tylko rezerwującemu" in page and not EntrySignatureRequest.objects.exists()
    _send_for_signature(owner_client, running)
    _send_for_signature(owner_client, running)  # ponowne wysłanie anuluje poprzedni link
    assert list(EntrySignatureRequest.objects.order_by("created_at").values_list("status", flat=True)) == ["anulowany", "oczekuje"]
    req = EntrySignatureRequest.objects.get(status="oczekuje")
    EntrySignatureRequest.objects.filter(pk=req.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    client.force_login(ps)
    assert "nieaktualny" in client.get(f"/ksiazka/podpis/{req.pk}/").content.decode()
    assert client.post(f"/ksiazka/podpis/{req.pk}/", {"signature": sig, "statement": "on"}).status_code == 400
    assert not Entry.objects.exists()
