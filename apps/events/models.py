from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Event(models.Model):
    """Wydarzenie (zawody, szkolenie): zapisy online z limitem miejsc i listą rezerwową, opłata startowa, blokada osi."""

    class Status(models.TextChoices):
        DRAFT = "szkic", "Szkic (niewidoczne)"
        PUBLISHED = "opublikowane", "Opublikowane — zapisy"
        CANCELLED = "odwolane", "Odwołane"

    title = models.CharField("nazwa", max_length=160)
    description = models.TextField("opis", max_length=5000, blank=True, help_text="Program, konkurencje, wymagany sprzęt, zasady.")
    starts_at = models.DateTimeField("początek")
    ends_at = models.DateTimeField("koniec")
    lanes = models.ManyToManyField("facilities.Lane", verbose_name="osie zajęte przez wydarzenie", blank=True, related_name="+")
    capacity = models.PositiveIntegerField("limit uczestników", null=True, blank=True, help_text="Puste — bez limitu.")
    fee = models.PositiveIntegerField("opłata startowa [gr]", default=0)
    registration_until = models.DateTimeField("zapisy do")
    payment_days = models.PositiveIntegerField(
        "opłata startowa: czas na zapłatę [dni]", default=3,
        help_text="Liczony od zapisu (albo wejścia z listy rezerwowej), najpóźniej do zamknięcia zapisów. Dzień przed terminem "
                  "przypomnienie; po terminie nieopłacony zapis zostaje wycofany, a miejsce dostaje pierwsza osoba z listy "
                  "rezerwowej. 0 — bez terminu.",
    )
    note_label = models.CharField("dodatkowe pole przy zapisie", max_length=80, blank=True,
                                  help_text="Np. „Klasa / kategoria” albo „Klub”. Puste — bez dodatkowego pola.")
    status = models.CharField("status", max_length=14, choices=Status.choices, default=Status.DRAFT, db_index=True)
    block = models.ForeignKey("bookings.Block", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["starts_at"]
        verbose_name = "wydarzenie"
        verbose_name_plural = "wydarzenia"
        permissions = [("manage_events", "Może prowadzić wydarzenia i listy startowe")]
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="event_positive_length")]

    def __str__(self):
        return self.title

    @property
    def registration_open(self):
        return self.status == self.Status.PUBLISHED and timezone.now() < self.registration_until

    def taken(self):
        return self.registrations.filter(status=Registration.Status.REGISTERED).count()

    def free_places(self):
        return None if self.capacity is None else max(self.capacity - self.taken(), 0)


class Registration(models.Model):
    class Status(models.TextChoices):
        REGISTERED = "zapisany", "Zapisany"
        RESERVE = "rezerwowy", "Lista rezerwowa"
        CANCELLED = "anulowany", "Wycofany"

    class Payment(models.TextChoices):
        DUE = "do_zaplaty", "Do zapłaty"
        PAID = "oplacona", "Opłacona"
        FREE = "bez_oplaty", "Bez opłaty"
        WAIVED = "zwolniona", "Zwolniona z opłaty"

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="registrations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="event_registrations")
    status = models.CharField("status", max_length=10, choices=Status.choices, default=Status.REGISTERED, db_index=True)
    note = models.CharField("dodatkowa informacja", max_length=120, blank=True)
    amount = models.PositiveIntegerField("opłata [gr]", default=0)
    paid_amount = models.PositiveIntegerField("zapłacono [gr]", default=0)
    payment_status = models.CharField("płatność", max_length=10, choices=Payment.choices, default=Payment.FREE)
    created_at = models.DateTimeField("zapisano", auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    pay_by = models.DateTimeField("opłać do (inaczej zapis zostanie wycofany)", null=True, blank=True)
    pay_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name = "zapis na wydarzenie"
        verbose_name_plural = "zapisy na wydarzenia"
        constraints = [models.UniqueConstraint(fields=["event", "user"], condition=~Q(status="anulowany"), name="registration_one_active")]

    def __str__(self):
        return f"{self.user.full_name} — {self.event}"

    @property
    def label(self):
        return f"Opłata startowa: {self.event.title}"
