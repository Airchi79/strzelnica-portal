"""Porządkowanie danych z GunTrackera z pliku zbiorczego (Klienci, Rezerwacje) i eksportu księgi pobytu: kartoteka
(aktualizacja, scalanie duplikatów, usuwanie spoza listy), lista PS, archiwum rezerwacji i książki. Dane zmyślone."""

import io
from datetime import datetime, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.bookings.models import ArchivedReservation, Block
from apps.clients import guntracker_sync
from apps.clients.models import Person
from apps.credentials.models import PreverifiedOfficer
from apps.logbook.models import ArchivedEntry

from .conftest import grant, login_with_otp, make_user
from .test_guntracker import book_file

pytestmark = pytest.mark.django_db
CLIENTS_HEADER = ["Klient", "Telefon", "E-mail", "Ulica i numer", "Kod pocztowy", "Miejscowość", "Numer pozwolenia", "Organ wydający",
                  "Adres (pełny ze źródła)", "Legitymacja prowadzącego — status", "Data wgrania legitymacji",
                  "Data weryfikacji legitymacji", "E-mail w wykazie legitymacji"]
RES_HEADER = ["data", "Godzina", "Klient", "Numer rezerwacji", "Oś", "Stanowisko", "Prowadzący", "Księga", "Płatność"]


def bundle(clients=None, reservations=None):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Rezerwacje"
    ws.append([])
    ws.append(RES_HEADER)
    for r in reservations if reservations is not None else [
        (datetime(2026, 10, 26), "09:00 – 11:00", "Jan Klient", "RAAA111", "Oś 25 m (2)", "1, 2", "Jan Klient", "Dodano2", "Online"),
        (datetime(2026, 10, 27), "12:00 – 13:00", "Ewa Klientka", "RBBB222", "Oś 100m", "1", "Ewa Klientka", "Książka papierowa", "Kod wejścia"),
    ]:
        ws.append(list(r))
    st = wb.create_sheet("rezerwacje statystyka")
    st.append(["Klienci i rezerwacje — podsumowanie"])
    kl = wb.create_sheet("Klienci")
    kl.append(CLIENTS_HEADER)
    for r in clients if clients is not None else [
        ("Jan Klient", "+48 600 000 001", "jan@example.com", "ul. Polna 1", "56-400", "Oleśnica", "aa 123", "KWP Wrocław",
         "ul. Polna 1, 56-400 Oleśnica", "Zweryfikowana", "1 lipca 2026", "2 lipca 2026", "jan@example.com"),
        ("Ewa Klientka", "600 000 002", "ewa@example.com", None, None, None, None, None, None, "Brak potwierdzenia w wykazie",
         "3 lipca 2026", "3 lipca 2026", "ewa@example.com"),
    ]:
        kl.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile("Rezerwacje_i_klienci.xlsx", buf.getvalue())


@pytest.fixture
def messy(owner):
    """Stan „namieszany” przez wcześniejsze importy."""
    dup1 = Person.objects.create(first_name="Jan", last_name="Klient", permit_number="AA123", source="guntracker",
                                 card_printed_at=timezone.now())
    Person.objects.create(first_name="JAN", last_name="klient", address="Inny 5", source="guntracker")  # duplikat
    Person.objects.create(first_name="Gość", last_name="Spoza", address="Leśna 1", source="guntracker")
    Person.objects.create(first_name="Ręczna", last_name="Osoba", address="Ręczna 1", source="panel")
    Person.objects.create(first_name="Z", last_name="Kontem", source="guntracker", user=make_user("konto@example.com"))
    PreverifiedOfficer.objects.create(name="Stary Wpis", email="stary@example.com")
    future = timezone.now() + timedelta(days=5)
    block = Block.objects.create(title="GunTracker X", starts_at=future, ends_at=future + timedelta(hours=1), created_by=owner)
    ArchivedReservation.objects.create(row_hash="x" * 64, number="RZZZ999", starts_at=future, ends_at=future + timedelta(hours=1),
                                       client="Ktoś", lane_label="Oś 35 m", block=block)
    owner_client_entry = ArchivedEntry.objects.create(row_hash="y" * 64, started_at=timezone.now(), first_name="Stary", last_name="Wpis")
    return dup1, owner_client_entry


