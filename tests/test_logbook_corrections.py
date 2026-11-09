"""Korekty wpisów w panelu (oryginał bez zmian), strzelanie bez rezerwacji w kiosku, „Wpis obsługi”."""

import json
import uuid
from datetime import timedelta

import pytest
from django.db import DatabaseError, transaction
from django.test import Client
from django.utils import timezone

from apps.kiosk import services as kiosk_services
from apps.logbook import services
from apps.logbook.models import Entry, EntryCorrection

from .conftest import grant, login_with_otp, make_user
from .test_logbook import permit_data, signature_data_url

pytestmark = pytest.mark.django_db


@pytest.fixture
def sig():
    return signature_data_url()


@pytest.fixture
def entry(sig):
    data = {**permit_data(), "purpose": "obserwator", "has_permit": True}
    return services.create_entry(data=data, signature_png=services.decode_signature(sig), source="qr_wejscia")


def test_correction_keeps_original_and_shows_corrected(owner_client, owner, entry):
    r = owner_client.post(f"/panel/ksiazka/{entry.pk}/korekta/", {
        "first_name": "Anna", "last_name": "Nowakowska", "permit_number": "ab 123", "permit_issuer": "KWP Wrocław",
        "purpose_note": "", "exited_at": "", "reason": "literówka w nazwisku",
    })
    assert r.status_code == 302, r.content.decode()[:1500]
    entry.refresh_from_db()
    assert entry.last_name == "Nowak" and entry.integrity_ok  # oryginał nietknięty
    c = EntryCorrection.objects.get()
    assert (c.field, c.old_value, c.new_value, c.created_by) == ("last_name", "Nowak", "Nowakowska", owner)
    detail = owner_client.get(f"/panel/ksiazka/{entry.pk}/").content.decode()
    assert "Nowakowska" in detail and "skorygowano" in detail and "literówka w nazwisku" in detail
    assert "oryginał wpisu: dane i podpis bez zmian" in detail
    assert "skorygowano" in owner_client.get("/panel/ksiazka/").content.decode()
    with pytest.raises(DatabaseError), transaction.atomic():  # korekt nie da się zmienić ani usunąć
        EntryCorrection.objects.filter(pk=c.pk).update(new_value="X")
    with pytest.raises(DatabaseError), transaction.atomic():
        EntryCorrection.objects.filter(pk=c.pk).delete()


def test_exit_open_entry_then_correct_time(owner, sig):
    data = {**permit_data(), "purpose": "obserwator", "has_permit": True}
    entry = services.create_entry(data=data, signature_png=services.decode_signature(sig), source="qr_wejscia",
                                  now=timezone.now() - timedelta(hours=1))
    t1 = timezone.now() - timedelta(minutes=5)
    assert services.correct_entry(entry, {"exited_at": t1}, "osoba wyszła bez wypisania", owner) == ["exited_at"]
    entry.refresh_from_db()
    assert entry.exited_at == t1 and not EntryCorrection.objects.exists()  # otwarty pobyt — zwykłe zapisanie wyjścia
    t2 = t1 - timedelta(minutes=2)
    services.correct_entry(entry, {"exited_at": t2}, "dokładna godzina wg obsługi", owner)
    entry.refresh_from_db()
    assert entry.exited_at == t1  # oryginał bez zmian
    assert services.apply_corrections([entry])[0].exited_at == t2


def test_correction_rules(owner, entry):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError, match="powód"):
        services.correct_entry(entry, {"last_name": "X"}, "", owner)
    with pytest.raises(ValidationError, match="Nic nie zmieniono"):
        services.correct_entry(entry, {"last_name": "Nowak"}, "bez zmian tutaj", owner)
    with pytest.raises(ValidationError, match="Adres"):
        services.correct_entry(entry, {"address": "Sątok 1"}, "adres do poprawy", owner)
    with pytest.raises(ValidationError, match="po wejściu"):
        services.correct_entry(entry, {"exited_at": entry.entered_at - timedelta(hours=1)}, "zła godzina", owner)


