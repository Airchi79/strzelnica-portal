"""Wyciąg bankowy w księgowości: import „Zestawienia operacji” BNP Paribas (XLSX), wpłaty strzelców rozliczane przez
system, zapłaty faktur KSeF, księgowanie pojedyncze i zbiorcze, pominięcie, cofanie. Dane w testach są zmyślone."""

import io
from datetime import date, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.utils.functional import empty

from apps.accounting import bank, ksef, services
from apps.accounting.models import BankOperation, Record
from apps.credentials.models import private_storage
from apps.payments.models import Payment

from .conftest import make_user
from .test_accounting_ksef_docs import KSEF_NO, invoice_xml

pytestmark = pytest.mark.django_db
OWN = "11222233334444555566667777"
HEADER = ["Data transakcji", "Data zaksięgowania", "Data odrzucenia", "Kwota", "Waluta", "Nadawca", "Odbiorca", "Opis", "Produkt",
          "Typ transakcji", "Kwota zlecenia", "Waluta zlecenia", "Status", "Saldo po transakcji"]


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = tmp_path
    private_storage._wrapped = empty
    yield
    private_storage._wrapped = empty


def op_row(day, amount, *, who="99888877776666555544443333\nJAN TESTOWY\nUL. PRZYKŁADOWA 1\n00-001 TESTOWO", title="opłata",
           status="Zrealizowana", rejected=None, balance=1000.0):
    when = datetime.combine(day, datetime.min.time()).replace(hour=10)
    own = f"{OWN}\nSTOWARZYSZENIE TESTOWE"
    inflow = amount > 0
    return [when, when, rejected, amount, "PLN", who if inflow else own, own if inflow else who, title,
            f"Rachunek Firmowy\n{OWN}", "Przelew przychodzący" if inflow else "Przelew wychodzący", abs(amount), "PLN", status, balance]


def xlsx(rows, name="Zestawienie_operacji.xlsx"):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "ExcelExportFile"
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile(name, buf.getvalue())


def test_parse_bnp_export_skips_rejected_and_minimises_data():
    d = date(2026, 3, 2)
    rows = bank.parse_xlsx(xlsx([op_row(d, 120.5), op_row(d, -45.0, who="12345678901234567890123456\nDRUKARNIA TESTOWA"),
                                 op_row(d, 10.0, status="Odrzucona"), op_row(d, 5.0, rejected=datetime(2026, 3, 3))]))
    assert len(rows) == 2
    inflow, outflow = rows
    assert inflow["amount"] == 12050 and inflow["counterparty"] == "JAN TESTOWY" and inflow["counterparty_account"] == "99888877776666555544443333"
    assert "PRZYKŁADOWA" not in str(inflow)  # adres nie jest zapisywany
    assert inflow["account"] == OWN and inflow["booked_on"] == d and inflow["balance_after"] == 100000
    assert outflow["amount"] == -4500 and outflow["counterparty"] == "DRUKARNIA TESTOWA"
    with pytest.raises(ValidationError, match="Brak kolumn"):
        from openpyxl import Workbook

        wb = Workbook()
        wb.active.append(["Data", "Kwota"])
        buf = io.BytesIO()
        wb.save(buf)
        bank.parse_xlsx(SimpleUploadedFile("x.xlsx", buf.getvalue()))


def test_import_dedupes_and_links_invoice_payment(owner):
    inv = ksef.import_files([SimpleUploadedFile(f"{KSEF_NO}.xml", invoice_xml(number="FV 3/10/2026", gross="45.00"))],
                            by=owner, own_nip="1111111111")[0][0]
    day = inv.issue_date + timedelta(days=3)
    rows = [op_row(day, 100.0, title="opłata za oś", balance=1100),
            op_row(day, -45.0, who="47000000000000000000000002\nHURTOWNIA TESTOWA", title="FV 3/10/2026", balance=1055)]
    created, dup, system, invoices = bank.import_xlsx(xlsx(rows), by=owner)
    assert len(created) == 2 and dup == 0 and system == 0 and invoices == 1
    out = BankOperation.objects.get(amount=-4500)
    assert out.status == BankOperation.Status.INVOICE and out.invoice == inv
    assert BankOperation.objects.get(amount=10000).status == BankOperation.Status.NEW
    # ponowne wgranie — bez duplikatów
    created, dup, *_ = bank.import_xlsx(xlsx(rows), by=owner)
    assert created == [] and dup == 2


