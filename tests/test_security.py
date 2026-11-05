import base64
import io

import pytest
from django.core import mail
from PIL import Image

from apps.accounts.models import User

from .conftest import PASSWORD, make_user

pytestmark = pytest.mark.django_db


def test_password_reset_email_throttled_per_address(client, user):
    for _ in range(6):
        client.post("/konto/haslo/reset/", {"email": user.email})
    assert len(mail.outbox) == 3  # dalej ta sama odpowiedź, ale bez wysyłki


def test_resend_verification_throttled(client, django_capture_on_commit_callbacks):
    u = make_user("nowy-v@example.com", verified=False)
    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(6):
            client.post("/konto/potwierdz/ponownie/", {"email": u.email})
    assert len(mail.outbox) == 3


def test_registration_throttled_per_ip(client, settings):
    from apps.accounts import ratelimit

    ratelimit.REGISTRATIONS_PER_IP_PER_HOUR = 2
    try:
        for i in range(3):
            client.post("/konto/rejestracja/", {"email": f"r{i}@example.com", "first_name": "Jan", "last_name": "Test",
                                                "phone": "600 100 200", "password1": PASSWORD, "password2": PASSWORD})
    finally:
        ratelimit.REGISTRATIONS_PER_IP_PER_HOUR = 10
    assert User.objects.filter(email__in=["r0@example.com", "r1@example.com"]).count() == 2
    assert not User.objects.filter(email="r2@example.com").exists()


def test_oversized_request_rejected_and_permissions_policy(client):
    response = client.post("/konto/logowanie/", data=b"x", content_type="text/plain", CONTENT_LENGTH=str(20 * 1024 * 1024))
    assert response.status_code == 413
    assert "camera=(self)" in client.get("/").headers["Permissions-Policy"]


def test_notification_subject_newlines_sanitized(django_capture_on_commit_callbacks, owner):
    from apps.core.notifications import notify_board

    with django_capture_on_commit_callbacks(execute=True):
        notify_board("Usterka\nBcc: ofiara@example.com", "emails/board_generic.txt", None, url="/x/", body="b", event="e", text="t")
    assert mail.outbox and "\n" not in mail.outbox[0].subject and mail.outbox[0].bcc == []


def test_decompression_bomb_signature_rejected():
    from django.core.exceptions import ValidationError

    from apps.logbook.services import decode_signature

    img = Image.new("1", (20000, 20000))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    with pytest.raises(ValidationError):
        decode_signature("data:image/png;base64," + base64.b64encode(out.getvalue()).decode())


def test_waitlist_requires_booking_eligibility():
    import datetime as dt

    from django.core.exceptions import ValidationError
    from django.utils import timezone

    from apps.bookings import waitlist
    from apps.facilities.models import Lane

    lane = Lane.objects.filter(is_active=True).first()
    no_ps = make_user("bezps@example.com")
    with pytest.raises(ValidationError):
        waitlist.join(no_ps, lane, timezone.localdate() + dt.timedelta(days=3), 10, 11)


def test_issue_reports_limited_per_day(user):
    from django.core.exceptions import ValidationError

    from apps.facilities import issues

    for i in range(issues.MAX_REPORTS_PER_DAY):
        issues.report(user, f"Usterka {i}")
    with pytest.raises(ValidationError):
        issues.report(user, "Jeszcze jedna")


def test_csp_header_and_frame_ancestors(client, owner_client, user):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in csp and "https://*.tpay.com" in csp
    from django.core.files.base import ContentFile

    from apps.credentials.models import Credential

    from .conftest import add_credential

    c = add_credential(user, status=Credential.Status.PENDING)
    c.scan.save("skan.pdf", ContentFile(b"%PDF-1.4\n"))
    r = owner_client.get(f"/panel/weryfikacja/{c.pk}/skan/")
    assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"]
    b"".join(r.streaming_content)
    c.scan.delete(save=False)


def test_no_inline_handlers_in_templates():
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "templates"
    bad = [str(p) for p in root.rglob("*.html") if re.search(r"\son(click|change|error|load|submit|input)=|javascript:", p.read_text())]
    assert bad == []
