import hashlib
import json
import secrets
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.bookings.models import Reservation
from apps.regulations.models import RegulationVersion

STATEMENT_TEXT = (
    "Oświadczam, że zapoznałem(-am) się z regulaminem strzelnicy i przepisami bezpieczeństwa "
    "i zobowiązuję się ich przestrzegać."
)


def new_gate_token():
    return secrets.token_urlsafe(18)


class LogbookSettings(models.Model):
    """Ustawienia książki pobytu (jeden rekord)."""

    retention_years = models.PositiveIntegerField("okres przechowywania wpisów [lata]", default=5)
    window_before_minutes = models.PositiveIntegerField("wpis do rezerwacji możliwy [min] przed jej początkiem", default=30)
    gate_token = models.CharField("token kodu QR przy wejściu", max_length=40, default=new_gate_token)
    gate_token_rotated_at = models.DateTimeField(null=True, blank=True)
    max_entries_per_hour = models.PositiveIntegerField("limit wpisów z jednego adresu IP na godzinę", default=30)
    auto_exit_hours = models.PositiveIntegerField(
        "automatyczne zamknięcie pobytu po [h]", default=12,
        help_text="Pobyt bez zapisanego wyjścia zamyka się sam: o końcu rezerwacji albo po tylu godzinach od wejścia. 0 — wyłączone.",
    )

    class Meta:
        verbose_name = "ustawienia książki pobytu"
        verbose_name_plural = "ustawienia książki pobytu"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class EntrySequence(models.Model):
    year = models.PositiveIntegerField(primary_key=True)
    last = models.PositiveIntegerField(default=0)


class Entry(models.Model):
    """Wpis w książce rejestru pobytu na strzelnicy.

    Zakres danych wg wzorcowego regulaminu strzelnic (rozp. MSWiA z 15.03.2000, zm. Dz.U. 2002 nr 23 poz. 238):
    imię i nazwisko; numer pozwolenia na broń i organ, który je wydał, albo adres (gdy brak pozwolenia);
    oświadczenie o zapoznaniu się z regulaminem i przepisami bezpieczeństwa potwierdzone podpisem.
    Wpisu nie można zmienić ani usunąć (trigger w bazie); wyjątki: jednorazowy wypis i usunięcie po okresie przechowywania.
    """

    class Purpose(models.TextChoices):
        SHOOTING = "strzelanie", "Strzelanie"
        OBSERVER = "obserwator", "Obserwator / osoba towarzysząca"
        WORK = "prace", "Prace / obsługa strzelnicy"
        OTHER = "inne", "Inny cel"

    class Source(models.TextChoices):
        RESERVATION_QR = "qr_rezerwacji", "Kod QR rezerwacji"
        GATE_QR = "qr_wejscia", "Kod QR przy wejściu"
        PERSONAL_QR = "qr_osobisty", "Dopisany przez prowadzącego (kod QR strzelca)"
        DEVICE = "urzadzenie", "Czytnik przy wejściu"
        STAFF = "obsluga", "Wpis przez obsługę (panel)"

    number = models.CharField("numer wpisu", max_length=24, unique=True)
    entered_at = models.DateTimeField("wejście", default=timezone.now, db_index=True)
    exited_at = models.DateTimeField("wyjście", null=True, blank=True)
    exit_auto = models.BooleanField("wyjście zamknięte automatycznie", default=False)

    purpose = models.CharField("cel pobytu", max_length=16, choices=Purpose.choices)
    purpose_note = models.CharField("opis celu", max_length=160, blank=True)
    reservation = models.ForeignKey(
        Reservation, verbose_name="rezerwacja", null=True, blank=True, on_delete=models.PROTECT, related_name="log_entries"
    )
    range_officer = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="prowadzący strzelanie", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="konto", null=True, blank=True, on_delete=models.PROTECT, related_name="log_entries"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    first_name = models.CharField("imię", max_length=80)
    last_name = models.CharField("nazwisko", max_length=120)
    has_permit = models.BooleanField("posiada pozwolenie na broń")
    permit_number = models.CharField("numer pozwolenia na broń", max_length=64, blank=True)
    permit_issuer = models.CharField("organ, który wydał pozwolenie", max_length=160, blank=True)
    address = models.CharField("adres", max_length=255, blank=True)

    statement = models.TextField("treść oświadczenia")
    regulation_version = models.ForeignKey(
        RegulationVersion, verbose_name="wersja regulaminu", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    signature = models.BinaryField("podpis (PNG)")
    signature_sha256 = models.CharField(max_length=64)
    content_sha256 = models.CharField("suma kontrolna wpisu", max_length=64)

    source = models.CharField("sposób wpisu", max_length=16, choices=Source.choices)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-entered_at", "-id"]
        verbose_name = "wpis w książce pobytu"
        verbose_name_plural = "książka pobytu"
        permissions = [
            ("view_logbook", "Może przeglądać i drukować książkę pobytu"),
            ("manage_logbook", "Może zmieniać ustawienia książki pobytu i kod QR wejścia"),
            ("staff_entry", "Może dodawać wpisy w imieniu osób przebywających na strzelnicy"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(has_permit=True) & ~models.Q(permit_number="") & ~models.Q(permit_issuer=""))
                | (models.Q(has_permit=False) & ~models.Q(address="")),
                name="entry_permit_or_address",
            ),
            models.CheckConstraint(
                condition=~models.Q(purpose="strzelanie") | models.Q(reservation__isnull=False),
                name="entry_shooting_needs_reservation",
            ),
        ]

    def __str__(self):
        return f"{self.number} {self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def identity_text(self):
        if self.has_permit:
            return f"pozwolenie nr {self.permit_number}, wydane przez {self.permit_issuer}"
        return f"adres: {self.address}"

    def compute_content_hash(self):
        payload = {
            "number": self.number,
            # Zawsze w UTC — suma nie może zależeć od strefy, w której podano godzinę (np. wpis obsługi z formularza).
            "entered_at": self.entered_at.astimezone(dt_timezone.utc).isoformat() if self.entered_at else None,
            "purpose": self.purpose,
            "purpose_note": self.purpose_note,
            "reservation": self.reservation_id,
            "range_officer": self.range_officer_id,
            "user": self.user_id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "has_permit": self.has_permit,
            "permit_number": self.permit_number,
            "permit_issuer": self.permit_issuer,
            "address": self.address,
            "statement": self.statement,
            "regulation_version": self.regulation_version_id,
            "signature_sha256": self.signature_sha256,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @property
    def integrity_ok(self):
        return (
            hashlib.sha256(bytes(self.signature)).hexdigest() == self.signature_sha256
            and self.compute_content_hash() == self.content_sha256
        )
