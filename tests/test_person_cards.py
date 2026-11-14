"""Karty osób z kartoteki (bez konta): kod QR → wpis do książki w kiosku i u prowadzącego, wydruk PDF, unieważnienie."""

import pytest
from django.test import Client

from apps.audit.models import AuditLog
from apps.clients.models import Person
from apps.kiosk import services as kiosk_services
from apps.logbook import services
from apps.logbook.models import Entry

from .conftest import make_user
from .test_logbook import permit_data, ps, running  # noqa: F401

pytestmark = pytest.mark.django_db
H = {"HTTP_X_KIOSK": "1"}


@pytest.fixture
def person(db):
    return Person.objects.create(first_name="Ewa", last_name="Zimportowana", permit_number="CD 22", permit_issuer="KWP Wrocław",
                                 source="guntracker")


def test_card_token_roundtrip_and_reset(person, owner_client):
    token = services.card_token(person)
    assert services.person_from_card_token(token) == person
    assert "Zimportowana" not in token and "CD 22" not in token  # kod bez danych osobowych
    assert services.person_from_card_token("zly-token") is None
    owner_client.post(f"/panel/uzytkownicy/osoby/{person.pk}/nowa-karta/")
    assert services.person_from_card_token(token) is None  # stara karta unieważniona
    assert AuditLog.objects.filter(action="klient.karta_uniewaznienie").exists()
    # nowe osoby mają różne sekrety
    other = Person.objects.create(first_name="Jan", last_name="Inny", address="Sątok 1")
    assert other.qr_secret and other.qr_secret != Person.objects.get(pk=person.pk).qr_secret


def test_kiosk_reads_card(owner, person):
    device, code = kiosk_services.create_device("Kiosk", owner)
    c = Client()
    c.post("/kiosk/parowanie/", {"code": code})
    r = c.get(f"/kiosk/api/karta/{services.card_token(person)}/", **H)
    assert r.status_code == 200
    assert r.json()["prefill"] == {"first_name": "Ewa", "last_name": "Zimportowana", "address": "", "has_permit": "tak",
                                   "permit_number": "CD 22", "permit_issuer": "KWP Wrocław"}
    assert c.get("/kiosk/api/karta/zly-token/", **H).status_code == 404
    assert Client().get(f"/kiosk/api/karta/{services.card_token(person)}/", **H).status_code == 401  # bez sparowania


def test_range_officer_adds_card_holder_to_own_reservation(client, ps, running, person):  # noqa: F811
    from .test_logbook import signature_data_url

    url = f"/ksiazka/karta/{services.card_token(person)}/"
    assert client.get(url).status_code == 302  # wymaga logowania
    client.force_login(make_user("bez-rezerwacji@example.com"))
    assert client.get(url).status_code == 403
    client.force_login(ps)
    page = client.get(url).content.decode()
    assert "Dane z karty" in page and 'value="CD 22"' in page
    data = {**permit_data(first_name="Ewa", last_name="Zimportowana", permit_number="CD 22"), "purpose": "strzelanie",
            "reservation": running.pk, "signature": signature_data_url()}
    r = client.post(url, data)
    assert r.status_code == 302
    e = Entry.objects.get()
    assert (e.source, e.user, e.created_by, e.reservation) == (Entry.Source.CARD_QR, None, ps, running)
    assert client.get("/ksiazka/karta/zly-token/").status_code == 404


def test_print_cards_pdf_only_for_persons_without_account(owner_client, person):
    with_account = Person.objects.create(first_name="Ma", last_name="Konto", address="X", user=make_user("konto@example.com"))
    r = owner_client.get(f"/panel/uzytkownicy/osoby/{person.pk}/karta/")
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf" and r.content.startswith(b"%PDF")
    person.refresh_from_db()
    assert person.card_printed_at is not None
    assert owner_client.get(f"/panel/uzytkownicy/osoby/{with_account.pk}/karta/").status_code == 404
    r = owner_client.post("/panel/uzytkownicy/osoby/karty/", {"ids": [person.pk, with_account.pk]})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert AuditLog.objects.filter(action="klient.karty_wydruk").count() == 2
    r = owner_client.post("/panel/uzytkownicy/osoby/karty/", {"ids": [with_account.pk]})
    assert r.status_code == 302  # nic do wydruku
    page = owner_client.get("/panel/uzytkownicy/osoby/").content.decode()
    assert f'name="ids" value="{person.pk}"' in page and f'name="ids" value="{with_account.pk}"' not in page


def test_card_pdf_requires_permission(client, person):
    staff = make_user("pracownik@example.com")
    client.force_login(staff)
    assert client.get(f"/panel/uzytkownicy/osoby/{person.pk}/karta/").status_code in (302, 403)
    person.refresh_from_db()
    assert person.card_printed_at is None
