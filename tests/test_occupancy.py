"""Obłożenie godzinowe: % godzin osi wobec godzin otwarcia (bez blokad), mapa dzień × godzina, najbardziej oblegane terminy."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.association import stats
from apps.bookings import services
from apps.facilities.models import Lane

from .conftest import add_credential, approve, make_user

pytestmark = pytest.mark.django_db


def _ps():
    u = approve(make_user("oblozenie@example.com"))
    add_credential(u)
    return u


def test_occupancy_counts_and_blocks(owner):
    lane = Lane.objects.get(name="Oś 100 m")
    day = timezone.localdate() + timedelta(days=7)
    oh = lane.hours_for(day)
    assert oh, "oś testowa powinna być czynna"
    ps = _ps()
    services.create_reservation(ps, lane, day, oh.opens.hour, oh.opens.hour + 2, 2)
    first, nxt = timezone.localdate(), timezone.localdate() + timedelta(days=30)
    before = stats.occupancy(first, nxt)
    assert before["booked"] == 2 and before["reservations"] == 1
    row = next(x for x in before["lanes"] if x["lane"].pk == lane.pk)
    assert row["booked"] == 2 and row["capacity"] > 0
    w = day.weekday()
    cell = next(c for c in before["heat"][w][2] if c[0] == oh.opens.hour)
    assert cell[1] >= 1 and cell[3] is not None
    assert before["top"][0]["hour"] in (oh.opens.hour, oh.opens.hour + 1)
    # blokada całego dnia: zmniejsza dostępne godziny osi
    occ = services.block_occurrences(day + timedelta(days=7), day + timedelta(days=7), 0, 24, False)
    services.create_blocks("Zawody", "", [lane], occ, owner, cancel_conflicts=False)
    after = stats.occupancy(first, nxt)
    row2 = next(x for x in after["lanes"] if x["lane"].pk == lane.pk)
    assert row2["capacity"] == row["capacity"] - (oh.closes.hour - oh.opens.hour)


def test_occupancy_page(owner_client):
    for k in ("30", "90", "365", "plan", "zly"):
        r = owner_client.get(f"/panel/raporty/oblozenie/?okres={k}")
        assert r.status_code == 200
        assert "Dzień tygodnia × godzina" in r.content.decode()


def test_occupancy_requires_permission(client):
    from .conftest import login_with_otp

    u = make_user("bezuprawnien@example.com", is_staff=True)
    login_with_otp(client, u)
    assert client.get("/panel/raporty/oblozenie/").status_code == 403
