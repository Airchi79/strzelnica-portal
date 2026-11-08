import json
import uuid
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.bookings import services as booking_services
from apps.bookings.models import Reservation
from apps.facilities.models import Lane
from apps.kiosk import services
from apps.kiosk.models import KioskDevice, token_hash
from apps.logbook import services as logbook
from apps.logbook.models import Entry

from .conftest import add_credential, approve, grant, login_with_otp, make_user
from .test_logbook import signature_data_url

pytestmark = pytest.mark.django_db
H = {"HTTP_X_KIOSK": "1"}


@pytest.fixture
def paired(owner):
    device, code = services.create_device("Tablet przy wejściu", owner)
    c = Client()
    response = c.post("/kiosk/parowanie/", {"code": code})
    assert response.status_code == 302 and services.COOKIE in response.cookies
    device.refresh_from_db()
    return device, c


@pytest.fixture
def running(db):
    ps = make_user("ps@example.com", first_name="Piotr", last_name="Prowadzący")
    add_credential(ps)
    approve(ps)
    lane = Lane.objects.get(name="Oś 25 m nr 1")
    start = booking_services.hour_floor(timezone.now())
    rule = lane.price_rule_for(start.date())
    return Reservation.objects.create(number="SATOK-R-K-1", lane=lane, user=ps, starts_at=start, ends_at=start + timedelta(hours=1),
                                      persons=2, price_rule=rule, hourly_amount=100, amount=100)


def payload(**kw):
    data = {"ref": str(uuid.uuid4()), "purpose": "obserwator", "first_name": "Anna", "last_name": "Nowak", "has_permit": "nie",
            "address": "Sątok 1", "statement": True, "signature": signature_data_url()}
    data.update(kw)
    return data


def post(c, path, data):
    return c.post(path, json.dumps(data), content_type="application/json", **H)


def test_pairing_flow_and_token_hashed(owner, paired):
    device, c = paired
    raw = c.cookies[services.COOKIE].value
    assert device.token_hash == token_hash(raw) and raw not in device.token_hash and device.paired_at
    assert c.cookies[services.COOKIE]["httponly"] and c.cookies[services.COOKIE]["samesite"] == "Strict"
    body = c.get("/kiosk/").content.decode()
    assert "Wpis do książki pobytu" in body and "Witamy" in body
    # Kod jest jednorazowy.
    assert Client().post("/kiosk/parowanie/", {"code": "00000000"}).status_code == 400
    assert "Parowanie kiosku" in Client().get("/kiosk/").content.decode()


def test_api_requires_device_and_header(paired):
    _, c = paired
    assert c.get("/kiosk/api/stan/").status_code == 400  # bez nagłówka X-Kiosk
    assert Client().get("/kiosk/api/stan/", **H).status_code == 401
    assert c.get("/kiosk/api/stan/", **H).status_code == 200


def test_state_lists_todays_reservations_with_tokens(paired, running):
    _, c = paired
    data = c.get("/kiosk/api/stan/", **H).json()
    r = data["reservations"][0]
    assert r["id"] == running.pk and r["token"] == logbook.reservation_token(running) and "Prowadzący" not in r["label"]


def test_entry_online_and_idempotent_retry(paired, running):
    device, c = paired
    item = payload(purpose="strzelanie", reservation=running.pk)
    first = post(c, "/kiosk/api/wpis/", item)
    assert first.status_code == 200, first.content
    again = post(c, "/kiosk/api/wpis/", item)
    assert again.json()["duplicate"] and Entry.objects.count() == 1
    e = Entry.objects.get()
    assert e.source == Entry.Source.DEVICE and e.device == device and e.reservation == running and e.integrity_ok and e.received_at


def test_offline_queued_entry_keeps_original_time(paired, running):
    _, c = paired
    at = running.starts_at + timedelta(minutes=5)
    if at > timezone.now():
        at = timezone.now() - timedelta(minutes=10)
        at = max(at, running.starts_at - timedelta(minutes=20))
    item = payload(purpose="strzelanie", reservation=running.pk, entered_at=at.isoformat())
    response = post(c, "/kiosk/api/wpis/", item)
    assert response.status_code == 200, response.content
    e = Entry.objects.get()
    if timezone.now() - at > timedelta(minutes=2):
        assert abs((e.entered_at - at).total_seconds()) < 1


