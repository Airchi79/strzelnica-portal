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
from apps.payments.models import LedgerEntry, Payment, PaymentSettings

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

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)  # tryb „nadpłata opłaca…” — przy wyłączonym saldzie
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


def test_tpay_event_registration_start_and_notification(client, ps, owner, tpay_keys, monkeypatch):
    import datetime as dt

    from django.utils import timezone

    from apps.events import services as event_services
    from apps.events.models import Event, Registration

    start = timezone.now() + dt.timedelta(days=5)
    e = Event(title="Zawody", starts_at=start, ends_at=start + dt.timedelta(hours=3), fee=4000,
              registration_until=start - dt.timedelta(hours=1), status=Event.Status.PUBLISHED)
    event_services.save_event(e, [], owner)
    reg = event_services.register(ps, e)
    monkeypatch.setattr(tpay, "create_transaction", lambda **kw: {"transaction_id": "01EV", "title": "TR-TEST-1", "payment_url": "https://secure.sandbox.tpay.com/?t=1"})
    client.force_login(ps)
    response = client.post("/platnosci/tpay/start/", {"registration": reg.pk})
    assert response.status_code == 302
    payment = Payment.objects.get(event_registration=reg)
    assert payment.amount == 4000
    assert _post(client, tpay_keys, _notification(payment)).content == b"TRUE"
    reg.refresh_from_db()
    assert reg.payment_status == Registration.Payment.PAID and reg.paid_amount == 4000


def test_payment_receipt_pdf_owner_only(client, ps, owner_client):
    from apps.payments.services import register_manual_payment

    r = book(ps)
    p = register_manual_payment(ps, r.amount, "gotowka", ps, reservation=r)
    assert owner_client.get(f"/panel/platnosci/{p.pk}/potwierdzenie.pdf").status_code == 200  # ten sam klient — najpierw panel
    client.force_login(ps)
    response = client.get(f"/platnosci/potwierdzenie/{p.pk}.pdf")
    assert response.status_code == 200 and response["Content-Type"] == "application/pdf" and response.content.startswith(b"%PDF")
    assert f"/platnosci/potwierdzenie/{p.pk}.pdf" in client.get("/platnosci/").content.decode()
    assert f"/platnosci/potwierdzenie/{p.pk}.pdf" in client.get(f"/rezerwacje/{r.pk}/").content.decode()
    other = make_user("obcy@example.com")
    client.force_login(other)
    assert client.get(f"/platnosci/potwierdzenie/{p.pk}.pdf").status_code == 404


from apps.payments.services import balance_of  # noqa: E402


def _newbie(email):
    u = make_user(email, first_name="Nowy", last_name="Klient")
    add_credential(u)
    return u  # bez zatwierdzenia — grupa „nowy”, rezerwacje czekają na zatwierdzenie


def _pay_pending_online(client, user, tpay_keys, monkeypatch):
    r = book(user)
    assert r.status == Reservation.Status.PENDING_APPROVAL
    monkeypatch.setattr(tpay, "create_transaction", lambda **kw: {"transaction_id": "01PEND", "title": "TR-TEST-1", "payment_url": "https://secure.sandbox.tpay.com/?t=1"})
    client.force_login(user)
    assert "Zapłać online" in client.get(f"/rezerwacje/{r.pk}/").content.decode()
    assert client.post("/platnosci/tpay/start/", {"reservation": r.pk}).status_code == 302
    payment = Payment.objects.get(reservation=r)
    assert _post(client, tpay_keys, _notification(payment)).content == b"TRUE"
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID and r.status == Reservation.Status.PENDING_APPROVAL
    return r, payment


