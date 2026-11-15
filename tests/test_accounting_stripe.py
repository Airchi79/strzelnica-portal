"""Płatności online Stripe w ewidencji: import historii salda i wypłat (CSV), przychód dzienny (wpłaty − zwroty),
prowizje miesięczne, łączenie wypłat z wyciągiem bankowym. Dane zmyślone."""

from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounting import bank, services, stripe_import
from apps.accounting.models import BankOperation, Record, StripePosting, StripeTransaction

from .test_accounting_bank import op_row, xlsx

pytestmark = pytest.mark.django_db
BAL_HEADER = ("id,Type,Source,Amount,Fee,Destination Platform Fee,Destination Platform Fee Currency,Net,Currency,Created (UTC),"
              "Available On (UTC),Description,Customer Facing Amount,Customer Facing Currency,Transfer,Transfer Date (UTC),Transfer Group")
PO_HEADER = ("id,Amount,Created (UTC),Currency,Livemode,Arrival Date (UTC),Source Type,Destination,Status,Type,Method,Description,"
             "Balance Transaction,Failure Balance Transaction,Failure Message,Failure Code,Statement Descriptor,Trace ID,"
             "Trace ID Status,Destination Name,Destination Country,Destination Last 4")


def bal(rows):
    # jak w eksporcie Stripe: dla wypłaty kolumna Source to identyfikator wypłaty (po_…)
    lines = [BAL_HEADER] + [f"{i},{t},{'po_0001' if t == 'payout' else 'src_' + i},{a},{f},,,{n},pln,{c},{c},{d},{a},pln,,,"
                            for i, t, a, f, n, c, d in rows]
    return SimpleUploadedFile("balance_history.csv", ("\n".join(lines) + "\n").encode())


def payouts(rows):
    lines = [PO_HEADER] + [f"{i},{a},{c},pln,true,{arr},card,ba_x,{st},bank_account,standard,STRIPE PAYOUT,txn_{i},,,,,TEST-{i[-4:]},supported,BANK,PL,0001"
                           for i, a, c, arr, st in rows]
    return SimpleUploadedFile("payouts.csv", ("\n".join(lines) + "\n").encode())


BALANCE = [
    # 2026-03-01 (czas polski): dwie płatności; 23:30 UTC 1.03 to już 2.03 w Polsce
    ("txn_a1", "payment", "104.00", "4.16", "99.84", "2026-03-01 10:00", ""),
    ("txn_a2", "payment", "72.80", "2.91", "69.89", "2026-03-01 12:00", ""),
    ("txn_a3", "payment", "62.40", "2.50", "59.90", "2026-03-01 23:30", ""),
    ("txn_r1", "payment_refund", "-62.40", "0.00", "-62.40", "2026-03-05 16:00", "Refund on Guntracker.pl"),
    ("txn_f1", "adjustment", "2.50", "0.00", "2.50", "2026-03-05 16:00", "Fee refund on Guntracker.pl"),
    ("txn_p1", "payout", "-169.73", "0.00", "-169.73", "2026-03-02 00:10", "STRIPE PAYOUT"),
    ("txn_b1", "payment", "36.40", "1.46", "34.94", "2026-04-02 09:00", ""),
]


def test_import_balance_and_payouts_dedup_and_totals(owner):
    r = stripe_import.import_files([bal(BALANCE), payouts([("po_0001", "169.73", "2026-03-02 00:10", "2026-03-03 00:00", "paid"),
                                                            ("po_0002", "10.00", "2026-04-03 00:10", "2026-04-04 00:00", "in_transit")])],
                                   by=owner)
    assert r["transactions"] == 6 and r["payouts"] == 1  # wypłata z historii + uzupełniona z eksportu; „w drodze” pominięta
    assert StripeTransaction.objects.get(stripe_id="txn_a3").day == date(2026, 3, 2)  # strefa czasowa Warszawy
    t = stripe_import.day_totals()
    assert t[date(2026, 3, 1)] == {"gross": 17680, "refunds": 0, "fees": 707, "fee_adjustments": 0, "count": 2, "revenue": 17680, "fee_cost": 707}
    assert t[date(2026, 3, 5)]["revenue"] == -6240 and t[date(2026, 3, 5)]["fee_cost"] == -250
    r = stripe_import.import_files([bal(BALANCE)], by=owner)
    assert r["transactions"] == 0 and r["duplicates"] == 6
    with pytest.raises(ValidationError, match="nierozpoznany"):
        stripe_import.import_files([SimpleUploadedFile("x.csv", b"a,b\n1,2\n")], by=owner)


