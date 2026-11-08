"""Powiadomienia push: szyfrowanie RFC 8291, podpis VAPID, subskrypcje, wysyłka i sprzątanie."""

import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from apps.core import webpush
from apps.core.models import PushSubscription
from apps.core.notifications import notify_board, notify_user

from .conftest import make_user

pytestmark = pytest.mark.django_db

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def _receiver():
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    return key, webpush.b64u(pub), webpush.b64u(auth), auth


def test_encryption_matches_reference_implementation():
    http_ece = pytest.importorskip("http_ece")
    key, p256dh, auth_b64, auth = _receiver()
    body = webpush.encrypt("Rezerwacja potwierdzona — Oś 25 m".encode(), p256dh, auth_b64)
    plain = http_ece.decrypt(body, private_key=key, auth_secret=auth, version="aes128gcm")
    assert plain.decode() == "Rezerwacja potwierdzona — Oś 25 m"


def test_vapid_header_signature_valid(db):
    header = webpush._vapid_header(ENDPOINT, webpush._private_key())
    t = header.split("t=")[1].split(",")[0]
    k = header.split("k=")[1]
    head, claims, sig = t.split(".")
    assert json.loads(webpush.b64u_decode(claims))["aud"] == "https://fcm.googleapis.com"
    assert k == webpush.public_key_b64()  # ten sam klucz przy kolejnym użyciu (zapisany w bazie)
    raw = webpush.b64u_decode(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.b64u_decode(k))
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # wyjątek = zły podpis


@pytest.mark.parametrize("url,ok", [
    (ENDPOINT, True),
    ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
    ("https://web.push.apple.com/QG", True),
    ("https://wns2-db5p.notify.windows.com/w/?token=x", True),
    ("http://fcm.googleapis.com/x", False),
    ("https://evil.example.com/x", False),
    ("https://fcm.googleapis.com.evil.com/x", False),
    ("https://user:pw@fcm.googleapis.com/x", False),
    ("https://fcm.googleapis.com:8443/x", False),
    ("https://127.0.0.1/x", False),
])
def test_endpoint_allowlist(url, ok):
    assert webpush.endpoint_allowed(url) is ok


def _subscribe(client, endpoint=ENDPOINT):
    key, p256dh, auth_b64, _ = _receiver()
    r = client.post("/powiadomienia/push/", json.dumps({"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth_b64}}),
                    content_type="application/json")
    return r, key


def test_subscribe_view(client):
    u = make_user("push@example.com")
    assert client.get("/powiadomienia/push/").status_code == 302  # wymaga logowania
    client.force_login(u)
    assert len(webpush.b64u_decode(client.get("/powiadomienia/push/").json()["key"])) == 65
    r, _ = _subscribe(client, "https://evil.example.com/x")
    assert r.status_code == 400 and not PushSubscription.objects.exists()
    r, _ = _subscribe(client)
    assert r.json()["ok"] and PushSubscription.objects.get().user == u
    # to samo urządzenie, inne konto — subskrypcja przechodzi na nowe konto
    other = make_user("push2@example.com")
    client.force_login(other)
    _subscribe(client)
    assert PushSubscription.objects.get().user == other
    client.post("/powiadomienia/push/", json.dumps({"action": "remove", "endpoint": ENDPOINT}), content_type="application/json")
    assert not PushSubscription.objects.exists()


def test_page_shows_push_box(client):
    u = make_user("box@example.com")
    client.force_login(u)
    body = client.get("/powiadomienia/").content.decode()
    assert "data-push=" in body and "push.js" in body


class _Resp:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_notify_sends_encrypted_push_and_removes_gone(monkeypatch, django_capture_on_commit_callbacks, owner):
    http_ece = pytest.importorskip("http_ece")
    u = make_user("telefon@example.com")
    key, p256dh, auth_b64, auth = _receiver()
    PushSubscription.objects.create(user=u, endpoint=ENDPOINT, p256dh=p256dh, auth=auth_b64)
    PushSubscription.objects.create(user=u, endpoint="https://web.push.apple.com/stary", p256dh=p256dh, auth=auth_b64)
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        if "stary" in req.full_url:
            import urllib.error

            raise urllib.error.HTTPError(req.full_url, 410, "Gone", {}, None)
        return _Resp()

    monkeypatch.setattr(webpush.urllib.request, "urlopen", fake_urlopen)
    with django_capture_on_commit_callbacks(execute=True):
        notify_user(u, "Rezerwacja SATOK-R-1 potwierdzona", "emails/welcome.txt", link="/rezerwacje/5/")
    assert len(sent) == 2
    req = next(r for r in sent if r.full_url == ENDPOINT)
    assert req.headers["Content-encoding"] == "aes128gcm" and req.headers["Authorization"].startswith("vapid t=")
    msg = json.loads(http_ece.decrypt(req.data, private_key=key, auth_secret=auth, version="aes128gcm"))
    assert msg["title"] == "Rezerwacja SATOK-R-1 potwierdzona" and msg["url"] == "/rezerwacje/5/" and msg["badge"] == 1
    assert list(PushSubscription.objects.values_list("endpoint", flat=True)) == [ENDPOINT]  # 410 — usunięta
    # zarząd: powiadomienie panelu też idzie push
    PushSubscription.objects.create(user=owner, endpoint=ENDPOINT + "x", p256dh=p256dh, auth=auth_b64)
    sent.clear()
    with django_capture_on_commit_callbacks(execute=True):
        notify_board("Nowa usterka", "emails/welcome.txt", url="/panel/usterki/", body="Oś 25 m")
    assert [r.full_url for r in sent] == [ENDPOINT + "x"]


def test_push_failure_never_breaks_operation(monkeypatch, django_capture_on_commit_callbacks):
    u = make_user("blad@example.com")
    _, p256dh, auth_b64, _ = _receiver()
    PushSubscription.objects.create(user=u, endpoint=ENDPOINT, p256dh=p256dh, auth=auth_b64)

    def boom(*a, **k):
        raise OSError("brak sieci")

    monkeypatch.setattr(webpush.urllib.request, "urlopen", boom)
    with django_capture_on_commit_callbacks(execute=True):
        notify_user(u, "Test", "emails/welcome.txt")
    assert PushSubscription.objects.count() == 1  # błąd sieci — subskrypcja zostaje


def test_gdpr_anonymize_removes_subscriptions(owner):
    from apps.accounts.gdpr import anonymize

    u = make_user("rodo@example.com")
    _, p256dh, auth_b64, _ = _receiver()
    PushSubscription.objects.create(user=u, endpoint=ENDPOINT, p256dh=p256dh, auth=auth_b64)
    u.is_active = False
    u.save(update_fields=["is_active"])
    anonymize(u, owner)
    assert not PushSubscription.objects.exists()


def test_send_test_action(client, monkeypatch):
    u = make_user("testpush@example.com")
    client.force_login(u)
    _, p256dh, auth_b64, _ = _receiver()
    PushSubscription.objects.create(user=u, endpoint=ENDPOINT, p256dh=p256dh, auth=auth_b64)
    monkeypatch.setattr(webpush.urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    r = client.post("/powiadomienia/push/", json.dumps({"action": "test"}), content_type="application/json").json()
    assert r == {"ok": True, "sent": 1, "gone": 0, "errors": []}

    import urllib.error

    def err(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(webpush.urllib.request, "urlopen", err)
    r = client.post("/powiadomienia/push/", json.dumps({"action": "test"}), content_type="application/json").json()
    assert not r["ok"] and r["errors"] == ["fcm.googleapis.com: błąd 403"]