def test_pending_paid_then_approved_sends_paid_confirmation(client, owner, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    u = _newbie("nowy-op@example.com")
    r, _ = _pay_pending_online(client, u, tpay_keys, monkeypatch)
    from django.core import mail

    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.approve_reservation(r, owner)
    msg = [m for m in mail.outbox if m.to == [u.email]][0]
    assert "potwierdzona i opłacona" in msg.subject and "opłacona" in msg.body


def test_pending_paid_then_rejected_refunds_via_tpay(client, owner, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    from django.core import mail

    u = _newbie("nowy-zw@example.com")
    r, payment = _pay_pending_online(client, u, tpay_keys, monkeypatch)
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append((tid, amount)) or {"result": "success"})
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.reject_reservation(r, owner, "Brak wolnego prowadzącego")
    assert calls == [("01PEND", None)]  # pełny zwrot
    assert balance_of(u) == 0  # nie zostaje „nadpłata” — pieniądze poszły do płacącego
    assert LedgerEntry.objects.filter(payment=payment, kind=LedgerEntry.Kind.CHARGEBACK).count() == 1
    body = [m for m in mail.outbox if m.to == [u.email]][0].body
    assert "Zwrot" in body and "Brak wolnego prowadzącego" in body
    # późniejsze powiadomienie Tpay o zwrocie nie księguje drugi raz
    assert _post(client, tpay_keys, _notification(payment, status="CHARGEBACK")).content == b"TRUE"
    assert LedgerEntry.objects.filter(payment=payment, kind=LedgerEntry.Kind.CHARGEBACK).count() == 1


def test_reject_refund_failure_notifies_board(client, owner, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    from django.core import mail

    u = _newbie("nowy-err@example.com")
    r, _ = _pay_pending_online(client, u, tpay_keys, monkeypatch)

    def boom(*a, **k):
        raise tpay.TpayError("awaria")

    monkeypatch.setattr(tpay, "refund_transaction", boom)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.reject_reservation(r, owner, "x")
    assert balance_of(u) == r.amount  # Tpay odrzucił zlecony zwrot → kwota wraca na saldo (nadpłata)
    assert any("Zwrot Tpay nie powiódł się" in m.subject for m in mail.outbox)
    user_subjects = [m.subject for m in mail.outbox if m.to == [u.email]]
    assert any("nie została zatwierdzona" in x for x in user_subjects) and any("Zwrot przez Tpay nie powiódł się" in x for x in user_subjects)
    assert services.ledger_consistent(u)


def test_prepay_setting_off_hides_and_refuses_payment(client, tpay_keys, monkeypatch):
    from apps.bookings.models import BookingSettings

    cfg = BookingSettings.load()
    cfg.prepay_before_approval = False
    cfg.save()
    u = _newbie("nowy-off@example.com")
    r = book(u)
    client.force_login(u)
    assert "Zapłać online" not in client.get(f"/rezerwacje/{r.pk}/").content.decode()
    client.post("/platnosci/tpay/start/", {"reservation": r.pk})
    assert not Payment.objects.filter(reservation=r).exists()


def _confirmed_paid_online(client, ps, tpay_keys, monkeypatch):
    r = book(ps)
    monkeypatch.setattr(tpay, "create_transaction", lambda **kw: {"transaction_id": "01CANC", "title": "TR-TEST-1", "payment_url": "https://secure.sandbox.tpay.com/?t=1"})
    client.force_login(ps)
    client.post("/platnosci/tpay/start/", {"reservation": r.pk})
    payment = Payment.objects.get(reservation=r)
    _post(client, tpay_keys, _notification(payment))
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID
    return r, payment


def test_board_cancel_with_refund_checkbox(client, owner, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    from django.core import mail

    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append(tid) or {"result": "success"})
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.cancel_by_board(r, owner, "Awaria osi", refund=True)
    assert calls == ["01CANC"] and balance_of(ps) == 0
    assert "Zwrot" in [m for m in mail.outbox if m.to == [ps.email]][0].body


def test_board_cancel_without_refund_keeps_overpayment(client, owner, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    from django.core import mail

    r, _ = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    monkeypatch.setattr(tpay, "refund_transaction", lambda *a, **k: pytest.fail("nie powinno być zwrotu"))
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.cancel_by_board(r, owner, "Na prośbę klienta", refund=False)
    assert balance_of(ps) == r.amount
    assert "nadpłata" in [m for m in mail.outbox if m.to == [ps.email]][0].body


def test_board_cancel_keep_fee_never_refunds(client, owner, ps, tpay_keys, monkeypatch):
    from apps.bookings.forms import BoardCancelForm

    form = BoardCancelForm({"c-reason": "późno", "c-keep_fee": "on", "c-refund_to_payer": "on"}, prefix="c")
    assert form.is_valid() and form.cleaned_data["refund_to_payer"] is False
    r, _ = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    monkeypatch.setattr(tpay, "refund_transaction", lambda *a, **k: pytest.fail("nie powinno być zwrotu"))
    booking_services.cancel_by_board(r, owner, "późno", keep_fee=True, refund=True)
    r.refresh_from_db()
    assert r.late_cancellation


def test_panel_cancel_form_has_refund_checkbox(owner_client, ps):
    r = book(ps)
    assert "Zwróć wpłatę na rachunek / kartę strzelca" in owner_client.get(f"/panel/rezerwacje/{r.pk}/").content.decode()


def test_change_term_surcharge_and_partial_refund(client, owner, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    from django.core import mail

    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)  # Oś 100 m, 10–12
    paid = r.paid_amount
    day = timezone.localtime(r.starts_at).date()
    # dłużej → dopłata
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        r2 = booking_services.change_term(r, r.lane, day, 10, 13, owner)
    assert r2.amount > paid and r2.payment_status == Reservation.Payment.DUE and r2.paid_amount == paid
    assert "Do dopłaty" in [m for m in mail.outbox if m.to == [ps.email]][0].body
    # krócej niż zapłacono → częściowy zwrot przez Tpay
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append((tid, amount)) or {"result": "success"})
    with django_capture_on_commit_callbacks(execute=True):
        r3 = booking_services.change_term(r2, r.lane, day, 10, 11, owner, refund_difference=True)
    assert r3.payment_status == Reservation.Payment.PAID and calls and calls[0][1] == paid - r3.amount
    assert balance_of(ps) == 0


def test_change_term_rejects_taken_slot_and_frees_old_for_waitlist(owner, ps, django_capture_on_commit_callbacks):
    from django.core.exceptions import ValidationError

    from apps.bookings import waitlist
    from apps.bookings.models import WaitlistEntry

    r = book(ps, start=10, end=12)
    other = make_user("inny-ct@example.com")
    add_credential(other)
    approve(other)
    taken = book(other, start=14, end=15)
    day = timezone.localtime(r.starts_at).date()
    with pytest.raises(ValidationError, match="zajęty"):
        booking_services.change_term(r, r.lane, day, 14, 16, owner)
    waiting = make_user("czeka-ct@example.com")
    add_credential(waiting)
    approve(waiting)
    entry = waitlist.join(waiting, r.lane, day, 10, 11)
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.change_term(r, r.lane, day, 16, 17, owner)
    entry.refresh_from_db()
    assert entry.status == WaitlistEntry.Status.OFFERED  # zwolniony stary termin poszedł do oczekującego
    taken.refresh_from_db()
    assert taken.status == Reservation.Status.CONFIRMED


def test_panel_change_term_form(owner_client, ps):
    r = book(ps, start=10, end=12)
    page = owner_client.get(f"/panel/rezerwacje/{r.pk}/").content.decode()
    assert "Zmień oś lub termin" in page
    day = timezone.localtime(r.starts_at).date().isoformat()
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "change_term", "t-lane": r.lane_id, "t-day": day, "t-start_hour": 12, "t-end_hour": 14})
    r.refresh_from_db()
    assert timezone.localtime(r.starts_at).hour == 12


def test_client_cancel_paid_online_refunds_to_card(client, owner, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    """Klient sam anuluje opłaconą online rezerwację (saldo wyłączone) — zwrot przez Tpay, informacja w e-mailu i na stronie."""
    from django.core import mail

    from apps.core.models import Notification

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)  # tryb domyślny: bez salda
    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append((tid, amount)) or {"result": "success"})
    mail.outbox.clear()
    client.force_login(ps)
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(f"/rezerwacje/{r.pk}/", {"reason": "zmiana planów"}, follow=True)
    assert calls == [("01CANC", None)]
    assert "Zwrot 240.00 zł zlecony" in resp.content.decode()
    r.refresh_from_db()
    payment.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED and payment.raw["zwrot_api"]["kwota_gr"] == 24000
    assert balance_of(ps) == 0
    assert "Zwrot" in [m for m in mail.outbox if m.to == [ps.email]][0].body
    assert Notification.objects.filter(recipient=ps, channel="portal", title__contains="anulowana").exists()


def test_client_cancel_paid_cash_needs_manual_refund(ps, owner, django_capture_on_commit_callbacks):
    from django.core import mail

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r = book(ps, days=10)
    services.register_manual_payment(ps, 24000, Payment.Method.CASH, owner, reservation=r)
    r.refresh_from_db()
    assert r.paid_amount == 24000
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        r, refunded, manual, to_wallet = booking_services.cancel_by_client(r, ps, now=r.starts_at - timedelta(days=3))
    assert (refunded, manual, to_wallet) == (0, 24000, 0)
    assert services.balance_of(ps) == 24000  # nadpłata do zwrotu ręcznego, zarząd ma przypomnienie
    assert any("Zwrot do wykonania ręcznie" in m.subject for m in mail.outbox)
    assert "zwrócimy ręcznie" in [m for m in mail.outbox if m.to == [ps.email]][0].body


