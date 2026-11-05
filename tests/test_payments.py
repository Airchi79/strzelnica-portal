import base64
import datetime as dt
import hashlib
import json
from datetime import timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone

from apps.bookings import services as booking_services
from apps.bookings.models import Reservation
from apps.facilities.models import Lane
from apps.payments import services, tpay
from apps.payments.models import LedgerEntry, Payment

from .conftest import approve, add_credential, grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def wallet_on(db):
    """Testy w tym pliku sprawdzają tryb salda (przedpłaty). Tryb bezpośredni: test_direct_payment_mode_*."""
    from apps.payments.models import PaymentSettings

    cfg = PaymentSettings.load()
    cfg.wallet_enabled = True
    cfg.save()


@pytest.fixture
def ps(db):
    user = make_user("ps@example.com", first_name="Piotr", last_name="Prowadzący")
    add_credential(user)
    approve(user)
    return user


def book(user, lane_name="Oś 100 m", persons=2, start=10, end=12, days=7):
    return booking_services.create_reservation(
        user, Lane.objects.get(name=lane_name), timezone.localdate() + timedelta(days=days), start, end, persons
    )


# --- saldo, opłaty, zwroty ------------------------------------------------------------


def test_booking_unpaid_then_paid_by_deposit(ps, owner):
    r = book(ps)  # 2 × 60 zł × 2 h = 240 zł
    assert r.payment_status == Reservation.Payment.DUE and r.paid_amount == 0
    services.register_manual_payment(ps, 30000, Payment.Method.CASH, owner)
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID and r.paid_amount == 24000
    assert services.balance_of(ps) == 6000
    assert services.ledger_consistent(ps)


def test_booking_paid_immediately_from_balance(ps, owner):
    services.register_manual_payment(ps, 50000, Payment.Method.BLIK_PHONE, owner)
    r = book(ps)
    assert r.payment_status == Reservation.Payment.PAID
    assert services.balance_of(ps) == 26000


def test_oldest_due_paid_first_and_partial_balance_waits(ps, owner):
    r1 = book(ps, days=5)  # 240 zł
    r2 = book(ps, lane_name="Oś 35 m", persons=1, days=3, start=9, end=10)  # 35 zł, wcześniejsza
    services.register_manual_payment(ps, 10000, Payment.Method.CASH, owner)
    r1.refresh_from_db()
    r2.refresh_from_db()
    assert r2.payment_status == Reservation.Payment.PAID and r1.payment_status == Reservation.Payment.DUE
    assert services.balance_of(ps) == 6500  # nie wystarcza na 240 zł — czeka


def test_free_cancellation_refunds_to_balance(ps, owner):
    services.register_manual_payment(ps, 24000, Payment.Method.CASH, owner)
    r = book(ps, days=10)
    assert services.balance_of(ps) == 0
    booking_services.cancel_reservation(r, ps, now=r.starts_at - timedelta(days=3))
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.NONE and r.paid_amount == 0
    assert services.balance_of(ps) == 24000
    assert LedgerEntry.objects.filter(user=ps, kind=LedgerEntry.Kind.REFUND, amount=24000).exists()


def test_late_cancellation_keeps_fee(ps, owner):
    services.register_manual_payment(ps, 24000, Payment.Method.CASH, owner)
    r = book(ps, days=1)
    booking_services.cancel_reservation(r, owner, "późne odwołanie", as_board=True, keep_fee=True, now=r.starts_at - timedelta(hours=10))
    r.refresh_from_db()
    assert r.late_cancellation and r.payment_status == Reservation.Payment.PAID and services.balance_of(ps) == 0


def test_board_cancellation_refunds(ps, owner):
    services.register_manual_payment(ps, 24000, Payment.Method.CASH, owner)
    r = book(ps, days=1)
    booking_services.cancel_reservation(r, owner, "awaria", as_board=True, now=r.starts_at - timedelta(hours=1))
    assert services.balance_of(ps) == 24000


def test_change_persons_refunds_excess(ps, owner):
    services.register_manual_payment(ps, 40000, Payment.Method.CASH, owner)
    r = book(ps, persons=4)  # ryczałt 200 zł × 2 h = 400 zł
    assert services.balance_of(ps) == 0
    booking_services.change_persons(r, 2, owner)  # 240 zł
    r.refresh_from_db()
    assert r.amount == 24000 and r.paid_amount == 24000 and services.balance_of(ps) == 16000


