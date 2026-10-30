import datetime

import pytest
from django.core.exceptions import ValidationError

from apps.facilities.models import Lane, PriceRule, format_pln

pytestmark = pytest.mark.django_db

# Oczekiwana należność za 1 h (zł) dla 1..10 osób — cennik zarządu z 30.10.2026, reguła „mniejsza z kwot”.
EXPECTED = {
    "Oś 25 m nr 1": [35, 70, 100, 100, 100, 100, 100, 100, 100, 100],
    "Oś 25 m nr 2": [35, 70, 100, 100, 100, 100, 100, 100, 100, 100],
    "Oś 35 m": [35, 70, 100, 100, 100, 100, 100, 100, 100, 100],
    "Oś 100 m": [60, 120, 180, 200, 200, 200, 200, 200, 200, 200],
    "Oś dynamiczno-myśliwska": [150] * 10,
}


def test_seeded_lanes_hours_and_prices():
    lanes = list(Lane.objects.order_by("order"))
    assert [lane.name for lane in lanes] == list(EXPECTED)
    assert Lane.objects.get(kind="dynamiczna").exclusive_only
    for lane in lanes:
        hours = list(lane.opening_hours.all())
        assert len(hours) == 7
        assert all(h.opens == datetime.time(9) and h.closes == datetime.time(18) for h in hours)


@pytest.mark.parametrize("lane_name", list(EXPECTED))
def test_price_table(lane_name):
    lane = Lane.objects.get(name=lane_name)
    rule = lane.price_rule_for(datetime.date(2026, 11, 2))
    for persons, zl in enumerate(EXPECTED[lane_name], start=1):
        assert rule.amount(persons, 1) == zl * 100, (lane_name, persons)
    assert rule.amount(4, 3) == EXPECTED[lane_name][3] * 300


def test_price_validation():
    rule = Lane.objects.get(name="Oś 100 m").price_rule_for(datetime.date(2026, 11, 2))
    with pytest.raises(ValidationError):
        rule.amount(0, 1)
    with pytest.raises(ValidationError):
        rule.amount(2, 1.5)


def test_price_versioning_by_date():
    lane = Lane.objects.get(name="Oś 100 m")
    PriceRule.objects.create(lane=lane, per_person_hourly=7000, flat_hourly=25000, valid_from=datetime.date(2027, 1, 1))
    assert lane.price_rule_for(datetime.date(2026, 12, 31)).flat_hourly == 20000
    assert lane.price_rule_for(datetime.date(2027, 1, 1)).flat_hourly == 25000


def test_panel_price_add_converts_zloty_to_grosze(owner_client):
    lane = Lane.objects.get(name="Oś 35 m")
    response = owner_client.post(f"/panel/osie/{lane.pk}/cennik/", {"valid_from": "2027-01-01", "per_person": "37,50", "flat": "110"})
    assert response.status_code == 302, response.content
    rule = lane.price_rules.get(valid_from=datetime.date(2027, 1, 1))
    assert (rule.per_person_hourly, rule.flat_hourly) == (3750, 11000)


def test_panel_lane_hours_edit(owner_client):
    lane = Lane.objects.get(name="Oś 35 m")
    hours = list(lane.opening_hours.order_by("weekday"))
    data = {
        "lane-name": lane.name, "lane-kind": lane.kind, "lane-length_m": "35", "lane-description": "",
        "lane-is_active": "on", "lane-order": "3",
        "hours-TOTAL_FORMS": "7", "hours-INITIAL_FORMS": "7", "hours-MIN_NUM_FORMS": "0", "hours-MAX_NUM_FORMS": "7",
    }
    for i, h in enumerate(hours):
        data.update({f"hours-{i}-id": h.pk, f"hours-{i}-lane": lane.pk, f"hours-{i}-weekday": h.weekday, f"hours-{i}-opens": "09:00", f"hours-{i}-closes": "18:00"})
    data["hours-0-closes"] = "20:00"
    data["hours-6-DELETE"] = "on"  # niedziela zamknięta
    response = owner_client.post(f"/panel/osie/{lane.pk}/", data)
    assert response.status_code == 302, response.context["hours"].errors if response.context else response
    assert lane.opening_hours.count() == 6
    assert lane.hours_for(datetime.date(2026, 11, 2)).closes == datetime.time(20)  # poniedziałek

    data["hours-0-closes"] = "08:00"
    response = owner_client.post(f"/panel/osie/{lane.pk}/", data)
    assert response.status_code == 200


def test_format_pln():
    assert format_pln(10000) == "100 zł"
    assert format_pln(3750) == "37,50 zł"
    assert format_pln(123456) == "1 234,56 zł"
