"""Kartoteka strzelców (notatki, tagi, zatwierdzenie) i kody wejścia strzelców (karnety, abonamenty)."""

import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q


class ClientTag(models.Model):
    name = models.CharField("nazwa", max_length=40, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "tag strzelca"
        verbose_name_plural = "tagi strzelców"

    def __str__(self):
        return self.name


class ClientProfile(models.Model):
    """Dane kartoteki prowadzone przez obsługę — niewidoczne dla strzelca."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="client_profile")
    notes = models.TextField("notatki obsługi", blank=True, max_length=4000)
    tags = models.ManyToManyField(ClientTag, verbose_name="tagi", blank=True, related_name="profiles")
    approved_at = models.DateTimeField("zatwierdzony jako stały strzelec", null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "kartoteka strzelca"
        verbose_name_plural = "kartoteka strzelców"
        permissions = [("manage_clients", "Może prowadzić kartotekę strzelców i wydawać kody wejścia")]

    def __str__(self):
        return f"Kartoteka: {self.user}"


def new_pass_code():
    # Ten sam alfabet co kody klubów (bez mylących znaków); prefiks W odróżnia kody wejścia.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "W" + "".join(secrets.choice(alphabet) for _ in range(7))


class ClientPass(models.Model):
    """Kod wejścia strzelca: przedpłacony karnet (N wejść) albo abonament (bez limitu wejść) ważny w okresie.

    Jedno wejście = jedna rezerwacja posiadacza kodu (w granicach limitu godzin i osób). Sprzedaż kodu jest
    przychodem w chwili zapłaty; rezerwacje z kodem nie mają już osobnej należności.
    """

    class Kind(models.TextChoices):
        SINGLE = "pojedyncze", "Pojedyncze wejście"
        PACK = "karnet", "Karnet (liczba wejść)"
        SUBSCRIPTION = "abonament", "Abonament (bez limitu wejść)"

    class PaidWith(models.TextChoices):
        UNPAID = "", "Nieopłacony"
        CASH = "gotowka", "Gotówka"
        TRANSFER = "przelew", "Przelew"
        CARD = "karta", "Karta"
        FREE = "bezplatny", "Bezpłatny (np. nagroda)"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="strzelec", on_delete=models.PROTECT, related_name="client_passes")
    code = models.CharField("kod", max_length=16, unique=True, default=new_pass_code)
    kind = models.CharField("rodzaj", max_length=12, choices=Kind.choices, default=Kind.PACK)
    entries_limit = models.PositiveIntegerField("liczba wejść", null=True, blank=True, help_text="Puste = bez limitu (abonament).")
    valid_from = models.DateField("ważny od")
    valid_until = models.DateField("ważny do")
    max_hours = models.PositiveIntegerField("maks. godzin na jedno wejście", default=2)
    max_persons = models.PositiveIntegerField("maks. osób na jedno wejście", default=1)
    lanes = models.ManyToManyField("facilities.Lane", verbose_name="dozwolone osie", blank=True, help_text="Puste = wszystkie osie.")
    price = models.PositiveIntegerField("opłata [gr]", default=0)
    paid_with = models.CharField("zapłata", max_length=12, choices=PaidWith.choices, blank=True, default="")
    paid_at = models.DateTimeField("zapłacono", null=True, blank=True)
    is_active = models.BooleanField("aktywny", default=True)
    note = models.CharField("uwagi", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-valid_until", "-id"]
        verbose_name = "kod wejścia"
        verbose_name_plural = "kody wejścia"
        constraints = [
            models.CheckConstraint(condition=Q(valid_until__gte=models.F("valid_from")), name="pass_period_order"),
            models.CheckConstraint(condition=Q(max_hours__gte=1) & Q(max_persons__gte=1), name="pass_limits_positive"),
            models.CheckConstraint(condition=Q(entries_limit__isnull=True) | Q(entries_limit__gte=1), name="pass_entries_positive"),
        ]

    def __str__(self):
        return f"{self.code} ({self.get_kind_display()})"

    @property
    def is_paid(self):
        return bool(self.paid_with)


class Person(models.Model):
    """Osoba z kartoteki bez konta w systemie (np. strzelcy przeniesieni z GunTrackera). Służy obsłudze do szybkiego
    uzupełnienia wpisu w książce pobytu; dane są widoczne tylko w panelu (z rejestrem wglądu)."""

    class Source(models.TextChoices):
        GUNTRACKER = "guntracker", "Import z GunTrackera"
        PANEL = "panel", "Dodana w panelu"

    first_name = models.CharField("imię", max_length=80)
    last_name = models.CharField("nazwisko", max_length=120, db_index=True)
    permit_number = models.CharField("numer pozwolenia na broń", max_length=64, blank=True)
    permit_issuer = models.CharField("organ, który wydał pozwolenie", max_length=160, blank=True)
    address = models.CharField("adres", max_length=255, blank=True)
    phone = models.CharField("telefon", max_length=32, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="konto", null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="kartoteka_persons")
    source = models.CharField("pochodzenie", max_length=12, choices=Source.choices, default=Source.PANEL)
    note = models.CharField("uwagi", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        verbose_name = "osoba z kartoteki"
        verbose_name_plural = "osoby z kartoteki"

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def complete_for_logbook(self):
        """Wystarcza do wpisu w książce: pozwolenie z organem albo adres."""
        return bool((self.permit_number and self.permit_issuer) or self.address)
