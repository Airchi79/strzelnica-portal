"""Konto z dostępem do panelu działa wyłącznie jako panel zarządu (bez funkcji strzelca)."""

import pytest
from django.test import Client

from .conftest import grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db

CLIENT_PAGES = ["/pulpit/", "/rezerwacje/nowa/", "/rezerwacje/moje/", "/dokumenty/", "/ksiazka/moj-kod/", "/ksiazka/dziennik/",
                "/platnosci/", "/regulamin/akceptacja/"]


def test_panel_account_redirected_from_client_pages(owner_client):
    for path in CLIENT_PAGES:
        r = owner_client.get(path)
        assert r.status_code == 302 and r.url == "/panel/", path
    page = owner_client.get("/panel/").content.decode()
    assert "wyłącznie do panelu zarządu" in page  # wyjaśnienie po przekierowaniu
    home = owner_client.get("/").content.decode()
    assert "Mój pulpit" not in home and "Panel zarządu" in home
    for path in ["/teraz/", "/rezerwacje/", "/osie/", "/wydarzenia/", "/regulamin/", "/pytania/", "/konto/profil/", "/usterki/nowa/"]:
        assert owner_client.get(path).status_code == 200, path  # strony publiczne, konto i zgłoszenie usterki — tak


def test_staff_member_with_limited_permissions_also_panel_only():
    staff = grant(make_user("dyzurny@example.com"), "logbook.view_logbook")
    c = Client()
    login_with_otp(c, staff)
    assert c.get("/rezerwacje/nowa/").url == "/panel/"


def test_client_account_unchanged(client):
    client.force_login(make_user("strzelec@example.com"))
    assert client.get("/pulpit/").status_code == 200
    assert "Mój pulpit" in client.get("/").content.decode()


def test_login_of_panel_account_lands_in_panel(owner):
    from .conftest import PASSWORD

    c = Client()
    c.post("/konto/logowanie/", {"email": owner.email, "password": PASSWORD})
    login_with_otp(c, owner)
    assert c.get("/pulpit/").url == "/panel/"


def test_my_account_for_panel_account_in_panel_layout_without_address(owner_client, owner):
    page = owner_client.get("/konto/profil/").content.decode()
    assert "Moje konto" in page and "sidebar-nav" in page  # wygląd panelu
    assert 'name="street"' not in page and 'name="city"' not in page and "Dezaktywuj konto" not in page
    r = owner_client.post("/konto/profil/", {"first_name": "Adam", "last_name": "Nowy", "phone": "600 100 200"})
    assert r.status_code == 302
    owner.refresh_from_db()
    assert owner.last_name == "Nowy" and owner.phone == "600 100 200"
    assert "sidebar-nav" in owner_client.get("/konto/profil/haslo/").content.decode()


def test_client_profile_keeps_address(client):
    client.force_login(make_user("strzelec@example.com"))
    page = client.get("/konto/profil/").content.decode()
    assert 'name="street"' in page and "sidebar-nav" not in page and "Dezaktywuj konto" in page
