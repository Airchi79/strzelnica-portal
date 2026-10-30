import datetime

import pytest
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import User
from apps.credentials.models import Credential

PASSWORD = "Bardzo-Dlugie-Haslo-2026"


@pytest.fixture(autouse=True)
def _clear_cache(db):
    cache.clear()
    yield
    cache.clear()


def make_user(email="jan@example.com", verified=True, **extra):
    user = User.objects.create_user(email=email, password=PASSWORD, first_name="Jan", last_name="Kowalski", **extra)
    if verified:
        user.email_verified_at = timezone.now()
        user.save()
    return user


@pytest.fixture
def user(db):
    return make_user()


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(email="wlasciciel@example.com", password=PASSWORD, first_name="Adam", last_name="Właściciel")


def totp_token(device):
    return f"{totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits, drift=device.drift):06d}"


def login_with_otp(client, user):
    """Pełne logowanie członka zarządu: hasło + kod TOTP."""
    device = TOTPDevice.objects.filter(user=user, confirmed=True).first() or TOTPDevice.objects.create(
        user=user, name="test", confirmed=True
    )
    client.post("/konto/logowanie/", {"email": user.email, "password": PASSWORD})
    response = client.post("/konto/2fa/kod/", {"token": totp_token(device)})
    assert response.status_code == 302, response.content
    return device


def grant(user, *codes):
    for code in codes:
        app_label, codename = code.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))
    user.is_staff = True
    user.save()
    return User.objects.get(pk=user.pk)  # świeży obiekt — bez pamięci podręcznej uprawnień


@pytest.fixture
def owner_client(client, owner):
    login_with_otp(client, owner)
    return client


def add_credential(user, kind=Credential.Kind.RANGE_OFFICER, status=Credential.Status.VERIFIED, valid_until=None, number="PS-1"):
    return Credential.objects.create(
        user=user,
        kind=kind,
        number=number,
        issuer="PZSS",
        issued_on=datetime.date(2020, 1, 1),
        valid_until=valid_until,
        status=status,
    )
