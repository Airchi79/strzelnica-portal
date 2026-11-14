"""Uproszczona ewidencja przychodów i kosztów (art. 10a ustawy o działalności pożytku publicznego; wzór z zał. nr 1
do rozporządzenia, Dz. U. 2025 poz. 1880): zapisy trwałe, korekty nowym zapisem, przychody z raportów wpływów,
sumy „w okresie” i „od początku roku”, wydruk PDF i XLSX, uprawnienia."""

import io
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.functional import empty

from apps.accounting import exports, services
from apps.accounting.models import AccountingSettings, Record
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


def test_numbering_per_year_sums_and_immutability(owner):
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
    with pytest.raises(DatabaseError), transaction.atomic():
        Record.objects.filter(pk=a.pk).update(description="zmiana")
    with pytest.raises(DatabaseError), transaction.atomic():
        Record.objects.filter(pk=a.pk).delete()
    assert AuditLog.objects.filter(action="ksiegowosc.zapis").count() == 4


def test_validation_of_manual_records(owner):
    with pytest.raises(ValidationError, match="przyszłości"):
        _cost(owner, timezone.localdate() + timedelta(days=1))
    with pytest.raises(ValidationError, match="większa od zera"):
        _cost(owner, date(2026, 1, 2), 0)
    with pytest.raises(ValidationError, match="kolumnę"):
        _cost(owner, date(2026, 1, 2), column="revenue")
    assert not Record.objects.exists()


def test_correction_is_storno_once_and_not_of_correction(owner):
    rec = _cost(owner, date(2026, 3, 5), 4000)
    with pytest.raises(ValidationError, match="przyczynę"):
        services.correct(rec, reason="  ", by=owner, day=date(2026, 3, 6))
    with pytest.raises(ValidationError, match="wcześniejsza"):
        services.correct(rec, reason="błędna kwota", by=owner, day=date(2026, 3, 4))
    with pytest.raises(ValidationError, match="z roku"):
        services.correct(_cost(owner, date(2025, 12, 30)), reason="błędna kwota", by=owner, day=date(2026, 1, 5))
    fix = services.correct(rec, reason="błędna kwota", by=owner, day=date(2026, 3, 6))
    assert fix.kind == Record.Kind.CORRECTION and fix.corrects == rec and fix.cost_deductible == -4000 and fix.notes == "błędna kwota"
    assert "Lp. 1/2026" in fix.description
    with pytest.raises(ValidationError, match="już skorygowany"):
        services.correct(rec, reason="znowu", by=owner, day=date(2026, 3, 6))
    with pytest.raises(ValidationError, match="korygującego"):
        services.correct(fix, reason="x", by=owner, day=date(2026, 3, 6))
    assert services.statement(2026, 3)["period"]["cost_deductible"] == 0


def test_sync_from_daily_sales_reports(owner):
    shooter = make_user("klient@example.com", first_name="Kasia", last_name="Klientka")
    day1 = timezone.localdate() - timedelta(days=3)
    day0 = day1 - timedelta(days=1)
    _pay(shooter, owner, 1500, Payment.Method.CASH, day0)  # przed dniem uruchomienia — nie trafia
    _pay(shooter, owner, 7000, Payment.Method.CASH, day1, 10)
    _pay(shooter, owner, 3000, Payment.Method.TRANSFER, day1, 11)
    assert services.sync_from_sales_reports() == []  # zasilanie wyłączone, dopóki zarząd nie ustawi dnia
    assert not Record.objects.exists()
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
    assert services.sync_from_sales_reports() == []  # idempotentnie
    assert Record.objects.count() == 1


