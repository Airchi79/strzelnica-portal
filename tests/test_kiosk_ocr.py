"""Automatyczne porównanie danych z legitymacją PS (kiosk, etap 5). Teksty wzorowane na legitymacjach PZSS, LOK i PZŁ
(fikcyjne dane, PESEL zastąpiony zerami)."""

from datetime import timedelta
from io import BytesIO

import pytest
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

from apps.bookings import services as bookings
from apps.bookings.models import BookingSettings, Reservation
from apps.core.models import OutgoingEmail
from apps.credentials.models import Credential
from apps.facilities.models import Lane
from apps.kiosk import ocr, onsite
from apps.kiosk.models import OnsiteBooking

from .test_kiosk_onsite import last_code, photo_data_url  # noqa: F401 — wspólne pomocnicze

pytestmark = pytest.mark.django_db

PZSS = """o: POLSKI ZWIĄZEK
/4 STRZELECTWA SPORTOWEGO
LEGITYMACJA PROWADZĄCEGO STRZELANIE
nr 98655/2024
KOWALSKI Janusz
00000000000
Warszawa, 10.10.2024"""

LOK = """|--LIGA OBRONY KRAJU Gl
PROWADZACY STRZELANIE . LOK
Nr uprawnienia _ 0079/BZGL0K/2022 .
Imię i nazwisko Anna Nowak
Numer PESEL 00000000000
Prowadzący kurs PISTOL RANGE 209/2021
11.01.2022 r. Ok
- Data wydania ppłk ( r ) mgr Andrzej Zuba"""


def test_compare_pzss_match_and_ocr_noise():
    assert ocr.compare(PZSS, "Janusz", "Kowalski", "98655/2024", "PZSS (Polski Związek Strzelectwa Sportowego)") == []
    assert ocr.compare(PZSS, "janusz", "KOWALSKI", "98655 / 2024", "PZSS (x)") == []  # wielkość liter, spacje
    no_diacritics = PZSS.replace("Ą", "A").replace("Ł", "L")
    assert ocr.compare(no_diacritics, "Janusz", "Kowalski", "98655/2024", "PZSS (x)") == []


def test_compare_rejects_wrong_data():
    assert ocr.compare(PZSS, "Janusz", "Kowalski", "98656/2024", "PZSS (x)") == ["numer"]  # inna cyfra = inny numer
    assert ocr.compare(PZSS, "Janusz", "Kowalska", "98655/2024", "PZSS (x)") == ["imię i nazwisko"]
    assert ocr.compare(PZSS, "Jan", "Kowalski", "98655/2024", "PZSS (x)") == ["imię i nazwisko"]
    assert ocr.compare(PZSS, "Janusz", "Kowalski", "98655/2024", "LOK (Liga Obrony Kraju)") == ["wystawca"]
    assert "numer" in ocr.compare(PZSS, "Janusz", "Kowalski", "655", "PZSS (x)")  # za krótki numer — bez automatu
    assert "rodzaj dokumentu (prowadzący strzelanie)" in ocr.compare(
        PZSS.replace("PROWADZĄCEGO", "ZAWODNIKA"), "Janusz", "Kowalski", "98655/2024", "PZSS (x)")


def test_compare_lok_number_ocr_confusion_and_holder_line():
    assert ocr.compare(LOK, "Anna", "Nowak", "0079/BZGLOK/2022", "LOK (x)") == []  # O odczytane jako 0
    # imię z podpisu wystawcy, nazwisko posiadacza — nie razem, więc bez automatu
    assert ocr.compare(LOK, "Andrzej", "Nowak", "0079/BZGLOK/2022", "LOK (x)") == ["imię i nazwisko"]
    assert ocr.compare(LOK, "Anna", "Nowak", "0079/BZGLOK/2022", "Klub Strzelecki X") == []  # inny wystawca — bez sprawdzania


def test_issuer_code():
    assert ocr.issuer_code("PZŁ (Polski Związek Łowiecki)") == "PZŁ"
    assert ocr.issuer_code("Klub X") == ""


@pytest.mark.skipif(not ocr.available(), reason="brak programu tesseract")
def test_real_ocr_on_generated_card():
    img = Image.new("RGB", (1600, 1010), (232, 232, 228))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 58)
    except OSError:
        pytest.skip("brak czcionki DejaVu")
    for i, line in enumerate(["POLSKI ZWIĄZEK STRZELECTWA SPORTOWEGO", "LEGITYMACJA PROWADZĄCEGO STRZELANIE",
                              "nr 12345/2024", "NOWAK Anna", "Warszawa, 10.10.2024"]):
        d.text((70, 90 + i * 150), line, fill=(25, 25, 25), font=font)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=90)
    text = ocr.read_text(buf.getvalue())
    assert text and ocr.compare(text, "Anna", "Nowak", "12345/2024", "PZSS (x)") == []