def test_waive_and_unwaive(ps, owner):
    r = book(ps)
    booking_services.set_payment(r, "waive", owner)
    services.register_manual_payment(ps, 50000, Payment.Method.CASH, owner)
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.WAIVED and services.balance_of(ps) == 50000
    booking_services.set_payment(r, "unwaive", owner)
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID and services.balance_of(ps) == 26000


def test_correction_and_negative_balance(ps, owner):
    services.correction(ps, -1500, "opłata za tarcze", owner)
    assert services.balance_of(ps) == -1500
    with pytest.raises(ValidationError):
        services.correction(ps, 100, "  ", owner)
    services.correction(ps, 1500, "wyrównanie", owner)
    assert services.balance_of(ps) == 0 and services.ledger_consistent(ps)


def test_ledger_is_append_only(ps, owner):
    entry = services.correction(ps, 1000, "test", owner)
    with pytest.raises(DatabaseError), transaction.atomic():
        LedgerEntry.objects.filter(pk=entry.pk).update(amount=999999)
    with pytest.raises(DatabaseError), transaction.atomic():
        LedgerEntry.objects.filter(pk=entry.pk).delete()


def test_settlement_raises_due_amount_then_balance_pays(ps, owner):
    from apps.logbook import services as logbook_services

    r = book(ps, lane_name="Oś 25 m nr 1", persons=1)  # 35 × 2 = 70 zł, saldo puste → do zapłaty
    sig = _signature()
    for i in range(3):
        logbook_services.create_entry(
            data={"first_name": "A", "last_name": f"B{i}", "has_permit": True, "permit_number": "1", "permit_issuer": "KWP", "purpose": "strzelanie"},
            signature_png=sig, source="qr_rezerwacji", reservation=r, now=r.starts_at,
        )
    logbook_services.settle_finished_reservations(now=r.ends_at + timedelta(minutes=1))
    r.refresh_from_db()
    assert r.amount == 20000 and r.payment_status == Reservation.Payment.DUE  # 3 osoby → ryczałt 100 zł/h
    services.register_manual_payment(ps, 20000, Payment.Method.CASH, owner)
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID and services.balance_of(ps) == 0


def _signature():
    from io import BytesIO

    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (400, 150), (0, 0, 0, 0))
    ImageDraw.Draw(img).line([(10, 100), (100, 20), (200, 120), (380, 30)], fill=(0, 0, 0, 255), width=5)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --- kody tytułów i import wyciągu ---------------------------------------------------


def test_topup_code_check_letter(ps):
    code = services.topup_code(ps)
    assert services.match_title(f"Doładowanie {code.lower()}") == (ps, None)
    wrong_letter = code[:-1] + ("A" if code[-1] != "A" else "B")
    assert services.match_title(wrong_letter) == (None, None)
    other = make_user("x@example.com")
    assert services.match_title(services.topup_code(other)) == (other, None)


def test_reservation_number_in_title_variants(ps):
    r = book(ps)
    for title in [r.number, r.number.lower(), r.number.replace("-", " "), f"opłata za {r.number.replace('-', '')} dzięki"]:
        user, res = services.match_title(title)
        assert res == r and user == ps, title


PKO_CSV = """"Data operacji","Data waluty","Typ transakcji","Kwota","Waluta","Saldo po transakcji","Opis transakcji","",""
"{d}","{d}","Przelew na konto","+240.00","PLN","1000.00","Tytuł: {title}","Nazwa nadawcy: Piotr Prowadzący",""
"{d}","{d}","Płatność kartą","-50.00","PLN","950.00","Tytuł: zakupy","",""
"{d}","{d}","Przelew na konto","+100.00","PLN","1050.00","Tytuł: darowizna na strzelnicę","Nazwa nadawcy: Ktoś",""
"""

MBANK_CSV = """mBank S.A. Bankowość Detaliczna;
Elektroniczne zestawienie operacji;

#Data operacji;#Opis operacji;#Rachunek;#Kategoria;#Kwota;
{d};PRZELEW PRZYCHODZĄCY {title} PIOTR PROWADZĄCY;eKonto;Wpływy;1 000,50 PLN;
{d};ZAKUP PRZY UŻYCIU KARTY;eKonto;Zakupy;-20,00 PLN;
"""


