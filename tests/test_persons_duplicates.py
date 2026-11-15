"""Kartoteka osób bez konta: import bez duplikatów (pisownia, brak pozwolenia), eksport XLSX z grupami możliwych
duplikatów, scalanie, zaznaczanie wszystkich. Dane zmyślone."""

import io

import pytest
from django.utils import timezone

from apps.audit.models import SensitiveAccessLog
from apps.clients import duplicates
from apps.clients.models import Person

from .conftest import make_user
from .test_clients import GT_HEADER_FULL, _gt_xlsx

pytestmark = pytest.mark.django_db


def test_import_matches_spelling_variants_and_rows_without_permit(owner_client):
    Person.objects.create(first_name="Łukasz", last_name="Kiełbasa", permit_number="AB1", permit_issuer="KWP Wrocław")
    rows = [("lukasz", "KIELBASA", "", "", "Polna 1, Sątok", "", ""),           # bez pozwolenia → ta sama osoba
            ("Łukasz", "Kiełbasa", "ZZ9", "KWP Opole", "Leśna 2, Opole", "", "")]  # inne pozwolenie → inna osoba
    owner_client.post("/panel/uzytkownicy/osoby/import/", {"plik": _gt_xlsx(rows, header=GT_HEADER_FULL), "tryb": "importuj"})
    assert Person.objects.count() == 2
    assert Person.objects.get(permit_number="AB1").address == "Polna 1, Sątok"
    # ponowny import tych samych wierszy nie dodaje nikogo
    owner_client.post("/panel/uzytkownicy/osoby/import/", {"plik": _gt_xlsx(rows, header=GT_HEADER_FULL), "tryb": "importuj"})
    assert Person.objects.count() == 2
    assert [len(g) for g in duplicates.groups()] == [2]  # dwie osoby o tym samym nazwisku — do decyzji w panelu


def test_export_xlsx_marks_duplicate_groups_and_is_logged(owner_client):
    from openpyxl import load_workbook

    Person.objects.create(first_name="Jan", last_name="Dubel", permit_number="P1", permit_issuer="KWP")
    Person.objects.create(first_name="JAN", last_name="Dubel", address="Sątok 3")
    Person.objects.create(first_name="Ewa", last_name="Inna", permit_number="P1")  # ten sam nr pozwolenia → ta sama grupa
    Person.objects.create(first_name="Ola", last_name="Sama", email="ola@example.com")
    r = owner_client.get("/panel/uzytkownicy/osoby/eksport.xlsx")
    assert r.status_code == 200 and "attachment" in r["Content-Disposition"]
    ws = load_workbook(io.BytesIO(r.content)).active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == tuple(duplicates.EXPORT_HEADER) and len(rows) == 5
    group = {(r[1], r[2]): r[-1] for r in rows[1:]}
    assert group[("Jan", "Dubel")] == group[("JAN", "Dubel")] == group[("Ewa", "Inna")] == 1 and group[("Ola", "Sama")] is None
    assert SensitiveAccessLog.objects.filter(resource="kartoteka:eksport").exists()


def test_merge_fills_missing_keeps_differences_and_deletes_copies(owner_client):
    main = Person.objects.create(first_name="Jan", last_name="Dubel", permit_number="P1", permit_issuer="KWP", address="Sątok 3")
    copy = Person.objects.create(first_name="jan", last_name="dubel", address="Inny 5", phone="+48 600 000 000",
                                 card_printed_at=timezone.now())
    page = owner_client.get("/panel/uzytkownicy/osoby/duplikaty/").content.decode()
    assert "Możliwe duplikaty: 1 grup" in page and f'value="{main.pk}" checked' in page  # główna: z pozwoleniem
    r = owner_client.post("/panel/uzytkownicy/osoby/duplikaty/", {"glowna": main.pk, "scal": [copy.pk]}, follow=True)
    main.refresh_from_db()
    assert not Person.objects.filter(pk=copy.pk).exists() and main.phone == "+48 600 000 000"
    assert "adres: Inny 5" in main.note and "przestały działać" in r.content.decode()
    assert "Brak możliwych duplikatów" in owner_client.get("/panel/uzytkownicy/osoby/duplikaty/").content.decode()


def test_merge_refuses_persons_with_different_accounts(owner):
    a = Person.objects.create(first_name="Jan", last_name="Dubel", user=make_user("a@example.com"))
    b = Person.objects.create(first_name="Jan", last_name="Dubel", user=make_user("b@example.com"))
    with pytest.raises(Exception, match="różnymi kontami"):
        duplicates.merge(a, [b], by=owner)
    assert Person.objects.count() == 2


def test_persons_list_has_select_all_export_and_duplicates_links(owner_client):
    Person.objects.create(first_name="Ola", last_name="Sama")
    page = owner_client.get("/panel/uzytkownicy/osoby/").content.decode()
    assert 'data-check-all="ids"' in page and "/panel/uzytkownicy/osoby/eksport.xlsx" in page and "Możliwe duplikaty" in page
