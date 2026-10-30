import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import Func, Q
from django.utils import timezone

from apps.facilities.models import Lane, PriceRule


class TsTzRange(Func):
    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class BookingSettings(models.Model):
    """Ustawienia rezerwacji (jeden rekord)."""

    free_cancel_hours = models.PositiveIntegerField("bezpłatne anulowanie do [h] przed terminem", default=48)
    max_advance_days = models.PositiveIntegerField("rezerwacja najwyżej [dni] naprzód", default=180)
    max_persons = models.PositiveIntegerField("maksymalna liczba osób w rezerwacji", default=50)

    class Meta:
        verbose_name = "ustawienia rezerwacji"
        verbose_name_plural = "ustawienia rezerwacji"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class NumberSequence(models.Model):
    """Licznik numerów rezerwacji w danym roku (blokowany wierszowo przy nadawaniu)."""

    year = models.PositiveIntegerField(primary_key=True)
    last = models.PositiveIntegerField(default=0)


class Reservation(models.Model):
    class Status(models.TextChoices):
        CONFIRMED = "potwierdzona", "Potwierdzona"
        CANCELLED = "anulowana", "Anulowana"
        NO_SHOW = "nieodbyta", "Nieodbyta"

    class Payment(models.TextChoices):
        DUE = "do_zaplaty", "Do zapłaty"
        PAID = "oplacona", "Opłacona"
        WAIVED = "zwolniona", "Zwolniona z opłaty"
        NONE = "brak", "Bez opłaty (anulowana)"

    number = models.CharField("numer", max_length=24, unique=True)
    lane = models.ForeignKey(Lane, verbose_name="oś", on_delete=models.PROTECT, related_name="reservations")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="rezerwujący (PS)", on_delete=models.PROTECT, related_name="reservations"
    )
    starts_at = models.DateTimeField("początek", db_index=True)
    ends_at = models.DateTimeField("koniec")
    persons = models.PositiveIntegerField("liczba osób")
    notes = models.CharField("uwagi", max_length=500, blank=True)

    price_rule = models.ForeignKey(PriceRule, verbose_name="cennik", on_delete=models.PROTECT, related_name="+")
    hourly_amount = models.PositiveIntegerField("stawka za godzinę [gr]")
    amount = models.PositiveIntegerField("należność [gr]")
    paid_amount = models.PositiveIntegerField("zapłacono [gr]", default=0)

    status = models.CharField("status", max_length=16, choices=Status.choices, default=Status.CONFIRMED, db_index=True)
    payment_status = models.CharField("płatność", max_length=16, choices=Payment.choices, default=Payment.DUE, db_index=True)
    late_cancellation = models.BooleanField("anulowana po terminie bezpłatnego anulowania", default=False)
    cancelled_at = models.DateTimeField("anulowano", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="anulował", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cancel_reason = models.CharField("powód anulowania", max_length=255, blank=True)
    payment_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    payment_marked_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    # Rozliczenie po zakończeniu: dopłata, gdy w książce pobytu było więcej strzelających niż zadeklarowano.
    settled_at = models.DateTimeField("rozliczono po terminie", null=True, blank=True)
    declared_persons = models.PositiveIntegerField("zadeklarowana liczba osób", null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField("utworzono", auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at", "lane__order"]
        verbose_name = "rezerwacja"
        verbose_name_plural = "rezerwacje"
        permissions = [("manage_reservations", "Może zarządzać rezerwacjami i blokadami osi")]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="reservation_positive_length"),
            models.CheckConstraint(condition=Q(persons__gte=1), name="reservation_persons_min"),
            # Gwarancja na poziomie bazy: na jednej osi aktywne rezerwacje nie mogą się nakładać w czasie.
            ExclusionConstraint(
                name="reservation_no_overlap",
                expressions=[
                    ("lane", RangeOperators.EQUAL),
                    (TsTzRange("starts_at", "ends_at"), RangeOperators.OVERLAPS),
                ],
                condition=~Q(status="anulowana"),
            ),
        ]

    def __str__(self):
        return f"{self.number} · {self.lane} · {timezone.localtime(self.starts_at):%d.%m.%Y %H:%M}"

    @property
    def hours(self):
        return int((self.ends_at - self.starts_at).total_seconds() // 3600)

    @property
    def is_active(self):
        return self.status == self.Status.CONFIRMED

    @property
    def is_finished(self):
        return self.status == self.Status.CONFIRMED and self.ends_at <= timezone.now()

    @property
    def display_status(self):
        if self.is_finished:
            return "Zakończona"
        return self.get_status_display()

    @property
    def free_cancel_deadline(self):
        return self.starts_at - timedelta(hours=BookingSettings.load().free_cancel_hours)

    def can_user_cancel(self, now=None):
        return self.status == self.Status.CONFIRMED and self.starts_at > (now or timezone.now())


class Block(models.Model):
    """Blokada administracyjna osi (zawody, prace, awaria). Wpisy cykliczne mają wspólny `series`."""

    title = models.CharField("tytuł", max_length=120)
    reason = models.CharField("opis", max_length=255, blank=True)
    lanes = models.ManyToManyField(Lane, verbose_name="osie", related_name="blocks")
    starts_at = models.DateTimeField("początek", db_index=True)
    ends_at = models.DateTimeField("koniec")
    series = models.UUIDField("seria", default=uuid.uuid4, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["starts_at"]
        verbose_name = "blokada osi"
        verbose_name_plural = "blokady osi"
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="block_positive_length")]

    def __str__(self):
        return f"{self.title} · {timezone.localtime(self.starts_at):%d.%m.%Y %H:%M}–{timezone.localtime(self.ends_at):%H:%M}"
