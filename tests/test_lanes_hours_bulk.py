"""Godziny otwarcia — zmiana zbiorcza: cały tydzień dla wielu osi naraz, walidacja, ostrzeżenie o rezerwacjach poza
nowymi godzinami."""

from datetime import time, timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.bookings import services
from apps.facilities.models import Lane, OpeningHours

from .conftest import add_credential, approve, login_with_otp, make_user

pytestmark = pytest.mark.django_db
URL = "/panel/osie/godziny/"


def week(**override):
    data = {}
    for d in range(7):
        data.update({f"open_{d}": "on", f"opens_{d}": "10:00", f"closes_{d}": "18:00"})
    data.update(override)
    return data


def test_page_prefills_common_hours_and_all_active_lanes(owner_client):
    page = owner_client.get(URL).content.decode()
    assert "Godziny otwarcia — zmiana zbiorcza" in page and 'data-check-all="lanes"' in page
    active = Lane.objects.filter(is_active=True, facility__is_active=True)
    for lane in active:
        assert f'value="{lane.pk}"' in page
    assert "Godziny otwarcia — zmiana zbiorcza" in owner_client.get("/panel/osie/").content.decode()


def test_save_replaces_whole_week_on_selected_lanes(owner_client):
    lanes = list(Lane.objects.all()[:2])
    other = Lane.objects.exclude(pk__in=[lane.pk for lane in lanes]).first()
    other_before = list(other.opening_hours.values_list("weekday", "opens", "closes"))
    data = week(open_6="", opens_5="09:00", closes_5="17:00")
    data["lanes"] = [lane.pk for lane in lanes]
    r = owner_client.post(URL, data, follow=True)
    assert "Zapisano godziny otwarcia dla osi: 2" in r.content.decode()
    for lane in lanes:
        hours = {h.weekday: (h.opens, h.closes) for h in lane.opening_hours.all()}
        assert set(hours) == {0, 1, 2, 3, 4, 5}  # niedziela zamknięta
        assert hours[0] == (time(10), time(18)) and hours[5] == (time(9), time(17))
    assert list(other.opening_hours.values_list("weekday", "opens", "closes")) == other_before  # niezaznaczona — bez zmian


@pytest.mark.parametrize("override, error", [
    ({"opens_0": "10:30"}, "tylko pełne godziny"),
    ({"opens_1": "", "closes_1": ""}, "podaj godzinę otwarcia"),
    ({"opens_2": "18:00", "closes_2": "10:00"}, "zamknięcie musi być później"),
])
def test_validation_keeps_hours_unchanged(owner_client, override, error):
    lane = Lane.objects.first()
    before = list(lane.opening_hours.values_list("weekday", "opens", "closes"))
    data = week(**override)
    data["lanes"] = [lane.pk]
    page = owner_client.post(URL, data).content.decode()
    assert error in page and list(lane.opening_hours.values_list("weekday", "opens", "closes")) == before
    assert "Zaznacz co najmniej jedną oś" in owner_client.post(URL, week()).content.decode()


def test_warns_about_future_reservations_outside_new_hours(owner_client):
    lane = Lane.objects.get(name="Oś 100 m")
    day = timezone.localdate() + timedelta(days=7)
    oh = lane.hours_for(day)
    ps = approve(make_user("godziny@example.com"))
    add_credential(ps)
    res = services.create_reservation(ps, lane, day, oh.opens.hour, oh.opens.hour + 1, 1)
    data = week(**{f"open_{day.weekday()}": ""})  # ten dzień tygodnia zamknięty
    data["lanes"] = [lane.pk]
    page = owner_client.post(URL, data, follow=True).content.decode()
    assert "poza nowymi godzinami: 1" in page and res.number in page
    assert not OpeningHours.objects.filter(lane=lane, weekday=day.weekday()).exists()


def test_requires_permission():
    c = Client()
    login_with_otp(c, make_user("bezosi@example.com", is_staff=True))
    assert c.get(URL).status_code == 403
