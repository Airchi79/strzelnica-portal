"""Kiosk: strzelanie poza godzinami otwarcia (rezerwacja „teraz”, do północy), potwierdzenie prowadzącego kodem QR konta
albo nazwiskiem i numerem legitymacji, rezygnacja z opłaty → wezwanie do opuszczenia obiektu."""

import json
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from apps.bookings import services as bookings
from apps.bookings.models import BookingSettings, Reservation
from apps.core.models import OutgoingEmail
from apps.facilities.models import Lane
from apps.kiosk import ocr, onsite, services
from apps.kiosk.models import OnsiteBooking
from apps.logbook import services as logbook

from .conftest import add_credential, approve, make_user
from .test_kiosk_ocr import LOK
from .test_kiosk_onsite import last_code, photo_data_url

pytestmark = pytest.mark.django_db
DAY = timezone.localdate() + timedelta(days=7)  # ten sam dzień tygodnia co dziś — oś 100 m czynna 9–18


@pytest.fixture
def lane(db):
    return Lane.objects.get(name="Oś 100 m")


@pytest.fixture
def night():
    return bookings.local_dt(DAY, 20) + timedelta(minutes=10)


@pytest.fixture
def enabled(db):
    cfg = BookingSettings.load()
    cfg.after_hours_kiosk = True
    cfg.save()
    return cfg


@pytest.fixture
def device(owner):
    return services.create_device("Kiosk", owner)[0]


@pytest.fixture
def ps(db):
    user = make_user("ps@example.com", first_name="Piotr", last_name="Prowadzący")
    add_credential(user, number="PS 777")
    approve(user)
    return user


def qr_flow(device, user):
    flow, _ = onsite.start_qr(device, logbook.personal_token(user))
    onsite.accept_statement(flow)
    return flow


def test_after_hours_run_and_slots(lane, enabled, night):
    hours = lane.hours_for(DAY)
    assert hours.opens.hour <= 9 and hours.closes.hour <= 20
    assert bookings.after_hours_run(lane, night) == [20, 21, 22, 23]  # do północy
    morning = bookings.local_dt(DAY, hours.opens.hour - 2) + timedelta(minutes=5)
    assert bookings.after_hours_run(lane, morning) == [hours.opens.hour - 2, hours.opens.hour - 1]  # do otwarcia
    assert bookings.after_hours_run(lane, bookings.local_dt(DAY, 12)) == []  # oś czynna
    slot = next(s for s in onsite.free_slots(night) if s["lane"] == lane.pk)
    assert slot["night"] == [20, 21, 22, 23] and slot["hours"] == []


def test_disabled_by_default(lane, night, ps, device):
    assert BookingSettings.load().after_hours_kiosk is False
    assert all(not s["night"] for s in onsite.free_slots(night))
    flow = qr_flow(device, ps)
    with pytest.raises(ValidationError, match="czynna"):
        onsite.book(flow, lane.pk, 20, 21, 1, now=night)


def test_only_now_until_midnight(lane, enabled, night):
    assert onsite.price(lane.pk, 20, 24, 2, now=night)["night"] is True
    with pytest.raises(ValidationError, match="od bieżącej godziny"):
        onsite.price(lane.pk, 21, 22, 2, now=night)  # nie z wyprzedzeniem
    with pytest.raises(ValueError):  # poza godzinami tylko na miejscu
        bookings.create_reservation(make_user("x@example.com"), lane, DAY, 20, 21, 1, now=night, after_hours=True)


def test_verified_officer_books_night_and_pays(lane, enabled, night, ps, device):
    flow = qr_flow(device, ps)
    assert flow.identified_by == "kod QR" and flow.status == OnsiteBooking.Status.VERIFIED
    r = onsite.book(flow, lane.pk, 20, 23, 3, now=night)
    flow.refresh_from_db()
    assert r.status == Reservation.Status.HOLD and r.ends_at == bookings.local_dt(DAY, 23) and flow.after_hours
    assert "poza godzinami" in r.notes and r.pay_by > night
    r2 = Reservation.objects.get(pk=r.pk)
    assert r2.amount > 0  # ta sama stawka osi


def test_group_requiring_approval_refused_at_night(lane, enabled, night, device):
    user = make_user("nowy@example.com", first_name="Nowy", last_name="Strzelec")
    add_credential(user)  # PS zweryfikowane, ale grupa „nowi” wymaga zatwierdzenia
    flow = qr_flow(device, user)
    with pytest.raises(ValidationError, match="zatwierdzenia"):
        onsite.book(flow, lane.pk, 20, 21, 1, now=night)


