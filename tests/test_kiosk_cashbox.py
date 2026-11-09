"""Gotówka do skarbonki (kiosk) z oświadczeniem i rozliczenie skarbonki w panelu."""

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
from apps.kiosk import cashbox, onsite, services
from apps.kiosk.models import CashDeclaration
from apps.logbook import services as logbook

from .conftest import add_credential, approve, grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def lane(db):
    return Lane.objects.get(name="Oś 100 m")


@pytest.fixture
def now():
    return bookings.local_dt(timezone.localdate() + timedelta(days=7), 10) + timedelta(minutes=20)


@pytest.fixture
def device(owner):
    return services.create_device("Kiosk", owner)[0]


def officer(email="ps@example.com", last="Prowadzący"):
    user = make_user(email, first_name="Piotr", last_name=last, phone="600 700 800")
    add_credential(user)
    approve(user)  # grupa „powracający” — gotówka dozwolona
    return user


def booked(device, user, lane, now, start=10):
    flow, _ = onsite.start_qr(device, logbook.personal_token(user))
    onsite.accept_statement(flow)
    r = onsite.book(flow, lane.pk, start, start + 1, 1, now=now)
    flow.refresh_from_db()
    return flow, r


def test_declare_cash_confirms_reservation_with_statement(device, lane, now):
    flow, r = booked(device, officer(), lane, now)
    st = onsite.state(flow, now=now)
    assert st["cash"]["amount"] == r.amount and r.number in st["cash"]["statement"] and "skarbonki" in st["cash"]["statement"]
    with pytest.raises(ValidationError, match="oświadczenie"):
        cashbox.declare(flow, False, now=now)
    d = cashbox.declare(flow, True, now=now)
    r.refresh_from_db()
    assert r.status == Reservation.Status.CONFIRMED and r.pay_method == Reservation.PayMethod.CASH
    assert r.payment_status == Reservation.Payment.DUE and d.amount == r.amount
    st = onsite.state(flow, now=now)
    assert st["cash_declared"] is True and "cash" not in st
    with pytest.raises(ValidationError, match="skarbonki"):
        onsite.refuse(flow, now=now)


def test_no_cash_when_disabled_or_group_forbids(device, lane, now):
    cfg = BookingSettings.load()
    cfg.kiosk_cash_box = False
    cfg.save()
    flow, r = booked(device, officer(), lane, now)
    assert "cash" not in onsite.state(flow, now=now)
    with pytest.raises(ValidationError, match="niedostępna|nie jest dostępna"):
        cashbox.declare(flow, True, now=now)


def test_settlement_all_present(device, lane, now, owner):
    flow1, r1 = booked(device, officer(), lane, now, start=10)
    flow2, r2 = booked(device, officer("b@example.com", "Drugi"), lane, now, start=12)
    d1, d2 = cashbox.declare(flow1, True, now=now), cashbox.declare(flow2, True, now=now)
    OutgoingEmail.objects.all().delete()
    s = cashbox.settle(d1.amount + d2.amount, [d1.pk, d2.pk], owner)
    r1.refresh_from_db()
    r2.refresh_from_db()
    assert r1.payment_status == r2.payment_status == Reservation.Payment.PAID
    assert s.difference == 0 and set(CashDeclaration.objects.values_list("status", flat=True)) == {"potwierdzona"}
    assert not OutgoingEmail.objects.filter(subject__contains="Skarbonka").exists()
    with pytest.raises(ValidationError, match="Brak oświadczeń"):
        cashbox.settle(0, [], owner)


def test_settlement_missing_payment_flags_contacts(device, lane, now, owner, owner_client):
    flow1, r1 = booked(device, officer(), lane, now, start=10)
    flow2, r2 = booked(device, officer("b@example.com", "Drugi"), lane, now, start=12)
    d1, d2 = cashbox.declare(flow1, True, now=now), cashbox.declare(flow2, True, now=now)
    body = owner_client.get("/panel/platnosci/skarbonka/").content.decode()
    assert r1.number in body and r2.number in body
    r = owner_client.post("/panel/platnosci/skarbonka/", {"counted": f"{d1.amount / 100:.2f}".replace(".", ","), "confirmed": [d1.pk]})
    assert r.status_code == 302
    r1.refresh_from_db()
    r2.refresh_from_db()
    d2.refresh_from_db()
    assert r1.payment_status == Reservation.Payment.PAID and r2.payment_status == Reservation.Payment.DUE
    assert d2.status == CashDeclaration.Status.MISSING
    assert OutgoingEmail.objects.filter(subject__contains="Skarbonka: niezgodność").exists()
    page = owner_client.get("/panel/platnosci/skarbonka/").content.decode()
    assert "brak wpłaty" in page and "Drugi" in page and "600 700 800" in page


def test_cashbox_requires_permission():
    staff = grant(make_user("v@example.com"), "logbook.view_logbook")
    c = Client()
    login_with_otp(c, staff)
    assert c.get("/panel/platnosci/skarbonka/").status_code == 403


def test_api_cash_step(owner, lane, now):
    from apps.kiosk.models import OnsiteBooking

    d, code = services.create_device("Kiosk API", owner)
    c = Client()
    c.post("/kiosk/parowanie/", {"code": code})
    user = officer()
    post = lambda path, data: c.post(path, json.dumps(data), content_type="application/json", HTTP_X_KIOSK="1")  # noqa: E731
    start = post("/kiosk/api/na-miejscu/start/", {"mode": "qr", "token": logbook.personal_token(user)}).json()
    ref, secret = start["ref"], start["secret"]
    post("/kiosk/api/na-miejscu/oswiadczenie/", {"ref": ref, "secret": secret, "accept": True})
    onsite.book(OnsiteBooking.objects.get(ref=ref), lane.pk, 10, 11, 1, now=now)  # termin w stałej dacie (bez zależności od godziny)
    r = post("/kiosk/api/na-miejscu/stan/", {"ref": ref, "secret": secret})
    assert r.json()["state"]["cash"]["amount"] > 0
    r = post("/kiosk/api/na-miejscu/gotowka/", {"ref": ref, "secret": secret, "accept": False})
    assert r.status_code == 400
    r = post("/kiosk/api/na-miejscu/gotowka/", {"ref": ref, "secret": secret, "accept": True})
    assert r.status_code == 200 and r.json()["state"]["cash_declared"] is True