def test_panel_add_cost_with_attachment_and_correct(client, owner):
    staff = grant(make_user("skarbnik@example.com", first_name="Ewa", last_name="Skarbnik"), "accounting.manage_accounting")
    login_with_otp(client, staff)
    day = timezone.localdate()
    r = client.post("/panel/ksiegowosc/dodaj/koszt/", {
        "date": day.isoformat(), "document": "FV 7/11/2026", "description": "Tarcze", "counterparty": "Hurtownia",
        "amount": "1 234,56", "column": "cost_other", "notes": "", "attachment": SimpleUploadedFile("faktura.pdf", PDF)})
    assert r.status_code == 302
    rec = Record.objects.get()
    assert rec.cost_other == 123456 and rec.created_by == staff and rec.attachment and len(rec.attachment_sha256) == 64
    assert rec.attachment.name.endswith(".pdf")
    page = client.get(f"/panel/ksiegowosc/?rok={day.year}&miesiac={day.month}")
    assert page.status_code == 200 and "FV 7/11/2026" in page.content.decode() and "Razem od początku roku" in page.content.decode()
    download = client.get(f"/panel/ksiegowosc/zapis/{rec.pk}/dowod/")
    assert download.status_code == 200 and b"".join(download.streaming_content) == PDF
    bad = client.post("/panel/ksiegowosc/dodaj/koszt/", {
        "date": day.isoformat(), "document": "X", "description": "Y", "amount": "10", "column": "cost_other",
        "attachment": SimpleUploadedFile("wirus.pdf", b"MZ\x90\x00 nie pdf")})
    assert bad.status_code == 200 and "Dozwolone są tylko pliki" in bad.content.decode()
    r = client.post(f"/panel/ksiegowosc/zapis/{rec.pk}/korekta/", {"date": day.isoformat(), "reason": "zła kolumna"})
    fix = Record.objects.get(corrects=rec)
    assert r.url == f"/panel/ksiegowosc/zapis/{fix.pk}/" and fix.cost_other == -123456
    detail = client.get(f"/panel/ksiegowosc/zapis/{rec.pk}/").content.decode()
    assert "Skorygowany zapisem" in detail and "Dodaj zapis korygujący" not in detail


def test_amount_parsing_rejects_bad_values(owner_client):
    day = timezone.localdate().isoformat()
    for amount in ("-5", "0", "12,345", "abc"):
        r = owner_client.post("/panel/ksiegowosc/dodaj/przychod/", {"date": day, "document": "D", "description": "Darowizna", "amount": amount})
        assert r.status_code == 200, amount
    assert not Record.objects.exists()


def test_permission_required(client):
    staff = grant(make_user("dyzurny@example.com"), "payments.manage_payments")
    login_with_otp(client, staff)
    for url in ("/panel/ksiegowosc/", "/panel/ksiegowosc/dodaj/koszt/", "/panel/ksiegowosc/ustawienia/", "/panel/ksiegowosc/wydruk.pdf"):
        assert client.get(url).status_code == 403, url


def test_settings_and_sync_button(owner_client):
    r = owner_client.post("/panel/ksiegowosc/uzupelnij/")
    assert r.url == "/panel/ksiegowosc/ustawienia/"
    r = owner_client.post("/panel/ksiegowosc/ustawienia/", {"auto_from": "2026-01-01", "label_skladka": "  Składki   członkowskie "})
    assert r.status_code == 302
    s = AccountingSettings.load()
    assert s.auto_from == date(2026, 1, 1) and s.labels == {"skladka": "Składki członkowskie"}
    assert s.label("rezerwacja").startswith("Dochody z majątku stowarzyszenia")


def test_exports_pdf_and_xlsx(owner_client, owner):
    from openpyxl import load_workbook

    a = AssociationSettings.load()
    _cost(owner, date(2026, 1, 15), 10000, document="=HYPERLINK(1)")
    _cost(owner, date(2026, 2, 15), 2550, column="cost_other")
    r = owner_client.get("/panel/ksiegowosc/wydruk.pdf?rok=2026&miesiac=2")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "2026-02.pdf" in r["Content-Disposition"]
    r = owner_client.get("/panel/ksiegowosc/wydruk.xlsx?rok=2026&miesiac=0")
    ws = load_workbook(io.BytesIO(r.content)).active
    values = [[c for c in row] for row in ws.iter_rows(values_only=True)]
    assert values[0][0] == "Zestawienie przychodów i kosztów — rok 2026"
    assert values[1][0] == f"Nazwa podatnika: {a.name}"
    assert values[5][:4] == ["Lp.", "Data zdarzenia", "Nr dowodu księgowego", "Opis zdarzenia"]
    assert values[6][:8] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert values[7][2] == "'=HYPERLINK(1)"  # bez formuł z danych
    totals = {row[3]: row[4:7] for row in values if row[3] in ("Razem w okresie sprawozdawczym", "Razem od początku roku")}
    assert list(totals["Razem od początku roku"]) == [0, 100, 25.5]
    assert exports.filename(services.statement(2026), "xlsx") == "zestawienie-przychodow-i-kosztow-2026.xlsx"
