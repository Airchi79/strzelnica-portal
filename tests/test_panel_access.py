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


def test_duty_preset_grants_limited_access(owner_client, client):
    from datetime import timedelta

    from django.utils import timezone

    from apps.bookings import services as booking_services
    from apps.payments.models import Payment
    from apps.facilities.models import Lane

    from .conftest import add_credential, approve, login_with_otp

    duty = make_user("dyzur@example.com")
    response = owner_client.post(f"/panel/uzytkownicy/{duty.pk}/", {"preset": "dyzurny"})
    assert response.status_code == 302
    duty.refresh_from_db()
    assert duty.is_staff
    assert duty.has_perm("bookings.manage_reservations") and duty.has_perm("payments.accept_onsite")
    assert not duty.has_perm("payments.manage_payments") and not duty.has_perm("bookings.manage_booking_settings")
    assert AuditLog.objects.filter(action="uprawnienia.zmiana", changes__zestaw__isnull=False).exists()
    owner_client.logout()

    shooter = make_user("ps-dyzur@example.com")
    add_credential(shooter)
    approve(shooter)
    lane = Lane.objects.filter(is_active=True).first()
    day = timezone.localdate() + timedelta(days=1)
    while lane.hours_for(day) is None:
        day += timedelta(days=1)
    hours = lane.hours_for(day)
    r = booking_services.create_reservation(shooter, lane, day, hours.opens.hour, hours.opens.hour + 1, 2)

    login_with_otp(client, duty)
    assert client.get("/panel/dzis/").status_code == 200
    assert client.get("/panel/rezerwacje/ustawienia/").status_code == 403
    assert client.get("/panel/platnosci/import/").status_code == 403
    assert client.get("/panel/platnosci/").status_code == 403
    # bez rezerwacji — odesłanie na „Dziś”
    assert client.get("/panel/platnosci/wplata/").url == "/panel/dzis/"
    page = client.get(f"/panel/platnosci/wplata/?user={shooter.pk}&rezerwacja={r.pk}&kwota=10")
    assert [c[0] for c in page.context["form"].fields["method"].choices] == ["gotowka", "blik"]
    # przelew odrzucony, gotówka przyjęta
    bad = client.post(f"/panel/platnosci/wplata/?rezerwacja={r.pk}", {"user": shooter.pk, "amount": "10", "method": "przelew", "reservation": r.pk})
    assert bad.status_code == 200 and not Payment.objects.exists()
    ok = client.post(f"/panel/platnosci/wplata/?rezerwacja={r.pk}", {"user": shooter.pk, "amount": "10", "method": "gotowka", "reservation": r.pk})
    assert ok.status_code == 302 and ok.url == f"/panel/rezerwacje/{r.pk}/"
    p = Payment.objects.get()
    assert p.method == "gotowka" and p.created_by == duty and p.reservation_id == r.pk


def test_existing_staff_keep_settings_after_permission_split():
    import importlib

    from django.apps import apps as django_apps
    from django.contrib.auth.models import Permission

    from apps.core.permissions import PANEL_PERMISSIONS, ROLE_PRESETS

    assert set(ROLE_PRESETS["dyzurny"]["permissions"]) <= {c for c, _ in PANEL_PERMISSIONS}
    staff = grant(make_user("stary@example.com"), "bookings.manage_reservations", "payments.manage_payments")
    for codename in ("manage_booking_settings", "accept_onsite"):
        staff.user_permissions.remove(Permission.objects.get(codename=codename))
    staff = User.objects.get(pk=staff.pk)
    assert not staff.has_perm("bookings.manage_booking_settings")
    migration = importlib.import_module("apps.bookings.migrations.0010_split_permissions")
    migration.forwards(django_apps, None)
    staff = User.objects.get(pk=staff.pk)
    assert staff.has_perm("bookings.manage_booking_settings") and staff.has_perm("payments.accept_onsite")
