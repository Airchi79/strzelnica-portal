import logging
from datetime import timedelta

import pytest
from django.core import mail
from django.db import transaction
from django.utils import timezone

from apps.core import notifications
from apps.core.models import OutgoingEmail

from .conftest import login_with_otp, make_user

pytestmark = pytest.mark.django_db


def _notify(user):
    notifications.notify_user(user, "Test wysyłki", "emails/board_generic.txt", portal=False, event="x", text="treść")


def test_email_sent_after_commit_and_recorded(django_capture_on_commit_callbacks):
    u = make_user("a@example.com")
    with django_capture_on_commit_callbacks(execute=True):
        _notify(u)
    m = OutgoingEmail.objects.get()
    assert m.status == OutgoingEmail.Status.SENT and len(mail.outbox) == 1 and mail.outbox[0].to == ["a@example.com"]


def test_rolled_back_operation_sends_nothing(django_capture_on_commit_callbacks):
    u = make_user("b@example.com")
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                _notify(u)
                raise RuntimeError("operacja wycofana")
    assert not OutgoingEmail.objects.exists() and mail.outbox == []


def test_smtp_failure_is_retried_by_scheduler(monkeypatch, django_capture_on_commit_callbacks):
    u = make_user("c@example.com")

    def down(*a, **k):
        raise ConnectionRefusedError("serwer poczty niedostępny")

    monkeypatch.setattr(notifications, "send_mail", down)
    with django_capture_on_commit_callbacks(execute=True):
        _notify(u)  # błąd poczty nie przerywa operacji
    m = OutgoingEmail.objects.get()
    assert m.status == OutgoingEmail.Status.PENDING and m.attempts == 1 and "ConnectionRefusedError" in m.last_error
    assert notifications.send_outbox(now=timezone.now()) == (0, 0)  # jeszcze nie pora
    monkeypatch.undo()
    assert notifications.send_outbox(now=m.next_attempt_at + timedelta(seconds=1)) == (1, 0)
    m.refresh_from_db()
    assert m.status == OutgoingEmail.Status.SENT and len(mail.outbox) == 1


def test_email_marked_failed_after_all_retries(monkeypatch, caplog):
    u = make_user("d@example.com")
    monkeypatch.setattr(notifications, "send_mail", lambda *a, **k: (_ for _ in ()).throw(OSError("brak sieci")))
    _notify(u)  # bez on_commit — wysyłkę zrobi harmonogram
    m = OutgoingEmail.objects.get()
    now = m.created_at + timedelta(minutes=3)
    with caplog.at_level(logging.ERROR, logger="apps.core.notifications"):
        for _ in range(len(notifications.RETRY_DELAYS) + 1):
            notifications.send_outbox(now=now)
            m.refresh_from_db()
            now = (m.next_attempt_at or now) + timedelta(seconds=1)
    assert m.status == OutgoingEmail.Status.FAILED and m.attempts == len(notifications.RETRY_DELAYS) + 1
    assert any("nie wysłany" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


def test_sent_emails_purged_after_30_days(django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        _notify(make_user("e@example.com"))
    m = OutgoingEmail.objects.get()
    notifications.send_outbox(now=m.sent_at + timedelta(days=29))
    assert OutgoingEmail.objects.exists()
    notifications.send_outbox(now=m.sent_at + timedelta(days=31))
    assert not OutgoingEmail.objects.exists()


def test_panel_outbox_lists_problems_and_retries(client, owner, monkeypatch, django_capture_on_commit_callbacks):
    monkeypatch.setattr(notifications, "send_mail", lambda *a, **k: (_ for _ in ()).throw(OSError("535 auth failed")))
    with django_capture_on_commit_callbacks(execute=True):
        _notify(make_user("f@example.com"))
    monkeypatch.undo()
    login_with_otp(client, owner)
    page = client.get("/panel/poczta/").content.decode()
    assert "Wiadomości niewysłane: 1" in page and "535 auth failed" in page
    assert "Wysyłka e-maili" in client.get("/panel/").content.decode()
    client.post("/panel/poczta/", {"action": "retry"})
    notifications.send_outbox(now=timezone.now() + timedelta(seconds=1))
    assert OutgoingEmail.objects.get().status == OutgoingEmail.Status.SENT


def test_outbox_page_requires_settings_permission(client):
    staff = make_user("g@example.com", is_staff=True)
    login_with_otp(client, staff)
    assert client.get("/panel/poczta/").status_code == 403


# --- alarm o błędach serwera -----------------------------------------------------------------------------------------


def _error_record(msg="Boom"):
    try:
        raise ValueError(msg)
    except ValueError:
        import sys

        return logging.LogRecord("django.request", logging.ERROR, __file__, 1, "Internal Server Error: /x", None, sys.exc_info())


def test_error_alarm_disabled_without_address(settings):
    from apps.core.errorlog import BoardErrorEmailHandler

    settings.ERROR_EMAILS = []
    BoardErrorEmailHandler().emit(_error_record())
    assert mail.outbox == []


def test_error_alarm_sent_once_per_error(settings):
    from apps.core.errorlog import BoardErrorEmailHandler

    settings.ERROR_EMAILS = ["alarm@example.com"]
    h = BoardErrorEmailHandler()
    for _ in range(5):
        h.emit(_error_record())
    h.emit(_error_record("Inny błąd"))
    assert [m.to for m in mail.outbox] == [["alarm@example.com"], ["alarm@example.com"]]
    assert "ValueError: Boom" in mail.outbox[0].body and "BŁĄD" in mail.outbox[0].subject


def test_server_error_triggers_alarm(client, settings, monkeypatch):
    from apps.core import views

    settings.ERROR_EMAILS = ["alarm@example.com"]
    client.raise_request_exception = False
    monkeypatch.setattr(views, "AssociationSettings", None)  # wymuszony błąd w widoku strony „Pytania”
    assert client.get("/pytania/").status_code == 500
    assert any("BŁĄD" in m.subject and "GET /pytania/" in m.body for m in mail.outbox)


def test_anonymize_removes_outbox_copies(owner, django_capture_on_commit_callbacks):
    from apps.accounts import gdpr

    u = make_user("h@example.com")
    other = make_user("i@example.com")
    with django_capture_on_commit_callbacks(execute=True):
        _notify(u)
        _notify(other)
    u.is_active = False
    u.save(update_fields=["is_active"])
    gdpr.anonymize(u, owner)
    assert list(OutgoingEmail.objects.values_list("recipients", flat=True)) == [["i@example.com"]]