def test_client_cancel_with_wallet_keeps_money_on_balance(ps, owner, monkeypatch, django_capture_on_commit_callbacks):
    from django.core import mail

    cfg = PaymentSettings.load()
    cfg.wallet_enabled = True
    cfg.save()
    services.register_manual_payment(ps, 24000, Payment.Method.CASH, owner)
    r = book(ps, days=10)
    called = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda *a, **k: called.append(a))
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        r, refunded, manual, to_wallet = booking_services.cancel_by_client(r, ps, now=r.starts_at - timedelta(days=3))
    assert (refunded, manual, to_wallet) == (0, 0, 24000) and not called
    assert services.balance_of(ps) == 24000
    assert "wróciła na Twoje saldo" in [m for m in mail.outbox if m.to == [ps.email]][0].body


def test_panel_refund_overpayment_via_tpay(client, owner, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    """Nadpłata po anulowaniu bez zwrotu (np. stary przypadek, blokada osi) — zarząd zwraca ją z panelu, tylko raz."""
    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    booking_services.cancel_reservation(r, ps)  # dawna ścieżka: wpłata zostaje nadpłatą
    assert balance_of(ps) == 24000
    assert services.refundable_overpayments(ps) == [(payment, 24000)]
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append((tid, amount)) or {"result": "success"})
    client.logout()
    other = make_user("bez-uprawnien@example.com", is_staff=True)
    login_with_otp(client, other)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"/panel/salda/{ps.pk}/", {"action": "refund_tpay", "payment": str(payment.pk)})
    assert not calls  # bez uprawnień do płatności
    client.logout()
    login_with_otp(client, owner)
    page = client.get(f"/panel/salda/{ps.pk}/").content.decode()
    assert "Zwróć przez Tpay" in page and r.number in page
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"/panel/salda/{ps.pk}/", {"action": "refund_tpay", "payment": str(payment.pk)})
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"/panel/salda/{ps.pk}/", {"action": "refund_tpay", "payment": str(payment.pk)})  # drugi raz — nic
    assert calls == [("01CANC", None)] and balance_of(ps) == 0
    assert services.refundable_overpayments(ps) == []
    assert "Zwróć przez Tpay" not in client.get(f"/panel/salda/{ps.pk}/").content.decode()


def test_client_cancel_with_wallet_still_refunds_online_payment(client, owner, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    """Saldo włączone (autouse w tym pliku) — wpłata online i tak wraca przez Tpay, nie na saldo."""
    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append(tid) or {"result": "success"})
    with django_capture_on_commit_callbacks(execute=True):
        r, refunded, manual, to_wallet = booking_services.cancel_by_client(r, ps)
    assert calls == ["01CANC"] and (refunded, manual, to_wallet) == (24000, 0, 0) and balance_of(ps) == 0


# --- opłata za płatność online (bezzwrotna) -------------------------------------------------


def _fee_on(percent="1.59", fixed=30, wallet=False):
    from decimal import Decimal

    PaymentSettings.objects.filter(pk=1).update(online_fee_percent=Decimal(percent), online_fee_fixed=fixed, wallet_enabled=wallet)


def test_online_fee_calculation(db):
    assert services.online_fee(24000) == 0  # domyślnie bez opłaty
    _fee_on()
    # gross-up: najmniejsza płatność T, przy której T − (1,59% z T w górę + 0,30 zł) ≥ 240 zł → T = 244,19 zł
    assert services.online_fee(24000) == 419
    assert 24419 - services.operator_commission(24419) == 24000 and 24418 - services.operator_commission(24418) < 24000
    assert services.online_fee(0) == 0


def test_online_fee_charged_booked_and_not_refunded(client, owner, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    from django.core import mail

    _fee_on()
    r = book(ps)
    sent = {}
    monkeypatch.setattr(tpay, "create_transaction", lambda **kw: sent.update(kw) or {"transaction_id": "01FEE", "title": "TR-TEST-1",
                                                                                      "payment_url": "https://secure.sandbox.tpay.com/?t=1"})
    client.force_login(ps)
    page = client.get(f"/rezerwacje/{r.pk}/").content.decode()
    assert "opłata za płatność online 4,19\xa0zł" in page and "244,19\xa0zł" in page
    client.post("/platnosci/tpay/start/", {"reservation": r.pk})
    payment = Payment.objects.get(reservation=r)
    assert sent["amount_grosze"] == 24419 and payment.amount == 24419 and payment.surcharge == 419
    _post(client, tpay_keys, _notification(payment))
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID and r.paid_amount == 24000 and balance_of(ps) == 0
    assert LedgerEntry.objects.filter(user=ps, kind=LedgerEntry.Kind.ONLINE_FEE, amount=-419).exists()
    assert services.ledger_consistent(ps)
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append((tid, amount)) or {"result": "success"})
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        r, refunded, manual, to_wallet = booking_services.cancel_by_client(r, ps)
    assert calls == [("01FEE", 24000)]  # zwrot częściowy: tylko cena rezerwacji
    assert (refunded, manual) == (24000, 0) and balance_of(ps) == 0 and services.ledger_consistent(ps)
    assert services.refundable_overpayments(ps) == []
    body = [m for m in mail.outbox if m.to == [ps.email]][0].body
    assert "Zwrot 240\xa0zł" in body and "Opłata za płatność online (4,19\xa0zł) nie podlega zwrotowi" in body


def test_online_fee_not_refunded_on_reject(client, owner, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    _fee_on(percent="0", fixed=100)
    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    assert payment.surcharge == 100 and payment.amount == 24100
    Reservation.objects.filter(pk=r.pk).update(status=Reservation.Status.PENDING_APPROVAL)
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append(amount) or {"result": "success"})
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.reject_reservation(r, owner, "brak miejsc")
    assert calls == [24000] and balance_of(ps) == 0


