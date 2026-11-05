import json
from datetime import timedelta

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from apps.core.backups import read_status

pytestmark = pytest.mark.django_db


@pytest.fixture
def bdir(tmp_path, settings):
    settings.BACKUP_DIR = str(tmp_path)
    return tmp_path


def write(bdir, ok=True, age=timedelta(hours=2), error=""):
    at = (timezone.now() - age).strftime("%Y-%m-%dT%H:%M:%SZ")
    (bdir / "status.json").write_text(json.dumps({"ok": ok, "file": "satok-2026-11-05.dump", "size": 1000, "checked_at": at,
                                                  "error": error, "counts": {"accounts_user": 3}}))
    (bdir / "satok-2026-11-05.dump").write_bytes(b"PGDMP")


def test_status_states(bdir):
    assert read_status()["state"] == "brak"
    write(bdir)
    st = read_status()
    assert st["state"] == "ok" and st["counts"] == {"accounts_user": 3} and st["files"][0]["name"] == "satok-2026-11-05.dump"
    write(bdir, age=timedelta(hours=40))
    assert read_status()["state"] == "stara"
    write(bdir, ok=False, error="odtworzenie nie powiodło się")
    st = read_status()
    assert st["state"] == "blad" and "odtworzenie" in st["message"]
    (bdir / "status.json").write_text("{zepsute")
    assert read_status()["state"] == "blad"


def test_alarm_sent_once_per_day(bdir, owner, django_capture_on_commit_callbacks):
    write(bdir, ok=False, error="plik kopii jest uszkodzony")
    with django_capture_on_commit_callbacks(execute=True):
        call_command("check_backups")
        call_command("check_backups")
    alarms = [m for m in mail.outbox if "kopią zapasową" in m.subject]
    assert len(alarms) == 1 and "uszkodzony" in alarms[0].body


def test_no_alarm_when_ok_and_panel_page(bdir, owner_client):
    write(bdir)
    call_command("check_backups")
    assert not mail.outbox
    page = owner_client.get("/panel/kopie/")
    assert page.status_code == 200 and "Kopia działa" in page.content.decode()