def test_import_pko_like_csv_matches_and_skips_debits(ps, owner):
    r = book(ps)
    raw = PKO_CSV.format(d=timezone.localdate().isoformat(), title=r.number).encode("utf-8")
    batch = services.import_bank_csv(raw, "pko.csv", owner)
    assert (batch.rows_total, batch.rows_new, batch.rows_matched) == (2, 2, 1)
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID
    unmatched = Payment.objects.get(status=Payment.Status.UNMATCHED)
    assert unmatched.amount == 10000 and "darowizna" in unmatched.title
    again = services.import_bank_csv(raw, "pko.csv", owner)  # ponowny import nie dubluje
    assert again.rows_new == 0 and Payment.objects.count() == 2


def test_import_mbank_like_cp1250_with_preamble(ps, owner):
    code = services.topup_code(ps)
    raw = MBANK_CSV.format(d=timezone.localdate().strftime("%d-%m-%Y"), title=code).encode("cp1250")
    batch = services.import_bank_csv(raw, "mbank.csv", owner)
    assert batch.rows_matched == 1
    assert services.balance_of(ps) == 100050


def test_import_rejects_unknown_format(owner):
    with pytest.raises(ValidationError):
        services.import_bank_csv(b"a;b;c\n1;2;3\n", "zly.csv", owner)


def test_assign_and_ignore_unmatched(ps, owner):
    raw = PKO_CSV.format(d=timezone.localdate().isoformat(), title="bez kodu").encode()
    services.import_bank_csv(raw, "x.csv", owner)
    p1, p2 = Payment.objects.filter(status=Payment.Status.UNMATCHED).order_by("amount")
    services.assign_unmatched(p2, ps, owner)
    assert services.balance_of(ps) == 24000
    services.ignore_unmatched(p1, "darowizna", owner)
    p1.refresh_from_db()
    assert p1.status == Payment.Status.IGNORED
    with pytest.raises(ValidationError):
        services.assign_unmatched(p1, ps, owner)


# --- Tpay: podpis JWS i md5 -------------------------------------------------------------


def _cert(subject, issuer_key, public_key, issuer=None, ca=False):
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer or name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )


@pytest.fixture
def tpay_keys(settings, monkeypatch):
    settings.TPAY_CLIENT_ID, settings.TPAY_CLIENT_SECRET, settings.TPAY_SECURITY_CODE = "cid", "secret", "kod-bezp"
    settings.TPAY_ENABLED, settings.TPAY_SANDBOX = True, True
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root = _cert("Tpay Root TEST", root_key, root_key.public_key(), ca=True)
    sign_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = _cert("Tpay JWS TEST", root_key, sign_key.public_key(), issuer=root.subject)
    certs = {
        "https://secure.sandbox.tpay.com/x509/notifications-jws.pem": signer,
        "https://secure.sandbox.tpay.com/x509/tpay-jws-root.pem": root,
    }
    monkeypatch.setattr(tpay, "_fetch_cert", lambda url: certs[url])
    original = tpay.verify_jws

    def verify(header, body, cert_loader=None):
        return original(header, body, cert_loader=lambda url: certs[url])

    monkeypatch.setattr(tpay, "verify_jws", verify)
    return sign_key