def test_online_fee_settings_form_and_receipt(owner_client, ps, owner):
    response = owner_client.post("/panel/platnosci/ustawienia/", {"online_fee_percent": "1,59", "online_fee_fixed_pln": "0,30"})
    assert response.status_code == 302
    cfg = PaymentSettings.load()
    assert str(cfg.online_fee_percent) == "1.59" and cfg.online_fee_fixed == 30
    assert "0,30" in owner_client.get("/panel/platnosci/ustawienia/").content.decode()
    assert owner_client.post("/panel/platnosci/ustawienia/", {"online_fee_percent": "11"}).status_code == 200  # ponad 10%
    from apps.core.models import AssociationSettings
    from apps.payments.receipt import to_pdf

    p = Payment.objects.create(user=ps, amount=24412, surcharge=412, method=Payment.Method.TPAY, status=Payment.Status.COMPLETED,
                               completed_at=timezone.now(), title="x")
    assert to_pdf(p, AssociationSettings.load()).startswith(b"%PDF")


def test_block_cancellation_refunds_online_payment(client, owner, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    from django.core import mail

    _fee_on(percent="0", fixed=100)
    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append((tid, amount)) or {"result": "success"})
    occ = [(r.starts_at, r.ends_at)]
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        _, _, cancelled = booking_services.create_blocks("Zawody", "", [r.lane], occ, owner, cancel_conflicts=True)
    assert [c.pk for c in cancelled] == [r.pk]
    assert calls == [("01CANC", 24000)] and balance_of(ps) == 0 and services.ledger_consistent(ps)
    body = [m for m in mail.outbox if m.to == [ps.email]][0].body
    assert "Blokada osi: Zawody" in body and "Zwrot 240\xa0zł" in body


def test_manual_refund_and_sales_report(owner_client, owner, ps):
    from apps.reports.services import collect, summarize

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    services.register_manual_payment(ps, 30000, Payment.Method.CASH, owner)  # nadpłata 300 zł
    url = f"/panel/salda/{ps.pk}/"
    assert "Zwrot ręczny do strzelca" in owner_client.get(url).content.decode()
    # ponad nadpłatę — odmowa
    owner_client.post(url, {"action": "manual_refund", "zwrot-method": "przelew", "zwrot-amount": "400", "zwrot-reason": "x"})
    assert balance_of(ps) == 30000
    owner_client.post(url, {"action": "manual_refund", "zwrot-method": "tpay_panel", "zwrot-amount": "272,28", "zwrot-reason": "SATOK-R-1"})
    assert balance_of(ps) == 2772 and services.ledger_consistent(ps)
    e = LedgerEntry.objects.get(user=ps, kind=LedgerEntry.Kind.CHARGEBACK)
    assert e.amount == -27228 and "panel Tpay" in e.description
    # korekta salda dalej działa (bez pola action)
    owner_client.post(url, {"direction": "minus", "amount": "27,72", "reason": "prowizja"})
    assert balance_of(ps) == 0
    rows = collect(timezone.localdate(), timezone.localdate())
    refund_rows = [r for r in rows if r["category"] == "zwrot"]
    assert [(r["method"], r["amount"]) for r in refund_rows] == [("zwrot_reczny", -27228)]


def test_sales_report_online_fees(ps, owner, monkeypatch):
    from apps.reports.services import collect, summarize, to_csv
    from apps.reports.models import SalesReport

    _fee_on(percent="0", fixed=150)
    p = Payment.objects.create(user=ps, amount=24150, surcharge=150, method=Payment.Method.TPAY, external_id="tpay:01REP", title="x")
    services.complete_payment(p)
    s = summarize(collect(timezone.localdate(), timezone.localdate()))
    assert s["online_fees"] == 150 and s["inflow"] == 24150
    report = SalesReport(number="RS/T/1", kind=SalesReport.Kind.DAILY, period_start=timezone.localdate(), period_end=timezone.localdate(),
                         data={"rows": [], "summary": s})
    assert "opłaty za płatność online (bezzwrotne);1,50" in to_csv(report)


def test_online_fee_regulation_reminder(owner_client, owner):
    from apps.regulations.models import RegulationDocument, RegulationVersion

    assert not services.online_fee_missing_in_rules()  # opłata wyłączona — bez ostrzeżenia
    _fee_on(percent="1.59", fixed=30)
    assert services.online_fee_missing_in_rules()
    text = services.online_fee_rule_text()
    assert "1,59% kwoty płatności + 0,30\xa0zł" in text
    page = owner_client.get("/panel/platnosci/ustawienia/").content.decode()
    assert "Regulamin nie wspomina o opłacie za płatność online" in page
    assert "Regulamin a opłata online" in owner_client.get("/panel/").content.decode()
    doc = RegulationDocument.objects.get(slug="regulamin-rezerwacji")
    last = doc.versions.order_by("-number").first()
    RegulationVersion.objects.create(document=doc, number=(last.number if last else 0) + 1, content="§ 9. " + text,
                                     effective_from=timezone.now() - timedelta(minutes=1), published_at=timezone.now())
    assert not services.online_fee_missing_in_rules()
    assert "Regulamin nie wspomina" not in owner_client.get("/panel/platnosci/ustawienia/").content.decode()



@pytest.mark.parametrize("pct,fixed", [("0.99", 0), ("1.59", 30), ("2.50", 0), ("0.19", 0)])
def test_online_fee_always_covers_refund(db, pct, fixed):
    """Po potrąceniu prowizji operatora (od całej płatności, w górę) zawsze zostaje pełna cena — zwrot ma pokrycie;
    i nie pobieramy ani grosza więcej, niż trzeba."""
    from decimal import Decimal

    cfg = PaymentSettings(online_fee_percent=Decimal(pct), online_fee_fixed=fixed)
    for amount in list(range(100, 50001, 37)) + [10000, 24000, 30000]:
        total = amount + services.online_fee(amount, cfg)
        assert total - services.operator_commission(total, cfg) >= amount
        assert (total - 1) - services.operator_commission(total - 1, cfg) < amount
    assert services.online_fee(10000, PaymentSettings(online_fee_percent=Decimal("0.99"))) == 100  # 100 zł → 1,00 zł