def test_shooter_transfer_by_reservation_title_is_settled_by_system(owner, monkeypatch):
    from apps.payments import services as payments

    shooter = make_user("strzelec2@example.com")
    monkeypatch.setattr(payments, "match_title", lambda title: (shooter, None) if "SATOK" in title else (None, None))
    day = timezone.localdate() - timedelta(days=1)
    created, _, system, _ = bank.import_xlsx(xlsx([op_row(day, 50.0, title="SATOK doładowanie salda"),
                                                   op_row(day, 30.0, title="darowizna", balance=1030)]), by=owner)
    assert system == 1
    settled = BankOperation.objects.get(amount=5000)
    assert settled.status == BankOperation.Status.SYSTEM and settled.payment.status == Payment.Status.COMPLETED
    assert BankOperation.objects.get(amount=3000).status == BankOperation.Status.NEW
    assert not Payment.objects.filter(status=Payment.Status.UNMATCHED).exists()  # nierozpoznane nie zaśmiecają Płatności


def test_book_skip_reopen_and_record_removal(owner):
    day = date(2026, 3, 2)
    bank.import_xlsx(xlsx([op_row(day, 200.0, who="51000000000000000000000001\nSTRIPE TECHNOLOGY EUROPE LTD", title="payout"),
                           op_row(day, -12.5, who="", title="Opłata za prowadzenie rachunku", balance=1187.5),
                           op_row(day, 7.0, title="zwrot pomyłki", balance=1194.5)]), by=owner)
    stripe, fee, mistake = (BankOperation.objects.get(amount=a) for a in (20000, -1250, 700))
    assert "operatora płatności" in bank.suggestion(stripe) and bank.suggestion(fee) == "Opłaty i prowizje bankowe"
    with pytest.raises(ValidationError, match="przychód"):
        bank.book(stripe, by=owner, description="x", column="cost_other")
    rec = bank.book(stripe, by=owner, description="Wypłata od operatora płatności", column="revenue", activity="pozostala")
    assert rec.revenue == 20000 and rec.document == "Wyciąg bankowy 02.03.2026" and rec.activity == "pozostala"
    stripe.refresh_from_db()
    assert stripe.status == BankOperation.Status.BOOKED and stripe.record == rec
    with pytest.raises(ValidationError, match="nie czeka"):
        bank.book(stripe, by=owner, description="x", column="revenue")
    fee_rec = bank.book(fee, by=owner, description="Opłaty bankowe", column="cost_other")
    assert fee_rec.cost_other == 1250
    with pytest.raises(ValidationError, match="przyczynę"):
        bank.skip(mistake, by=owner, reason="")
    bank.skip(mistake, by=owner, reason="zwrot pomyłkowej wpłaty")
    bank.reopen(mistake, by=owner)
    assert BankOperation.objects.get(pk=mistake.pk).status == BankOperation.Status.NEW
    # usunięcie zapisu → operacja wraca do decyzji; przywrócenie zapisu → znów zaksięgowana
    with pytest.raises(ValidationError, match="usuń zapis"):
        bank.reopen(stripe, by=owner)
    services.remove_record(rec, by=owner, reason="pomyłka")
    assert BankOperation.objects.get(pk=stripe.pk).status == BankOperation.Status.NEW
    services.restore_record(rec, by=owner)
    assert BankOperation.objects.get(pk=stripe.pk).status == BankOperation.Status.BOOKED


def test_panel_import_bulk_and_detail(owner_client):
    day = date(2026, 3, 2)
    f = xlsx([op_row(day, 40.0, title="oś 1"), op_row(day, 60.0, title="oś 2", balance=1060), op_row(day, -10.0, title="opłata", balance=1050)])
    r = owner_client.post("/panel/ksiegowosc/wyciag/import/", {"plik": f}, follow=True)
    assert "nowe operacje: 3" in r.content.decode()
    ops = list(BankOperation.objects.order_by("amount"))
    page = owner_client.get("/panel/ksiegowosc/wyciag/").content.decode()
    assert "Do decyzji (3)" in page and "JAN TESTOWY" in page and 'data-check-all="operacja"' in page
    r = owner_client.post("/panel/ksiegowosc/wyciag/zbiorczo/", {"operacja": [o.pk for o in ops], "description": "Udostępnianie strzelnicy (GunTracker)",
                                                                  "cost_column": "cost_other", "activity": "pozostala"}, follow=True)
    assert "Zaksięgowano operacji: 3" in r.content.decode()
    assert sorted(Record.objects.values_list("revenue", "cost_other")) == [(0, 1000), (4000, 0), (6000, 0)]
    detail = owner_client.get(f"/panel/ksiegowosc/wyciag/{ops[0].pk}/").content.decode()
    assert "Zaksięgowana" in detail and "Lp. " in detail
    owner_client.post("/panel/ksiegowosc/wyciag/import/", {"plik": SimpleUploadedFile("x.csv", b"a;b")})
    assert BankOperation.objects.count() == 3


