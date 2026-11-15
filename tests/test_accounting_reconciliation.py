"""Faktury zapłacone prywatnie (zwrot przelewem z rachunku), sposób zapłaty faktur i uzgodnienie salda rachunku
z ewidencją. Dane w testach są zmyślone."""

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounting import bank, ksef, reconciliation, stripe_import
from apps.accounting.models import (PAYMENT_OFFSET, PAYMENT_PRIVATE, REIMBURSE_CASH, REIMBURSE_PENDING, REIMBURSE_TRANSFER,
                                    BankOperation, KsefInvoice)

from .test_accounting_bank import _invoice, op_row, xlsx
from .test_accounting_ksef_docs import invoice_xml
from .test_accounting_stripe import bal, payouts

pytestmark = pytest.mark.django_db
END = date(2026, 12, 31)


def _accept(owner, inv):
    ksef.accept(inv, by=owner, day=inv.issue_date, column="cost_deductible", description="Koszt testowy")
    inv.refresh_from_db()
    return inv


def _private(owner, inv, who="Osoba Testowa"):
    bank.set_payment(inv, by=owner, method=PAYMENT_PRIVATE, paid_by=who)
    inv.refresh_from_db()
    return inv


def _commission(owner, number, gross, issue):
    xml = invoice_xml(number=number, gross=gross, seller_nip="5555555555").replace(b"<P_1>2026-02-10</P_1>",
                                                                                   f"<P_1>{issue.isoformat()}</P_1>".encode())
    return ksef.import_files([SimpleUploadedFile(f"{number.replace('/', '_')}.xml", xml)], by=owner, own_nip="1111111111")[0][0]


def test_private_invoices_reimbursed_by_one_transfer(owner, owner_client):
    b = _accept(owner, _private(owner, _invoice(owner, "B/1", "10.00", date(2026, 6, 20))))
    c = _accept(owner, _private(owner, _invoice(owner, "C/1", "20.00", date(2026, 6, 25))))
    bank.import_xlsx(xlsx([op_row(date(2026, 7, 10), -30.0, title="zwrot za zakupy", balance=70)]), by=owner)
    op = BankOperation.objects.get()
    assert op.status == BankOperation.Status.NEW  # faktury prywatne nie są „zapłatą z rachunku”
    assert bank.reimbursement_match(op) == [b, c]
    assert "możliwy zwrot za faktury zapłacone prywatnie" in owner_client.get("/panel/ksiegowosc/wyciag/").content.decode()
    # księgowanie zbiorcze nie tworzy drugiego kosztu
    r = owner_client.post("/panel/ksiegowosc/wyciag/zbiorczo/", {"operacja": [op.pk], "description": "Zwrot", "cost_column": "cost_deductible",
                                                                 "activity": "pozostala"}, follow=True)
    assert "połącz ją ze zwrotem" in r.content.decode() and BankOperation.objects.get(pk=op.pk).status == BankOperation.Status.NEW
    page = owner_client.get(f"/panel/ksiegowosc/wyciag/{op.pk}/").content.decode()
    assert "możliwy zwrot" in page and "B/1" in page and "C/1" in page
    # suma musi się zgadzać
    with pytest.raises(ValidationError, match="różni się od kwoty przelewu"):
        bank.link_reimbursement(op, [b], by=owner)
    owner_client.post(f"/panel/ksiegowosc/wyciag/{op.pk}/zwrot/", {"invoices": [b.pk, c.pk]})
    op.refresh_from_db()
    b.refresh_from_db()
    assert op.status == BankOperation.Status.REIMBURSEMENT and b.reimbursement == REIMBURSE_TRANSFER and b.reimbursement_op == op
    assert b.reimbursed_on == date(2026, 7, 10)
    # stanu „zwrot przelewem” nie da się zmienić na karcie faktury, dopóki jest połączony przelew
    with pytest.raises(ValidationError, match="przywróć tamtą operację"):
        bank.set_payment(b, by=owner, method=PAYMENT_PRIVATE, paid_by="Osoba Testowa", reimbursement=REIMBURSE_CASH)
    # cofnięcie decyzji przywraca faktury do zwrotu
    owner_client.post(f"/panel/ksiegowosc/wyciag/{op.pk}/przywroc/")
    b.refresh_from_db()
    assert b.reimbursement == REIMBURSE_PENDING and b.reimbursement_op is None
    assert BankOperation.objects.get(pk=op.pk).status == BankOperation.Status.NEW


