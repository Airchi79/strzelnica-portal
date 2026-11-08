"""Urządzenia kiosku (tablet przy wejściu, Raspberry Pi z czytnikiem). Parowanie jednorazowym kodem z panelu;
urządzenie dostaje losowy token (w bazie przechowywany wyłącznie jako skrót SHA-256)."""

import hashlib
import secrets

from django.conf import settings
from django.db import models


def token_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class KioskDevice(models.Model):
    class Camera(models.TextChoices):
        FRONT = "user", "przednia (urządzenie zamocowane na stałe)"
        BACK = "environment", "tylna (urządzenie trzymane w ręku)"

    name = models.CharField("nazwa urządzenia", max_length=80)
    token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    pairing_code_hash = models.CharField(max_length=64, blank=True)
    pairing_expires_at = models.DateTimeField(null=True, blank=True)
    paired_at = models.DateTimeField("sparowano", null=True, blank=True)
    last_seen_at = models.DateTimeField("ostatni kontakt", null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    is_active = models.BooleanField("aktywne", default=True)
    camera = models.CharField("kamera do kodów QR", max_length=12, choices=Camera.choices, default=Camera.FRONT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "urządzenie kiosku"
        verbose_name_plural = "urządzenia kiosku"

    def __str__(self):
        return self.name

    @staticmethod
    def new_pairing_code():
        return f"{secrets.randbelow(10**8):08d}"

    @staticmethod
    def new_token():
        return secrets.token_urlsafe(32)


class OnsiteBooking(models.Model):
    """Rezerwacja na miejscu w kiosku — przebieg krok po kroku (dane → kod z e-maila → regulamin i oświadczenie →
    legitymacja PS → rezerwacja). Konto osoby zakładane jest w tle (bez hasła) po potwierdzeniu adresu e-mail kodem.
    Dane przebiegu są usuwane po kilku dniach; trwałe są konto, dokument i rezerwacja."""

    class Status(models.TextChoices):
        CODE_SENT = "kod", "Wysłano kod e-mail"
        VERIFIED = "potwierdzony", "E-mail potwierdzony"
        AWAITING = "weryfikacja", "Rezerwacja czeka na weryfikację"
        BOOKED = "zarezerwowano", "Zarezerwowano"
        EXPIRED = "wygasla", "Wygasła (brak weryfikacji)"
        REJECTED = "odrzucona", "Odrzucona"

    ref = models.UUIDField(unique=True, editable=False)
    secret_hash = models.CharField(max_length=64)
    device = models.ForeignKey(KioskDevice, on_delete=models.CASCADE, related_name="onsite_bookings")
    status = models.CharField("stan", max_length=16, choices=Status.choices, default=Status.CODE_SENT, db_index=True)
    email = models.EmailField("e-mail")
    first_name = models.CharField("imię", max_length=80)
    last_name = models.CharField("nazwisko", max_length=120)
    phone = models.CharField("telefon", max_length=32)
    code_hash = models.CharField(max_length=64, blank=True)
    code_expires_at = models.DateTimeField(null=True, blank=True)
    code_attempts = models.PositiveSmallIntegerField(default=0)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    statement_at = models.DateTimeField("oświadczenie o prawdziwości danych", null=True, blank=True)
    credential = models.ForeignKey("credentials.Credential", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reservation = models.ForeignKey("bookings.Reservation", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    awaiting_until = models.DateTimeField("czeka na weryfikację do", null=True, blank=True, db_index=True)
    # Wynik automatycznego porównania danych z legitymacją (bez odczytanego tekstu — legitymacje zawierają PESEL).
    ocr_result = models.CharField("automatyczne porównanie z legitymacją", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "rezerwacja na miejscu (kiosk)"
        verbose_name_plural = "rezerwacje na miejscu (kiosk)"

    def __str__(self):
        return f"{self.first_name} {self.last_name} · {self.get_status_display()}"
