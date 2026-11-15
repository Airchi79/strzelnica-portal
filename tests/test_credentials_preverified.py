"""PS zweryfikowani wcześniej (lista z GunTrackera): import z podglądem, automatyczne zatwierdzenie uprawnień PS i klienta
po potwierdzeniu e-maila, uzupełnianie e-maili w kartotece, łączenie kartoteki z kontem. Dane zmyślone."""

import io
from datetime import date

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.tokens import email_verification_token
from apps.clients.models import Person
from apps.clients.services import profile_for
from apps.credentials import preverified
from apps.credentials.models import Credential, PreverifiedOfficer

from .conftest import make_user

pytestmark = pytest.mark.django_db


def gt_list(rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Legitymacje Prowadzących Strzelanie, które zweryfikował Twój zespół.", None, None, None, None])
    ws.append(["Lista do weryfikacji ponownej i cofnięcia uprawnień.", None, None, None, None])
    ws.append(["Użytkownik", "Email", "Wgrana", "Zweryfikowano", "Akcje"])
    for name, email, when in rows:
        ws.append([name, email, "01.06.2026", when, "Cofnij"])
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile("Zweryfikowane_legitymacje_prowadzacego.xlsx", buf.getvalue())


ROWS = [("Paweł Testowy", "pawel@example.com", "2 czerwca 2026"), ("Anna Próbna", "anna@example.com", "3 czerwca 2026"),
        ("Ewa Dwojga", "ewa@example.com", "03.06.2026"), ("Bez Maila", "", "03.06.2026")]


def ps_post(client):
    return client.post("/dokumenty/dodaj/", {"kind": "ps", "number": "ps 123/2020", "issuer": "PZSS", "issued_on": "2020-05-01"},
                       follow=True)


def test_name_key_ignores_case_diacritics_and_order():
    assert preverified.name_key("Paweł", "Łukasiński") == preverified.name_key("LUKASINSKI  pawel")
    assert preverified.name_key("Jan Kowalski") != preverified.name_key("Jan Kowalska")


def test_dates_in_guntracker_and_numeric_formats():
    assert preverified._date("5 października 2025") == date(2025, 10, 5)
    assert preverified._date("14 listopada 2026") == date(2026, 11, 14)
    assert preverified._date("03.06.2026") == date(2026, 6, 3)
    assert preverified._date("2026-06-03 10:00") == date(2026, 6, 3)
    assert preverified._date("31 lutego 2026") is None and preverified._date("") is None


def test_import_preview_then_save_fills_kartoteka_and_approves_waiting_account(owner_client, owner):
    Person.objects.create(first_name="Paweł", last_name="Testowy", source="guntracker")  # bez e-maila → uzupełnić
    Person.objects.create(first_name="Ewa", last_name="Dwojga")
    Person.objects.create(first_name="Ewa", last_name="Dwojga")  # dwie takie osoby → nie zgadujemy
    waiting = make_user("anna@example.com", first_name="Anna", last_name="Próbna")
    Credential.objects.create(user=waiting, kind="ps", number="PS-9", issuer="PZSS", issued_on=date(2020, 1, 1))
    mismatch = make_user("ewa@example.com", first_name="Ewa", last_name="Inna")
    Credential.objects.create(user=mismatch, kind="ps", number="PS-8", issuer="PZSS", issued_on=date(2020, 1, 1))
    mail.outbox.clear()

    before = owner_client.get("/panel/weryfikacja/ps-zweryfikowani/").content.decode()
    assert "Lista jest pusta" in before
    r = owner_client.post("/panel/weryfikacja/ps-zweryfikowani/", {"plik": gt_list(ROWS), "tryb": "podglad",
                                                                    "zatwierdz_klienta": "1", "uzupelnij_emaile": "1"})
    page = r.content.decode()
    assert "Podgląd — nic nie zapisano" in page and "do zatwierdzenia" in page and "e-mail do uzupełnienia" in page
    assert PreverifiedOfficer.objects.count() == 0 and Person.objects.get(last_name="Testowy").email == ""
    assert Credential.objects.get(user=waiting).status == Credential.Status.PENDING and not mail.outbox  # podgląd bez e-maili

    r = owner_client.post("/panel/weryfikacja/ps-zweryfikowani/", {"plik": gt_list(ROWS), "tryb": "importuj",
                                                                    "zatwierdz_klienta": "1", "uzupelnij_emaile": "1"})
    page = r.content.decode()
    assert PreverifiedOfficer.objects.count() == 3 and "brak lub błędny adres e-mail" in page
    assert PreverifiedOfficer.objects.get(email="anna@example.com").verified_on == date(2026, 6, 3)
    assert Person.objects.get(last_name="Testowy").email == "pawel@example.com"
    assert not Person.objects.filter(last_name="Dwojga").exclude(email="").exists() and "2 osoby o tym imieniu i nazwisku" in page
    assert "Anna Próbna — brak w kartotece" in page  # raport pokazuje też osoby spoza kartoteki
    listing = owner_client.get("/panel/weryfikacja/ps-zweryfikowani/").content.decode()
    assert "e-mail już jest — <strong>1</strong>" in listing and "brak w kartotece — <strong>1</strong>" in listing
    assert "do sprawdzenia ręcznie — <strong>1</strong>" in listing
    c = Credential.objects.get(user=waiting)
    assert c.status == Credential.Status.VERIFIED and c.reviewed_by is None and "GunTracker (03.06.2026)" in c.review_note
    assert profile_for(waiting).approved_at is not None  # klient zatwierdzony
    assert Credential.objects.get(user=mismatch).status == Credential.Status.PENDING and "inne imię lub nazwisko" in page
    auto = owner_client.get("/panel/weryfikacja/?status=auto").content.decode()
    assert "Anna Próbna" in auto
    # ponowny import — bez duplikatów
    owner_client.post("/panel/weryfikacja/ps-zweryfikowani/", {"plik": gt_list(ROWS), "tryb": "importuj"})
    assert PreverifiedOfficer.objects.count() == 3


def test_new_account_approved_after_email_confirmation_and_linked_to_kartoteka(client):
    preverified.run(gt_list(ROWS), by=None)
    person = Person.objects.create(first_name="Paweł", last_name="Testowy", email="pawel@example.com")
    user = make_user("pawel@example.com", verified=False, first_name="Paweł", last_name="Testowy")
    client.force_login(user)
    ps_post(client)
    credential = Credential.objects.get(user=user)
    assert credential.status == Credential.Status.PENDING  # e-mail jeszcze niepotwierdzony — nikt nie podszyje się pod adres
    uid, token = urlsafe_base64_encode(force_bytes(user.pk)), email_verification_token.make_token(user)
    client.get(f"/konto/potwierdz/{uid}/{token}/")
    credential.refresh_from_db()
    person.refresh_from_db()
    assert credential.status == Credential.Status.VERIFIED and profile_for(user).approved_at is not None
    assert person.user == user
    assert PreverifiedOfficer.objects.get(email="pawel@example.com").applied_user == user


def test_verified_account_adding_ps_is_approved_immediately_without_board_queue(client):
    preverified.run(gt_list(ROWS), by=None, approve_client=False)
    user = make_user("pawel@example.com", first_name="Pawel", last_name="TESTOWY")
    client.force_login(user)
    mail.outbox.clear()
    r = ps_post(client)
    assert "możesz od razu rezerwować" in r.content.decode()
    assert Credential.objects.get(user=user).status == Credential.Status.VERIFIED
    assert not any("Nowy dokument do weryfikacji" in m.subject for m in mail.outbox)  # bez kolejki do obsługi
    assert profile_for(user).approved_at is None  # import bez zatwierdzania klienta


def test_name_mismatch_or_unknown_email_goes_to_normal_review(client):
    preverified.run(gt_list(ROWS), by=None)
    for email, first, last in [("pawel@example.com", "Piotr", "Testowy"), ("obcy@example.com", "Paweł", "Testowy")]:
        user = make_user(email, first_name=first, last_name=last)
        client.force_login(user)
        r = ps_post(client)
        assert "czeka na weryfikację" in r.content.decode()
        assert Credential.objects.get(user=user).status == Credential.Status.PENDING
