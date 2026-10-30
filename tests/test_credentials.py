import datetime
from pathlib import Path

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from apps.audit.models import AuditLog, SensitiveAccessLog
from apps.credentials.models import Credential, is_range_officer
from apps.credentials.services import expire_outdated

from .conftest import add_credential, make_user

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4\n%test\n"


def form_data(**overrides):
    data = {
        "kind": "ps",
        "number": " ps 123/2024 ",
        "issuer": "PZSS",
        "issued_on": "2024-03-01",
        "valid_until": "",
        "details": "",
    }
    data.update(overrides)
    return data


def test_submit_with_scan_notifies_board_and_review_deletes_scan(client, user, owner, django_capture_on_commit_callbacks):
    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post("/dokumenty/dodaj/", {**form_data(), "scan": SimpleUploadedFile("dok.pdf", PDF, "application/pdf")})
    assert response.status_code == 302
    credential = Credential.objects.get()
    assert credential.number == "PS 123/2024"
    assert credential.status == Credential.Status.PENDING
    scan_path = Path(credential.scan.path)
    assert scan_path.exists()
    assert any(owner.email in m.to for m in mail.outbox)
    assert not is_range_officer(user)

    from .conftest import login_with_otp

    client.logout()
    login_with_otp(client, owner)
    client.get(f"/panel/weryfikacja/{credential.pk}/")
    response = client.get(f"/panel/weryfikacja/{credential.pk}/skan/")
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert SensitiveAccessLog.objects.filter(resource=f"skan:{credential.pk}", actor=owner).exists()

    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"/panel/weryfikacja/{credential.pk}/", {"decision": "approve", "note": ""})
    credential.refresh_from_db()
    assert credential.status == Credential.Status.VERIFIED and credential.reviewed_by == owner
    assert not credential.scan and not scan_path.exists()
    assert mail.outbox[0].to == [user.email]
    assert is_range_officer(user)
    assert AuditLog.objects.filter(action="dokument.weryfikacja").exists()


def test_reject_requires_note(owner_client, user):
    credential = add_credential(user, status=Credential.Status.PENDING)
    response = owner_client.post(f"/panel/weryfikacja/{credential.pk}/", {"decision": "reject", "note": ""})
    assert response.status_code == 200
    owner_client.post(f"/panel/weryfikacja/{credential.pk}/", {"decision": "reject", "note": "Nieczytelny numer"})
    credential.refresh_from_db()
    assert credential.status == Credential.Status.REJECTED and credential.review_note == "Nieczytelny numer"


def test_review_twice_is_refused(owner_client, user):
    credential = add_credential(user, status=Credential.Status.VERIFIED)
    owner_client.post(f"/panel/weryfikacja/{credential.pk}/", {"decision": "reject", "note": "x"})
    credential.refresh_from_db()
    assert credential.status == Credential.Status.VERIFIED


def test_rejects_non_document_upload(client, user):
    client.force_login(user)
    response = client.post("/dokumenty/dodaj/", {**form_data(), "scan": SimpleUploadedFile("x.pdf", b"<html>zlosliwy</html>", "application/pdf")})
    assert response.status_code == 200 and "scan" in response.context["form"].errors
    assert not Credential.objects.exists()


def test_rejects_future_issue_date_and_inverted_validity(client, user):
    client.force_login(user)
    response = client.post("/dokumenty/dodaj/", form_data(issued_on="2999-01-01"))
    assert "issued_on" in response.context["form"].errors
    response = client.post("/dokumenty/dodaj/", form_data(valid_until="2020-01-01"))
    assert "valid_until" in response.context["form"].errors


def test_editing_verified_credential_resets_verification(client, user):
    credential = add_credential(user)
    client.force_login(user)
    client.post(f"/dokumenty/{credential.pk}/", form_data(number="PS-2"))
    credential.refresh_from_db()
    assert credential.status == Credential.Status.PENDING and credential.reviewed_by is None
    assert not is_range_officer(user)


def test_user_cannot_touch_foreign_credentials(client, user):
    other = make_user("inny@example.com")
    credential = add_credential(other, status=Credential.Status.PENDING)
    client.force_login(user)
    assert client.get(f"/dokumenty/{credential.pk}/").status_code == 404
    assert client.post(f"/dokumenty/{credential.pk}/usun/").status_code == 404


def test_verified_credential_cannot_be_deleted_by_user(client, user):
    credential = add_credential(user)
    client.force_login(user)
    client.post(f"/dokumenty/{credential.pk}/usun/")
    assert Credential.objects.filter(pk=credential.pk).exists()


def test_expiry_by_date_and_nightly_command(user, django_capture_on_commit_callbacks):
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    credential = add_credential(user, valid_until=yesterday)
    assert credential.effective_status == Credential.Status.EXPIRED
    assert not is_range_officer(user)  # działa także przed uruchomieniem zadania nocnego
    with django_capture_on_commit_callbacks(execute=True):
        call_command("expire_credentials")
    credential.refresh_from_db()
    assert credential.status == Credential.Status.EXPIRED
    assert mail.outbox and mail.outbox[0].to == [user.email]
    assert expire_outdated() == 0


def test_valid_until_today_is_still_valid(user):
    add_credential(user, valid_until=datetime.date.today())
    assert is_range_officer(user)
