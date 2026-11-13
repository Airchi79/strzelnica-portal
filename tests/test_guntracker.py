"""Importy z GunTrackera: archiwum książki pobytu i archiwum rezerwacji."""

from datetime import timedelta

import pytest
from django.db import DatabaseError, transaction
from django.test import Client
from django.utils import timezone

from apps.audit.models import SensitiveAccessLog
from apps.bookings.models import ArchivedReservation, Block
from apps.clients.models import Person
from apps.logbook.models import ArchivedEntry

from .conftest import grant, login_with_otp, make_user
from .test_clients import _broken_dimension, _gt_xlsx

pytestmark = pytest.mark.django_db

BOOK_HEADER = ("Rozpoczęcie", "Zakończenie", "Czas (min)", "Imię", "Nazwisko", "Numer pozwolenia", "Organ wydający", "Adres", "Oś",
               "Stanowisko", "Typ wpisu", "Potwierdzenie", "Dodał administrator")
BOOK_ROWS = [
    ("2026-10-26 09:16", "2026-10-26 10:48", 91, "Łukasz", "Archiwalny", "AA0286992", "KSP WARSZAWA", "Kasprowicza 9", "Oś 100m", "", "Użytkownik", "", ""),
    ("2026-10-25 12:17", "2026-10-25 14:35", 137, "Joanna", "Bezpozwolenia", "", "", "Bukowa 7L", "Oś 25 m (1)", "1", "Wpis ręczny",
     "Fizyczna księga pobytu", "STRZELNICA SĄTOK"),
]
RES_HEADER = ("Godzina", "Numer", "Klient", "Oś / Instruktor", "Stanowiska", "Prowadzący strzelanie", "Księga pobytu", "Płatność", "Kontakt")


def book_file(rows=BOOK_ROWS):
    return _broken_dimension(_gt_xlsx(rows, header=BOOK_HEADER, name="ksiega_pobytu.xlsx"))


def res_file(rows):
    return _broken_dimension(_gt_xlsx(rows, header=RES_HEADER, name="Rezerwacje.xlsx"))


def test_logbook_archive_import_preview_import_dedupe_and_persons(owner_client):
    body = owner_client.post("/panel/ksiazka/archiwum/import/", {"plik": book_file(), "tryb": "podglad", "osoby": "1"}).content.decode()
    assert "nic nie zapisano" in body and not ArchivedEntry.objects.exists() and not Person.objects.exists()
    owner_client.post("/panel/ksiazka/archiwum/import/", {"plik": book_file(), "tryb": "importuj", "osoby": "1"})
    assert ArchivedEntry.objects.count() == 2 and Person.objects.count() == 2
    e = ArchivedEntry.objects.get(last_name="Archiwalny")
    assert e.duration_minutes == 91 and timezone.localtime(e.started_at).hour == 9 and e.lane == "Oś 100m"
    owner_client.post("/panel/ksiazka/archiwum/import/", {"plik": book_file(), "tryb": "importuj", "osoby": "1"})
    assert ArchivedEntry.objects.count() == 2 and Person.objects.count() == 2
    with pytest.raises(DatabaseError), transaction.atomic():
        ArchivedEntry.objects.filter(pk=e.pk).update(last_name="Zmieniony")
    with pytest.raises(DatabaseError), transaction.atomic():
        ArchivedEntry.objects.filter(pk=e.pk).delete()


def test_logbook_archive_import_fills_missing_person_data(owner_client):
    Person.objects.create(first_name="Łukasz", last_name="Archiwalny", permit_number="AA0286992", source="guntracker")
    Person.objects.create(first_name="Joanna", last_name="Bezpozwolenia", address="Stary adres 1", source="panel")
    body = owner_client.post("/panel/ksiazka/archiwum/import/", {"plik": book_file(), "tryb": "importuj", "osoby": "1"}).content.decode()
    lukasz = Person.objects.get(last_name="Archiwalny")
    assert lukasz.permit_issuer == "KSP WARSZAWA" and lukasz.address == "Kasprowicza 9"  # puste pola uzupełnione
    assert Person.objects.get(last_name="Bezpozwolenia").address == "Stary adres 1"  # inny adres — bez nadpisania
    assert Person.objects.count() == 2 and "Uzupełnione osoby" in body


def test_logbook_archive_list_masked_reveal_and_print(owner_client):
    owner_client.post("/panel/ksiazka/archiwum/import/", {"plik": book_file(), "tryb": "importuj"})
    body = owner_client.get("/panel/ksiazka/archiwum/").content.decode()
    assert "Archiwalny" in body and "AA0286992" not in body
    body = owner_client.post("/panel/ksiazka/archiwum/", {"pokaz": "1"}).content.decode()
    assert "AA0286992" in body and SensitiveAccessLog.objects.filter(resource__startswith="ksiazka:archiwum:odkrycie").exists()
    printed = owner_client.get("/panel/ksiazka/archiwum/druk/?od=2026-10-01&do=2026-10-31").content.decode()
    assert "AA0286992" in printed and "Fizyczna księga pobytu" in printed


def test_logbook_archive_purged_after_retention(owner_client):
    from apps.logbook import services

    owner_client.post("/panel/ksiazka/archiwum/import/", {"plik": book_file(), "tryb": "importuj"})
    assert services.purge_expired(now=timezone.now() + timedelta(days=6 * 365)) == 2 and not ArchivedEntry.objects.exists()


