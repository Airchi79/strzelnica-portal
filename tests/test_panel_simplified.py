"""Uproszczony panel: jedna pozycja „Dziś na strzelnicy”, grupa „Raporty”, strona „Ustawienia”, archiwum GunTrackera w „System”."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core import panel_menu

from .conftest import grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db


class _Req:
    def __init__(self, user, name=""):
        self.user = user
        self.resolver_match = type("M", (), {"url_name": name})()


def _menu(user, name=""):
    return {s["title"]: [i["label"] for i in s["items"]] for s in panel_menu.build(_Req(user, name))}


def _staff(email, *perms):
    return grant(make_user(email), *perms)


def test_owner_menu_structure(owner):
    m = _menu(owner)
    flat = [x for items in m.values() for x in items]
    assert flat.count("Dziś na strzelnicy") == 1 and "Teraz na strzelnicy" not in flat
    assert m["Raporty"] == ["Statystyki i raporty", "Raporty wpływów"]
    assert "Raporty wpływów" not in m["Płatności"] and "Raporty" not in m["Stowarzyszenie"]
    assert "Ustawienia" in m["System"] and "Archiwum GunTracker" in m["System"]
    for gone in ("Ustawienia rezerwacji", "Ustawienia książki", "Archiwum książki", "Dane stowarzyszenia"):
        assert gone not in flat
    active = [i for s in panel_menu.build(_Req(owner, "now")) for i in s["items"] if i["active"]]
    assert [i["label"] for i in active] == ["Dziś na strzelnicy"]
    active = [i for s in panel_menu.build(_Req(owner, "booking_settings")) for i in s["items"] if i["active"]]
    assert [i["label"] for i in active] == ["Ustawienia"]


def test_menu_target_depends_on_permissions():
    logbook_only = _staff("ksiazka@example.com", "logbook.view_logbook")
    items = {i["label"]: i["url"] for s in panel_menu.build(_Req(logbook_only)) for i in s["items"]}
    assert items["Dziś na strzelnicy"] == "/panel/teraz/" and items["Archiwum GunTracker"] == "/panel/ksiazka/archiwum/"
    assert "Ustawienia" not in items and "Raporty wpływów" not in items
    duty = _staff("dyzur@example.com", "bookings.manage_reservations")
    items = {i["label"]: i["url"] for s in panel_menu.build(_Req(duty)) for i in s["items"]}
    assert items["Dziś na strzelnicy"] == "/panel/dzis/" and items["Archiwum GunTracker"] == "/panel/rezerwacje/archiwum/"


def test_settings_hub_lists_only_allowed_pages(client):
    u = _staff("ustawienia@example.com", "bookings.manage_booking_settings")
    login_with_otp(client, u)
    page = client.get("/panel/ustawienia/wszystkie/").content.decode()
    assert "/panel/rezerwacje/ustawienia/" in page and "/panel/ustawienia/\"" not in page and "Księgowość" not in page
    assert "Wszystkie ustawienia" in client.get("/panel/rezerwacje/ustawienia/").content.decode()
    nobody = _staff("nic@example.com", "logbook.view_logbook")
    client.logout()
    login_with_otp(client, nobody)
    assert client.get("/panel/ustawienia/wszystkie/").status_code == 403


def test_owner_pages_with_new_tabs(owner_client):
    for url in ["/panel/ustawienia/wszystkie/", "/panel/teraz/", "/panel/dzis/", "/panel/raporty/", "/panel/raporty/miesiac/",
                "/panel/raporty/oblozenie/", "/panel/raporty/rezerwacje/", "/panel/raporty-wplywow/",
                "/panel/rezerwacje/archiwum/", "/panel/ksiazka/archiwum/", "/panel/czlonkowie/"]:
        assert owner_client.get(url).status_code == 200, url
    page = owner_client.get("/panel/raporty/miesiac/").content.decode()
    assert "Raporty wpływów" in page and "Obłożenie godzinowe" in page
    assert "/panel/raporty/miesiac/" not in owner_client.get("/panel/czlonkowie/").content.decode()
    assert "Plan dnia" in owner_client.get("/panel/teraz/").content.decode()
    page = owner_client.get("/panel/ksiazka/archiwum/").content.decode()
    assert "Archiwum GunTracker" in page and "/panel/rezerwacje/archiwum/" in page


def test_month_stats_include_guntracker_archive():
    from apps.association import stats
    from apps.bookings.models import ArchivedReservation
    from apps.bookings.services import local_dt
    from apps.facilities.models import Lane

    lane = Lane.objects.get(name="Oś 100 m")
    day = timezone.localdate().replace(day=1) - timedelta(days=20)
    while not lane.hours_for(day):
        day += timedelta(days=1)
    h = lane.hours_for(day).opens.hour
    before = stats.month_stats(day.year, day.month)
    for n, client_name in enumerate(["Jan Archiwalny", "jan  archiwalny", "Ewa Inna"]):
        ArchivedReservation.objects.create(row_hash=f"m{n}", number=f"GT-{n}", starts_at=local_dt(day, h + n), ends_at=local_dt(day, h + n + 1),
                                           client=client_name, lane_label=lane.name, lane=lane, positions="1, 2")
    after = stats.month_stats(day.year, day.month)
    assert after["archived"] == 3
    assert after["kpi"]["reservations"] == before["kpi"]["reservations"] + 3
    assert after["kpi"]["hours"] == before["kpi"]["hours"] + 3
    assert after["kpi"]["persons"] == before["kpi"]["persons"] + 6
    assert after["kpi"]["officers"] == before["kpi"]["officers"] + 2  # ta sama osoba mimo innej pisowni


def test_install_app_card_on_dashboard_and_panel(client, owner_client, user):
    page = owner_client.get("/panel/").content.decode()
    assert "data-install-card" in page and "js/install" in page
    client.force_login(user)
    page = client.get("/pulpit/").content.decode()
    assert "data-install-card" in page and "Zainstaluj aplikację" in page and "Dodaj do Docka" in page and "Do ekranu początkowego" in page