def test_payment_method_rules_and_same_seller(owner, owner_client):
    inv = _invoice(owner, "FV/1", "45.00", date(2026, 7, 1))
    r = owner_client.post(f"/panel/ksiegowosc/ksef/{inv.pk}/zaplata/", {"method": PAYMENT_PRIVATE}, follow=True)
    assert "kto zapłacił" in r.content.decode()
    owner_client.post(f"/panel/ksiegowosc/ksef/{inv.pk}/zaplata/", {"method": PAYMENT_PRIVATE, "paid_by": "Osoba Testowa",
                                                                      "reimbursement": REIMBURSE_CASH, "reimbursed_on": "2026-07-05"})
    inv.refresh_from_db()
    assert inv.paid_by == "Osoba Testowa" and inv.reimbursement == REIMBURSE_CASH and inv.reimbursed_on == date(2026, 7, 5)
    assert "zwrócono gotówką" in owner_client.get(f"/panel/ksiegowosc/ksef/{inv.pk}/").content.decode()
    # powrót do „przelew z rachunku” czyści dane osoby
    bank.set_payment(inv, by=owner, method="")
    inv.refresh_from_db()
    assert inv.payment_method == "" and inv.paid_by == "" and inv.reimbursement == ""
    # faktura zapłacona z wyciągu nie może być jednocześnie „prywatna”
    bank.import_xlsx(xlsx([op_row(date(2026, 7, 2), -45.0, title="FV/1")]), by=owner)
    assert BankOperation.objects.get().invoice == inv
    with pytest.raises(ValidationError, match="połączona z zapłatą z wyciągu"):
        bank.set_payment(inv, by=owner, method=PAYMENT_PRIVATE, paid_by="X")
    # prowizje: ten sam sposób dla wszystkich faktur sprzedawcy
    c1 = _commission(owner, "GT/001", "2.40", date(2026, 7, 20))
    c2 = _commission(owner, "GT/002", "1.17", date(2026, 8, 1))
    r = owner_client.post(f"/panel/ksiegowosc/ksef/{c1.pk}/zaplata/", {"method": PAYMENT_OFFSET, "same_seller": "on"}, follow=True)
    assert "innych faktur sprzedawcy: 1" in r.content.decode()
    c2.refresh_from_db()
    assert c2.payment_method == PAYMENT_OFFSET and c2.reimbursement == ""
    page = owner_client.get(f"/panel/ksiegowosc/ksef/{c2.pk}/").content.decode()
    assert '<option value="" selected>— nie dotyczy' in page  # przy potrąceniu Stripe pole zwrotu nie podpowiada „Do zwrotu”
    # „Do zwrotu” przesłane razem z potrąceniem jest ignorowane
    bank.set_payment(c2, by=owner, method=PAYMENT_OFFSET, reimbursement=REIMBURSE_PENDING)
    c2.refresh_from_db()
    assert c2.reimbursement == ""
    # faktura potrącana przez Stripe nie jest dopasowywana do wydatku z rachunku o tej samej kwocie
    bank.import_xlsx(xlsx([op_row(date(2026, 8, 3), -1.17, title="opłata", balance=1)]), by=owner)
    assert BankOperation.objects.get(amount=-117).status == BankOperation.Status.NEW


