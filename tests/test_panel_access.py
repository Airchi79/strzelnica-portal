import pytest
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import User
from apps.audit.models import AuditLog

from .conftest import PASSWORD, grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db

PANEL_URLS = ["/panel/", "/panel/weryfikacja/", "/panel/uzytkownicy/", "/panel/osie/", "/panel/regulaminy/", "/panel/ustawienia/", "/panel/dziennik/"]


def test_anonymous_redirected_to_login(client):
    for url in PANEL_URLS:
        response = client.get(url)
        assert response.status_code == 302 and response.url.startswith("/konto/logowanie/"), url


def test_regular_user_forbidden(client, user):
    client.force_login(user)
    for url in PANEL_URLS:
        assert client.get(url).status_code == 403, url


def test_staff_without_otp_session_must_verify(client):
    staff = grant(make_user("z@example.com"), "credentials.review_credential")
    TOTPDevice.objects.create(user=staff, name="t", confirmed=True)
    client.force_login(staff)  # sesja bez drugiego składnika
    response = client.get("/panel/weryfikacja/")
    assert response.url == "/konto/2fa/kod/"


def test_staff_sees_only_granted_sections(client):
    staff = grant(make_user("z@example.com"), "credentials.review_credential")
    login_with_otp(client, staff)
    assert client.get("/panel/weryfikacja/").status_code == 200
    for url in ["/panel/uzytkownicy/", "/panel/osie/", "/panel/regulaminy/", "/panel/ustawienia/", "/panel/dziennik/"]:
        assert client.get(url).status_code == 403, url


def test_owner_sees_everything(owner_client):
    for url in PANEL_URLS:
        assert owner_client.get(url).status_code == 200, url


def test_owner_grants_and_revokes_permissions(owner_client, owner):
    member = make_user("czlonek@example.com")
    response = owner_client.post(f"/panel/uzytkownicy/{member.pk}/", {"permissions": ["credentials.review_credential", "audit.view_auditlog"]})
    assert response.status_code == 302
    member = User.objects.get(pk=member.pk)
    assert member.is_staff and member.has_perm("credentials.review_credential") and member.has_perm("audit.view_auditlog")
    assert AuditLog.objects.filter(action="uprawnienia.zmiana", object_id=str(member.pk)).exists()

    owner_client.post(f"/panel/uzytkownicy/{member.pk}/", {})
    member = User.objects.get(pk=member.pk)
    assert not member.is_staff and not member.has_perm("credentials.review_credential")


def test_cannot_change_own_or_owner_permissions(client, owner):
    admin = grant(make_user("admin2@example.com"), "accounts.view_users", "accounts.manage_permissions")
    login_with_otp(client, admin)
    assert client.post(f"/panel/uzytkownicy/{admin.pk}/", {"permissions": ["core.manage_settings"]}).status_code == 403
    assert client.post(f"/panel/uzytkownicy/{owner.pk}/", {}).status_code == 403
    assert not User.objects.get(pk=admin.pk).has_perm("core.manage_settings")


def test_permission_manager_needs_manage_perm(client):
    viewer = grant(make_user("v@example.com"), "accounts.view_users")
    other = make_user("o@example.com")
    login_with_otp(client, viewer)
    assert client.get(f"/panel/uzytkownicy/{other.pk}/").status_code == 200
    assert client.post(f"/panel/uzytkownicy/{other.pk}/", {"permissions": ["core.manage_settings"]}).status_code == 403


def test_technical_admin_requires_otp(client, owner):
    client.post("/konto/logowanie/", {"email": owner.email, "password": PASSWORD})
    response = client.get("/techniczny/")
    assert response.status_code == 302  # bez weryfikacji OTP — przekierowanie do logowania panelu technicznego