def _b64u(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _jws(key, body, x5u="https://secure.sandbox.tpay.com/x509/notifications-jws.pem"):
    header = _b64u(json.dumps({"alg": "RS256", "x5u": x5u}).encode())
    sig = key.sign(f"{header}.{_b64u(body)}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}..{_b64u(sig)}"


def _notification(payment, status="TRUE", amount=None, code="kod-bezp"):
    amount = amount or f"{payment.amount / 100:.2f}"
    fields = {"id": "1010", "tr_id": "TR-TEST-1", "tr_date": "2026-11-01 10:00:00", "tr_crc": str(payment.pk), "tr_amount": amount,
              "tr_paid": amount, "tr_desc": "x", "tr_status": status, "tr_error": "none", "tr_email": "a@b.pl", "test_mode": "1"}
    fields["md5sum"] = hashlib.md5((fields["id"] + fields["tr_id"] + amount + fields["tr_crc"] + code).encode()).hexdigest()
    from urllib.parse import urlencode

    return urlencode(fields).encode()


def _post(client, key, body, signature=None):
    return client.generic(
        "POST", "/platnosci/tpay/powiadomienie/", body, content_type="application/x-www-form-urlencoded",
        HTTP_X_JWS_SIGNATURE=signature if signature is not None else _jws(key, body),
    )


def test_tpay_notification_completes_payment_idempotently(client, ps, tpay_keys):
    r = book(ps)
    payment = Payment.objects.create(user=ps, amount=24000, method=Payment.Method.TPAY, reservation=r)
    body = _notification(payment)
    response = _post(client, tpay_keys, body)
    assert response.status_code == 200 and response.content == b"TRUE"
    payment.refresh_from_db()
    r.refresh_from_db()
    assert payment.status == Payment.Status.COMPLETED and r.payment_status == Reservation.Payment.PAID
    assert _post(client, tpay_keys, body).content == b"TRUE"  # powtórka nie księguje drugi raz
    assert LedgerEntry.objects.filter(payment=payment, kind=LedgerEntry.Kind.DEPOSIT).count() == 1


def test_tpay_rejects_bad_signature_md5_and_foreign_x5u(client, ps, tpay_keys):
    payment = Payment.objects.create(user=ps, amount=5000, method=Payment.Method.TPAY)
    body = _notification(payment)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert _post(client, tpay_keys, body, signature=_jws(other, body)).status_code == 400
    assert _post(client, tpay_keys, body, signature="").status_code == 400
    assert _post(client, tpay_keys, body, signature=_jws(tpay_keys, body, x5u="https://evil.example/cert.pem")).status_code == 400
    bad_md5 = _notification(payment, code="zly-kod")
    assert _post(client, tpay_keys, bad_md5).status_code == 400
    tampered = body.replace(b"tr_amount=50.00", b"tr_amount=5000.00")
    assert _post(client, tpay_keys, tampered, signature=_jws(tpay_keys, body)).status_code == 400
    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING and services.balance_of(ps) == 0


def test_tpay_amount_mismatch_goes_to_review(client, ps, tpay_keys):
    payment = Payment.objects.create(user=ps, amount=5000, method=Payment.Method.TPAY)
    _post(client, tpay_keys, _notification(payment, amount="40.00"))
    payment.refresh_from_db()
    assert payment.status == Payment.Status.UNMATCHED and services.balance_of(ps) == 0


def test_tpay_chargeback_debits_balance_once(client, ps, tpay_keys):
    payment = Payment.objects.create(user=ps, amount=5000, method=Payment.Method.TPAY)
    _post(client, tpay_keys, _notification(payment))
    assert services.balance_of(ps) == 5000
    _post(client, tpay_keys, _notification(payment, status="CHARGEBACK"))
    _post(client, tpay_keys, _notification(payment, status="CHARGEBACK"))
    assert services.balance_of(ps) == 0


def test_tpay_start_redirects_to_gateway(client, ps, tpay_keys, monkeypatch):
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return {"transaction_id": "01TEST", "title": "TR-1", "payment_url": "https://secure.sandbox.tpay.com/?title=TR-1"}

    monkeypatch.setattr(tpay, "create_transaction", fake_create)
    client.force_login(ps)
    response = client.post("/platnosci/tpay/start/", {"amount": "50"})
    assert response.status_code == 302 and response.url.startswith("https://secure.sandbox.tpay.com/")
    payment = Payment.objects.get()
    assert payment.amount == 5000 and payment.external_id == "tpay:01TEST" and captured["hidden_description"] == str(payment.pk)
    assert captured["notification_url"].endswith("/platnosci/tpay/powiadomienie/")
    assert client.post("/platnosci/tpay/start/", {"amount": "1"}).status_code == 302
    assert Payment.objects.count() == 1  # kwota poniżej minimum odrzucona


def test_tpay_disabled_hides_and_refuses(client, ps, settings):
    settings.TPAY_ENABLED = False
    client.force_login(ps)
    assert "Zapłać online" not in client.get("/platnosci/").content.decode()
    client.post("/platnosci/tpay/start/", {"amount": "50"})
    assert not Payment.objects.exists()
    assert client.post("/platnosci/tpay/powiadomienie/", {}).status_code == 404


def test_tpay_gateway_error_marks_failed(client, ps, tpay_keys, monkeypatch):
    def boom(**kw):
        raise tpay.TpayError("x")

    monkeypatch.setattr(tpay, "create_transaction", boom)
    client.force_login(ps)
    client.post("/platnosci/tpay/start/", {"amount": "50"})
    assert Payment.objects.get().status == Payment.Status.FAILED


# --- widoki -------------------------------------------------------------------------


def test_wallet_page(client, ps):
    book(ps)
    client.force_login(ps)
    body = client.get("/platnosci/").content.decode()
    assert services.topup_code(ps) in body and "240 zł" in body


def test_panel_payments_permissions_and_flows(owner_client, ps, owner):
    for url in ["/panel/platnosci/", "/panel/platnosci/wplata/", "/panel/platnosci/import/", "/panel/salda/", f"/panel/salda/{ps.pk}/"]:
        assert owner_client.get(url).status_code == 200, url
    response = owner_client.post("/panel/platnosci/wplata/", {"user": ps.pk, "amount": "120,50", "method": "gotowka", "note": ""})
    assert response.status_code == 302 and services.balance_of(ps) == 12050
    owner_client.post(f"/panel/salda/{ps.pk}/", {"direction": "minus", "amount": "20,50", "reason": "tarcze"})
    assert services.balance_of(ps) == 10000

    from django.core.files.uploadedfile import SimpleUploadedFile

    raw = PKO_CSV.format(d=timezone.localdate().isoformat(), title=services.topup_code(ps)).encode()
    owner_client.post("/panel/platnosci/import/", {"file": SimpleUploadedFile("wyciag.csv", raw, "text/csv")})
    assert services.balance_of(ps) == 34000

    from django.test import Client

    client = Client()
    staff = grant(make_user("z@example.com"), "credentials.review_credential")
    login_with_otp(client, staff)
    assert client.get("/panel/platnosci/").status_code == 403
    assert client.post("/panel/platnosci/wplata/", {"user": ps.pk, "amount": "1000", "method": "gotowka"}).status_code == 403


# --- tryb domyślny: płatność bezpośrednia (saldo wyłączone) ----------------------------------


def _wallet_off():
    from apps.payments.models import PaymentSettings

    cfg = PaymentSettings.load()
    cfg.wallet_enabled = False
    cfg.save()


def test_direct_mode_payment_pays_only_its_reservation(ps, owner):
    _wallet_off()
    r1 = book(ps, days=5)
    r2 = book(ps, lane_name="Oś 35 m", persons=1, days=3, start=9, end=10)
    services.register_manual_payment(ps, 30000, Payment.Method.CASH, owner, reservation=r1)
    r1.refresh_from_db()
    r2.refresh_from_db()
    assert r1.payment_status == Reservation.Payment.PAID
    assert r2.payment_status == Reservation.Payment.DUE  # nadpłata nie opłaca automatycznie innej rezerwacji
    assert services.balance_of(ps) == 6000  # nadpłata widoczna dla zarządu


def test_direct_mode_no_auto_charge_at_booking(ps, owner):
    _wallet_off()
    services.correction(ps, 50000, "nadpłata z poprzedniego okresu", owner)
    r = book(ps)
    assert r.payment_status == Reservation.Payment.DUE and services.balance_of(ps) == 50000


def test_direct_mode_topup_refused(client, ps, tpay_keys):
    _wallet_off()
    client.force_login(ps)
    client.post("/platnosci/tpay/start/", {"amount": "50"})
    assert not Payment.objects.exists()
    assert "Doładuj saldo" not in client.get("/platnosci/").content.decode()


def test_direct_mode_bank_transfer_with_reservation_number(ps, owner):
    _wallet_off()
    r = book(ps)
    raw = PKO_CSV.format(d=timezone.localdate().isoformat(), title=r.number).encode()
    services.import_bank_csv(raw, "pko.csv", owner)
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID


def test_tpay_notification_with_foreign_transaction_id_goes_to_review(client, ps, tpay_keys):
    payment = Payment.objects.create(user=ps, amount=5000, method=Payment.Method.TPAY, raw={"tpay_title": "TR-INNY-9"})
    assert _post(client, tpay_keys, _notification(payment)).content == b"TRUE"
    payment.refresh_from_db()
    assert payment.status == Payment.Status.UNMATCHED and "Numer transakcji" in payment.note and services.balance_of(ps) == 0
    ok = Payment.objects.create(user=ps, amount=5000, method=Payment.Method.TPAY, raw={"tpay_title": "TR-TEST-1"})
    _post(client, tpay_keys, _notification(ok))
    ok.refresh_from_db()
    assert ok.status == Payment.Status.COMPLETED


def test_tpay_test_notification_not_booked_in_production(client, ps, tpay_keys, settings, monkeypatch):
    settings.TPAY_SANDBOX = False
    monkeypatch.setattr(tpay, "verify_jws", lambda header, body, cert_loader=None: True)  # podpis poprawny — sprawdzamy test_mode
    payment = Payment.objects.create(user=ps, amount=5000, method=Payment.Method.TPAY)
    assert _post(client, tpay_keys, _notification(payment)).content == b"TRUE"  # test_mode=1
    payment.refresh_from_db()
    assert payment.status == Payment.Status.UNMATCHED and "testowe" in payment.note and services.balance_of(ps) == 0
    live = Payment.objects.create(user=ps, amount=5000, method=Payment.Method.TPAY)
    _post(client, tpay_keys, _notification(live).replace(b"test_mode=1", b"test_mode=0"))
    live.refresh_from_db()
    assert live.status == Payment.Status.COMPLETED


def test_overpayment_pays_reservations_setting(owner_client, django_capture_on_commit_callbacks):
    from datetime import timedelta

    from django.utils import timezone

    from apps.bookings import services as booking_services
    from apps.bookings.models import Reservation
    from apps.facilities.models import Lane
    from apps.payments.models import PaymentSettings
    from apps.payments.services import balance_of, post
    from apps.payments.models import LedgerEntry

    from .conftest import add_credential, approve, make_user

    shooter = make_user("nadplata@example.com")
    add_credential(shooter)
    approve(shooter)
    lane = Lane.objects.filter(is_active=True).first()
    day = timezone.localdate() + timedelta(days=2)
    while lane.hours_for(day) is None:
        day += timedelta(days=1)
    h = lane.hours_for(day).opens.hour
    r1 = booking_services.create_reservation(shooter, lane, day, h, h + 1, 1)
    assert r1.payment_status == Reservation.Payment.DUE
    post(shooter, r1.amount + 1000, LedgerEntry.Kind.REFUND, "Zwrot testowy", by=None)  # nadpłata
    assert "Nadpłata opłaca kolejne rezerwacje" in owner_client.get("/panel/platnosci/ustawienia/").content.decode()
    # wyłączone: nadpłata leży, rezerwacja nieopłacona
    r2 = booking_services.create_reservation(shooter, lane, day, h + 1, h + 2, 1)
    assert Reservation.objects.get(pk=r2.pk).payment_status == Reservation.Payment.DUE
    # włączenie w panelu rozlicza istniejące nadpłaty (od najwcześniejszej rezerwacji)
    response = owner_client.post("/panel/platnosci/ustawienia/", {"overpayment_pays_reservations": "on"})
    assert response.status_code == 302 and PaymentSettings.load().overpayment_pays_reservations
    assert Reservation.objects.get(pk=r1.pk).payment_status == Reservation.Payment.PAID
    assert Reservation.objects.get(pk=r2.pk).payment_status == Reservation.Payment.DUE  # nie starczyło na drugą
    assert balance_of(shooter) == 1000


def test_pay_from_balance_button(owner_client):
    from datetime import timedelta

    from django.utils import timezone

    from apps.bookings import services as booking_services
    from apps.bookings.models import Reservation
    from apps.facilities.models import Lane
    from apps.payments.models import LedgerEntry
    from apps.payments.services import post

    from .conftest import add_credential, approve, make_user

    shooter = make_user("przycisk@example.com")
    add_credential(shooter)
    approve(shooter)
    lane = Lane.objects.filter(is_active=True).first()
    day = timezone.localdate() + timedelta(days=2)
    while lane.hours_for(day) is None:
        day += timedelta(days=1)
    h = lane.hours_for(day).opens.hour
    r = booking_services.create_reservation(shooter, lane, day, h, h + 1, 1)
    page = owner_client.get(f"/panel/rezerwacje/{r.pk}/").content.decode()
    assert "pay_from_balance" not in page  # brak nadpłaty
    post(shooter, r.amount, LedgerEntry.Kind.REFUND, "Zwrot testowy", by=None)
    assert "pay_from_balance" in owner_client.get(f"/panel/rezerwacje/{r.pk}/").content.decode()
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "pay_from_balance"})
    assert Reservation.objects.get(pk=r.pk).payment_status == Reservation.Payment.PAID