def test_failed_tpay_refund_with_wallet_notifies_board_and_can_be_retried(client, owner, ps, tpay_keys, monkeypatch,
                                                                         django_capture_on_commit_callbacks):
    """Saldo włączone: Tpay odrzuca zwrot → kwota na saldzie + powiadomienie zarządu; ponowienie z panelu zeruje saldo."""
    from django.core import mail

    from apps.core.models import Notification

    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)  # saldo włączone (autouse w tym pliku)

    def reject(tid, amount=None):
        raise tpay.TpayError("Tpay odpowiedział błędem 400")

    monkeypatch.setattr(tpay, "refund_transaction", reject)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        r, refunded, manual, to_wallet = booking_services.cancel_by_client(r, ps)
    # zwrot zlecony (strzelec dostał „zwrot zlecony”); Tpay odrzucił go po zatwierdzeniu → kwota wraca na saldo
    assert (refunded, manual, to_wallet) == (24000, 0, 0) and balance_of(ps) == 24000
    assert payment.tpay_refunds.get().status == "nieudany"
    assert any("Zwrot przez Tpay nie powiódł się" in m.subject for m in mail.outbox if m.to == [ps.email])
    n = Notification.objects.get(recipient=owner, channel="panel", title__startswith="Zwrot Tpay nie powiódł się")
    assert n.url == f"/panel/salda/{ps.pk}/" and "Ponów" in n.body
    assert any("Zwrot Tpay nie powiódł się" in m.subject for m in mail.outbox)
    # ponowienie z panelu — Tpay już przyjmuje
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append(tid) or {"result": "success"})
    client.logout()
    login_with_otp(client, owner)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"/panel/salda/{ps.pk}/", {"action": "refund_tpay", "payment": str(payment.pk)})
    assert calls == ["01CANC"] and balance_of(ps) == 0
    assert sorted(payment.tpay_refunds.values_list("status", flat=True)) == ["nieudany", "wykonany"]
    assert services.ledger_consistent(ps)


def test_failed_tpay_refund_without_wallet_single_reminder(client, owner, ps, tpay_keys, monkeypatch,
                                                           django_capture_on_commit_callbacks):
    from apps.core.models import Notification

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)

    def reject(tid, amount=None):
        raise tpay.TpayError("Brak połączenia z Tpay")

    monkeypatch.setattr(tpay, "refund_transaction", reject)
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.cancel_by_client(r, ps)
    titles = list(Notification.objects.filter(recipient=owner, channel="panel").values_list("title", flat=True))
    # jedno powiadomienie o nieudanym zwrocie (z instrukcją: ponów albo zwróć ręcznie), bez dodatkowego „ręcznie”
    assert sum(t.startswith("Zwrot Tpay nie powiódł się") for t in titles) == 1
    assert not any(t.startswith("Zwrot do wykonania ręcznie") for t in titles)
    assert balance_of(ps) == 24000


def test_wallet_first_partial_then_online_and_refund_split(client, owner, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    """Saldo 70 zł, rezerwacja 240 zł: z salda 70 zł od razu, online tylko 170 zł (+ opłata od 170 zł);
    przy anulowaniu 170 zł wraca przez Tpay, 70 zł na saldo."""
    _fee_on(percent="0.99", fixed=0, wallet=True)
    services.register_manual_payment(ps, 7000, Payment.Method.CASH, owner)  # saldo 70 zł
    client.force_login(ps)
    page = client.post("/rezerwacje/nowa/", {"lane": Lane.objects.get(name="Oś 100 m").pk,
                                             "day": (timezone.localdate() + timedelta(days=7)).isoformat(),
                                             "start_hour": 10, "end_hour": 12, "persons": 2}).content.decode()
    assert "Z Twojego salda" in page and "70\xa0zł" in page and "170\xa0zł" in page
    r = book(ps)
    assert r.paid_amount == 7000 and r.payment_status == Reservation.Payment.DUE and balance_of(ps) == 0
    detail = client.get(f"/rezerwacje/{r.pk}/").content.decode()
    assert "Do zapłaty: 170\xa0zł" in detail and "Zapłać online 170\xa0zł" in detail
    fee = services.online_fee(17000)
    assert f"opłata za płatność online {fee // 100},{fee % 100:02d}\xa0zł" in detail
    sent = {}
    monkeypatch.setattr(tpay, "create_transaction", lambda **kw: sent.update(kw) or {"transaction_id": "01MIX", "title": "TR-TEST-1",
                                                                                      "payment_url": "https://secure.sandbox.tpay.com/?t=1"})
    client.post("/platnosci/tpay/start/", {"reservation": r.pk})
    payment = Payment.objects.get(reservation=r)
    assert sent["amount_grosze"] == 17000 + fee
    _post(client, tpay_keys, _notification(payment))
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID and r.paid_amount == 24000 and balance_of(ps) == 0
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append((tid, amount)) or {"result": "success"})
    with django_capture_on_commit_callbacks(execute=True):
        r, refunded, manual, to_wallet = booking_services.cancel_by_client(r, ps)
    assert calls == [("01MIX", 17000)] and (refunded, to_wallet) == (17000, 7000) and balance_of(ps) == 7000
    assert services.ledger_consistent(ps)


def test_use_wallet_button_for_existing_reservation(client, owner, ps):
    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r = book(ps)  # saldo wyłączone przy rezerwacji — nic nie pobrano
    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=True)
    services.post(ps, 7000, LedgerEntry.Kind.CORRECTION, "test")
    client.force_login(ps)
    assert "Użyj salda (70\xa0zł)" in client.get(f"/rezerwacje/{r.pk}/").content.decode()
    client.post(f"/rezerwacje/{r.pk}/", {"action": "use_wallet"})
    r.refresh_from_db()
    assert r.paid_amount == 7000 and balance_of(ps) == 0 and r.payment_status == Reservation.Payment.DUE
    assert "Użyj salda" not in client.get(f"/rezerwacje/{r.pk}/").content.decode()


# --- rezerwacja ze strony: blokada terminu, wybór płatności, potwierdzenie po zapłacie -----------------------------


def _checkout(user, days=7, start=10, end=12, lane_name="Oś 100 m"):
    return booking_services.create_reservation(user, Lane.objects.get(name=lane_name), timezone.localdate() + timedelta(days=days),
                                               start, end, 2, checkout=True)


def test_checkout_hold_then_cash(ps, owner, django_capture_on_commit_callbacks, client):
    from django.core import mail

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        r = _checkout(ps)
    assert r.status == Reservation.Status.HOLD and r.payment_status == Reservation.Payment.DUE
    assert timedelta(minutes=4) < r.pay_by - timezone.now() <= timedelta(minutes=5)
    assert not [m for m in mail.outbox if m.to == [ps.email]]  # bez „potwierdzona” przed wyborem płatności
    client.force_login(ps)
    page = client.get(f"/rezerwacje/{r.pk}/").content.decode()
    assert "wybierz sposób płatności" in page and "Zapłacę gotówką na miejscu" in page and "Zapłacę przelewem tradycyjnym" in page
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"/rezerwacje/{r.pk}/", {"action": "choose_cash"})
    r.refresh_from_db()
    assert r.status == Reservation.Status.CONFIRMED and r.pay_method == "gotowka" and r.pay_by is None
    body = [m for m in mail.outbox if m.to == [ps.email]][0].body
    assert "potwierdzamy rezerwację" in body and "gotówką na miejscu" in body


