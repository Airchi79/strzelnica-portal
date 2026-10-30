import re

import pytest
from django.core import mail
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import User
from apps.audit.models import AuditLog

from .conftest import PASSWORD, make_user, totp_token

pytestmark = pytest.mark.django_db


def register(client, email="Nowy@Example.com", password=PASSWORD):
    return client.post(
        "/konto/rejestracja/",
        {"email": email, "first_name": "Ewa", "last_name": "Nowak", "phone": "600 100 200", "password1": password, "password2": password},
    )


def test_registration_verification_and_login(client, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        response = register(client)
    assert response.status_code == 200
    user = User.objects.get()
    assert user.email == "nowy@example.com"
    assert not user.email_verified

    # Logowanie przed potwierdzeniem e-maila jest blokowane.
    response = client.post("/konto/logowanie/", {"email": "nowy@example.com", "password": PASSWORD})
    assert "potwierdź adres e-mail" in response.content.decode()

    link = re.search(r"http://testserver(/konto/potwierdz/\S+)", mail.outbox[0].body).group(1)
    assert client.get(link).status_code == 302
    user.refresh_from_db()
    assert user.email_verified

    response = client.post("/konto/logowanie/", {"email": "NOWY@example.com", "password": PASSWORD})
    assert response.status_code == 302 and response.url == "/pulpit/"
    assert AuditLog.objects.filter(action="konto.logowanie", actor=user).exists()


def test_registration_rejects_weak_password_and_duplicate_email(client, user):
    response = register(client, password="kowalski")
    assert response.status_code == 200 and "password1" in response.context["form"].errors
    response = register(client, email="JAN@example.com")
    assert "email" in response.context["form"].errors


def test_verification_link_is_single_purpose(client, user):
    response = client.get("/konto/potwierdz/xx/zly-token/")
    assert response.status_code == 302 and "ponownie" in response.url


def test_login_rate_limit(client, user):
    for _ in range(5):
        client.post("/konto/logowanie/", {"email": user.email, "password": "zle-haslo-123456"})
    response = client.post("/konto/logowanie/", {"email": user.email, "password": PASSWORD})
    assert "Zbyt wiele nieudanych prób" in response.content.decode()
    assert "_auth_user_id" not in client.session


def test_open_redirect_is_blocked(client, user):
    response = client.post("/konto/logowanie/?next=https://evil.example/", {"email": user.email, "password": PASSWORD})
    assert response.url == "/pulpit/"


def test_staff_must_set_up_2fa_before_login(client, owner):
    response = client.post("/konto/logowanie/", {"email": owner.email, "password": PASSWORD})
    assert response.url == "/konto/2fa/konfiguracja/"
    assert "_auth_user_id" not in client.session  # bez drugiego składnika nie ma sesji

    client.get("/konto/2fa/konfiguracja/")
    device = TOTPDevice.objects.get(user=owner, confirmed=False)
    response = client.post("/konto/2fa/konfiguracja/", {"token": "000000" if totp_token(device) != "000000" else "111111"})
    assert response.status_code == 200 and "_auth_user_id" not in client.session

    device.refresh_from_db()
    device.throttle_reset()  # django-otp wstrzymuje kolejną próbę po błędnym kodzie
    response = client.post("/konto/2fa/konfiguracja/", {"token": totp_token(device)})
    assert response.status_code == 302
    assert client.get("/panel/").status_code == 200


def test_staff_login_with_existing_device(client, owner):
    device = TOTPDevice.objects.create(user=owner, name="tel", confirmed=True)
    response = client.post("/konto/logowanie/", {"email": owner.email, "password": PASSWORD})
    assert response.url == "/konto/2fa/kod/"
    client.post("/konto/2fa/kod/", {"token": totp_token(device)})
    assert client.get("/panel/").status_code == 200


def test_password_reset_flow(client, user, django_capture_on_commit_callbacks):
    client.post("/konto/haslo/reset/", {"email": user.email})
    link = re.search(r"http://testserver(/konto/haslo/reset/\S+)", mail.outbox[0].body).group(1)
    response = client.get(link, follow=True)
    new = "Inne-Dlugie-Haslo-2027"
    response = client.post(response.redirect_chain[-1][0], {"new_password1": new, "new_password2": new})
    assert response.status_code == 302
    assert User.objects.get(pk=user.pk).check_password(new)


def test_profile_validation_and_audit(client, user):
    client.force_login(user)
    response = client.post("/konto/profil/", {"first_name": "Jan", "last_name": "Kowalski", "postal_code": "56420", "phone": "", "street": "", "city": ""})
    assert "postal_code" in response.context["form"].errors
    client.post("/konto/profil/", {"first_name": "Jan", "last_name": "Kowalski", "postal_code": "56-420", "phone": "+48 600 100 200", "street": "Polna 1", "city": "Bierutów"})
    user.refresh_from_db()
    assert user.city == "Bierutów"
    log = AuditLog.objects.get(action="konto.profil_zmiana")
    assert "city" in log.changes


def test_last_owner_cannot_deactivate(client, owner):
    client.force_login(owner)
    response = client.post("/konto/profil/dezaktywuj/", {"password": PASSWORD})
    assert "jedynym administratorem" in response.content.decode()
    owner.refresh_from_db()
    assert owner.is_active


def test_user_can_deactivate(client):
    user = make_user()
    client.force_login(user)
    client.post("/konto/profil/dezaktywuj/", {"password": PASSWORD})
    user.refresh_from_db()
    assert not user.is_active and user.deactivated_at
