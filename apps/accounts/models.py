import secrets

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


def new_qr_secret():
    return secrets.token_hex(16)


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Adres e-mail jest wymagany")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True, email_verified_at=timezone.now())
        return self._create_user(email, password, **extra)

    def get_by_natural_key(self, email):
        return self.get(email__iexact=email)


class User(AbstractBaseUser, PermissionsMixin):
    """Konto użytkownika. Logowanie adresem e-mail.

    `is_staff` oznacza dostęp do panelu zarządu (wymaga logowania dwuskładnikowego).
    Zakres uprawnień w panelu wynika z grup i uprawnień Django.
    """

    email = models.EmailField("e-mail", unique=True)
    first_name = models.CharField("imię", max_length=80)
    last_name = models.CharField("nazwisko", max_length=120)
    phone = models.CharField("telefon", max_length=32, blank=True)
    street = models.CharField("ulica i numer", max_length=160, blank=True)
    postal_code = models.CharField("kod pocztowy", max_length=10, blank=True)
    city = models.CharField("miejscowość", max_length=100, blank=True)

    is_active = models.BooleanField("aktywne", default=True)
    is_staff = models.BooleanField("dostęp do panelu zarządu", default=False)
    email_verified_at = models.DateTimeField("e-mail potwierdzony", null=True, blank=True)
    date_joined = models.DateTimeField("data rejestracji", default=timezone.now)
    deactivated_at = models.DateTimeField("dezaktywowane", null=True, blank=True)
    anonymized_at = models.DateTimeField("dane osobowe usunięte (RODO)", null=True, blank=True)
    # Sekret stałego kodu QR strzelca; zmiana unieważnia wcześniej wydane kody.
    qr_secret = models.CharField(max_length=32, default=new_qr_secret, editable=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        ordering = ["last_name", "first_name"]
        verbose_name = "użytkownik"
        verbose_name_plural = "użytkownicy"
        constraints = [models.UniqueConstraint(Lower("email"), name="user_email_ci_unique")]
        permissions = [
            ("manage_permissions", "Może nadawać uprawnienia w panelu zarządu"),
            ("view_users", "Może przeglądać konta użytkowników"),
        ]

    def __str__(self):
        return f"{self.full_name} <{self.email}>"

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def email_verified(self):
        return self.email_verified_at is not None

    @property
    def address(self):
        return ", ".join(p for p in [self.street, f"{self.postal_code} {self.city}".strip()] if p)

    @property
    def requires_2fa(self):
        return self.is_staff or self.is_superuser
