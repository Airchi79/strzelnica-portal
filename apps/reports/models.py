"""Numerowane raporty sprzedaży (dzienne, miesięczne). Raport to zamrożony obraz danych z chwili wygenerowania —
nie można go zmienić ani usunąć (trigger w bazie); późniejsze korekty trafiają do raportu za okres, w którym je zaksięgowano."""

import hashlib
import json

from django.conf import settings
from django.db import models


class ReportSequence(models.Model):
    kind = models.CharField(max_length=1)
    year = models.PositiveIntegerField()

    last = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "year"], name="report_sequence_unique")]


class SalesReport(models.Model):
    class Kind(models.TextChoices):
        DAILY = "D", "Dzienny"
        MONTHLY = "M", "Miesięczny"

    kind = models.CharField("rodzaj", max_length=1, choices=Kind.choices)
    number = models.CharField("numer", max_length=24, unique=True)
    period_start = models.DateField("okres od")
    period_end = models.DateField("okres do")
    data = models.JSONField("dane raportu")
    sha256 = models.CharField("suma kontrolna", max_length=64)
    generated_at = models.DateTimeField("wygenerowano", auto_now_add=True)
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-period_start", "-kind"]
        verbose_name = "raport sprzedaży"
        verbose_name_plural = "raporty sprzedaży"
        constraints = [
            models.UniqueConstraint(fields=["kind", "period_start"], name="report_one_per_period"),
            models.CheckConstraint(condition=models.Q(period_end__gte=models.F("period_start")), name="report_period_order"),
        ]

    def __str__(self):
        return self.number

    @staticmethod
    def hash_data(data):
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @property
    def integrity_ok(self):
        return self.hash_data(self.data) == self.sha256
