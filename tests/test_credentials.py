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


def test_add_form_defaults_to_ps_with_hints(client, user):
    client.force_login(user)
    html = client.get("/dokumenty/dodaj/").content.decode()
    assert 'value="ps"' in html and 'id="id_kind_0"' in html
    assert "Numer legitymacji PS" in html and "PS/123/2019" in html and 'id="kind-hints"' in html
    assert 'capture=' not in html  # aparat włącza dopiero przycisk „Zrób zdjęcie”
    assert "credential.js" in html


def test_hints_follow_submitted_kind_on_error(client, user):
    client.force_login(user)
    response = client.post("/dokumenty/dodaj/", form_data(kind="licencja", number=""))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Numer licencji" in html and "Polski Związek Strzelectwa Sportowego" in html


def test_add_form_png_scan_accepted_and_renamed(client, user):
    client.force_login(user)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    response = client.post("/dokumenty/dodaj/", {**form_data(), "scan": SimpleUploadedFile("IMG_0001.png", png, "image/png")})
    assert response.status_code == 302
    c = Credential.objects.get(user=user)
    assert c.scan.name.endswith(".png") and "IMG_0001" not in c.scan.name
    c.scan.delete(save=False)


def test_heic_rejected_with_hint(client, user):
    client.force_login(user)
    heic = b"\x00\x00\x00\x18ftypheic" + b"0" * 32
    response = client.post("/dokumenty/dodaj/", {**form_data(), "scan": SimpleUploadedFile("a.heic", heic, "image/heic")})
    assert response.status_code == 200 and "HEIC" in str(response.context["form"].errors["scan"])


def test_my_list_shows_ps_state(client, user):
    client.force_login(user)
    html = client.get("/dokumenty/").content.decode()
    assert "potrzebujesz zweryfikowanych uprawnień PS" in html and "?rodzaj=ps" in html
    add_credential(user)
    html = client.get("/dokumenty/").content.decode()
    assert "Uprawnienia PS zweryfikowane" in html


def test_rejected_credential_shows_reason_on_edit(client, user):
    client.force_login(user)
    c = Credential.objects.create(user=user, kind="ps", number="PS/1", issuer="PZSS", issued_on=datetime.date(2020, 1, 1),
                                  status=Credential.Status.REJECTED, review_note="Nieczytelny numer")
    assert "Popraw i wyślij ponownie" in client.get("/dokumenty/").content.decode()
    assert "Nieczytelny numer" in client.get(f"/dokumenty/{c.pk}/").content.decode()


def test_panel_list_counts_search_and_menu_badge(owner_client, user):
    other = make_user("druga@example.com")
    add_credential(user, status=Credential.Status.PENDING, number="PS-A")
    add_credential(other, status=Credential.Status.PENDING, number="PS-B")
    add_credential(other, kind=Credential.Kind.LICENSE, status=Credential.Status.VERIFIED, number="L-1")
    response = owner_client.get("/panel/weryfikacja/")
    tabs = {v: n for v, _, n in response.context["tabs"]}
    assert tabs["oczekuje"] == 2 and tabs["zweryfikowany"] == 1
    assert 'title="czeka na decyzję">2<' in response.content.decode()
    response = owner_client.get("/panel/weryfikacja/?q=PS-B")
    assert [c.number for c in response.context["page"]] == ["PS-B"]
    response = owner_client.get("/panel/weryfikacja/?status=zweryfikowany&rodzaj=licencja")
    assert [c.number for c in response.context["page"]] == ["L-1"]


def test_panel_review_goes_to_next_in_queue(owner_client, user):
    first = add_credential(user, status=Credential.Status.PENDING, number="PS-1")
    second = add_credential(make_user("b@example.com"), status=Credential.Status.PENDING, number="PS-2")
    page = owner_client.get(f"/panel/weryfikacja/{first.pk}/")
    assert page.context["queue_pos"] == 1 and page.context["has_next"]
    response = owner_client.post(f"/panel/weryfikacja/{first.pk}/", {"decision": "approve", "note": "", "then": "next"})
    assert response.status_code == 302 and response.url == f"/panel/weryfikacja/{second.pk}/"
    response = owner_client.post(f"/panel/weryfikacja/{second.pk}/", {"decision": "approve", "note": "", "then": "next"})
    assert response.url == "/panel/weryfikacja/"
    history = owner_client.get(f"/panel/weryfikacja/{first.pk}/").context["history"]
    assert any(h.action == "dokument.weryfikacja" for h in history)


def test_panel_scan_can_be_embedded_same_origin_only(owner_client, user):
    credential = add_credential(user, status=Credential.Status.PENDING)
    credential.scan.save("skan.pdf", __import__("django.core.files.base", fromlist=["ContentFile"]).ContentFile(PDF))
    page = owner_client.get(f"/panel/weryfikacja/{credential.pk}/").content.decode()
    assert 'class="scan-pdf"' in page
    response = owner_client.get(f"/panel/weryfikacja/{credential.pk}/skan/")
    assert response["X-Frame-Options"] == "SAMEORIGIN" and response["Cache-Control"] == "no-store"
    b"".join(response.streaming_content)
    credential.scan.delete(save=False)


def test_revoke_verification(owner_client, user, django_capture_on_commit_callbacks):
    credential = add_credential(user)
    assert is_range_officer(user)
    page = owner_client.get(f"/panel/weryfikacja/{credential.pk}/").content.decode()
    assert "Cofnij weryfikację" in page
    # bez powodu — odmowa
    owner_client.post(f"/panel/weryfikacja/{credential.pk}/cofnij/", {"reason": "  "})
    credential.refresh_from_db()
    assert credential.status == Credential.Status.VERIFIED
    with django_capture_on_commit_callbacks(execute=True):
        response = owner_client.post(f"/panel/weryfikacja/{credential.pk}/cofnij/", {"reason": "Uprawnienia odebrane przez PZSS"})
    assert response.status_code == 302
    credential.refresh_from_db()
    assert credential.status == Credential.Status.REJECTED
    assert credential.review_note == "Weryfikacja cofnięta: Uprawnienia odebrane przez PZSS"
    assert not is_range_officer(user)
    log = AuditLog.objects.get(action="dokument.cofniecie_weryfikacji")
    assert log.changes["powod"] == "Uprawnienia odebrane przez PZSS"
    assert any("Cofnięto weryfikację" in m.subject and m.to == [user.email] for m in mail.outbox)
    # drugi raz — już nie zweryfikowany
    owner_client.post(f"/panel/weryfikacja/{credential.pk}/cofnij/", {"reason": "x"})
    assert AuditLog.objects.filter(action="dokument.cofniecie_weryfikacji").count() == 1


def test_revoke_requires_permission_and_post(client, user):
    credential = add_credential(user)
    client.force_login(user)
    assert client.post(f"/panel/weryfikacja/{credential.pk}/cofnij/", {"reason": "x"}).status_code in (302, 403)
    credential.refresh_from_db()
    assert credential.status == Credential.Status.VERIFIED
