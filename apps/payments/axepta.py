"""Integracja z Axepta BNP Paribas (RESTful API, https://api.axepta.pl/v1, sandbox: https://api.sandbox.axepta.pl/v1).

Dokumentacja: https://bump.sh/pgw/doc/axepta-api (wersja 1.0.3) oraz „Dokumentacja techniczna integracji z systemem
transakcyjnym Axepta” (bnpparibas.pl/axepta).

- Płatność: „link płatności” (POST /merchant/{merchantId}/payment-link) — strzelec wybiera na stronie Axepty BLIK,
  szybki przelew, kartę albo Google Pay / Apple Pay. Dane kart nie trafiają do naszego systemu (PCI DSS).
- Autoryzacja: nagłówek `Authorization: Bearer <token>` (panel Axepta → Ustawienia → Klucze API).
- Powiadomienie: POST JSON na adres ustawiony w panelu Axepta; nagłówek
  `X-Axepta-Signature: merchantid=…;serviceid=…;signature=…;alg=sha256`, gdzie
  signature = sha256(surowa treść żądania + klucz sklepu) zapisany szesnastkowo. Odpowiedź 200 z {"status":"ok"}
  kończy ponawianie (cykl: 3×1 s, 5×5 min, 5×60 min, 5×6 h, 5×12 h).
- Zwrot: POST /merchant/{merchantId}/payment/{paymentId}/refund (kwota w groszach); kolejne zwroty co najmniej 5 s.
- Kwoty zawsze w groszach (integer).
"""

import hashlib
import hmac
import json
import logging
import re
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.core.cache import cache

from .gateway import GatewayError

logger = logging.getLogger(__name__)

TIMEOUT = 20
USER_AGENT = "StrzelnicaSatok-Rezerwacje/1.0"
REFUND_SPACING_SECONDS = 5
# Adresy, z których Axepta wysyła powiadomienia (informacyjnie — o autentyczności decyduje podpis).
NOTIFY_NETWORKS = ("141.95.10.64/28", "51.83.82.16/28")
FINAL_OK = "settled"
FINAL_FAILED = {"rejected", "error", "cancelled"}

# Dozwolone znaki w polach tekstowych (wg dokumentacji): litery łacińskie, cyfry, wybrane znaki i zakres U+00C0–U+02C0
# (m.in. polskie litery). Inne znaki (np. „—”, emotikony) zastępujemy spacją, żeby Axepta nie odrzuciła żądania.
_TEXT_EXTRA = "#&_-\"',./ "
_ORDER_EXTRA = "#_-./ "
_CID_RE = re.compile(r"[^A-Za-z0-9-]")


class AxeptaError(GatewayError):
    pass


def api_base():
    return "https://api.sandbox.axepta.pl/v1" if settings.AXEPTA_SANDBOX else "https://api.axepta.pl/v1"


def _clean(text, extra, limit):
    out = []
    for ch in str(text or ""):
        if ch.isascii() and (ch.isalnum() or ch in extra):
            out.append(ch)
        elif 0x00C0 <= ord(ch) <= 0x02C0:
            out.append(ch)
        else:
            out.append(" ")
    return " ".join("".join(out).split())[:limit]


def clean_text(text, limit=255):
    return _clean(text, _TEXT_EXTRA, limit)


def clean_order_id(text):
    return _clean(text, _ORDER_EXTRA, 100)


