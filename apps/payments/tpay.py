"""Integracja z Tpay Open API: tworzenie transakcji i weryfikacja powiadomień.

Dokumentacja: https://docs-api.tpay.com — powiadomienie (POST x-www-form-urlencoded) weryfikujemy dwukrotnie:
1) podpis JWS z nagłówka X-JWS-Signature (RS256, payload odłączony, certyfikat z `x5u` podpisany przez CA Tpay),
2) md5sum = md5(id + tr_id + tr_amount + tr_crc + kod_bezpieczeństwa).
Odpowiedź „TRUE” kończy ponawianie powiadomień.
"""

import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
import urllib.request
from decimal import Decimal

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from django.conf import settings
from django.core.cache import cache

from .gateway import GatewayError

logger = logging.getLogger(__name__)

TIMEOUT = 20
# Własny identyfikator klienta HTTP: Cloudflare przed API Tpay odrzuca domyślny „Python-urllib” (błąd 1010).
USER_AGENT = "StrzelnicaSatok-Rezerwacje/1.0 (+https://github.com/Airchi79/strzelnica-portal)"


class TpayError(GatewayError):
    pass


def api_base():
    return "https://openapi.sandbox.tpay.com" if settings.TPAY_SANDBOX else "https://api.tpay.com"


def secure_base():
    return "https://secure.sandbox.tpay.com" if settings.TPAY_SANDBOX else "https://secure.tpay.com"


def _request(method, url, *, data=None, json_body=None, token=None):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = " ".join(e.read().decode(errors="replace").split())[:800]  # jedna linia w logu
        logger.warning("Tpay %s %s → %s %s", method, url, e.code, detail)
        raise TpayError(f"Tpay odpowiedział błędem {e.code}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise TpayError("Brak połączenia z Tpay") from e


def access_token():
    key = f"tpay-token:{settings.TPAY_SANDBOX}:{settings.TPAY_CLIENT_ID}"
    token = cache.get(key)
    if token:
        return token
    data = _request(
        "POST", f"{api_base()}/oauth/auth", data={"client_id": settings.TPAY_CLIENT_ID, "client_secret": settings.TPAY_CLIENT_SECRET}
    )
    token = data.get("access_token")
    if not token:
        raise TpayError("Tpay nie zwrócił tokenu dostępu")
    cache.set(key, token, max(60, int(data.get("expires_in", 3600)) - 120))
    return token


def create_transaction(*, amount_grosze, description, hidden_description, payer_email, payer_name, notification_url, success_url, error_url):
    payload = {
        "amount": float(Decimal(amount_grosze) / 100),
        "description": description[:128],
        "hiddenDescription": hidden_description,
        "lang": "pl",
        "payer": {"email": payer_email, "name": payer_name[:255]},
        "callbacks": {
            "notification": {"url": notification_url},
            "payerUrls": {"success": success_url, "error": error_url},
        },
    }
    data = _request("POST", f"{api_base()}/transactions", json_body=payload, token=access_token())
    url = data.get("transactionPaymentUrl")
    if data.get("result") not in (None, "success") or not url:
        raise TpayError("Tpay nie utworzył transakcji")
    return {"transaction_id": data.get("transactionId", ""), "title": data.get("title", ""), "payment_url": url}


def refund_transaction(transaction_id, amount_grosze=None):
    """Zwrot (całości albo części) zaksięgowanej transakcji na rachunek/kartę płacącego — Open API: POST /transactions/{id}/refunds."""
    if not transaction_id:
        raise TpayError("Brak identyfikatora transakcji Tpay")
    payload = {} if amount_grosze is None else {"amount": float(Decimal(amount_grosze) / 100)}
    data = _request("POST", f"{api_base()}/transactions/{transaction_id}/refunds", json_body=payload, token=access_token())
    logger.warning("Tpay zwrot %s (%s) → %s", transaction_id, "całość" if amount_grosze is None else f"{amount_grosze} gr",
                   json.dumps(data, ensure_ascii=False)[:500])
    if data.get("result") not in (None, "success"):
        raise TpayError("Tpay odrzucił zwrot")
    return data


# --- weryfikacja powiadomień ----------------------------------------------------------------


def _b64url_decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _fetch_cert(url):
    key = "tpay-cert:" + hashlib.sha256(url.encode()).hexdigest()
    pem = cache.get(key)
    if pem is None:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            pem = resp.read().decode()
        cache.set(key, pem, 24 * 3600)
    return x509.load_pem_x509_certificate(pem.encode())


def verify_jws(signature_header, body: bytes, *, cert_loader=_fetch_cert):
    """Weryfikacja podpisu JWS powiadomienia. Zwraca True/False (bez wyjątków)."""
    try:
        header_b64, payload_b64, sig_b64 = signature_header.split(".")
        if payload_b64:
            return False  # Tpay wysyła podpis z odłączonym payloadem
        header = json.loads(_b64url_decode(header_b64))
        x5u = header.get("x5u", "")
        if header.get("alg") != "RS256" or not x5u.startswith(secure_base() + "/"):
            return False
        cert = cert_loader(x5u)
        root = cert_loader(f"{secure_base()}/x509/tpay-jws-root.pem")
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
            return False
        root.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
        signing_input = f"{header_b64}.{_b64url_encode(body)}".encode()
        cert.public_key().verify(_b64url_decode(sig_b64), signing_input, padding.PKCS1v15(), hashes.SHA256())
        return True
    except (ValueError, KeyError, InvalidSignature, json.JSONDecodeError, OSError, TypeError) as e:
        logger.warning("Odrzucono podpis powiadomienia Tpay: %s", e)
        return False


def verify_md5(fields):
    expected = hashlib.md5(
        (fields.get("id", "") + fields.get("tr_id", "") + fields.get("tr_amount", "") + fields.get("tr_crc", "") + settings.TPAY_SECURITY_CODE).encode()
    ).hexdigest()
    return hmac.compare_digest(expected, fields.get("md5sum", ""))