def test_reconciliation_explains_bank_balance_to_the_grosz(owner, owner_client):
    # faktura zapłacona z rachunku, dwie prywatne (zwrócone jednym przelewem), jedna prywatna do zwrotu, prowizja Stripe
    a = _accept(owner, _invoice(owner, "A/1", "45.00", date(2026, 7, 1)))
    _accept(owner, _private(owner, _invoice(owner, "B/1", "10.00", date(2026, 6, 20))))
    _accept(owner, _private(owner, _invoice(owner, "C/1", "20.00", date(2026, 6, 25))))
    e = _accept(owner, _private(owner, _invoice(owner, "E/1", "7.00", date(2026, 7, 12))))
    d = _commission(owner, "GT/001", "3.00", date(2026, 7, 1))
    bank.set_payment(d, by=owner, method=PAYMENT_OFFSET)
    _accept(owner, d)
    # Stripe: 122 zł płatności (prowizje 5 zł), wypłata 100 zł; reszta (17 zł) w saldzie Stripe
    stripe_import.import_files([bal([
        ("txn_1", "payment", "110.00", "4.00", "106.00", "2026-06-29 10:00", ""),
        ("txn_2", "payment", "12.00", "1.00", "11.00", "2026-06-30 10:00", ""),
        ("txn_p", "payout", "-100.00", "0.00", "-100.00", "2026-06-29 20:00", "STRIPE PAYOUT")]),
        payouts([("po_0001", "100.00", "2026-06-29 20:00", "2026-06-30 00:00", "paid")])], by=owner)
    stripe_import.post(by=owner)
    bank.import_xlsx(xlsx([
        op_row(date(2026, 6, 30), 100.0, who="11110000000000000000000000\nSTRIPE", title="STRIPE PAYOUT", balance=100),
        op_row(date(2026, 7, 1), -45.0, title="A/1", balance=55),
        op_row(date(2026, 7, 10), -30.0, title="zwrot", balance=25),
        op_row(date(2026, 7, 15), 20.0, title="darowizna", balance=45),
        op_row(date(2026, 7, 20), -5.0, title="coś", balance=40)]), by=owner)
    ops = {o.amount: o for o in BankOperation.objects.all()}
    assert ops[10000].status == BankOperation.Status.STRIPE and ops[-4500].invoice == a
    bank.link_reimbursement(ops[-3000], bank.reimbursement_match(ops[-3000]), by=owner)
    bank.book(ops[2000], by=owner, description="Darowizna", column="revenue")

    r = reconciliation.reconcile(2026, END)
    assert r["opening"] == 0 and r["closing"] == 4000 and r["closing_statement"] == 4000 and not r["warnings"]
    assert r["result"] == 12200 + 2000 - (4500 + 1000 + 2000 + 700 + 300)  # 57 zł
    groups = {g.key: g for g in r["groups"]}
    assert groups["stripe"].total == -1900
    assert dict(groups["stripe"].lines) == {"Środki w saldzie Stripe albo w drodze na rachunek": -1700,
                                            "Prowizje potrącone przez Stripe, jeszcze bez faktury / zapisu w ewidencji": -200,
                                            "Płatności online jeszcze nieujęte w ewidencji": 0}
    assert groups["private"].total == 700 and dict(groups["private"].lines) == {"Do zwrotu — pieniądze jeszcze na rachunku": 700}
    assert groups["pending"].total == -500
    assert r["unexplained"] == 0 and r["opening"] + r["result"] + sum(g.total for g in r["groups"]) == r["closing"]
    assert {g.key for g in r["zero_groups"]} == {"matched"}
    page = owner_client.get("/panel/ksiegowosc/uzgodnienie/?rok=2026&do=2026-12-31").content.decode()
    assert "Uzgodnienie salda rachunku z ewidencją" in page and "Koszty zapłacone prywatnie" in page and "E/1" in page
    assert "Różnica niewyjaśniona" not in page
    # zwrot gotówką przenosi kwotę z „do zwrotu” do „zwrócone gotówką” — suma bez zmian
    bank.set_payment(e, by=owner, method=PAYMENT_PRIVATE, paid_by="Osoba Testowa", reimbursement=REIMBURSE_CASH)
    groups = {g.key: g for g in reconciliation.reconcile(2026, END)["groups"]}
    assert dict(groups["private"].lines) == {"Zwrócone gotówką": 700}
    # okres do 5.07: bez późniejszych operacji i zapisów — nadal zgodne co do grosza
    early = reconciliation.reconcile(2026, date(2026, 7, 5))
    assert early["closing"] == 5500 and early["unexplained"] == 0


def test_reconciliation_page_without_statement(owner_client):
    page = owner_client.get("/panel/ksiegowosc/uzgodnienie/").content.decode()
    assert "Brak operacji z wyciągu" in page
    assert KsefInvoice.objects.count() == 0


def test_new_commission_invoice_inherits_stripe_offset(owner):
    first = _commission(owner, "GT/001", "2.40", date(2026, 7, 20))
    assert first.payment_method == ""  # pierwsza faktura sprzedawcy — bez zgadywania
    bank.set_payment(first, by=owner, method=PAYMENT_OFFSET)
    nxt = _commission(owner, "GT/002", "1.17", date(2026, 8, 1))
    assert nxt.payment_method == PAYMENT_OFFSET and "automatycznie" in nxt.payment_note
    other = _invoice(owner, "FV/9", "45.00", date(2026, 8, 2))  # inny sprzedawca — bez zmian
    assert other.payment_method == ""
    # gdy ostatnią fakturę sprzedawcy opłacono przelewem z rachunku — kolejna nie jest oznaczana
    third = _commission(owner, "GT/003", "9.99", date(2026, 9, 1))
    bank.set_payment(third, by=owner, method="")
    bank.import_xlsx(xlsx([op_row(date(2026, 9, 2), -9.99, title="GT/003")]), by=owner)
    assert BankOperation.objects.get().invoice == third
    assert _commission(owner, "GT/004", "3.33", date(2026, 10, 1)).payment_method == ""


