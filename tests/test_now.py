import datetime as dt

import pytest
from django.utils import timezone

from apps.bookings import services as booking_services
from apps.bookings.models import Reservation
from apps.facilities.models import Lane
from apps.logbook import now, services

from .conftest import add_credential, approve, make_user
from .test_logbook import permit_data

pytestmark = pytest.mark.django_db


@pytest.fixture
def sig():
    from .test_logbook import signature_data_url

    return signature_data_url()


def _running(ps, hours=1, start_offset=0):
    lane = Lane.objects.get(name="Oś 25 m nr 1")
    start = booking_services.hour_floor(timezone.now()) + dt.timedelta(hours=start_offset)
    rule = lane.price_rule_for(start.date())
    return Reservation.objects.create(number=f"SATOK-R-NOW-{start_offset}", lane=lane, user=ps, starts_at=start,
                                      ends_at=start + dt.timedelta(hours=hours), persons=2, price_rule=rule,
                                      hourly_amount=rule.hourly_amount(2), amount=rule.hourly_amount(2))


def test_verdicts_ok_overtime_and_unauthorized(sig):
    ps = make_user("ps-now@example.com")
    add_credential(ps)
    approve(ps)
    r = _running(ps)
    shooter = services.create_entry(data={**permit_data(), "purpose": "strzelanie", "has_permit": True},
                                    signature_png=services.decode_signature(sig), source="qr_rezerwacji", reservation=r)
    v = {p["entry"].pk: p["verdict"] for p in now.present()}
    assert v[shooter.pk] == "ok"
    # po końcu rezerwacji — „po czasie” (zanim zamknie go auto-wypis)
    later = r.ends_at + dt.timedelta(minutes=1)
    assert {p["entry"].pk: p["verdict"] for p in now.present(later)}.get(shooter.pk) in (None, "po_czasie")
    # wejście bez rezerwacji, gdy nikt nie ma rezerwacji
    r.status = Reservation.Status.CANCELLED
    r.save()
    shooter.exited_at = timezone.now()
    shooter.save()
    guest = services.create_entry(data={**permit_data(first_name="Obcy"), "purpose": "obserwator", "has_permit": True},
                                  signature_png=services.decode_signature(sig), source="qr_wejscia")
    s = now.summary()
    assert {p["entry"].pk: p["verdict"] for p in s["people"]}[guest.pk] == "bez_rezerwacji" and s["alarms"] == 1


def test_public_now_page_has_no_personal_data(client):
    ps = make_user("ps-pub@example.com", last_name="Tajnowski")
    add_credential(ps)
    approve(ps)
    _running(ps)
    html = client.get("/teraz/").content.decode()
    assert "Oś 25 m nr 1" in html and "zajęta" in html and "Tajnowski" not in html
    assert 'http-equiv="refresh"' in html.split("</head>")[0]


def test_panel_now_page_and_manifest_shortcuts(owner_client, client):
    assert owner_client.get("/panel/teraz/").status_code == 200
    m = client.get("/manifest.webmanifest").json()
    assert [s["url"] for s in m["shortcuts"]][:2] == ["/teraz/", "/panel/teraz/"]