def test_negative_amounts_format_correctly():
    """Regresja: divmod liczby ujemnej zaokrąglał w dół — −12,50 zł wyświetlało się jako „−13,50 zł”."""
    from apps.core.templatetags.satok import pln

    assert pln(-1250) == "-12,50\xa0zł" and pln(-50) == "-0,50\xa0zł" and pln(-100000) == "-1\xa0000\xa0zł"
    assert pln(1250) == "12,50\xa0zł" and pln(0) == "0\xa0zł"


# --- łączenie wydatków z fakturami KSeF: przedpłaty, faktury późniejsze niż wyciąg, podwójny koszt ---------------


def _invoice(owner, number, gross, issue):
    xml = invoice_xml(number=number, gross=gross).replace(b"<P_1>2026-02-10</P_1>", f"<P_1>{issue.isoformat()}</P_1>".encode())
    return ksef.import_files([SimpleUploadedFile(f"{number.replace('/', '_')}.xml", xml)], by=owner, own_nip="1111111111")[0][0]


def test_prepayment_before_invoice_date_is_matched(owner):
    inv = _invoice(owner, "129/09/2026", "99.96", date(2026, 9, 16))
    bank.import_xlsx(xlsx([op_row(date(2026, 9, 12), -99.96, who="47000000000000000000000002\nDRUKARNIA TESTOWA",
                                  title="Zamówienie nr 303817")]), by=owner)
    op = BankOperation.objects.get()
    assert op.status == BankOperation.Status.INVOICE and op.invoice == inv  # zapłata 4 dni przed wystawieniem faktury
    # przedpłata dawniej niż 30 dni przed fakturą — bez dopasowania
    _invoice(owner, "FV/9", "12.34", date(2026, 9, 30))
    bank.import_xlsx(xlsx([op_row(date(2026, 8, 1), -12.34, balance=500)]), by=owner)
    assert BankOperation.objects.get(amount=-1234).status == BankOperation.Status.NEW


def test_invoice_arriving_after_statement_links_pending_payment(owner):
    bank.import_xlsx(xlsx([op_row(date(2026, 10, 7), -3300.0, who="42000000000000000000000003\nSTUDIO TESTOWE", title="FV 3/10/2026")]), by=owner)
    op = BankOperation.objects.get()
    assert op.status == BankOperation.Status.NEW
    inv = _invoice(owner, "3/10/2026", "3300.00", date(2026, 10, 7))  # faktura przyszła później
    op.refresh_from_db()
    assert op.status == BankOperation.Status.INVOICE and op.invoice == inv


def test_booked_bank_cost_blocks_double_invoice_cost(owner, owner_client):
    bank.import_xlsx(xlsx([op_row(date(2026, 9, 12), -99.96, title="Zamówienie")]), by=owner)
    op = BankOperation.objects.get()
    page = owner_client.get(f"/panel/ksiegowosc/wyciag/{op.pk}/").content.decode()
    assert "pasująca faktura KSeF" not in page
    rec = bank.book(op, by=owner, description="Druk", column="cost_deductible")
    inv = _invoice(owner, "129/09/2026", "99.96", date(2026, 9, 16))
    assert BankOperation.objects.get(pk=op.pk).status == BankOperation.Status.BOOKED  # zaksięgowanej nie zmienia
    page = owner_client.get(f"/panel/ksiegowosc/ksef/{inv.pk}/").content.decode()
    assert "możliwy podwójny koszt" in page and f"Lp. {rec.lp}" in page
    with pytest.raises(ValidationError, match="podwoiłoby koszt"):
        ksef.accept(inv, by=owner, day=inv.issue_date, column="cost_deductible", description="Druk")
    records, errors = ksef.accept_many([inv], by=owner, description="Druk")
    assert records == [] and "podwoiłoby koszt" in errors[0]
    # poprawka: usunięcie zapisu z wyciągu → operacja do decyzji → podpowiedź faktury → połączenie
    services.remove_record(rec, by=owner, reason="koszt z faktury")
    listing = owner_client.get("/panel/ksiegowosc/wyciag/").content.decode()
    assert "pasuje do faktury 129/09/2026" in listing
    r = owner_client.post("/panel/ksiegowosc/wyciag/zbiorczo/", {"operacja": [op.pk], "description": "Druk", "cost_column": "cost_deductible",
                                                                 "activity": "pozostala"}, follow=True)
    assert "połącz ją z fakturą" in r.content.decode() and BankOperation.objects.get(pk=op.pk).status == BankOperation.Status.NEW
    page = owner_client.get(f"/panel/ksiegowosc/wyciag/{op.pk}/").content.decode()
    assert "pasująca faktura KSeF" in page and "129/09/2026" in page
    owner_client.post(f"/panel/ksiegowosc/wyciag/{op.pk}/faktura/", {"invoice": inv.pk})
    assert BankOperation.objects.get(pk=op.pk).invoice == inv
    ksef.accept(inv, by=owner, day=inv.issue_date, column="cost_deductible", description="Druk")  # teraz bez podwójnego kosztu
