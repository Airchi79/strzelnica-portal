"""Dzienne wydruki książki pobytu (PDF): tworzenie po zakończeniu dnia, wersje po zmianach, łańcuch sum, ochrona
przed zmianą i usunięciem, czyszczenie po okresie przechowywania, panel."""

import hashlib
import io
import zipfile
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.test import Client

from apps.audit.models import SensitiveAccessLog
from apps.logbook import daily_print, services
from apps.logbook.models import DailyPrint, Entry

from .conftest import grant, login_with_otp, make_user
from .test_logbook import permit_data, signature_data_url

pytestmark = pytest.mark.django_db


@pytest.fixture
def day():
    from django.utils import timezone

    return timezone.localdate() - timedelta(days=3)


def entry_on(day, hour=10, exit_after=2, **kw):
    start, _ = daily_print.day_bounds(day)
    at = start + timedelta(hours=hour)
    data = {**permit_data(**kw), "purpose": "obserwator", "has_permit": True}
    e = services.create_entry(data=data, signature_png=services.decode_signature(signature_data_url()), source="qr_wejscia", now=at)
    if exit_after is not None:
        services.register_exit(e, now=at + timedelta(hours=exit_after))
        e.refresh_from_db()
    return e


def test_print_created_after_day_with_chain(day):
    entry_on(day, last_name="Nowak")
    entry_on(day + timedelta(days=1), last_name="Kowalski")
    created = daily_print.run()
    # dzień pierwszego wpisu, dzień drugi i wszystkie kolejne aż do wczoraj (także dni bez wpisów)
    assert [p.day for p in sorted(created, key=lambda p: p.day)] == [day + timedelta(days=i) for i in range(3)]
    first = DailyPrint.objects.get(day=day)
    assert bytes(first.pdf).startswith(b"%PDF") and first.entries_count == 1 and first.version == 1 and first.prev_sha256 == ""
    assert first.pdf_sha256 == hashlib.sha256(bytes(first.pdf)).hexdigest()
    second = DailyPrint.objects.get(day=day + timedelta(days=1))
    assert second.prev_sha256 == first.pdf_sha256
    assert DailyPrint.objects.get(day=day + timedelta(days=2)).entries_count == 0
    assert daily_print.run() == []  # bez zmian — brak nowych wersji
    assert daily_print.verify_chain() == (3, [])


def test_not_printed_before_day_ends_or_with_open_stay(day):
    entry_on(day, hour=22, exit_after=None)
    end = daily_print.day_bounds(day)[1]
    assert not daily_print.day_ready(day, end - timedelta(minutes=1))
    assert not daily_print.day_ready(day, end + timedelta(hours=1))  # pobyt otwarty
    assert daily_print.day_ready(day, end + timedelta(hours=14))  # po automatycznym zamknięciu (12 h) + 1 h
    assert daily_print.run(now=end + timedelta(hours=1)) == []


def test_new_version_after_correction(day, owner):
    e = entry_on(day)
    daily_print.run()
    services.correct_entry(e, {"last_name": "Nowakowska"}, "literówka w nazwisku", owner)
    created = daily_print.run()
    assert [(p.day, p.version) for p in created] == [(day, 2)]
    v1, v2 = DailyPrint.objects.filter(day=day).order_by("version")
    assert v1.data_sha256 != v2.data_sha256 and "zmiana" in v2.reason
    assert daily_print.verify_chain()[1] == []


def test_print_cannot_be_changed_or_deleted_and_tamper_detected(day):
    entry_on(day)
    daily_print.run()
    p = DailyPrint.objects.get(day=day)
    with pytest.raises(DatabaseError), transaction.atomic():
        DailyPrint.objects.filter(pk=p.pk).update(pdf=b"%PDF-falszywy")
    with pytest.raises(DatabaseError), transaction.atomic():
        DailyPrint.objects.filter(pk=p.pk).delete()
    # obejście triggera (np. bezpośrednio w bazie) jest wykrywane przez kontrolę sum
    with connection.cursor() as cur:
        cur.execute("ALTER TABLE logbook_dailyprint DISABLE TRIGGER satok_logbook_print_guard")
        cur.execute("UPDATE logbook_dailyprint SET pdf = %s WHERE id = %s", [b"%PDF-falszywy", p.pk])
        cur.execute("ALTER TABLE logbook_dailyprint ENABLE TRIGGER satok_logbook_print_guard")
    n, problems = daily_print.verify_chain()
    assert n == DailyPrint.objects.count() and any("niezgodny" in x for x in problems)


def test_purge_removes_old_prints(day):
    entry_on(day)
    daily_print.run()
    count = DailyPrint.objects.count()
    from django.utils import timezone

    services.purge_expired(now=timezone.now() + timedelta(days=5 * 366 + 10))
    assert not Entry.objects.exists() and DailyPrint.objects.count() == 0 and count > 0


def test_command_and_alarm(day, owner):
    from django.core.cache import cache

    from apps.core.models import OutgoingEmail

    entry_on(day)
    out = io.StringIO()
    call_command("daily_logbook_print", stdout=out)
    assert "problemów: 0" in out.getvalue()
    p = DailyPrint.objects.order_by("id").first()
    with connection.cursor() as cur:
        cur.execute("ALTER TABLE logbook_dailyprint DISABLE TRIGGER satok_logbook_print_guard")
        cur.execute("UPDATE logbook_dailyprint SET pdf_sha256 = %s WHERE id = %s", ["0" * 64, p.pk])
        cur.execute("ALTER TABLE logbook_dailyprint ENABLE TRIGGER satok_logbook_print_guard")
    cache.clear()
    call_command("daily_logbook_print", stdout=io.StringIO())
    assert OutgoingEmail.objects.filter(subject__contains="niezgodność dziennych wydruków").exists()


def test_panel_list_download_zip_and_permissions(day, owner_client):
    entry_on(day)
    daily_print.run()
    p = DailyPrint.objects.get(day=day)
    page = owner_client.get(f"/panel/ksiazka/wydruki-dzienne/?miesiac={day:%Y-%m}").content.decode()
    assert "zgodne" in page and p.pdf_sha256[:16] in page
    r = owner_client.get(f"/panel/ksiazka/wydruki-dzienne/{p.pk}.pdf")
    assert r.status_code == 200 and r.content == bytes(p.pdf) and p.filename in r["Content-Disposition"]
    assert SensitiveAccessLog.objects.filter(resource__startswith="ksiazka:wydruk_dzienny:").exists()
    z = owner_client.get(f"/panel/ksiazka/wydruki-dzienne/miesiac.zip?miesiac={day:%Y-%m}")
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert p.filename in names and "SHA256SUMS.txt" in names
    other = Client()
    login_with_otp(other, grant(make_user("x@example.com"), "bookings.manage_reservations"))
    assert other.get("/panel/ksiazka/wydruki-dzienne/").status_code == 403
    assert other.get(f"/panel/ksiazka/wydruki-dzienne/{p.pk}.pdf").status_code == 403
