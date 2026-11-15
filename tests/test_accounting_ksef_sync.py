"""Pobieranie faktur z KSeF 2.0: uwierzytelnienie tokenem (szyfrowanie RSA-OAEP), pobieranie przyrostowe wg daty
trwałego zapisu, kolejka, limity i błędy, panel. Wywołania API są tu zastąpione atrapami — przebieg na żywym
środowisku testowym KSeF sprawdzono osobno."""

import base64
import datetime as dt
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from django.utils.functional import empty

from apps.accounting import ksef_api, ksef_sync
from apps.accounting.models import KsefInvoice, KsefSync
from apps.core.models import AssociationSettings
from apps.credentials.models import private_storage

from .test_accounting_ksef_docs import invoice_xml

pytestmark = pytest.mark.django_db
NIP = "6260181591"


@pytest.fixture(autouse=True)
def _env(settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = tmp_path
    settings.KSEF_TOKEN, settings.KSEF_ENABLED = "token-testowy", True
    private_storage._wrapped = empty
    a = AssociationSettings.load()
    a.nip = NIP
    a.save()
    cache.delete(ksef_sync.LOCK_KEY)
    yield
    private_storage._wrapped = empty


def _state(**kw):
    s = KsefSync.load()
    s.enabled, s.sync_from = True, timezone.localdate() - timedelta(days=10)
    for k, v in kw.items():
        setattr(s, k, v)
    s.save()
    return s


def _meta(n, minutes):
    at = (timezone.now() - timedelta(days=5) + timedelta(minutes=minutes)).isoformat()
    return {"ksefNumber": n, "permanentStorageDate": at}


def ks(i):
    return f"2222222222-20260210-{i:012X}-E4"


class FakeKsef:
    def __init__(self, pages, xml=None, fail=None):
        self.pages, self.xml, self.fail = list(pages), xml or {}, fail or {}
        self.queries, self.downloads = [], []

    def install(self, monkeypatch):
        monkeypatch.setattr(ksef_api, "authenticate", lambda nip, token=None, **kw: "ACCESS")
        monkeypatch.setattr(ksef_api, "query_metadata", self.query)
        monkeypatch.setattr(ksef_api, "download_invoice", self.download)
        return self

    def query(self, access, **kw):
        assert access == "ACCESS"
        self.queries.append(kw)
        return self.pages.pop(0) if self.pages else {"invoices": [], "hasMore": False, "isTruncated": False}

    def download(self, access, number):
        self.downloads.append(number)
        if number in self.fail:
            raise self.fail[number]
        return self.xml.get(number) or invoice_xml(number=f"FV/{number[-6:]}")


def test_encrypt_token_rsa_oaep_sha256_roundtrip():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encrypted = ksef_api.encrypt_token(key.public_key(), "TOKEN-ABC", 1763198585123)
    plain = key.decrypt(base64.b64decode(encrypted),
                        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    assert plain == b"TOKEN-ABC|1763198585123"


def test_authenticate_flow_and_error_status(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    calls = []
    statuses = iter([{"code": 100}, {"code": 200}])

    def fake(method, path, *, token=None, payload=None, **kw):
        calls.append((method, path.split("?")[0], token, payload))
        if path == "/auth/challenge":
            return {"challenge": "20261115-CR-0000000000-0000000000-00", "timestampMs": 1763198585123}
        if path == "/auth/ksef-token":
            return {"referenceNumber": "REF", "authenticationToken": {"token": "AUTH"}}
        if path == "/auth/REF":
            return {"status": next(statuses)}
        if path == "/auth/token/redeem":
            return {"accessToken": {"token": "ACCESS"}, "refreshToken": {"token": "R"}}
        raise AssertionError(path)

    monkeypatch.setattr(ksef_api, "_request", fake)
    monkeypatch.setattr(ksef_api, "_token_key", lambda: ("KEY-ID", key))
    assert ksef_api.authenticate(NIP, sleep=lambda s: None) == "ACCESS"
    init = next(c for c in calls if c[1] == "/auth/ksef-token")[3]
    assert init["contextIdentifier"] == {"type": "Nip", "value": NIP} and init["publicKeyId"] == "KEY-ID"
    assert [c[2] for c in calls if c[1] in ("/auth/REF", "/auth/token/redeem")] == ["AUTH", "AUTH", "AUTH"]
    statuses = iter([{"code": 450, "description": "Uwierzytelnianie zakończone niepowodzeniem z powodu błędnego tokenu",
                      "details": ["Token unieważniony"]}])
    with pytest.raises(ksef_api.KsefError, match="Token unieważniony") as e:
        ksef_api.authenticate(NIP, sleep=lambda s: None)
    assert e.value.status == 450 and not e.value.transient


def test_incremental_sync_pagination_and_dedupe(monkeypatch, owner):
    _state()
    page1 = {"invoices": [_meta(ks(1), 1), _meta(ks(2), 2)], "hasMore": True, "isTruncated": False}
    page2 = {"invoices": [_meta(ks(3), 3)], "hasMore": False, "isTruncated": False,
             "permanentStorageHwmDate": (timezone.now() - timedelta(hours=1)).isoformat()}
    fake = FakeKsef([page1, page2]).install(monkeypatch)
    result = ksef_sync.run()
    assert result["ok"] and len(result["created"]) == 3
    assert fake.queries[0]["subject_type"] == "Subject2" and fake.queries[1]["page_offset"] == ksef_sync.PAGE_SIZE
    invoices = KsefInvoice.objects.order_by("ksef_number")
    assert [i.ksef_number for i in invoices] == [ks(1), ks(2), ks(3)] and {i.source for i in invoices} == {"api"}
    state = KsefSync.load()
    assert state.pending == [] and state.last_success_at and "nowe faktury: 3" in state.last_result
    hwm = dt.datetime.fromisoformat(state.hwm["Subject2"])
    assert abs((hwm - (timezone.now() - timedelta(hours=1))).total_seconds()) < 5  # znacznik = data potwierdzona przez KSeF
    # kolejny przebieg zaczyna od znacznika; faktury już w systemie nie trafiają do kolejki
    fake = FakeKsef([{"invoices": [_meta(ks(3), 3)], "hasMore": False, "isTruncated": False}]).install(monkeypatch)
    ksef_sync.run()
    assert dt.datetime.fromisoformat(fake.queries[0]["date_from"].isoformat()) == hwm and fake.downloads == []


def test_truncated_results_continue_from_last_date(monkeypatch):
    _state()
    first = {"invoices": [_meta(ks(1), 1), _meta(ks(2), 50)], "hasMore": True, "isTruncated": True}
    fake = FakeKsef([first, {"invoices": [], "hasMore": False, "isTruncated": False}]).install(monkeypatch)
    ksef_sync.run()
    assert fake.queries[1]["page_offset"] == 0
    assert fake.queries[1]["date_from"] == dt.datetime.fromisoformat(first["invoices"][-1]["permanentStorageDate"])


def test_rate_limit_keeps_queue_and_permanent_errors_are_dropped(monkeypatch):
    _state()
    page = {"invoices": [_meta(ks(i), i) for i in range(1, 5)], "hasMore": False, "isTruncated": False}
    fake = FakeKsef([page], fail={ks(2): ksef_api.KsefError("Nie znaleziono w KSeF", status=404),
                                  ks(3): ksef_api.KsefError("limit", status=429, transient=True)}).install(monkeypatch)
    result = ksef_sync.run()
    assert not result["ok"] and "limit" in result["message"]
    state = KsefSync.load()
    assert [p["n"] for p in state.pending] == [ks(3), ks(4)]  # przerwane na limicie — dokończy następny przebieg
    assert KsefInvoice.objects.count() == 1 and "błędne: 1" in state.last_result and state.last_error == "limit"
    fake = FakeKsef([]).install(monkeypatch)
    ksef_sync.run()
    assert fake.downloads == [ks(3), ks(4)] and KsefInvoice.objects.count() == 3 and KsefSync.load().pending == []


def test_sales_invoices_optional_and_direction(monkeypatch):
    _state(include_sales=True)
    sale = {"invoices": [_meta(ks(9), 1)], "hasMore": False, "isTruncated": False}
    fake = FakeKsef([{"invoices": [], "hasMore": False, "isTruncated": False}, sale],
                    xml={ks(9): invoice_xml(number="S/1", seller_nip=NIP, buyer_nip="3333333333")}).install(monkeypatch)
    ksef_sync.run()
    assert [q["subject_type"] for q in fake.queries] == ["Subject2", "Subject1"]
    assert KsefInvoice.objects.get().direction == KsefInvoice.Direction.SALE


def test_stopped_run_is_visible_as_last_run(monkeypatch):
    FakeKsef([]).install(monkeypatch)
    _state(sync_from=None)
    ksef_sync.run(force=True)
    s = KsefSync.load()
    assert s.last_run_at and s.last_result.startswith("nie pobrano") and "datę" in s.last_error


def test_guards(monkeypatch, settings):
    fake = FakeKsef([]).install(monkeypatch)
    _state(enabled=False)
    assert "wyłączone" in ksef_sync.run()["message"]
    _state(enabled=True, sync_from=None)
    assert "datę" in ksef_sync.run()["message"]
    _state()
    a = AssociationSettings.load()
    a.nip = ""
    a.save()
    assert "NIP" in ksef_sync.run()["message"]
    a.nip = NIP
    a.save()
    cache.add(ksef_sync.LOCK_KEY, "1", 60)
    assert "trwa" in ksef_sync.run()["message"]
    cache.delete(ksef_sync.LOCK_KEY)
    settings.KSEF_ENABLED = False
    assert "tokena" in ksef_sync.run()["message"] and fake.queries == []


def test_panel_settings_and_sync_now(owner_client, monkeypatch):
    page = owner_client.get("/panel/ksiegowosc/ksef/").content.decode()
    assert "Pobieranie z KSeF" in page and "Zapisz i pobierz teraz" in page and "token-testowy" not in page
    r = owner_client.post("/panel/ksiegowosc/ksef/pobieranie/", {"enabled": "on", "sync_from": "2026-02-01", "include_sales": ""})
    assert r.status_code == 302
    s = KsefSync.load()
    assert s.enabled and s.sync_from == dt.date(2026, 2, 1)
    s.hwm = {"Subject2": "2026-03-01T00:00:00+00:00"}
    s.save()
    owner_client.post("/panel/ksiegowosc/ksef/pobieranie/", {"enabled": "on", "sync_from": "2026-01-15"})
    assert KsefSync.load().hwm == {}  # nowy zakres — od początku
    r = owner_client.post("/panel/ksiegowosc/ksef/pobieranie/", {"enabled": "on", "sync_from": ""})
    assert KsefSync.load().sync_from == dt.date(2026, 1, 15)  # błąd walidacji — bez zmian
    FakeKsef([{"invoices": [_meta(ks(1), 1)], "hasMore": False, "isTruncated": False}]).install(monkeypatch)
    # „Zapisz i pobierz teraz” zapisuje zaznaczone ustawienia przed pobraniem (wcześniej osobny przycisk je pomijał)
    KsefSync.objects.filter(pk=1).update(enabled=False, sync_from=None)
    r = owner_client.post("/panel/ksiegowosc/ksef/pobieranie/", {"enabled": "on", "sync_from": "2026-02-01", "pobierz": "1"},
                          follow=True)
    assert "nowe faktury: 1" in r.content.decode()
    s = KsefSync.load()
    assert s.enabled and s.sync_from == dt.date(2026, 2, 1) and s.last_error == ""
    assert "z KSeF" in owner_client.get("/panel/ksiegowosc/ksef/").content.decode()