def test_logbook_archive_bad_file(owner_client):
    body = owner_client.post("/panel/ksiazka/archiwum/import/", {"plik": _gt_xlsx([("x",)], header=("Coś",)), "tryb": "podglad"}).content.decode()
    assert "Brak kolumn" in body
    bad = [("26.10.2026 rano", "", "", "A", "B", "", "", "x", "", "", "", "", "")]
    assert "nieprawidłowa data" in owner_client.post("/panel/ksiazka/archiwum/import/", {"plik": book_file(bad), "tryb": "podglad"}).content.decode()


def _res_rows():
    future = timezone.localdate() + timedelta(days=10)
    past = timezone.localdate() - timedelta(days=10)
    return [
        (f"{future:%d.%m} 09:00 – 12:00", "ABC1", "Klub Przyszły", "Oś 25 m (2) (5)", "na wyłączność", "Jan Prowadzący", "Wpisy3", "Kod wejścia", 48600111222),
        (f"{past:%d.%m} 15:00 – 17:00", "XYZ9", "Anna Przeszła", "Oś 100m (5)", 1, "Anna Przeszła", "Brak", "Opłacone online", 48600333444),
        (f"{future:%d.%m} 13:00 – 14:00", "QQQ7", "Ktoś", "Instruktor Kowalski (5)", 1, "Ktoś", "", "Opłacone na miejscu", 48600555666),
    ], future


def test_reservations_archive_import_with_future_blocks(owner_client):
    rows, future = _res_rows()
    body = owner_client.post("/panel/rezerwacje/archiwum/import/", {"plik": res_file(rows), "rok": future.year, "tryb": "podglad"}).content.decode()
    assert "nic nie zapisano" in body and not ArchivedReservation.objects.exists() and not Block.objects.exists()
    body = owner_client.post("/panel/rezerwacje/archiwum/import/", {"plik": res_file(rows), "rok": future.year, "tryb": "importuj"}).content.decode()
    assert ArchivedReservation.objects.count() == 3 and "Instruktor Kowalski" in body
    a = ArchivedReservation.objects.get(number="ABC1")
    assert a.lane.name == "Oś 25 m nr 2" and a.block and a.contact == "48600111222"
    assert a.block.lanes.get() == a.lane and timezone.localtime(a.block.starts_at).hour == 9
    assert ArchivedReservation.objects.get(number="XYZ9").block is None  # przeszła — bez blokady
    assert ArchivedReservation.objects.get(number="QQQ7").lane is None
    owner_client.post("/panel/rezerwacje/archiwum/import/", {"plik": res_file(rows), "rok": future.year, "tryb": "importuj"})
    assert ArchivedReservation.objects.count() == 3 and Block.objects.count() == 1
    assert "Klub Przyszły" in owner_client.get("/panel/rezerwacje/archiwum/?przyszle=1").content.decode()


def test_reservations_archive_conflict_not_blocked(owner_client):
    from apps.bookings import services as booking_services
    from apps.facilities.models import Lane

    from .conftest import add_credential, approve

    rows, future = _res_rows()
    ps = make_user("ps@example.com")
    add_credential(ps)
    approve(ps)
    booking_services.create_reservation(ps, Lane.objects.get(name="Oś 25 m nr 2"), future, 10, 11, 1, created_by=ps)
    body = owner_client.post("/panel/rezerwacje/archiwum/import/", {"plik": res_file(rows[:1]), "rok": future.year, "tryb": "importuj"}).content.decode()
    assert "koliduje" in body and not Block.objects.exists() and ArchivedReservation.objects.count() == 1


def test_guntracker_imports_permissions():
    staff = grant(make_user("s@example.com"), "logbook.view_logbook")
    c = Client()
    login_with_otp(c, staff)
    assert c.get("/panel/ksiazka/archiwum/").status_code == 200
    assert c.get("/panel/ksiazka/archiwum/import/").status_code == 403
    assert c.get("/panel/rezerwacje/archiwum/").status_code == 403


def test_reservations_import_fills_person_phones(owner_client):
    rows, future = _res_rows()
    Person.objects.create(first_name="Anna", last_name="Przeszła", address="x")
    Person.objects.create(first_name="Jan", last_name="Prowadzący", address="y", phone="+48 111 222 333")
    body = owner_client.post("/panel/rezerwacje/archiwum/import/", {"plik": res_file(rows), "rok": future.year, "tryb": "podglad", "kontakty": "1"}).content.decode()
    assert Person.objects.get(last_name="Przeszła").phone == ""  # podgląd nic nie zapisuje
    owner_client.post("/panel/rezerwacje/archiwum/import/", {"plik": res_file(rows), "rok": future.year, "tryb": "importuj", "kontakty": "1"})
    assert Person.objects.get(last_name="Przeszła").phone == "+48 600 333 444"
    assert Person.objects.get(last_name="Prowadzący").phone == "+48 111 222 333"  # bez nadpisywania
    body = owner_client.get("/panel/uzytkownicy/osoby/").content.decode()
    assert "600 333 444" not in body and "tel. ukryty" in body


def test_old_persons_without_account_are_purged_with_logbook(owner):
    from datetime import timedelta

    from apps.logbook.services import purge_expired

    old = Person.objects.create(first_name="Stary", last_name="Wpis", address="x")
    linked = Person.objects.create(first_name="Z", last_name="Kontem", address="x", user=owner)
    fresh = Person.objects.create(first_name="Nowy", last_name="Wpis", address="x")
    Person.objects.filter(pk__in=[old.pk, linked.pk]).update(created_at=timezone.now() - timedelta(days=6 * 366))
    purge_expired()
    assert set(Person.objects.values_list("pk", flat=True)) == {linked.pk, fresh.pk}