def test_checkout_transfer_waits_for_booking(ps, owner, django_capture_on_commit_callbacks):
    from django.core import mail

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r = _checkout(ps, days=10)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        r = booking_services.choose_payment(r, ps, Reservation.PayMethod.TRANSFER)
    assert r.status == Reservation.Status.HOLD and r.pay_method == "przelew"
    assert abs(r.pay_by - (timezone.now() + timedelta(days=3))) < timedelta(minutes=1)
    assert "oczekuje na przelew" in mail.outbox[0].subject and r.number in mail.outbox[0].body
    # zaksięgowany przelew → potwierdzona
    with django_capture_on_commit_callbacks(execute=True):
        services.register_manual_payment(ps, r.amount, Payment.Method.TRANSFER, owner, reservation=r)
    r.refresh_from_db()
    assert r.status == Reservation.Status.CONFIRMED and r.payment_status == Reservation.Payment.PAID
    assert any("potwierdzona" in m.subject for m in mail.outbox if m.to == [ps.email])


def test_transfer_unavailable_close_to_date_and_expiry(ps):
    r = _checkout(ps, days=2)
    assert booking_services.payment_options(r)["transfer_deadline"] is None
    with pytest.raises(ValidationError, match="za późno"):
        booking_services.choose_payment(r, ps, Reservation.PayMethod.TRANSFER)
    # przelew niezaksięgowany do terminu → NIE zwalnia się sam (decyzja obsługi); zwalnia się dopiero w chwili rozpoczęcia
    r2 = _checkout(ps, days=10, start=13, end=14)
    r2 = booking_services.choose_payment(r2, ps, Reservation.PayMethod.TRANSFER)
    booking_services.expire_unpaid(now=r2.pay_by + timedelta(minutes=1))
    r2.refresh_from_db()
    assert r2.status == Reservation.Status.HOLD and r2.overdue_notified_at is not None
    assert booking_services.expire_unpaid(now=r2.starts_at) == 1
    r2.refresh_from_db()
    assert r2.status == Reservation.Status.CANCELLED and "przed rozpoczęciem" in r2.cancel_reason


def test_checkout_online_confirms_after_payment(client, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    from django.core import mail

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r = _checkout(ps)
    monkeypatch.setattr(tpay, "create_transaction", lambda **kw: {"transaction_id": "01HOLD", "title": "TR-TEST-1",
                                                                   "payment_url": "https://secure.sandbox.tpay.com/?t=1"})
    client.force_login(ps)
    client.post("/platnosci/tpay/start/", {"reservation": r.pk})
    r.refresh_from_db()
    assert r.status == Reservation.Status.HOLD and r.pay_method == "online"
    assert r.pay_by - timezone.now() > timedelta(minutes=14)  # termin trzymany na czas płatności
    payment = Payment.objects.get(reservation=r)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        _post(client, tpay_keys, _notification(payment))
    r.refresh_from_db()
    assert r.status == Reservation.Status.CONFIRMED and r.payment_status == Reservation.Payment.PAID and r.pay_by is None
    assert any("potwierdzona" in m.subject for m in mail.outbox if m.to == [ps.email])


def test_abandoned_hold_released_silently_and_slot_free(ps, owner, django_capture_on_commit_callbacks):
    from django.core import mail

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r = _checkout(ps)
    other = make_user("drugi@example.com")
    add_credential(other)
    approve(other)
    with pytest.raises(ValidationError, match="zajęty"):
        _checkout(other)  # termin trzymany dla pierwszego strzelca
    Reservation.objects.filter(pk=r.pk).update(pay_by=timezone.now() - timedelta(seconds=1))
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        r2 = _checkout(other)  # wygasła blokada zwolniona przy zapisie — bez czekania na zadanie okresowe
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED and r2.status == Reservation.Status.HOLD
    assert not [m for m in mail.outbox if m.to == [ps.email]]  # porzucony wybór płatności — bez e-maila


def test_new_shooter_prepays_then_waits_for_approval(client, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    newbie = make_user("nowy3@example.com")
    add_credential(newbie)  # PS, ale bez wizyt — grupa „nowy”
    r = _checkout(newbie)
    assert r.status == Reservation.Status.HOLD and r.needs_approval
    opts = booking_services.payment_options(r)
    assert opts["online"] and not opts["cash"] and not opts["transfer"]
    monkeypatch.setattr(tpay, "create_transaction", lambda **kw: {"transaction_id": "01NEW", "title": "TR-TEST-1",
                                                                   "payment_url": "https://secure.sandbox.tpay.com/?t=1"})
    client.force_login(newbie)
    client.post("/platnosci/tpay/start/", {"reservation": r.pk})
    with django_capture_on_commit_callbacks(execute=True):
        _post(client, tpay_keys, _notification(Payment.objects.get(reservation=r)))
    r.refresh_from_db()
    assert r.status == Reservation.Status.PENDING_APPROVAL and r.payment_status == Reservation.Payment.PAID


def test_wallet_covers_all_confirmed_immediately_and_partial_hold_refunded_on_expiry(ps, owner):
    services.register_manual_payment(ps, 50000, Payment.Method.CASH, owner)  # saldo 500 zł (saldo włączone)
    r = _checkout(ps)
    assert r.status == Reservation.Status.CONFIRMED and r.payment_status == Reservation.Payment.PAID
    services.correction(ps, -(services.balance_of(ps) - 7000), "test", owner)  # zostaje 70 zł
    r2 = _checkout(ps, start=13, end=15)
    assert r2.status == Reservation.Status.HOLD and r2.paid_amount == 7000 and services.balance_of(ps) == 0
    booking_services.expire_unpaid(now=r2.pay_by + timedelta(minutes=1))
    r2.refresh_from_db()
    assert r2.status == Reservation.Status.CANCELLED and services.balance_of(ps) == 7000  # saldo wróciło


# --- przelew po terminie (decyzja obsługi), przypomnienie, blokady osi, wpłaty częściowe -----------------------------------


def _transfer_hold(ps, days=10, start=13, end=14):
    r = _checkout(ps, days=days, start=start, end=end)
    return booking_services.choose_payment(r, ps, Reservation.PayMethod.TRANSFER)


def test_transfer_overdue_notifies_board_once_and_keeps_slot(ps, owner, django_capture_on_commit_callbacks):
    from django.core import mail

    from apps.core.models import Notification

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r = _transfer_hold(ps)
    mail.outbox.clear()
    after = r.pay_by + timedelta(minutes=1)
    with django_capture_on_commit_callbacks(execute=True):
        assert booking_services.expire_unpaid(now=after) == 0
        assert booking_services.expire_unpaid(now=after + timedelta(minutes=5)) == 0  # drugi przebieg — bez ponownych powiadomień
    r.refresh_from_db()
    assert r.status == Reservation.Status.HOLD and r.overdue_notified_at == after
    panel = Notification.objects.filter(channel=Notification.Channel.PANEL, title__startswith="Przelew po terminie")
    assert panel.count() == Notification.objects.filter(channel="panel").count() == panel.filter(recipient=owner).count() == 1
    user_mails = [m for m in mail.outbox if m.to == [ps.email]]
    assert len(user_mails) == 1 and "termin przelewu minął" in user_mails[0].subject
    # termin nadal zajęty — nikt inny go nie zarezerwuje
    other = make_user("inny@example.com")
    add_credential(other, number="PS-2")
    approve(other)
    with pytest.raises(ValidationError):
        booking_services.create_reservation(other, r.lane, timezone.localtime(r.starts_at).date(), 13, 14, 1, now=after)
    # zaksięgowany przelew po terminie → potwierdzona
    p = Payment.objects.create(user=ps, amount=r.amount, method=Payment.Method.TRANSFER, reservation=r, title=r.number)
    services.complete_payment(p, by=owner)
    r.refresh_from_db()
    assert r.status == Reservation.Status.CONFIRMED and r.payment_status == Reservation.Payment.PAID


def test_board_can_cancel_overdue_transfer(client, ps, owner):
    r = _transfer_hold(ps)
    Reservation.objects.filter(pk=r.pk).update(pay_by=timezone.now() - timedelta(hours=1))
    client.force_login(ps)
    page = client.get(f"/rezerwacje/{r.pk}/").content.decode()
    assert "Termin przelewu minął" in page and "termin nadal jest trzymany" in page
    client.logout()
    login_with_otp(client, owner)
    page = client.get(f"/panel/rezerwacje/{r.pk}/").content.decode()
    assert "termin minął" in page and "Anuluj rezerwację" in page
    client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "cancel", "c-reason": "Brak przelewu", "c-refund_to_payer": "on"})
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED


