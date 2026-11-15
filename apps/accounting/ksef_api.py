"""Klient API Krajowego Systemu e-Faktur 2.0 (https://api.ksef.mf.gov.pl/v2, test: https://api-test.ksef.mf.gov.pl/v2) —
tylko odczyt faktur. Specyfikacja: OpenAPI MF (/docs/v2/openapi.json).

Uwierzytelnienie tokenem KSeF (wygenerowanym w Aplikacji Podatnika KSeF z uprawnieniem do przeglądania faktur):
1. POST /auth/challenge → challenge i timestampMs,
2. POST /auth/ksef-token: „token|timestampMs” zaszyfrowany RSA-OAEP (SHA-256) kluczem publicznym MF
   o przeznaczeniu KsefTokenEncryption (GET /security/public-key-certificates), kontekst = NIP,
3. GET /auth/{referenceNumber} do statusu 200 (100 — w toku),
4. POST /auth/token/redeem → accessToken (Bearer w kolejnych wywołaniach).

Faktury: POST /invoices/query/metadata (filtr subjectType, zakres dat do 100 dni; limit 20 zapytań na godzinę),
GET /invoices/ksef/{ksefNumber} → XML (limit 64 na godzinę). Token KSeF jest w .env (KSEF_TOKEN) — nigdy w bazie ani w logach."""

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = 30
USER_AGENT = "StrzelnicaSatok-Ksiegowosc/1.0"
BASES = {"produkcja": "https://api.ksef.mf.gov.pl/v2", "test": "https://api-test.ksef.mf.gov.pl/v2"}
AUTH_POLL_SECONDS = 20


class KsefError(Exception):
    """Błąd komunikacji z KSeF; `transient` — warto spróbować ponownie później (limit, brak połączenia, błąd serwera)."""

    def __init__(self, message, *, status=None, transient=False):
        super().__init__(message)
        self.status, self.transient = status, transient


def base_url():
    return BASES.get(settings.KSEF_ENVIRONMENT, BASES["produkcja"])


def _request(method, path, *, token=None, payload=None, accept="application/json", raw=False):
    headers = {"Accept": accept, "User-Agent": USER_AGENT, "X-Error-Format": "problem-details"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url() + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            content = resp.read()
    except urllib.error.HTTPError as e:
        detail = " ".join(e.read().decode(errors="replace").split())[:600]
        logger.warning("KSeF %s %s → %s %s", method, path.split("?")[0], e.code, detail)
        message = {401: "KSeF odrzucił uwierzytelnienie", 403: "Token KSeF nie ma uprawnienia do przeglądania faktur",
                   404: "Nie znaleziono w KSeF", 429: "Przekroczony limit zapytań do KSeF — dokończę przy kolejnym pobieraniu"}
        raise KsefError(message.get(e.code, f"KSeF odpowiedział błędem {e.code}"), status=e.code,
                        transient=e.code == 429 or e.code >= 500) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise KsefError("Brak połączenia z KSeF", transient=True) from e
    if raw:
        return content
    try:
        return json.loads(content.decode() or "{}")
    except json.JSONDecodeError as e:
        raise KsefError("Niezrozumiała odpowiedź KSeF", transient=True) from e


def _token_key():
    from cryptography import x509

    certs = _request("GET", "/security/public-key-certificates")
    for cert in certs:
        if "KsefTokenEncryption" in (cert.get("usage") or []):
            return cert.get("publicKeyId"), x509.load_der_x509_certificate(base64.b64decode(cert["certificate"])).public_key()
    raise KsefError("KSeF nie udostępnił klucza do szyfrowania tokena")


def encrypt_token(public_key, token, timestamp_ms):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    data = f"{token}|{timestamp_ms}".encode()
    encrypted = public_key.encrypt(data, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(encrypted).decode()


def authenticate(nip, token=None, *, sleep=time.sleep):
    """Zwraca token dostępu (accessToken) dla kontekstu NIP."""
    token = token or settings.KSEF_TOKEN
    if not token:
        raise KsefError("Brak tokena KSeF (KSEF_TOKEN w pliku .env)")
    challenge = _request("POST", "/auth/challenge")
    key_id, key = _token_key()
    payload = {"challenge": challenge["challenge"], "contextIdentifier": {"type": "Nip", "value": nip},
               "encryptedToken": encrypt_token(key, token, challenge["timestampMs"])}
    if key_id:
        payload["publicKeyId"] = key_id
    init = _request("POST", "/auth/ksef-token", payload=payload)
    auth_token, reference = init["authenticationToken"]["token"], init["referenceNumber"]
    deadline = time.monotonic() + AUTH_POLL_SECONDS
    while True:
        status = _request("GET", f"/auth/{urllib.parse.quote(reference)}", token=auth_token)["status"]
        code = status.get("code")
        if code == 200:
            break
        if code != 100:
            details = "; ".join(status.get("details") or [])
            raise KsefError(f"KSeF: {status.get('description', 'uwierzytelnienie nieudane')}" + (f" ({details})" if details else ""),
                            status=code, transient=code in (500, 550))
        if time.monotonic() > deadline:
            raise KsefError("KSeF nie zakończył uwierzytelnienia na czas", transient=True)
        sleep(1)
    tokens = _request("POST", "/auth/token/redeem", token=auth_token)
    return tokens["accessToken"]["token"]


def query_metadata(access_token, *, subject_type, date_from, date_to=None, page_offset=0, page_size=100):
    """Strona metadanych faktur (sortowanie rosnąco po dacie trwałego zapisu — pobieranie przyrostowe)."""
    date_range = {"dateType": "PermanentStorage", "from": date_from.isoformat(), "restrictToPermanentStorageHwmDate": True}
    if date_to:
        date_range["to"] = date_to.isoformat()
    query = urllib.parse.urlencode({"sortOrder": "Asc", "pageOffset": page_offset, "pageSize": page_size})
    return _request("POST", f"/invoices/query/metadata?{query}", token=access_token,
                    payload={"subjectType": subject_type, "dateRange": date_range})


def download_invoice(access_token, ksef_number):
    return _request("GET", f"/invoices/ksef/{urllib.parse.quote(ksef_number)}", token=access_token, accept="application/xml", raw=True)