# --- w przebiegu kiosku -------------------------------------------------------------------------------------------


@pytest.fixture
def device(owner):
    from apps.kiosk import services

    return services.create_device("Kiosk", owner)[0]


@pytest.fixture
def lane(db):
    return Lane.objects.get(name="Oś 100 m")


@pytest.fixture
def now():
    return bookings.local_dt(timezone.localdate() + timedelta(days=7), 10) + timedelta(minutes=20)


def _flow(device, first="Anna", last="Nowak"):
    flow, _ = onsite.start(device, "anna@example.com", first, last, "600700800")
    flow = onsite.verify_code(flow, last_code("anna@example.com"))
    onsite.accept_statement(flow)
    return flow


def test_matching_card_approved_automatically_without_waiting(device, lane, now, monkeypatch, owner_client):
    monkeypatch.setattr(ocr, "read_text", lambda data: LOK)
    flow = _flow(device)
    credential = onsite.submit_ps(flow, "0079/BZGLOK/2022", "LOK (Liga Obrony Kraju)", "2022-01-11", "", photo_data_url())
    flow.refresh_from_db()
    assert flow.ocr_result == "zgodne"
    OutgoingEmail.objects.all().delete()
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    credential.refresh_from_db()
    flow.refresh_from_db()
    assert credential.status == Credential.Status.VERIFIED and credential.reviewed_by is None
    assert credential.review_note == onsite.AUTO_NOTE and not credential.scan  # zdjęcie (z PESEL) usunięte od razu
    assert r.status == Reservation.Status.HOLD and flow.status == OnsiteBooking.Status.BOOKED
    assert not OutgoingEmail.objects.filter(subject__contains="PILNE").exists()  # bez pilnego powiadomienia obsługi
    body = owner_client.get("/panel/weryfikacja/?status=automatycznie").content.decode()
    assert "0079/BZGLOK/2022" in body
    assert "automatycznie (kiosk)" in owner_client.get(f"/panel/weryfikacja/{credential.pk}/").content.decode()