def test_new_person_at_night_only_with_automatic_ps(lane, enabled, night, device, monkeypatch):
    flow, _ = onsite.start(device, "anna@example.com", "Anna", "Nowak", "600700800")
    flow = onsite.verify_code(flow, last_code("anna@example.com"))
    onsite.accept_statement(flow)
    monkeypatch.setattr(ocr, "read_text", lambda data, **kw: "nieczytelne")
    onsite.submit_ps(flow, "0079/BZGLOK/2022", "LOK (x)", "2022-01-11", "", photo_data_url())
    with pytest.raises(ValidationError, match="Poza godzinami otwarcia rezerwować może tylko"):
        onsite.book(flow, lane.pk, 20, 21, 1, now=night)
    assert not Reservation.objects.exists()
    monkeypatch.setattr(ocr, "read_text", lambda data, **kw: LOK)
    onsite.submit_ps(flow, "0079/BZGLOK/2022", "LOK (x)", "2022-01-11", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 20, 21, 1, now=night)
    assert r.status == Reservation.Status.HOLD  # legitymacja potwierdzona automatycznie → od razu płatność


def test_identify_by_surname_and_ps_number(device, ps):
    with pytest.raises(ValidationError, match="Nie znaleziono"):
        onsite.start_ps(device, "Prowadzący", "PS 778")
    flow, _, masked = onsite.start_ps(device, "prowadzący", "ps 777")
    assert masked == "p***@example.com" and flow.identified_by == "nazwisko i nr PS"
    flow = onsite.verify_code(flow, last_code("ps@example.com"))
    assert flow.user == ps


def test_unknown_qr_rejected(device):
    with pytest.raises(ValidationError, match="Nieznany kod"):
        onsite.start_qr(device, "zly-token")


def test_refusal_cancels_and_notifies(lane, enabled, night, ps, device, owner_client):
    flow = qr_flow(device, ps)
    r = onsite.book(flow, lane.pk, 20, 21, 1, now=night)
    OutgoingEmail.objects.all().delete()
    flow = onsite.refuse(flow, now=night)
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED and flow.status == OnsiteBooking.Status.REFUSED
    subject = OutgoingEmail.objects.get(subject__contains="Rezygnacja").subject
    assert "PILNE" in subject and "opuszczenia obiektu" in subject
    body = owner_client.get("/panel/ksiazka/kioski/").content.decode()
    assert "Rezygnacje z opłaty" in body and "Piotr Prowadzący" in body and "poza godzinami" in body


def test_refusal_not_for_paid(lane, enabled, night, ps, device):
    flow = qr_flow(device, ps)
    r = onsite.book(flow, lane.pk, 20, 21, 1, now=night)
    Reservation.objects.filter(pk=r.pk).update(payment_status=Reservation.Payment.PAID)
    with pytest.raises(ValidationError, match="opłacona"):
        onsite.refuse(flow, now=night)


def test_unpaid_night_reservation_expiry_reported(lane, enabled, night, ps, device):
    flow = qr_flow(device, ps)
    r = onsite.book(flow, lane.pk, 20, 21, 1, now=night)
    bookings.expire_unpaid(now=r.pay_by + timedelta(minutes=1))
    OutgoingEmail.objects.all().delete()
    onsite.expire_pending(now=r.pay_by + timedelta(minutes=2))
    flow.refresh_from_db()
    assert flow.status == OnsiteBooking.Status.REFUSED
    assert OutgoingEmail.objects.filter(subject__contains="Rezygnacja").exists()
    onsite.expire_pending(now=r.pay_by + timedelta(minutes=3))
    assert OutgoingEmail.objects.filter(subject__contains="Rezygnacja").count() == 1  # tylko raz


def test_api_start_modes_and_refusal(owner, ps):
    d, code = services.create_device("Kiosk API", owner)
    c = Client()
    c.post("/kiosk/parowanie/", {"code": code})
    post = lambda path, data: c.post(path, json.dumps(data), content_type="application/json", HTTP_X_KIOSK="1")  # noqa: E731
    r = post("/kiosk/api/na-miejscu/start/", {"mode": "qr", "token": logbook.personal_token(ps)})
    assert r.status_code == 200 and r.json()["state"]["status"] == "potwierdzony"
    r2 = post("/kiosk/api/na-miejscu/start/", {"mode": "ps", "last_name": "Prowadzący", "number": "PS 777"})
    assert r2.status_code == 200 and r2.json()["masked_email"] == "p***@example.com"
    ref, secret = r.json()["ref"], r.json()["secret"]
    r3 = post("/kiosk/api/na-miejscu/rezygnacja/", {"ref": ref, "secret": secret})
    assert r3.status_code == 200 and r3.json()["state"]["status"] == "rezygnacja"