# --- lista kontrolna miesiąca ---------------------------------------------------------------


def _states(year, month, today=date(2026, 11, 15)):
    from apps.accounting import checklist

    return {s.title: s for s in checklist.month_steps(year, month, today)}


def test_month_checklist_walks_through_routine(owner, owner_client):
    s = _states(2026, 7)
    assert s["Płatności online (Stripe) wgrane i ujęte"].state == "warn"  # brak eksportu
    assert s["Wyciąg bankowy za cały miesiąc"].state == "warn"
    # Stripe za lipiec: płatność z prowizją 4 zł, wypłata 96 zł
    stripe_import.import_files([bal([
        ("txn_1", "payment", "100.00", "4.00", "96.00", "2026-07-02 10:00", ""),
        ("txn_x", "payment", "10.00", "0.40", "9.60", "2026-11-10 10:00", ""),  # późniejszy eksport — obejmuje lipiec
        ("txn_p", "payout", "-96.00", "0.00", "-96.00", "2026-07-03 20:00", "STRIPE PAYOUT")]),
        payouts([("po_0001", "96.00", "2026-07-03 20:00", "2026-07-04 00:00", "paid")])], by=owner)
    s = _states(2026, 7)
    assert s["Płatności online (Stripe) wgrane i ujęte"].state == "todo" and "nieujęte" in s["Płatności online (Stripe) wgrane i ujęte"].detail
    assert s["Wyciąg bankowy za cały miesiąc"].state == "warn"
    stripe_import.post(by=owner)
    bank.import_xlsx(xlsx([op_row(date(2026, 7, 4), 96.0, who="11110000000000000000000000\nSTRIPE", title="STRIPE", balance=96),
                           op_row(date(2026, 7, 20), -5.0, title="opłata", balance=91),
                           op_row(date(2026, 8, 2), -1.0, title="opłata", balance=90)]), by=owner)
    s = _states(2026, 7)
    assert s["Płatności online (Stripe) wgrane i ujęte"].state == "ok"
    assert s["Wyciąg bankowy za cały miesiąc"].state == "ok"
    assert s["Operacje z wyciągu rozstrzygnięte"].state == "todo"
    assert s["Faktura prowizyjna za płatności online"].state == "warn"  # 15.11 — faktury za lipiec już dawno powinny być
    bank.skip(BankOperation.objects.get(amount=-500), by=owner, reason="test")
    # faktura prowizyjna (sierpniowa data) z inną kwotą → sprawdź; po przyjęciu właściwej → gotowe
    wrong = _commission(owner, "GT/002", "3.50", date(2026, 8, 1))
    bank.set_payment(wrong, by=owner, method=PAYMENT_OFFSET)
    assert _states(2026, 7)["Faktura prowizyjna za płatności online"].state == "todo"  # czeka na przyjęcie
    _accept(owner, wrong)
    step = _states(2026, 7)["Faktura prowizyjna za płatności online"]
    assert step.state == "warn" and "3,50" in step.detail and "4\xa0zł" in step.detail
    ksef.reject(_commission(owner, "GT/003", "9.99", date(2026, 8, 5)), by=owner, note="test")  # odrzucona — nie liczy się
    s = _states(2026, 7)
    assert s["Operacje z wyciągu rozstrzygnięte"].state == "ok"
    # zwrot do oddania — przypomnienie
    _accept(owner, _private(owner, _invoice(owner, "P/1", "12.00", date(2026, 7, 8))))
    step = _states(2026, 7)["Zwroty za faktury zapłacone prywatnie"]
    assert step.state == "warn" and "12\xa0zł" in step.detail
    # bieżący miesiąc: wyciąg i Stripe „jeszcze nie teraz”, a nie błąd
    assert _states(2026, 11)["Wyciąg bankowy za cały miesiąc"].state in ("later", "todo")
    page = owner_client.get("/panel/ksiegowosc/miesiac/?rok=2026&miesiac=7").content.decode()
    assert "Lista kontrolna — lipiec 2026" in page and "Faktura prowizyjna za płatności online" in page and "następny miesiąc" in page
    assert owner_client.get("/panel/ksiegowosc/miesiac/?rok=abc").status_code == 200
