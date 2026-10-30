from django.conf import settings
from django.db import models


class AssociationSettings(models.Model):
    """Dane stowarzyszenia (jeden rekord)."""

    name = models.CharField("nazwa", max_length=160, default="Stowarzyszenie STRZELNICA SĄTOK")
    address = models.CharField("adres", max_length=255, blank=True)
    nip = models.CharField("NIP", max_length=13, blank=True)
    regon = models.CharField("REGON", max_length=14, blank=True)
    bank_account = models.CharField("numer rachunku", max_length=40, blank=True)
    contact_email = models.EmailField("e-mail kontaktowy", blank=True)
    contact_phone = models.CharField("telefon kontaktowy", max_length=32, blank=True)
    notify_email = models.EmailField("e-mail powiadomień zarządu", blank=True)

    class Meta:
        verbose_name = "ustawienia stowarzyszenia"
        verbose_name_plural = "ustawienia stowarzyszenia"
        permissions = [("manage_settings", "Może zmieniać dane stowarzyszenia")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Notification(models.Model):
    """Powiadomienie w panelu zarządu (lista „Powiadomienia”)."""

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=500, blank=True)
    url = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