def test_preview_changes_nothing_then_execute_cleans_everything(owner_client, messy):
    dup1, _ = messy
    page = owner_client.post("/panel/uzytkownicy/osoby/porzadkowanie-guntracker/", {
        "plik": bundle(), "ksiega": book_file(), "tryb": "podglad", "persons": "1", "prune": "1", "preverified": "1",
        "reservations": "1", "logbook": "1"}).content.decode()
    assert "Podgląd — nic nie zapisano" in page and Person.objects.count() == 5 and ArchivedReservation.objects.count() == 1
    assert ArchivedEntry.objects.count() == 1 and PreverifiedOfficer.objects.count() == 1

    page = owner_client.post("/panel/uzytkownicy/osoby/porzadkowanie-guntracker/", {
        "plik": bundle(), "ksiega": book_file(), "tryb": "wykonaj", "persons": "1", "prune": "1", "preverified": "1",
        "reservations": "1", "logbook": "1"}).content.decode()
    assert "Wykonano" in page
    # kartoteka: klienci z pliku, duplikat scalony (karta QR osoby głównej zostaje), spoza listy usunięty
    jan = Person.objects.get(last_name__iexact="Klient")
    assert jan.pk == dup1.pk and jan.card_printed_at and jan.email == "jan@example.com" and jan.phone == "+48 600 000 001"
    assert jan.address == "ul. Polna 1, 56-400 Oleśnica" and jan.permit_number == "AA123"
    ewa = Person.objects.get(last_name="Klientka")
    assert ewa.phone == "+48 600 000 002" and ewa.source == "guntracker"
    assert not Person.objects.filter(last_name="Spoza").exists()
    assert Person.objects.filter(last_name="Osoba").exists() and Person.objects.filter(last_name="Kontem").exists()
    # lista PS: tylko zweryfikowani z pliku
    assert list(PreverifiedOfficer.objects.values_list("email", flat=True)) == ["jan@example.com"]
    assert str(PreverifiedOfficer.objects.get().verified_on) == "2026-07-02"
    # archiwum rezerwacji: stare (z blokadą) usunięte, nowe z pliku z osią i telefonem
    assert not Block.objects.exists() and set(ArchivedReservation.objects.values_list("number", flat=True)) == {"RAAA111", "RBBB222"}
    r = ArchivedReservation.objects.get(number="RAAA111")
    assert r.lane.name == "Oś 25 m nr 2" and r.logbook_info == "Dodano 2" and r.contact == "+48 600 000 001"
    assert ArchivedReservation.objects.get(number="RBBB222").lane.name == "Oś 100 m"
    # archiwum książki: odtworzone z eksportu, bez dodawania gości do kartoteki
    assert ArchivedEntry.objects.count() == 2 and not ArchivedEntry.objects.filter(last_name="Wpis").exists()
    assert not Person.objects.filter(last_name="Archiwalny").exists()


def test_logbook_clear_requires_book_or_confirmation_and_owner_only(owner_client, messy):
    page = owner_client.post("/panel/uzytkownicy/osoby/porzadkowanie-guntracker/", {
        "plik": bundle(), "tryb": "wykonaj", "logbook": "1"}, follow=True).content.decode()
    assert "dołącz eksport" in page and ArchivedEntry.objects.count() == 1
    staff = grant(make_user("kartoteka@example.com", is_staff=True), "clients.manage_clients")
    c = Client()
    login_with_otp(c, staff)
    assert c.get("/panel/uzytkownicy/osoby/porzadkowanie-guntracker/").status_code == 403


def test_bad_bundle_reports_missing_sheet(owner_client):
    from openpyxl import Workbook

    wb = Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    page = owner_client.post("/panel/uzytkownicy/osoby/porzadkowanie-guntracker/", {
        "plik": SimpleUploadedFile("x.xlsx", buf.getvalue()), "tryb": "podglad", "persons": "1"}, follow=True).content.decode()
    assert "Brak arkusza" in page