def test_transfer_reminder_day_before_deadline(ps, django_capture_on_commit_callbacks):
    from django.core import mail

    r = _transfer_hold(ps)
    assert r.pay_reminder_sent_at is None
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert booking_services.send_transfer_reminders(now=r.pay_by - timedelta(hours=30)) == 0
        assert booking_services.send_transfer_reminders(now=r.pay_by - timedelta(hours=20)) == 1
        assert booking_services.send_transfer_reminders(now=r.pay_by - timedelta(hours=19)) == 0
    sent = [m for m in mail.outbox if m.to == [ps.email]]
    assert len(sent) == 1 and r.number in sent[0].body and "Przypomnienie" in sent[0].subject


def test_transfer_reminder_skipped_when_deadline_within_a_day(ps):
    from apps.bookings.models import BookingSettings

    BookingSettings.load()
    BookingSettings.objects.filter(pk=1).update(transfer_days=1)  # termin przelewu = doba od wyboru — wystarczy e-mail z wyboru
    r = _transfer_hold(ps)
    assert r.pay_reminder_sent_at is not None
    assert booking_services.send_transfer_reminders(now=r.pay_by - timedelta(hours=1)) == 0


def test_block_cancels_hold_reservations(ps, owner):
    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r = _transfer_hold(ps)
    lane = r.lane
    assert [x.pk for x in booking_services.block_conflicts([lane], [(r.starts_at, r.ends_at)])] == [r.pk]
    booking_services.create_blocks("Zawody", "", [lane], [(r.starts_at, r.ends_at)], owner, cancel_conflicts=True)
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED


def test_block_without_cancel_notifies_hold_owner(ps, owner, django_capture_on_commit_callbacks):
    from django.core import mail

    r = _transfer_hold(ps)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.create_blocks("Prace", "", [r.lane], [(r.starts_at, r.ends_at)], owner, cancel_conflicts=False)
    assert any(m.to == [ps.email] and "Blokada osi" in m.subject for m in mail.outbox)


def test_partial_transfer_counts_towards_hold_and_completes(ps, owner):
    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r = _transfer_hold(ps)
    half = r.amount // 2
    services.complete_payment(Payment.objects.create(user=ps, amount=half, method=Payment.Method.TRANSFER, reservation=r, title=r.number), by=owner)
    r.refresh_from_db()
    assert r.status == Reservation.Status.HOLD and r.paid_amount == half and balance_of(ps) == 0
    services.complete_payment(Payment.objects.create(user=ps, amount=r.amount - half, method=Payment.Method.TRANSFER, reservation=r,
                                                     title=r.number), by=owner)
    r.refresh_from_db()
    assert r.status == Reservation.Status.CONFIRMED and r.payment_status == Reservation.Payment.PAID and r.paid_amount == r.amount
    assert services.ledger_consistent(ps)


def test_partial_transfer_released_at_start_reminds_manual_refund(ps, owner, django_capture_on_commit_callbacks):
    from django.core import mail

    from apps.core.models import Notification

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r = _transfer_hold(ps)
    half = r.amount // 2
    services.complete_payment(Payment.objects.create(user=ps, amount=half, method=Payment.Method.TRANSFER, reservation=r, title=r.number), by=owner)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert booking_services.expire_unpaid(now=r.starts_at) == 1
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED and r.paid_amount == 0 and balance_of(ps) == half
    assert Notification.objects.filter(channel="panel", title__startswith="Zwrot do wykonania ręcznie").exists()
    body = [m for m in mail.outbox if m.to == [ps.email] and "anulowana" in m.subject][0].body
    assert "zwrócimy ręcznie" in body and f"{half // 100}" in body
    assert services.ledger_consistent(ps)


