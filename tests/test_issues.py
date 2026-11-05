import io

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.audit.models import AuditLog
from apps.core.models import Notification
from apps.facilities.models import Issue, Lane

from .conftest import grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db


def jpeg_with_gps(size=(3000, 2000)):
    img = Image.new("RGB", size, (120, 80, 40))
    exif = Image.Exif()
    exif[0x8825] = {2: (52.0, 13.0, 0.0)}  # GPS
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_report_issue_with_photo_notifies_board(client, user, owner, django_capture_on_commit_callbacks):
    client.force_login(user)
    lane = Lane.objects.filter(is_active=True).first()
    assert client.get(f"/usterki/nowa/?os={lane.pk}").status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post("/usterki/nowa/", {
            "lane": lane.pk, "title": "Nie działa transporter", "description": "Stoi na 10 m", "urgent": "on",
            "photo": SimpleUploadedFile("IMG_1.jpg", jpeg_with_gps(), "image/jpeg"),
        })
    assert response.status_code == 302
    issue = Issue.objects.get()
    assert issue.urgent and issue.lane == lane and issue.reported_by == user and issue.status == Issue.Status.NEW
    stored = Image.open(issue.photo.open("rb"))
    assert max(stored.size) <= 2000 and not stored.getexif().get(0x8825)  # zmniejszone, bez GPS
    issue.photo.close()
    assert Notification.objects.filter(title__startswith="PILNE").exists()
    assert any("PILNE" in m.subject for m in mail.outbox)
    assert "Nie działa transporter" in client.get("/usterki/").content.decode()
    issue.photo.delete(save=False)


def test_non_image_rejected(client, user):
    client.force_login(user)
    response = client.post("/usterki/nowa/", {"title": "x", "photo": SimpleUploadedFile("a.jpg", b"<html>", "image/jpeg")})
    assert response.status_code == 200 and "photo" in response.context["form"].errors
    assert not Issue.objects.exists()


def test_panel_status_flow_notifies_reporter(owner_client, owner, user, django_capture_on_commit_callbacks):
    issue = Issue.objects.create(title="Dziura w kulochwycie", reported_by=user)
    page = owner_client.get("/panel/usterki/")
    assert [i.pk for i in page.context["issues"]] == [issue.pk]
    # zamknięcie bez opisu — odmowa
    owner_client.post(f"/panel/usterki/{issue.pk}/", {"status": "naprawione", "note": ""})
    issue.refresh_from_db()
    assert issue.status == Issue.Status.NEW
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        owner_client.post(f"/panel/usterki/{issue.pk}/", {"status": "naprawione", "note": "Załatane 5.11"})
    issue.refresh_from_db()
    assert issue.status == Issue.Status.FIXED and issue.resolved_by == owner and issue.resolved_at
    assert [m.to for m in mail.outbox] == [[user.email]]
    assert AuditLog.objects.filter(action="usterka.status").exists()
    assert owner_client.get("/panel/usterki/?status=otwarte").context["issues"].count() == 0


def test_panel_issues_requires_permission(client):
    staff = grant(make_user("dz@example.com"), "bookings.manage_reservations")
    login_with_otp(client, staff)
    assert client.get("/panel/usterki/").status_code == 403
    assert client.get("/usterki/nowa/").status_code == 200  # zgłosić może każdy zalogowany