def test_offline_entry_too_old_rejected(paired):
    _, c = paired
    old = (timezone.now() - timedelta(hours=80)).isoformat()
    response = post(c, "/kiosk/api/wpis/", payload(entered_at=old))
    assert response.status_code == 400 and "72" in response.json()["error"]


def test_validation_errors(paired):
    _, c = paired
    response = post(c, "/kiosk/api/wpis/", payload(address="", signature=""))
    assert response.status_code == 400 and not Entry.objects.exists()
    assert post(c, "/kiosk/api/wpis/", {"ref": "zle"}).status_code == 400


def test_personal_qr_prefill_and_link(paired):
    _, c = paired
    person = make_user("osoba@example.com", first_name="Ola", last_name="Strzelczyk")
    token = logbook.personal_token(person)
    data = c.get(f"/kiosk/api/osoba/{token}/", **H).json()
    assert data["prefill"]["first_name"] == "Ola"
    assert post(c, "/kiosk/api/wpis/", payload(first_name="Ola", last_name="Strzelczyk", person=token)).status_code == 200
    assert Entry.objects.get().user == person
    assert c.get("/kiosk/api/osoba/zly-token/", **H).status_code == 404


def test_exit_search_and_exit(paired):
    _, c = paired
    post(c, "/kiosk/api/wpis/", payload(last_name="Wyjściowa"))
    found = c.get("/kiosk/api/wyjscie/?nazwisko=wyj", **H).json()["entries"]
    assert len(found) == 1 and found[0]["name"] == "Anna W."
    assert post(c, f"/kiosk/api/wyjscie/{found[0]['id']}/", {}).status_code == 200
    assert Entry.objects.get().exited_at is not None
    assert c.get("/kiosk/api/wyjscie/?nazwisko=wyj", **H).json()["entries"] == []


def test_revoked_device_and_new_code_invalidate(owner, paired):
    device, c = paired
    services.new_pairing_code(device, owner)
    assert c.get("/kiosk/api/stan/", **H).status_code == 401
    device2, code = services.create_device("RPi", owner)
    c2 = Client()
    c2.post("/kiosk/parowanie/", {"code": code})
    services.revoke(device2, owner)
    assert c2.get("/kiosk/api/stan/", **H).status_code == 401


def test_failed_report_notifies(paired, owner):
    from apps.core.models import Notification

    _, c = paired
    assert post(c, "/kiosk/api/odrzucony/", {"reason": "brak adresu"}).status_code == 200
    assert Notification.objects.filter(recipient=owner).exists()


def test_panel_kiosks(owner_client):
    body = owner_client.post("/panel/ksiazka/kioski/", {"action": "add", "name": "Tablet"}).content.decode()
    assert "Kod parowania" in body and KioskDevice.objects.filter(name="Tablet").exists()
    d = KioskDevice.objects.get()
    owner_client.post("/panel/ksiazka/kioski/", {"action": "revoke", "device": d.pk})
    d.refresh_from_db()
    assert not d.is_active
    staff = grant(make_user("v@example.com"), "logbook.view_logbook")
    c = Client()
    login_with_otp(c, staff)
    assert c.get("/panel/ksiazka/kioski/").status_code == 403


def test_pairing_rate_limit():
    c = Client()
    for _ in range(services.MAX_PAIR_ATTEMPTS):
        c.post("/kiosk/parowanie/", {"code": "12345678"})
    assert "Zbyt wiele prób" in c.post("/kiosk/parowanie/", {"code": "12345678"}).content.decode()


def test_camera_choice_front_by_default_and_set_in_panel(owner_client, paired):
    device, c = paired
    assert device.camera == KioskDevice.Camera.FRONT
    assert 'data-camera="user"' in c.get("/kiosk/").content.decode()
    owner_client.post("/panel/ksiazka/kioski/", {"action": "camera", "device": device.pk, "camera": "environment"})
    device.refresh_from_db()
    assert device.camera == KioskDevice.Camera.BACK
    assert 'data-camera="environment"' in c.get("/kiosk/").content.decode()
    owner_client.post("/panel/ksiazka/kioski/", {"action": "camera", "device": device.pk, "camera": "zla"})
    device.refresh_from_db()
    assert device.camera == KioskDevice.Camera.BACK
