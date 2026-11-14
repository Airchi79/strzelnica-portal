"""Płatności online przez Axepta BNP Paribas: link płatności, podpis i obsługa powiadomień, powrót z bramki, zwroty."""

import hashlib
import json

import pytest
from django.core.cache import cache

from apps.payments import axepta, gateway, services
from apps.payments.models import LedgerEntry, Payment, PaymentSettings, TpayRefund

from .test_payments import book, ps  # noqa: F401

pytestmark = pytest.mark.django_db

MERCHANT, SERVICE, KEY = "6yt3gjtm9p7b8h9xsdqz", "62f574ed-d4ad-4a7e-9981-89ed7284aaba", "klucz-sklepu"


@pytest.fixture(autouse=True)
def axepta_on(settings, db):
    settings.ONLINE_PAYMENTS_PROVIDER = "axepta"
    settings.AXEPTA_MERCHANT_ID, settings.AXEPTA_SERVICE_ID = MERCHANT, SERVICE
    settings.AXEPTA_API_TOKEN, settings.AXEPTA_SERVICE_KEY = "token-api", KEY
    settings.AXEPTA_ENABLED, settings.AXEPTA_SANDBOX = True, True
    cfg = PaymentSettings.load()
    cfg.wallet_enabled = True
    cfg.save()
    cache.delete("axepta-refund-slot")


