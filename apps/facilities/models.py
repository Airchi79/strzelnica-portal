import math
from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Facility(models.Model):
    class Kind(models.TextChoices):
        RIFLE_PISTOL = "kulowa", "Strzelnica kulowa"
        SHOTGUN = "srutowa", "Strzelnica śrutowa"

    name = models.CharField("nazwa", max_length=120)
    kind = models.CharField("rodzaj", max_length=16, choices=Kind.choices)
    is_active = models.BooleanField("aktywny", default=True)
    order = models.PositiveSmallIntegerField("kolejność", default=0)

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "obiekt"
        verbose_name_plural = "obiekty"

    def __str__(self):
        return self.name


class Lane(models.Model):
    """Rezerwowalna jednostka obiektu (oś strzelecka, w przyszłości także stanowisko śrutowe)."""

    class Kind(models.TextChoices):
        SHORT = "krotka", "Oś krótka"
        LONG = "dluga", "Oś długa"
        DYNAMIC = "dynamiczna", "Oś dynamiczno-myśliwska"
        SHOTGUN = "srutowa", "Stanowisko śrutowe"

    facility = models.ForeignKey(Facility, verbose_name="obiekt", on_delete=models.PROTECT, related_name="lanes")
    name = models.CharField("nazwa", max_length=80)
    kind = models.CharField("typ", max_length=16, choices=Kind.choices)
    length_m = models.PositiveIntegerField("długość [m]", null=True, blank=True)
    description = models.TextField("opis", blank=True)
    exclusive_only = models.BooleanField("tylko na wyłączność", default=False)
    is_active = models.BooleanField("aktywna", default=True)
    order = models.PositiveSmallIntegerField("kolejność", default=0)

    class Meta:
        ordering = ["facility__order", "order", "name"]
        verbose_name = "oś"
        verbose_name_plural = "osie"
        permissions = [("manage_facilities", "Może zarządzać osiami, godzinami i cennikiem")]

    def __str__(self):
        return self.name

    def hours_for(self, day: date):
        return self.opening_hours.filter(weekday=day.weekday()).first()

    def price_rule_for(self, day: date):
        return self.price_rules.filter(valid_from__lte=day).order_by("-valid_from", "-id").first()


class OpeningHours(models.Model):
    WEEKDAYS = [(0, "poniedziałek"), (1, "wtorek"), (2, "środa"), (3, "czwartek"), (4, "piątek"), (5, "sobota"), (6, "niedziela")]

    lane = models.ForeignKey(Lane, on_delete=models.CASCADE, related_name="opening_hours")
    weekday = models.PositiveSmallIntegerField("dzień tygodnia", choices=WEEKDAYS)
    opens = models.TimeField("otwarcie")
    closes = models.TimeField("zamknięcie")

    class Meta:
        ordering = ["lane", "weekday"]
        verbose_name = "godziny otwarcia"
        verbose_name_plural = "godziny otwarcia"
        constraints = [
            models.UniqueConstraint(fields=["lane", "weekday"], name="opening_hours_unique"),
            models.CheckConstraint(condition=models.Q(closes__gt=models.F("opens")), name="opening_hours_order"),
        ]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.opens:%H:%M}–{self.closes:%H:%M}"


class PriceRule(models.Model):
    """Cennik osi obowiązujący od danej daty. Kwoty w groszach.

    Opłata za godzinę = mniejsza z kwot: (liczba osób × stawka za osobę) albo ryczałt.
    Brak stawki za osobę oznacza, że oś jest rozliczana wyłącznie ryczałtem.
    """

    lane = models.ForeignKey(Lane, verbose_name="oś", on_delete=models.CASCADE, related_name="price_rules")
    per_person_hourly = models.PositiveIntegerField("stawka za osobę / h [gr]", null=True, blank=True)
    flat_hourly = models.PositiveIntegerField("ryczałt / h [gr]")
    valid_from = models.DateField("obowiązuje od")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["lane", "-valid_from"]
        verbose_name = "cennik"
        verbose_name_plural = "cenniki"
        constraints = [models.UniqueConstraint(fields=["lane", "valid_from"], name="price_rule_unique_from")]

    def __str__(self):
        return f"{self.lane} od {self.valid_from}"

    def hourly_amount(self, persons: int) -> int:
        if persons < 1:
            raise ValidationError("Liczba osób musi wynosić co najmniej 1.")
        if self.per_person_hourly is None:
            return self.flat_hourly
        return min(persons * self.per_person_hourly, self.flat_hourly)

    def amount(self, persons: int, hours) -> int:
        """Należność w groszach za `hours` godzin (rezerwacje są w pełnych godzinach)."""
        if hours <= 0 or hours != math.floor(hours):
            raise ValidationError("Rezerwacja musi obejmować pełne godziny.")
        return self.hourly_amount(persons) * int(hours)


def format_pln(grosze: int) -> str:
    zl, gr = divmod(int(grosze), 100)
    return f"{zl:,}".replace(",", " ") + (f",{gr:02d}" if gr else "") + " zł"
