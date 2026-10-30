from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Dziennik zmian. Wpisów nie można zmienić ani usunąć (blokada na poziomie bazy danych)."""

    at = models.DateTimeField("czas", auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="kto", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField("zdarzenie", max_length=64, db_index=True)
    object_type = models.CharField("typ obiektu", max_length=64, blank=True)
    object_id = models.CharField("id obiektu", max_length=64, blank=True)
    summary = models.CharField("opis", max_length=255, blank=True)
    changes = models.JSONField("zmiany", default=dict, blank=True)
    ip = models.GenericIPAddressField("adres IP", null=True, blank=True)

    class Meta:
        ordering = ["-at", "-id"]
        verbose_name = "wpis dziennika"
        verbose_name_plural = "dziennik zdarzeń"

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.action}"


class SensitiveAccessLog(models.Model):
    """Rejestr wglądu w dane wrażliwe (dokumenty, pozwolenia) — wymóg rozliczalności RODO."""

    at = models.DateTimeField("czas", auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="kto", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="czyje dane", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    resource = models.CharField("zasób", max_length=128)
    reason = models.CharField("cel", max_length=255, blank=True)
    ip = models.GenericIPAddressField("adres IP", null=True, blank=True)

    class Meta:
        ordering = ["-at", "-id"]
        verbose_name = "wgląd w dane wrażliwe"
        verbose_name_plural = "wglądy w dane wrażliwe"
