"""Kopia testowa: pasek „WERSJA TESTOWA”, przekierowanie e-maili, push tylko na urządzenia z kopii, przygotowanie kopii."""

import io
from datetime import timedelta

import pytest
from django.core import mail
from django.core.management import CommandError, call_command
from django.test import override_settings
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.core import webpush
from apps.core.models import OutgoingEmail, PushSubscription
from apps.kiosk import services as kiosk_services
from apps.kiosk.models import KioskDevice

from .conftest import make_user
from .test_webpush import _receiver, _Resp

pytestmark = pytest.mark.django_db

TEST_COPY = {"IS_TEST_COPY": True, "SITE_ENVIRONMENT": "test"}
REDIRECT = {"EMAIL_BACKEND": "apps.core.mail_redirect.RedirectingEmailBackend",
            "EMAIL_INNER_BACKEND": "django.core.mail.backends.locmem.EmailBackend", "EMAIL_REDIRECT_TO": "biuro@example.com"}


def test_banner_only_in_test_copy(client):
    assert "WERSJA TESTOWA" not in client.get("/").content.decode()
    with override_settings(**TEST_COPY):
        page = client.get("/").content.decode()
    assert "WERSJA TESTOWA" in page and "<title>[TEST]" in page


@override_settings(**REDIRECT)
def test_all_mail_redirected_with_original_recipient():
    mail.send_mail("Przypomnienie", "Treść", "noreply@example.com", ["czlonek@example.com"], fail_silently=False)
    msg = mail.outbox[-1]
    assert msg.to == ["biuro@example.com"] and not msg.cc and not msg.bcc
    assert msg.subject == "[TEST → czlonek@example.com] Przypomnienie"
    assert "czlonek@example.com" in msg.body and msg.extra_headers["X-Satok-Original-To"] == "czlonek@example.com"


def test_push_in_test_copy_only_to_devices_registered_after_preparation(monkeypatch):
    u = make_user("czlonek@example.com")
    _, p256dh, auth_b64, _ = _receiver()
    old = PushSubscription.objects.create(user=u, endpoint="https://fcm.googleapis.com/fcm/send/z-produkcji", p256dh=p256dh, auth=auth_b64)
    sent = []
    monkeypatch.setattr(webpush.urllib.request, "urlopen", lambda req, timeout=None: sent.append(req.full_url) or _Resp())
    with override_settings(**TEST_COPY):
        webpush._deliver([u.pk], {"title": "t", "body": "", "url": "/", "tag": "portal"})
        assert sent == []  # kopia nieprzygotowana — żadnych push
        AuditLog.objects.create(action="system.kopia_testowa")
        PushSubscription.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=1))
        new = PushSubscription.objects.create(user=u, endpoint="https://fcm.googleapis.com/fcm/send/z-testu", p256dh=p256dh, auth=auth_b64)
        webpush._deliver([u.pk], {"title": "t", "body": "", "url": "/", "tag": "portal"})
    assert sent == [new.endpoint]


def test_prepare_test_copy_refused_in_production():
    with pytest.raises(CommandError, match="wyłącznie w kopii testowej"):
        call_command("prepare_test_copy", stdout=io.StringIO())


def test_prepare_test_copy_removes_push_outbox_and_kiosk_pairing(owner):
    u = make_user("czlonek@example.com")
    _, p256dh, auth_b64, _ = _receiver()
    PushSubscription.objects.create(user=u, endpoint="https://fcm.googleapis.com/fcm/send/x", p256dh=p256dh, auth=auth_b64)
    OutgoingEmail.objects.create(recipients=["czlonek@example.com"], subject="s", body="b")
    device, code = kiosk_services.create_device("Kiosk", owner)
    kiosk_services.pair(code, ip="127.0.0.1")
    assert KioskDevice.objects.exclude(token_hash="").exists()
    with override_settings(**TEST_COPY):
        out = io.StringIO()
        call_command("prepare_test_copy", stdout=out)
    assert not PushSubscription.objects.exists() and not OutgoingEmail.objects.exists()
    assert not KioskDevice.objects.exclude(token_hash="").exists()
    assert AuditLog.objects.filter(action="system.kopia_testowa").exists() and "gotowa" in out.getvalue()
