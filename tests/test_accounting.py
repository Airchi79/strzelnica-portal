"""Uproszczona ewidencja przychodów i kosztów (art. 10a ustawy o działalności pożytku publicznego; rozporządzenie
Dz. U. 2025 poz. 1880): zapisy z historią wersji (zmiana treści, usunięcie, przywrócenie — § 7 ust. 2 pkt 1),
zamknięcie roku, przychody z raportów wpływów, sumy „w okresie” i „od początku roku”, wydruk PDF i XLSX, uprawnienia."""

import io
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.functional import empty

from apps.accounting import exports, services
from apps.accounting.models import AccountingSettings, AccountingYear, Record, RecordVersion
from apps.audit.models import AuditLog
from apps.core.models import AssociationSettings
from apps.credentials.models import private_storage
from apps.payments.models import Payment

from .conftest import grant, login_with_otp, make_user
from .test_reports import _pay

pytestmark = pytest.mark.django_db
PDF = b"%PDF-1.4\n%test\n"


@pytest.fixture(autouse=True)
def _private_media(settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = tmp_path
    private_storage._wrapped = empty
    yield
    private_storage._wrapped = empty


def _cost(owner, day, amount=12345, column="cost_deductible", **kw):
    return services.add_record(kind=Record.Kind.COST, day=day, document=kw.pop("document", "FV 1/2026"),
                               description=kw.pop("description", "Energia elektryczna"), amount=amount, column=column, by=owner, **kw)


def _update(record, owner, **kw):
    data = {"day": record.date, "document": record.document, "description": record.description,
            "amount": services.record_amount(record), "column": services.record_column(record), "counterparty": record.counterparty,
            "notes": record.notes, "reason": "poprawka"}
    data.update(kw)
    return services.update_record(record, by=owner, **data)


def test_numbering_per_year_and_sums(owner):
    a = _cost(owner, date(2026, 1, 15), 10000)
    b = services.add_record(kind=Record.Kind.REVENUE, day=date(2026, 2, 3), document="WB 2/2026", description="Darowizna",
                            amount=50000, column="revenue", by=owner)
    c = _cost(owner, date(2026, 2, 10), 2500, column="cost_other", description="Kara umowna")
    old = _cost(owner, date(2025, 12, 30), 999)
    assert (a.lp, b.lp, c.lp, old.lp) == ("1/2026", "2/2026", "3/2026", "1/2025")
    feb = services.statement(2026, 2)
    assert [r.pk for r in feb["records"]] == [b.pk, c.pk]
    assert feb["period"] == {"revenue": 50000, "cost_deductible": 0, "cost_other": 2500}
    assert feb["ytd"] == {"revenue": 50000, "cost_deductible": 10000, "cost_other": 2500}  # bez zapisów z 2025
    year = services.statement(2026)
    assert year["period"] == year["ytd"]
    assert AuditLog.objects.filter(action="ksiegowosc.zapis").count() == 4
    assert RecordVersion.objects.filter(action="utworzenie").count() == 4


def test_validation_of_manual_records(owner):
    with pytest.raises(ValidationError, match="przyszłości"):
        _cost(owner, timezone.localdate() + timedelta(days=1))
    with pytest.raises(ValidationError, match="większa od zera"):
        _cost(owner, date(2026, 1, 2), 0)
    with pytest.raises(ValidationError, match="kolumnę"):
        _cost(owner, date(2026, 1, 2), column="revenue")
    assert not Record.objects.exists()


def test_edit_keeps_number_and_full_history(owner):
    rec = _cost(owner, date(2026, 3, 5), 4000)
    with pytest.raises(ValidationError, match="powód"):
        _update(rec, owner, amount=4500, reason=" ")
    with pytest.raises(ValidationError, match="Nic się nie zmieniło"):
        _update(rec, owner)
    with pytest.raises(ValidationError, match="w roku 2026"):
        _update(rec, owner, day=date(2025, 12, 31))
    rec = _update(rec, owner, amount=4500, column="cost_other", description="Energia — luty", reason="kwota z korekty faktury")
    assert (rec.lp, rec.version, rec.cost_other, rec.cost_deductible) == ("1/2026", 2, 4500, 0)
    assert rec.updated_by == owner
    hist = services.history(rec)
    assert [h["v"].action for h in hist] == ["utworzenie", "zmiana"]
    changed = {f: (old, new) for f, old, new in hist[1]["changes"]}
    assert changed["cost_deductible"] == (4000, 0) and changed["cost_other"] == (0, 4500)
    assert hist[1]["v"].reason == "kwota z korekty faktury" and hist[0]["v"].data["description"] == "Energia elektryczna"
    # historia jest trwała (trigger w bazie)
    with pytest.raises(DatabaseError), transaction.atomic():
        RecordVersion.objects.filter(record=rec).update(reason="x")
    with pytest.raises(DatabaseError), transaction.atomic():
        RecordVersion.objects.filter(record=rec).delete()


def test_remove_and_restore(owner):
    a = _cost(owner, date(2026, 4, 1), 1000)
    b = _cost(owner, date(2026, 4, 2), 2000)
    with pytest.raises(ValidationError, match="przyczynę"):
        services.remove_record(a, by=owner, reason="")
    services.remove_record(a, by=owner, reason="dubel faktury")
    a.refresh_from_db()
    st = services.statement(2026, 4)
    assert [r.pk for r in st["records"]] == [a.pk, b.pk]  # usunięty zostaje na liście (numeracja ciągła)
    assert st["period"]["cost_deductible"] == 2000 and st["ytd"]["cost_deductible"] == 2000
    with pytest.raises(ValidationError, match="przywróć"):
        _update(a, owner, amount=5)
    services.restore_record(a, by=owner)
    assert services.statement(2026, 4)["period"]["cost_deductible"] == 3000
    assert [v.action for v in a.versions.order_by("pk")] == ["utworzenie", "usuniecie", "przywrocenie"]


def test_closed_year_blocks_changes(owner):
    rec = _cost(owner, date(2025, 6, 1), 1000)
    with pytest.raises(ValidationError, match="zakończony"):
        services.close_year(timezone.localdate().year, by=owner)
    services.close_year(2025, by=owner)
    for action in (lambda: _update(rec, owner, amount=2000), lambda: services.remove_record(rec, by=owner, reason="x"),
                   lambda: _cost(owner, date(2025, 7, 1))):
        with pytest.raises(ValidationError, match="zamknięty"):
            action()
    with pytest.raises(ValidationError, match="powód"):
        services.reopen_year(2025, by=owner, reason="")
    services.reopen_year(2025, by=owner, reason="korekta zeznania")
    _update(rec, owner, amount=2000)


def test_sync_from_daily_sales_reports_and_negative_day(owner):
    shooter = make_user("klient@example.com", first_name="Kasia", last_name="Klientka")
    day1 = timezone.localdate() - timedelta(days=3)
    day0 = day1 - timedelta(days=1)
    _pay(shooter, owner, 1500, Payment.Method.CASH, day0)  # przed dniem uruchomienia — nie trafia
    _pay(shooter, owner, 7000, Payment.Method.CASH, day1, 10)
    _pay(shooter, owner, 3000, Payment.Method.TRANSFER, day1, 11)
    assert services.sync_from_sales_reports() == []  # zasilanie wyłączone, dopóki zarząd nie ustawi dnia
    settings_obj = AccountingSettings.load()
    settings_obj.auto_from = day1
    settings_obj.labels = {"inne": "Dochody z majątku — udostępnianie strzelnicy (saldo)"}
    settings_obj.save()
    created = services.sync_from_sales_reports()
    assert len(created) == 1
    rec = created[0]
    assert rec.kind == Record.Kind.REVENUE_AUTO and rec.date == day1 and rec.revenue == 10000 and rec.category == "inne"
    assert rec.document == rec.sales_report.number and rec.document.startswith("RS/D/")
    assert rec.description == "Dochody z majątku — udostępnianie strzelnicy (saldo)" and rec.notes.endswith("liczba operacji: 2")
    assert rec.versions.get().by is None  # proces automatyczny
    # zapis z systemu można poprawić; usunięty nie wraca przy kolejnym uzupełnianiu
    _update(rec, owner, description="Udostępnianie strzelnicy")
    services.remove_record(rec, by=owner, reason="test")
    assert services.sync_from_sales_reports() == [] and Record.objects.count() == 1
    # przychody dnia mogą być ujemne (zwroty większe od wpływów) — taki zapis też da się poprawić
    services.restore_record(rec, by=owner)
    _update(Record.objects.get(), owner, amount=-500, reason="zwrot większy niż wpływy")
    assert Record.objects.get().revenue == -500


def test_panel_add_edit_remove_with_attachment(client, owner):
    staff = grant(make_user("skarbnik@example.com", first_name="Ewa", last_name="Skarbnik"), "accounting.manage_accounting")
    login_with_otp(client, staff)
    day = timezone.localdate()
    r = client.post("/panel/ksiegowosc/dodaj/koszt/", {
        "date": day.isoformat(), "document": "FV 7/11/2026", "description": "Tarcze", "counterparty": "Hurtownia",
        "amount": "1 234,56", "column": "cost_other", "notes": "", "attachment": SimpleUploadedFile("faktura.pdf", PDF)})
    assert r.status_code == 302
    rec = Record.objects.get()
    assert rec.cost_other == 123456 and rec.created_by == staff and rec.attachment and len(rec.attachment_sha256) == 64
    page = client.get(f"/panel/ksiegowosc/?rok={day.year}&miesiac={day.month}").content.decode()
    assert "FV 7/11/2026" in page and "Razem od początku roku" in page
    download = client.get(f"/panel/ksiegowosc/zapis/{rec.pk}/dowod/")
    assert download.status_code == 200 and b"".join(download.streaming_content) == PDF
    bad = client.post("/panel/ksiegowosc/dodaj/koszt/", {
        "date": day.isoformat(), "document": "X", "description": "Y", "amount": "10", "column": "cost_other",
        "attachment": SimpleUploadedFile("wirus.pdf", b"MZ\x90\x00 nie pdf")})
    assert bad.status_code == 200 and "Dozwolone są tylko pliki" in bad.content.decode()
    edit_page = client.get(f"/panel/ksiegowosc/zapis/{rec.pk}/zmien/").content.decode()
    assert 'value="1234,56"' in edit_page
    r = client.post(f"/panel/ksiegowosc/zapis/{rec.pk}/zmien/", {
        "date": day.isoformat(), "document": "FV 7/11/2026", "description": "Tarcze papierowe", "counterparty": "Hurtownia",
        "amount": "1234,56", "column": "cost_deductible", "notes": "", "reason": "zła kolumna"})
    assert r.status_code == 302
    rec.refresh_from_db()
    assert rec.cost_deductible == 123456 and rec.cost_other == 0 and rec.version == 2 and rec.attachment
    r = client.post(f"/panel/ksiegowosc/zapis/{rec.pk}/usun/", {"reason": "pomyłka"})
    rec.refresh_from_db()
    assert rec.is_removed and rec.removed_by == staff
    detail = client.get(f"/panel/ksiegowosc/zapis/{rec.pk}/").content.decode()
    assert "Przywróć do ewidencji" in detail and "zła kolumna" in detail and "Tarcze papierowe" in detail


def test_amount_parsing_rejects_bad_values(owner_client):
    day = timezone.localdate().isoformat()
    for amount in ("-5", "0", "12,345", "abc", "NaN"):
        r = owner_client.post("/panel/ksiegowosc/dodaj/przychod/", {"date": day, "document": "D", "description": "Darowizna", "amount": amount})
        assert r.status_code == 200, amount
    assert not Record.objects.exists()


def test_permission_required(client):
    staff = grant(make_user("dyzurny@example.com"), "payments.manage_payments")
    login_with_otp(client, staff)
    for url in ("/panel/ksiegowosc/", "/panel/ksiegowosc/dodaj/koszt/", "/panel/ksiegowosc/ustawienia/", "/panel/ksiegowosc/wydruk.pdf",
                "/panel/ksiegowosc/ksef/", "/panel/ksiegowosc/dokumenty/", "/panel/ksiegowosc/lata/",
                "/panel/ksiegowosc/dokumenty/nowy/opis_zasad/"):
        assert client.get(url).status_code == 403, url


def test_settings_and_sync_button(owner_client):
    r = owner_client.post("/panel/ksiegowosc/uzupelnij/")
    assert r.url == "/panel/ksiegowosc/ustawienia/"
    r = owner_client.post("/panel/ksiegowosc/ustawienia/", {"auto_from": "2026-01-01", "label_skladka": "  Składki   członkowskie ",
                                                            "approving_body": "Zebranie Członków", "tax_office": "US Oleśnica",
                                                            "signatories": "Jan Testowy — przedstawiciel"})
    assert r.status_code == 302
    s = AccountingSettings.load()
    assert s.auto_from == date(2026, 1, 1) and s.labels == {"skladka": "Składki członkowskie"} and s.tax_office == "US Oleśnica"
    assert s.label("rezerwacja").startswith("Dochody z majątku stowarzyszenia")


def test_years_page_close_and_reopen(owner_client, owner):
    _cost(owner, date(2025, 5, 5), 1000)
    page = owner_client.get("/panel/ksiegowosc/lata/").content.decode()
    assert "Zamknij rok" in page
    owner_client.post("/panel/ksiegowosc/lata/", {"rok": "2025", "akcja": "zamknij"})
    assert AccountingYear.is_closed(2025)
    owner_client.post("/panel/ksiegowosc/lata/", {"rok": "2025", "akcja": "otworz", "powod": "korekta"})
    assert not AccountingYear.is_closed(2025)


def test_exports_pdf_and_xlsx_mark_removed(owner_client, owner):
    from openpyxl import load_workbook

    a = AssociationSettings.load()
    _cost(owner, date(2026, 1, 15), 10000, document="=HYPERLINK(1)")
    _cost(owner, date(2026, 2, 15), 2550, column="cost_other")
    removed = _cost(owner, date(2026, 2, 16), 9999)
    services.remove_record(removed, by=owner, reason="dubel")
    r = owner_client.get("/panel/ksiegowosc/wydruk.pdf?rok=2026&miesiac=2")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "2026-02.pdf" in r["Content-Disposition"]
    r = owner_client.get("/panel/ksiegowosc/wydruk.xlsx?rok=2026&miesiac=0")
    ws = load_workbook(io.BytesIO(r.content)).active
    values = [list(row) for row in ws.iter_rows(values_only=True)]
    assert values[0][0] == "Zestawienie przychodów i kosztów — rok 2026"
    assert values[1][0] == f"Nazwa podatnika: {a.name}"
    assert values[5][:4] == ["Lp.", "Data zdarzenia", "Nr dowodu księgowego", "Opis zdarzenia"]
    assert values[6][:8] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert values[7][2] == "'=HYPERLINK(1)"  # bez formuł z danych
    removed_row = next(row for row in values if row[0] == "3/2026")
    assert removed_row[4:7] == [None, None, None] and removed_row[7].startswith("ZAPIS USUNIĘTY")
    totals = {row[3]: row[4:7] for row in values if row[3] in ("Razem w okresie sprawozdawczym", "Razem od początku roku")}
    assert list(totals["Razem od początku roku"]) == [0, 100, 25.5]
    assert exports.filename(services.statement(2026), "xlsx") == "zestawienie-przychodow-i-kosztow-2026.xlsx"
