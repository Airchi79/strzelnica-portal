"""Powiadomienia push w telefonie (Web Push) — bez zewnętrznych bibliotek poza `cryptography`.

- Szyfrowanie treści: RFC 8291 (aes128gcm, RFC 8188) — treść widzi tylko telefon odbiorcy, nie operator push.
- Podpis serwera: VAPID (RFC 8292), klucz EC P-256 generowany automatycznie i zapisany w bazie (PushConfig).
- Wysyłka tylko do znanych usług push (Google, Mozilla, Apple, Microsoft) — adres subskrypcji pochodzi od przeglądarki,
  więc lista dozwolonych hostów chroni serwer przed wysyłaniem żądań w dowolne miejsce (SSRF).
- Subskrypcje odrzucone przez usługę (404/410) są usuwane.
"""

import base64
import json
import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.db import connection, transaction

logger = logging.getLogger(__name__)

ALLOWED_PUSH_HOSTS = (
    "fcm.googleapis.com",             # Chrome, Edge na Androidzie, Opera
    "updates.push.services.mozilla.com",  # Firefox
    "web.push.apple.com",             # Safari / iPhone (aplikacja dodana do ekranu początkowego)
    ".push.apple.com",
    ".notify.windows.com",            # Edge na Windows
)
TIMEOUT = 5
TTL = 24 * 3600  # niedostarczone (telefon wyłączony) — ważne przez dobę
MAX_PER_USER = 10


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64u_decode(text: str) -> bytes:
    text = (text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def endpoint_allowed(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password or (parts.port not in (None, 443)):
        return False
    return any(host == h or (h.startswith(".") and host.endswith(h)) for h in ALLOWED_PUSH_HOSTS)


# --- klucze VAPID ---------------------------------------------------------------------------


def _private_key():
    from .models import PushConfig

    cfg = PushConfig.objects.filter(pk=1).first()
    if cfg is None:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        cfg, _ = PushConfig.objects.get_or_create(pk=1, defaults={"vapid_private_pem": pem})
    return serialization.load_pem_private_key(cfg.vapid_private_pem.encode(), password=None)


def _raw_public(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key_b64() -> str:
    """Klucz publiczny serwera dla przeglądarki (applicationServerKey)."""
    return b64u(_raw_public(_private_key().public_key()))


def _vapid_header(endpoint, key):
    parts = urlsplit(endpoint)
    sub = (getattr(settings, "WEBPUSH_CONTACT", "") or settings.SITE_URL or "https://localhost")
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": sub}
    signing_input = (b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "."
                     + b64u(json.dumps(claims, separators=(",", ":")).encode()))
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = signing_input + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={b64u(_raw_public(key.public_key()))}"


# --- szyfrowanie (RFC 8291) -----------------------------------------------------------------


def _hkdf(salt, ikm, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None, as_key=None) -> bytes:
    """Treść zaszyfrowana dla jednej subskrypcji (Content-Encoding: aes128gcm)."""
    ua_public = b64u_decode(p256dh)
    auth_secret = b64u_decode(auth)
    if len(ua_public) != 65 or ua_public[0] != 4 or len(auth_secret) != 16:
        raise ValueError("Nieprawidłowe klucze subskrypcji push.")
    as_key = as_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(as_key.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = payload + b"\x02"  # ostatni (jedyny) rekord, bez dopełnienia
    ciphertext = AESGCM(cek).encrypt(nonce, record, None)
    header = salt + struct.pack("!L", 4096) + bytes([len(as_public)]) + as_public
    return header + ciphertext


# --- wysyłka --------------------------------------------------------------------------------


def _post(sub, body, key):
    """Zwraca: 'ok', 'gone' (subskrypcja wygasła) albo 'error'."""
    if not endpoint_allowed(sub.endpoint):
        return "gone"
    req = urllib.request.Request(sub.endpoint, data=body, method="POST", headers={
        "Authorization": _vapid_header(sub.endpoint, key), "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream", "TTL": str(TTL), "Urgency": "normal",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return "ok" if 200 <= resp.status < 300 else "error"
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "gone"
        logger.warning("Push %s → %s %s", urlsplit(sub.endpoint).hostname, e.code, e.read()[:200])
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning("Push %s — brak połączenia: %s", urlsplit(sub.endpoint).hostname, e)
    return "error"


def _deliver(user_ids, message):
    from .models import PushSubscription

    try:
        subs = list(PushSubscription.objects.filter(user_id__in=user_ids, user__is_active=True))
        if not subs:
            return
        key = _private_key()
        gone = []
        for sub in subs:
            data = dict(message)
            unread = message.get("_unread", {}).get(sub.user_id)
            data.pop("_unread", None)
            if unread is not None:
                data["badge"] = unread
            try:
                body = encrypt(json.dumps(data, ensure_ascii=False).encode()[:3000], sub.p256dh, sub.auth)
            except ValueError:
                gone.append(sub.pk)
                continue
            result = _post(sub, body, key)
            if result == "gone":
                gone.append(sub.pk)
        if gone:
            PushSubscription.objects.filter(pk__in=gone).delete()
    except Exception:  # noqa: BLE001 — błąd push nigdy nie psuje operacji użytkownika
        logger.exception("Nie udało się wysłać powiadomień push")


def send(users, title, body="", url="", channel="portal"):
    """Wysyła push do wszystkich urządzeń podanych użytkowników — po zatwierdzeniu transakcji, w tle."""
    from .models import Notification

    user_ids = sorted({u.pk for u in users if u is not None and u.pk})
    if not user_ids:
        return
    unread = {}
    for uid in user_ids:
        unread[uid] = Notification.objects.filter(recipient_id=uid, read_at__isnull=True).count()
    message = {"title": " ".join(str(title).split())[:120], "body": " ".join(str(body).split())[:240],
               "url": url if url.startswith("/") and not url.startswith("//") else "/",
               "tag": channel, "_unread": unread}

    def run():
        if getattr(settings, "WEBPUSH_ASYNC", True):
            def worker():
                try:
                    _deliver(user_ids, message)
                finally:
                    connection.close()  # połączenie z bazą tego wątku
            threading.Thread(target=worker, name="webpush", daemon=False).start()
        else:
            _deliver(user_ids, message)

    transaction.on_commit(run)