@pytest.fixture
def api(monkeypatch):
    """Atrapa API Axepty: zapisuje żądania, zwraca odpowiedzi jak w dokumentacji."""
    calls = []
    state = {"payment": {}}

    def fake(method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/payment-link":
            return {"paymentLink": {"paymentId": "pay-1", "url": "https://paywall.sandbox.axepta.pl/pay/pay-1"}}
        if path.endswith("/refund"):
            return {"paymentRefundId": "ref-1", "status": "pending", "amount": payload["amount"],
                    "transactions": [{"transaction": {"id": f"tx-ref-{len(calls)}", "type": "refund", "status": "new"}}]}
        if method == "GET" and path.startswith("/payment/"):
            return {"payment": state["payment"]}
        return {}

    monkeypatch.setattr(axepta, "_request", fake)
    return calls, state


def signed(body: dict, key=KEY, merchant=MERCHANT, service=SERVICE, alg="sha256"):
    raw = json.dumps(body).encode()
    sig = hashlib.sha256(raw + key.encode()).hexdigest()
    return raw, f"merchantid={merchant};serviceid={service};signature={sig};alg={alg}"


def notify(client, body, **kw):
    raw, header = signed(body, **kw)
    return client.post("/platnosci/axepta/powiadomienie/", raw, content_type="application/json", HTTP_X_AXEPTA_SIGNATURE=header)


def axepta_payment(user, amount=5000, status="new"):
    p = Payment.objects.create(user=user, amount=amount, method=Payment.Method.TPAY, external_id="axepta:pay-1",
                               raw={"sandbox": True}, title="test")
    return p


def payload(p, status="settled", paid=None, **extra):
    return {"payment": {"id": "pay-1", "orderId": str(p.pk), "amount": p.amount, "amountPaid": p.amount if paid is None else paid,
                        "currency": "PLN", "status": status, "serviceId": SERVICE, "transactions": [], **extra}}


def test_text_cleaning_for_axepta_fields():
    assert axepta.clean_text("Strzelnica Sątok — rezerwacja SATOK-R-1 😀") == "Strzelnica Sątok rezerwacja SATOK-R-1"
    assert axepta.clean_order_id("1b2c-3d4e/5") == "1b2c-3d4e/5"


def test_signature_verification(settings):
    raw, header = signed({"payment": {"id": "x"}})
    assert axepta.verify_notification(header, raw)
    assert axepta.verify_notification(header.upper().replace("ALG=SHA256", "alg=sha256").replace("MERCHANTID", "merchantid")
                                      .replace("SERVICEID", "serviceid").replace("SIGNATURE", "signature")
                                      .replace(MERCHANT.upper(), MERCHANT).replace(SERVICE.upper(), SERVICE), raw)
    assert not axepta.verify_notification(header, raw + b" ")  # zmieniona treść
    assert not axepta.verify_notification(signed({"payment": {"id": "x"}}, key="inny")[1], raw)
    assert not axepta.verify_notification(signed({"payment": {"id": "x"}}, merchant="obcy")[1], raw)
    assert not axepta.verify_notification(signed({"payment": {"id": "x"}}, alg="md5")[1], raw)
    assert not axepta.verify_notification("", raw)


def test_start_creates_payment_link_and_redirects(client, ps, api):  # noqa: F811
    calls, _ = api
    client.force_login(ps)
    r = client.post("/platnosci/tpay/start/", {"amount": "50"})
    assert r.status_code == 302 and r.url == "https://paywall.sandbox.axepta.pl/pay/pay-1"
    p = Payment.objects.get()
    assert p.external_id == "axepta:pay-1" and p.raw["sandbox"] is True and p.is_test and p.provider_name == "Axepta BNP Paribas"
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/payment-link")
    assert body["amount"] == 5000 and body["currency"] == "PLN" and body["orderId"] == str(p.pk) and body["serviceId"] == SERVICE
    assert body["customer"] == {"firstName": "Piotr", "lastName": "Prowadzący", "email": "ps@example.com"}
    assert body["successReturnUrl"].endswith(f"/platnosci/tpay/powrot/{p.pk}/?wynik=ok") and body["activeTo"] > 0
    assert "—" not in body["title"]


def test_gateway_error_marks_failed(client, ps, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise axepta.AxeptaError("x")

    monkeypatch.setattr(axepta, "_request", boom)
    client.force_login(ps)
    client.post("/platnosci/tpay/start/", {"amount": "50"})
    assert Payment.objects.get().status == Payment.Status.FAILED


def test_notification_settled_books_payment_once(client, ps):  # noqa: F811
    p = axepta_payment(ps)
    r = notify(client, payload(p))
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    p.refresh_from_db()
    assert p.status == Payment.Status.COMPLETED and services.balance_of(ps) == 5000
    notify(client, payload(p))  # powtórzone powiadomienie — bez podwójnego księgowania
    assert services.balance_of(ps) == 5000 and LedgerEntry.objects.filter(payment=p, kind=LedgerEntry.Kind.DEPOSIT).count() == 1


def test_notification_bad_signature_rejected(client, ps):  # noqa: F811
    p = axepta_payment(ps)
    r = notify(client, payload(p), key="zly-klucz")
    assert r.status_code == 400
    p.refresh_from_db()
    assert p.status == Payment.Status.PENDING and services.balance_of(ps) == 0


def test_notification_amount_mismatch_and_rejected_and_unknown(client, ps):  # noqa: F811
    p = axepta_payment(ps)
    notify(client, payload(p, paid=100))
    p.refresh_from_db()
    assert p.status == Payment.Status.UNMATCHED and "różni się" in p.note and services.balance_of(ps) == 0
    p2 = Payment.objects.create(user=ps, amount=3000, method=Payment.Method.TPAY, external_id="axepta:pay-2")
    body = payload(p2, status="rejected")
    body["payment"]["id"] = "pay-2"
    notify(client, body)
    p2.refresh_from_db()
    assert p2.status == Payment.Status.FAILED
    unknown = {"payment": {"id": "nie-ma", "orderId": "x", "status": "settled"}}
    assert notify(client, unknown).json() == {"status": "ok"}  # nieznana płatność — koniec ponawiania, bez zmian


def test_order_id_must_match(client, ps):  # noqa: F811
    p = axepta_payment(ps)
    body = payload(p)
    body["payment"]["orderId"] = "inne-zamowienie"
    notify(client, body)
    p.refresh_from_db()
    assert p.status == Payment.Status.UNMATCHED


def test_return_page_checks_status_in_api(client, ps, api):  # noqa: F811
    _, state = api
    p = axepta_payment(ps)
    state["payment"] = payload(p)["payment"]
    client.force_login(ps)
    assert client.get(f"/platnosci/tpay/powrot/{p.pk}/?wynik=ok").status_code == 200
    p.refresh_from_db()
    assert p.status == Payment.Status.COMPLETED


def test_refunds_via_api_with_spacing_and_failed_refund_notice(client, ps, api, owner, django_capture_on_commit_callbacks):  # noqa: F811
    calls, _ = api
    p1, p2 = axepta_payment(ps), Payment.objects.create(user=ps, amount=3000, method=Payment.Method.TPAY, external_id="axepta:pay-2")
    services.complete_payment(p1)
    services.complete_payment(p2)
    p1.refresh_from_db()
    p2.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        assert services.refund_to_payer(p1, 2000, by=owner, reason="Anulowanie")
        assert services.refund_to_payer(p2, 3000, by=owner, reason="Anulowanie")
    refunds = [c for c in calls if c[1].endswith("/refund")]
    assert refunds == [("POST", "/payment/pay-1/refund", {"type": "refund", "serviceId": SERVICE, "amount": 2000})]
    r1 = TpayRefund.objects.get(payment=p1)
    r2 = TpayRefund.objects.get(payment=p2)
    assert r1.status == TpayRefund.Status.DONE and r1.response["transactionId"]
    assert r2.status == TpayRefund.Status.QUEUED  # odstęp 5 s — wyśle harmonogram
    cache.delete("axepta-refund-slot")
    from datetime import timedelta

    from django.utils import timezone

    services.process_refund_queue(now=timezone.now() + timedelta(minutes=3))
    r2.refresh_from_db()
    assert r2.status == TpayRefund.Status.DONE
    # Axepta zgłasza w powiadomieniu, że zwrot z p1 się nie udał → kwota wraca na saldo
    before = services.balance_of(ps)
    tx = r1.response["transactionId"]
    body = payload(p1, transactions=[{"id": tx, "type": "refund", "status": "rejected"}], notifyTransactionData={"id": tx, "type": "refund"})
    with django_capture_on_commit_callbacks(execute=True):
        notify(client, body)
    r1.refresh_from_db()
    assert r1.status == TpayRefund.Status.FAILED and services.balance_of(ps) == before + 2000


def test_provider_of_and_disabled_endpoint(client, settings):
    p = Payment(method=Payment.Method.TPAY, external_id="tpay:01A")
    assert gateway.provider_of(p) == "tpay" and gateway.provider_of(Payment(external_id="bank:1")) is None
    settings.AXEPTA_ENABLED = False
    assert client.post("/platnosci/axepta/powiadomienie/", b"{}", content_type="application/json").status_code == 404


def test_panel_list_shows_refund_and_filters(owner_client, ps, api, owner, django_capture_on_commit_callbacks):  # noqa: F811
    client = owner_client
    p1 = axepta_payment(ps)
    p2 = Payment.objects.create(user=ps, amount=3000, method=Payment.Method.TPAY, external_id="axepta:pay-2", title="bez zwrotu")
    services.complete_payment(p1)
    services.complete_payment(p2)
    p1.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        assert services.refund_to_payer(p1, 2000, by=owner, reason="Anulowanie")
    page = client.get("/panel/platnosci/").content.decode()
    assert page.count("zwrot 20\xa0zł") == 1 and "w toku" not in page  # tylko wpłata ze zwrotem, zwrot przyjęty
    only = client.get("/panel/platnosci/?zwrot=zwrocone").content.decode()
    assert "zwrot 20\xa0zł" in only and "bez zwrotu" not in only
    assert "zwrot 20\xa0zł" not in client.get("/panel/platnosci/?zwrot=do_sprawdzenia").content.decode()
    TpayRefund.objects.filter(payment=p1).update(status=TpayRefund.Status.UNKNOWN)
    pending = client.get("/panel/platnosci/?zwrot=do_sprawdzenia").content.decode()
    assert "zwrot 20\xa0zł do sprawdzenia" in pending and "bez zwrotu" not in pending
    detail = client.get(f"/panel/platnosci/{p1.pk}/").content.decode()
    assert "Do sprawdzenia w panelu operatora płatności" in detail and "Tpay" not in detail
