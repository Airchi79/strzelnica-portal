"""Publiczne API danych strzelnicy: bez logowania, bez danych osobowych, z cennikiem, wolnymi godzinami i wydarzeniami."""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.bookings import services
from apps.facilities.models import Lane

from .conftest import add_credential, approve, make_user

pytestmark = pytest.mark.django_db


def _get(client, url):
    r = client.get(url)
    assert r.status_code == 200 and r["Access-Control-Allow-Origin"] == "*" and "max-age" in r["Cache-Control"]
    return json.loads(r.content)


def test_lanes_with_hours_and_prices(client):
    data = _get(client, "/api/publiczne/osie/")
    lane = next(x for x in data["osie"] if x["nazwa"] == "Oś 100 m")
    assert len(lane["godziny"]) == 7 and lane["cennik"]["wylacznosc_godzina_zl"] > 0
    assert data["rezerwacja"].endswith("/rezerwacje/nowa/")


def test_free_slots_merge_hours_and_hide_people(client):
    lane = Lane.objects.get(name="Oś 100 m")
    day = timezone.localdate() + timedelta(days=3)
    while not lane.hours_for(day):
        day += timedelta(days=1)
    oh = lane.hours_for(day)
    u = approve(make_user("api@example.com", first_name="Tajny", last_name="Strzelec"))
    add_credential(u)
    services.create_reservation(u, lane, day, oh.opens.hour + 1, oh.opens.hour + 2, 1)
    raw = client.get(f"/api/publiczne/wolne-terminy/?od={day.isoformat()}&dni=1&os={lane.pk}").content.decode()
    assert "Tajny" not in raw and "api@example.com" not in raw
    d = json.loads(raw)["osie"][0]["dni"][0]
    total = oh.closes.hour - oh.opens.hour
    assert d["godzin_otwarcia"] == total and d["wolnych_godzin"] == total - 1
    assert d["wolne"][0] == {"od": f"{oh.opens.hour:02d}:00", "do": f"{oh.opens.hour + 1:02d}:00", "godzin": 1}
    assert client.get("/api/publiczne/wolne-terminy/?dni=abc").status_code == 400
    assert len(_get(client, "/api/publiczne/wolne-terminy/?dni=99")["osie"]) >= 1  # dni przycięte do 14


def test_events_only_published_future(client, owner):
    from apps.events.models import Event

    now = timezone.now()
    common = {"starts_at": now + timedelta(days=5), "ends_at": now + timedelta(days=5, hours=4),
              "registration_until": now + timedelta(days=4), "fee": 16000, "capacity": 40}
    Event.objects.create(title="Zawody jesienne", status=Event.Status.PUBLISHED, **common)
    Event.objects.create(title="Szkic tajny", status=Event.Status.DRAFT, **common)
    data = _get(client, "/api/publiczne/wydarzenia/")
    names = [e["nazwa"] for e in data["wydarzenia"]]
    assert names == ["Zawody jesienne"]
    e = data["wydarzenia"][0]
    assert e["oplata_zl"] == 160.0 and e["wolnych_miejsc"] == 40 and e["zapisy_otwarte"] and "/wydarzenia/" in e["link"]