def _request(method, path, payload=None):
    url = f"{api_base()}/merchant/{settings.AXEPTA_MERCHANT_ID}{path}"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {settings.AXEPTA_API_TOKEN}", "User-Agent": USER_AGENT}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = " ".join(e.read().decode(errors="replace").split())[:800]
        logger.warning("Axepta %s %s → %s %s", method, path, e.code, detail)
        raise AxeptaError(f"Axepta odpowiedziała błędem {e.code}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise AxeptaError("Brak połączenia z Axeptą") from e
    if data.get("status") not in (None, "SUCCESS"):
        logger.warning("Axepta %s %s → %s", method, path, json.dumps(data, ensure_ascii=False)[:800])
        raise AxeptaError("Axepta odrzuciła żądanie")
    return data.get("data") or {}


def create_payment_link(*, amount_grosze, order_id, title, first_name, last_name, email, phone="", success_url, failure_url,
                        return_url, active_to=None):
    """Nowy link płatności. Zwraca {"payment_id", "url"}."""
    if amount_grosze <= 0:
        raise AxeptaError("Kwota płatności musi być dodatnia")
    customer = {
        "firstName": clean_text(first_name, 100) or "Strzelec",
        "lastName": clean_text(last_name, 100) or "Strzelnica",
        "email": str(email or "")[:200],
    }
    phone = re.sub(r"[^0-9+\- ]", "", str(phone or ""))[:20].strip()
    if phone:
        customer["phone"] = phone
    payload = {
        "serviceId": settings.AXEPTA_SERVICE_ID,
        "amount": int(amount_grosze),
        "currency": "PLN",
        "orderId": clean_order_id(order_id),
        "title": clean_text(title, 255),
        "successReturnUrl": success_url[:300],
        "failureReturnUrl": failure_url[:300],
        "returnUrl": return_url[:300],
        "customer": customer,
    }
    if active_to:
        payload["activeTo"] = int(active_to)
    data = _request("POST", "/payment-link", payload)
    link = data.get("paymentLink") or {}
    if not link.get("paymentId") or not link.get("url"):
        raise AxeptaError("Axepta nie utworzyła linku płatności")
    return {"payment_id": link["paymentId"], "url": link["url"]}


def get_payment(payment_id):
    """Stan płatności (linku) — {"status", "amount", "amountPaid", "amountRefunded", …}."""
    return (_request("GET", f"/payment/{payment_id}") or {}).get("payment") or {}


def cancel_payment(payment_id):
    return _request("POST", "/payment/cancel", {"serviceId": settings.AXEPTA_SERVICE_ID, "paymentId": payment_id})


def refund_payment(payment_id, amount_grosze):
    """Zwrot (całości albo części) — kwota zawsze podana w groszach. Odstęp między zwrotami ≥ 5 s (wymóg Axepty)."""
    if not payment_id:
        raise AxeptaError("Brak identyfikatora płatności Axepta")
    data = _request("POST", f"/payment/{payment_id}/refund",
                    {"type": "refund", "serviceId": settings.AXEPTA_SERVICE_ID, "amount": int(amount_grosze)})
    logger.warning("Axepta zwrot %s (%s gr) → %s", payment_id, amount_grosze, json.dumps(data, ensure_ascii=False)[:500])
    return data


def take_refund_slot(wait=False, sleep=time.sleep):
    """Odstęp ≥ 5 s między kolejnymi zwrotami (wymóg Axepty; wspólny licznik dla wszystkich procesów).
    Bez czekania (żądanie użytkownika): False, gdy zwrot był przed chwilą — zostaje w kolejce dla harmonogramu.
    Z czekaniem (harmonogram): czeka na wolne miejsce."""
    deadline = time.monotonic() + (REFUND_SPACING_SECONDS + 1 if wait else 0)
    while True:
        if cache.add("axepta-refund-slot", 1, REFUND_SPACING_SECONDS):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.5)


# --- powiadomienia ----------------------------------------------------------------------------


def parse_signature_header(value):
    parts = {}
    for item in str(value or "").split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip().lower()] = v.strip()
    return parts


def compute_signature(body: bytes, key: str):
    return hashlib.sha256(body + key.encode()).hexdigest()


def verify_notification(signature_header, body: bytes):
    """Podpis powiadomienia: sha256(surowa treść + klucz sklepu); zgodne merchantid/serviceid; tylko alg=sha256.
    Zwraca True/False (bez wyjątków)."""
    parts = parse_signature_header(signature_header)
    if parts.get("alg", "").lower() != "sha256" or not parts.get("signature") or not settings.AXEPTA_SERVICE_KEY:
        return False
    if parts.get("merchantid") != settings.AXEPTA_MERCHANT_ID or parts.get("serviceid") != settings.AXEPTA_SERVICE_ID:
        logger.warning("Powiadomienie Axepta dla innego sklepu: merchantid=%s serviceid=%s", parts.get("merchantid"), parts.get("serviceid"))
        return False
    expected = compute_signature(body, settings.AXEPTA_SERVICE_KEY)
    return hmac.compare_digest(expected, parts["signature"].lower())