def test_post_daily_revenue_and_monthly_fees_once(owner):
    stripe_import.import_files([bal(BALANCE)], by=owner)
    # domyślnie bez prowizji — dokumentują je faktury od platformy w KSeF (bez podwójnego kosztu)
    assert stripe_import.post(by=owner, column="cost_other", until=date(2026, 4, 3)) == (4, 0)  # 1.03, 2.03, 5.03 (zwrot), 2.04
    assert not Record.objects.filter(kind="koszt").exists()
    days, months = stripe_import.post(by=owner, column="cost_other", until=date(2026, 4, 3), include_fees=True)
    assert (days, months) == (0, 1)  # prowizje za marzec — tylko na żądanie
    revenue = {p.period_start: p.record for p in StripePosting.objects.filter(kind="przychod")}
    assert revenue[date(2026, 3, 1)].revenue == 17680 and revenue[date(2026, 3, 5)].revenue == -6240
    assert revenue[date(2026, 3, 1)].document == "Stripe 01.03.2026" and revenue[date(2026, 3, 1)].activity == "pozostala"
    fees = StripePosting.objects.get(kind="prowizje").record
    assert fees.cost_other == 707 + 250 - 250 and fees.date == date(2026, 3, 31)  # opłaty 9,57 zł − zwrot opłaty 2,50 zł
    assert stripe_import.post(by=owner, column="cost_other", until=date(2026, 4, 3), include_fees=True) == (0, 0)
    # usunięty zapis dnia można ująć ponownie
    services.remove_record(revenue[date(2026, 3, 1)], by=owner, reason="test")
    assert stripe_import.post(by=owner, column="cost_other", until=date(2026, 4, 3)) == (1, 0)
    with pytest.raises(ValidationError, match="kolumnę"):
        stripe_import.post(by=owner, column="revenue")


def test_bank_inflow_matches_payout_both_import_orders(owner):
    stripe_import.import_files([payouts([("po_0001", "169.73", "2026-03-02 00:10", "2026-03-03 00:00", "paid")])], by=owner)
    bank.import_xlsx(xlsx([op_row(date(2026, 3, 2), 169.73, title="TEST-0001"), op_row(date(2026, 3, 2), 50.0, title="darowizna", balance=1219.73)]),
                     by=owner)
    op = BankOperation.objects.get(amount=16973)
    assert op.status == BankOperation.Status.STRIPE and op.stripe_payout.stripe_id == "po_0001"
    assert BankOperation.objects.get(amount=5000).status == BankOperation.Status.NEW
    bank.reopen(op, by=owner)
    op.refresh_from_db()
    assert op.status == BankOperation.Status.NEW and op.stripe_payout is None
    assert stripe_import.match_bank_operations() == 1  # import Stripe po wyciągu też łączy
    # wypłata o innej kwocie albo poza oknem dat — bez dopasowania
    stripe_import.import_files([payouts([("po_0009", "77.00", "2026-05-01 00:10", "2026-05-02 00:00", "paid")])], by=owner)
    bank.import_xlsx(xlsx([op_row(date(2026, 5, 20), 77.0, balance=2000)]), by=owner)
    assert BankOperation.objects.get(amount=7700).status == BankOperation.Status.NEW


def test_panel_stripe_page_import_post_and_pdf(owner_client):
    r = owner_client.post("/panel/ksiegowosc/stripe/", {"akcja": "import", "pliki": [bal(BALANCE)]}, follow=True)
    assert "nowe operacje 6" in r.content.decode()
    page = owner_client.get("/panel/ksiegowosc/stripe/").content.decode()
    assert "Ujmij w ewidencji" in page and "saldo wg eksportu" in page
    r = owner_client.post("/panel/ksiegowosc/stripe/", {"akcja": "ujmij", "kolumna": "cost_deductible", "forma": "pozostala"}, follow=True)
    assert "Ujęto w ewidencji" in r.content.decode() and Record.objects.filter(category="stripe").count() >= 3
    assert not Record.objects.filter(kind="koszt").exists()  # bez zaznaczenia — prowizji nie księguje
    pdf = owner_client.get("/panel/ksiegowosc/stripe/2026-3.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