def test_abandoned_hold_with_wallet_part_returns_to_wallet_with_notice(ps, django_capture_on_commit_callbacks):
    from django.core import mail

    services.post(ps, 5000, LedgerEntry.Kind.CORRECTION, "test")
    r = _checkout(ps)  # saldo włączone: 50 zł pobrane od razu, reszta do zapłaty
    assert r.status == Reservation.Status.HOLD and r.paid_amount == 5000 and not r.pay_method
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert booking_services.expire_unpaid(now=r.pay_by + timedelta(minutes=1)) == 1
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED and balance_of(ps) == 5000
    body = [m for m in mail.outbox if m.to == [ps.email]][0].body
    assert "wróciła na Twoje saldo" in body
    assert services.ledger_consistent(ps)


def test_abandoned_hold_without_money_stays_silent(ps, django_capture_on_commit_callbacks):
    from django.core import mail

    PaymentSettings.objects.filter(pk=1).update(wallet_enabled=False)
    r = _checkout(ps)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert booking_services.expire_unpaid(now=r.pay_by + timedelta(minutes=1)) == 1
    assert not [m for m in mail.outbox if m.to == [ps.email]]


# --- zwroty Tpay jako zlecenia: API dopiero po zatwierdzeniu transakcji, bez automatycznego ponawiania niepewnych -------


def _calls(monkeypatch):
    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append((tid, amount)) or {"result": "success"})
    return calls


def test_refund_not_sent_when_operation_rolls_back(client, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    from apps.payments.models import TpayRefund

    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    calls = _calls(monkeypatch)
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                booking_services.cancel_by_client(r, ps)
                raise RuntimeError("błąd po zleceniu zwrotu (np. w szablonie e-maila)")
    r.refresh_from_db()
    assert calls == [] and not TpayRefund.objects.exists()  # wycofane razem z operacją — Tpay nic nie dostał
    assert r.status == Reservation.Status.CONFIRMED and services.ledger_consistent(ps)
    with django_capture_on_commit_callbacks(execute=True):  # ponowne anulowanie — dokładnie jeden zwrot
        booking_services.cancel_by_client(r, ps)
    assert calls == [("01CANC", None)] and TpayRefund.objects.get().status == TpayRefund.Status.DONE


def test_lost_refund_is_sent_by_scheduler(client, ps, tpay_keys, monkeypatch):
    from apps.payments.models import TpayRefund

    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    calls = _calls(monkeypatch)
    booking_services.cancel_by_client(r, ps)  # bez wykonania on_commit — jak restart w chwili zatwierdzenia
    ref = TpayRefund.objects.get()
    assert ref.status == TpayRefund.Status.QUEUED and calls == [] and balance_of(ps) == 0
    assert services.process_refund_queue(now=timezone.now()) == (0, 0)  # jeszcze za wcześnie
    assert services.process_refund_queue(now=timezone.now() + timedelta(minutes=3)) == (1, 0)
    assert calls == [("01CANC", None)] and TpayRefund.objects.get().status == TpayRefund.Status.DONE
    assert services.process_refund_queue(now=timezone.now() + timedelta(minutes=10)) == (0, 0)
    assert len(calls) == 1


def test_stuck_refund_goes_to_board_and_is_not_retried(client, owner, ps, tpay_keys, monkeypatch):
    from apps.core.models import Notification
    from apps.payments.models import TpayRefund

    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    calls = _calls(monkeypatch)
    booking_services.cancel_by_client(r, ps)
    TpayRefund.objects.update(status=TpayRefund.Status.SENDING, sent_at=timezone.now())  # proces przerwany w trakcie wysyłania
    assert services.process_refund_queue(now=timezone.now() + timedelta(minutes=11)) == (0, 1)
    assert calls == [] and TpayRefund.objects.get().status == TpayRefund.Status.UNKNOWN
    assert Notification.objects.filter(recipient=owner, channel="panel", title__startswith="Zwrot Tpay do sprawdzenia").count() == 1
    assert services.process_refund_queue(now=timezone.now() + timedelta(minutes=30)) == (0, 0)  # bez ponawiania i bez powtórek
    assert services.refundable_overpayments(ps) == []  # w toku — nie da się zlecić drugiego zwrotu
    # zarząd sprawdza w panelu Tpay: zwrotu nie ma → kwota wraca na saldo, można zlecić ponownie
    client.logout()
    login_with_otp(client, owner)
    page = client.get(f"/panel/platnosci/{payment.pk}/").content.decode()
    assert "Do sprawdzenia w panelu Tpay" in page and "Zwrotu nie ma" in page
    ref = TpayRefund.objects.get()
    client.post(f"/panel/platnosci/{payment.pk}/", {"action": "refund_missing", "refund": ref.pk})
    ref.refresh_from_db()
    assert ref.status == TpayRefund.Status.FAILED and ref.resolved_by == owner and balance_of(ps) == 24000
    assert services.refundable_overpayments(ps) == [(payment, 24000)] and services.ledger_consistent(ps)


def test_stuck_refund_marked_done_by_board(client, owner, ps, tpay_keys, monkeypatch):
    from apps.payments.models import TpayRefund

    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    _calls(monkeypatch)
    booking_services.cancel_by_client(r, ps)
    TpayRefund.objects.update(status=TpayRefund.Status.UNKNOWN)
    client.logout()
    login_with_otp(client, owner)
    client.post(f"/panel/platnosci/{payment.pk}/", {"action": "refund_done", "refund": TpayRefund.objects.get().pk})
    assert TpayRefund.objects.get().status == TpayRefund.Status.DONE and balance_of(ps) == 0
    assert services.refundable_overpayments(ps) == []


def test_unexpected_refund_error_is_not_retried(client, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    from apps.payments.models import TpayRefund

    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)

    def broken(tid, amount=None):
        raise ConnectionResetError("zerwane połączenie")

    monkeypatch.setattr(tpay, "refund_transaction", broken)
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.cancel_by_client(r, ps)
    assert TpayRefund.objects.get().status == TpayRefund.Status.UNKNOWN and balance_of(ps) == 0


def test_tpay_chargeback_notice_does_not_double_count_refund(client, ps, tpay_keys, monkeypatch, django_capture_on_commit_callbacks):
    r, payment = _confirmed_paid_online(client, ps, tpay_keys, monkeypatch)
    _calls(monkeypatch)
    with django_capture_on_commit_callbacks(execute=True):
        booking_services.cancel_by_client(r, ps)
    _post(client, tpay_keys, _notification(payment, status="CHARGEBACK"))
    assert balance_of(ps) == 0
    assert payment.ledger_entries.filter(kind=LedgerEntry.Kind.CHARGEBACK).count() == 1 and services.ledger_consistent(ps)
