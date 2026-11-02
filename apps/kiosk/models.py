"""Urządzenia kiosku (tablet przy wejściu, Raspberry Pi z czytnikiem). Parowanie jednorazowym kodem z panelu;
urządzenie dostaje losowy token (w bazie przechowywany wyłącznie jako skrót SHA-256)."""

import hashlib
import secrets

from django.conf import settings
from django.db import models


def token_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class KioskDevice(models.Model):
    name = models.CharField("nazwa urządzenia", max_length=80)
    token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    pairing_code_hash = models.CharField(max_length=64, blank=True)
    pairing_expires_at = models.DateTimeField(null=True, blank=True)
    paired_at = models.DateTimeField("sparowano", null=True, blank=True)
    last_seen_at = models.DateTimeField("ostatni kontakt", null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    is_active = models.BooleanField("aktywne", default=True)
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