def test_correction_requires_permission(entry):
    staff = grant(make_user("v@example.com"), "logbook.view_logbook")
    c = Client()
    login_with_otp(c, staff)
    assert c.get(f"/panel/ksiazka/{entry.pk}/korekta/").status_code == 403
    assert "Popraw wpis" not in c.get(f"/panel/ksiazka/{entry.pk}/").content.decode()


def test_purge_removes_corrections_with_entries(owner, sig):
    data = {**permit_data(), "purpose": "obserwator", "has_permit": True}
    old = services.create_entry(data=data, signature_png=services.decode_signature(sig), source="qr_wejscia",
                                now=timezone.now() - timedelta(days=6 * 365))
    services.correct_entry(old, {"first_name": "Ania"}, "zdrobnienie w dokumencie", owner)
    assert services.purge_expired() == 1
    assert not EntryCorrection.objects.exists()


def test_export_and_print_mark_corrections(owner_client, owner, entry):
    services.correct_entry(entry, {"first_name": "Ania"}, "zdrobnienie w dokumencie", owner)
    day = timezone.localdate().isoformat()
    body = owner_client.get(f"/panel/ksiazka/druk/?od={day}&do={day}").content.decode()
    assert f"{entry.number}*" in body and "Ania" in body
    assert owner_client.get(f"/panel/ksiazka/eksport.xlsx?od={day}&do={day}").status_code == 200


# --- kiosk: strzelanie bez rezerwacji ----------------------------------------------------------------------------


@pytest.fixture
def kiosk(owner):
    device, code = kiosk_services.create_device("Kiosk", owner)
    c = Client()
    c.post("/kiosk/parowanie/", {"code": code})
    return c


def _kiosk_post(c, **kw):
    data = {"ref": str(uuid.uuid4()), "purpose": "strzelanie", "first_name": "Jan", "last_name": "Nocny", "has_permit": "nie",
            "address": "Sątok 2", "statement": True, "signature": signature_data_url(), "reservation": ""}
    data.update(kw)
    return c.post("/kiosk/api/wpis/", json.dumps(data), content_type="application/json", HTTP_X_KIOSK="1")


def test_kiosk_shooting_without_reservation_rejected(kiosk):
    """Wyłączone na prośbę właściciela — strzelanie poza godzinami będzie osobnym trybem przez rezerwację."""
    for note in ("", "Piotr Prowadzący"):
        r = _kiosk_post(kiosk, purpose_note=note)
        assert r.status_code == 400 and "rezerwację" in r.json()["error"]
    assert not Entry.objects.exists()
    r = _kiosk_post(kiosk, purpose="prace", purpose_note="koszenie trawy")
    assert r.status_code == 200 and Entry.objects.get().purpose == "prace"  # prace — bez dokumentów, bez rezerwacji


def test_remote_shooting_still_needs_reservation(client, sig):
    from apps.logbook.models import LogbookSettings

    token = LogbookSettings.load().gate_token
    client.post(f"/ksiazka/wpis/{token}/", {**permit_data(), "purpose": "strzelanie", "purpose_note": "ktoś", "signature": sig})
    assert not Entry.objects.exists()


# --- „Wpis obsługi” w panelu klienta --------------------------------------------------------------------------------


def test_staff_entry_button_only_with_permission(owner_client):
    plain = Client()
    plain.force_login(make_user("p@example.com"))
    assert "Wpis obsługi" not in plain.get("/ksiazka/moj-kod/").content.decode()
    body = owner_client.get("/ksiazka/moj-kod/").content.decode()
    assert "/panel/ksiazka/nowy-wpis/?cel=prace" in body
    form = owner_client.get("/panel/ksiazka/nowy-wpis/?cel=prace").content.decode()
    assert "obsługa strzelnicy" in form  # cel „prace” i opis ustawione z góry