def test_mismatch_goes_to_staff_with_reason(device, lane, now, monkeypatch, owner_client):
    monkeypatch.setattr(ocr, "read_text", lambda data: LOK)
    flow = _flow(device, first="Anna", last="Kowalska")
    credential = onsite.submit_ps(flow, "0079/BZGLOK/2022", "LOK (Liga Obrony Kraju)", "2022-01-11", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    flow.refresh_from_db()
    credential.refresh_from_db()
    assert r.status == Reservation.Status.PENDING_APPROVAL and flow.status == OnsiteBooking.Status.AWAITING
    assert credential.status == Credential.Status.PENDING and flow.ocr_result == "nie znaleziono: imię i nazwisko"
    assert OutgoingEmail.objects.filter(subject__contains="PILNE").exists()
    assert "nie znaleziono: imię i nazwisko" in owner_client.get(f"/panel/weryfikacja/{credential.pk}/").content.decode()


def test_ocr_unavailable_or_disabled_means_staff_review(device, lane, now, monkeypatch):
    monkeypatch.setattr(ocr, "read_text", lambda data: None)
    flow = _flow(device)
    onsite.submit_ps(flow, "0079/BZGLOK/2022", "LOK (x)", "2022-01-11", "", photo_data_url())
    flow.refresh_from_db()
    assert flow.ocr_result == "niedostępne"
    cfg = BookingSettings.load()
    cfg.onsite_auto_verify = False
    cfg.save()
    calls = []
    monkeypatch.setattr(ocr, "read_text", lambda data: calls.append(1) or LOK)
    flow2, _ = onsite.start(device, "b@example.com", "Anna", "Nowak", "600700800")
    flow2 = onsite.verify_code(flow2, last_code("b@example.com"))
    onsite.submit_ps(flow2, "0079/BZGLOK/2022", "LOK (x)", "2022-01-11", "", photo_data_url())
    flow2.refresh_from_db()
    assert calls == [] and flow2.ocr_result == ""


def test_no_ocr_text_stored(device, monkeypatch):
    from apps.audit.models import AuditLog

    monkeypatch.setattr(ocr, "read_text", lambda data: LOK)
    flow = _flow(device)
    onsite.submit_ps(flow, "0079/BZGLOK/2022", "LOK (x)", "2022-01-11", "", photo_data_url())
    dump = " ".join(str(v) for v in AuditLog.objects.values_list("summary", "changes"))
    assert "00000000000" not in dump and "PESEL" not in dump and "PISTOL" not in dump


# --- czas odczytu: łączny limit, odczyt poza transakcją bazy --------------------------------------------------------------


def test_read_text_total_budget_skips_second_pass(monkeypatch):
    import subprocess
    import types

    clock = {"t": 1000.0}
    timeouts = []

    def fake_run(cmd, input, capture_output, timeout, check):
        timeouts.append(timeout)
        clock["t"] += 17  # pierwszy przebieg trwa 17 s
        return types.SimpleNamespace(returncode=0, stdout=b"tekst")

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(subprocess, "run", fake_run)
    buf = BytesIO()
    Image.new("RGB", (400, 250), "white").save(buf, "JPEG")
    assert ocr.read_text(buf.getvalue()) == "tekst"
    assert timeouts == [ocr.BUDGET_SECONDS]  # drugi przebieg pominięty: zostały 3 s (< MIN_PASS_SECONDS)


def test_read_text_timeout_gives_none(monkeypatch):
    import subprocess

    def slow(cmd, input, capture_output, timeout, check):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(subprocess, "run", slow)
    buf = BytesIO()
    Image.new("RGB", (400, 250), "white").save(buf, "JPEG")
    assert ocr.read_text(buf.getvalue(), budget=10) is None


@pytest.mark.django_db(transaction=True)
def test_kiosk_ocr_runs_outside_db_transaction(owner, monkeypatch):
    """Krok „legitymacja” przez API kiosku: w chwili odczytu nie ma otwartej transakcji (legitymacja już zapisana),
    a wynik porównania jest zapisany."""
    import json

    from django.db import connection
    from django.test import Client

    from apps.kiosk import services as kiosk_services

    seen = {}

    def fake_read(data):
        seen["in_atomic"] = connection.in_atomic_block
        seen["saved"] = Credential.objects.filter(number="0079/BZGLOK/2022").exists()
        return LOK

    monkeypatch.setattr(ocr, "read_text", fake_read)
    d, code = kiosk_services.create_device("Kiosk", owner)
    c = Client()
    c.post("/kiosk/parowanie/", {"code": code})
    h = {"HTTP_X_KIOSK": "1"}

    def post(path, data):
        return c.post(path, json.dumps(data), content_type="application/json", **h)

    r = post("/kiosk/api/na-miejscu/start/", {"email": "anna@example.com", "first_name": "Anna", "last_name": "Nowak", "phone": "600700800"})
    ref, secret = r.json()["ref"], r.json()["secret"]
    post("/kiosk/api/na-miejscu/kod/", {"ref": ref, "secret": secret, "code": last_code("anna@example.com")})
    post("/kiosk/api/na-miejscu/oswiadczenie/", {"ref": ref, "secret": secret, "accept": True})
    r = post("/kiosk/api/na-miejscu/legitymacja/", {"ref": ref, "secret": secret, "number": "0079/BZGLOK/2022",
                                                    "issuer": "LOK (Liga Obrony Kraju)", "issued_on": "2022-01-11", "photo": photo_data_url()})
    assert r.status_code == 200, r.content
    assert seen == {"in_atomic": False, "saved": True}
    assert OnsiteBooking.objects.get(ref=ref).ocr_result == "zgodne"


def test_wrong_code_attempts_still_counted_via_api(kiosk_client):
    c, post = kiosk_client
    r = post("/kiosk/api/na-miejscu/start/", {"email": "b@example.com", "first_name": "B", "last_name": "C", "phone": "600700801"})
    ref, secret = r.json()["ref"], r.json()["secret"]
    assert post("/kiosk/api/na-miejscu/kod/", {"ref": ref, "secret": secret, "code": "000000"}).status_code == 400
    assert OnsiteBooking.objects.get(ref=ref).code_attempts == 1  # błąd walidacji nie cofa licznika prób


@pytest.fixture
def kiosk_client(owner):
    import json

    from django.test import Client

    from apps.kiosk import services as kiosk_services

    d, code = kiosk_services.create_device("Kiosk", owner)
    c = Client()
    c.post("/kiosk/parowanie/", {"code": code})

    def post(path, data):
        return c.post(path, json.dumps(data), content_type="application/json", HTTP_X_KIOSK="1")

    return c, post
