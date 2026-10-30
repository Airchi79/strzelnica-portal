import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.utils import timezone
from django.utils.functional import LazyObject


class PrivateStorage(LazyObject):
    """Magazyn plików, który nie jest dostępny pod żadnym publicznym adresem URL."""

    def _setup(self):
        self._wrapped = FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT, base_url=None)


private_storage = PrivateStorage()


def scan_upload_to(instance, filename):
    ext = Path(filename).suffix.lower()[:5]
    return f"scans/{uuid.uuid4().hex}{ext}"


class Credential(models.Model):
    class Kind(models.TextChoices):
        FIREARM_PERMIT = "pozwolenie", "Pozwolenie na broń"
        RANGE_OFFICER = "ps", "Uprawnienia prowadzącego strzelanie"
        LICENSE = "licencja", "Licencja zawodnicza"
        OTHER = "inne", "Inny dokument"

    class Status(models.TextChoices):
        PENDING = "oczekuje", "Oczekuje na weryfikację"
        VERIFIED = "zweryfikowany", "Zweryfikowany"
        REJECTED = "odrzucony", "Odrzucony"
        EXPIRED = "wygasly", "Wygasły"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="właściciel", on_delete=models.CASCADE, related_name="credentials"
    )
    kind = models.CharField("rodzaj", max_length=16, choices=Kind.choices)
    number = models.CharField("numer", max_length=64)
    issuer = models.CharField("organ wydający", max_length=160)
    issued_on = models.DateField("data wydania")
    valid_until = models.DateField("ważny do", null=True, blank=True, help_text="Puste, jeśli dokument jest bezterminowy.")
    details = models.CharField(
        "szczegóły", max_length=255, blank=True, help_text="Np. cel pozwolenia (sportowy, kolekcjonerski, łowiecki)."
    )
    scan = models.FileField("skan (do weryfikacji)", storage=private_storage, upload_to=scan_upload_to, blank=True)

    status = models.CharField("status", max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    review_note = models.CharField("uwagi weryfikującego", max_length=255, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="zweryfikował", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField("data weryfikacji", null=True, blank=True)
    created_at = models.DateTimeField("dodano", auto_now_add=True)
    updated_at = models.DateTimeField("zmieniono", auto_now=True)

    class Meta:
        ordering = ["user", "kind", "-issued_on"]
        verbose_name = "dokument / uprawnienie"
        verbose_name_plural = "dokumenty i uprawnienia"
        constraints = [
            models.UniqueConstraint(fields=["user", "kind", "number"], name="credential_unique_per_user"),
            models.CheckConstraint(
                condition=models.Q(valid_until__isnull=True) | models.Q(valid_until__gte=models.F("issued_on")),
                name="credential_valid_after_issue",
            ),
        ]
        permissions = [("review_credential", "Może weryfikować dokumenty i uprawnienia")]

    def __str__(self):
        return f"{self.get_kind_display()} {self.number}"

    @property
    def is_expired_by_date(self):
        return self.valid_until is not None and self.valid_until < timezone.localdate()

    @property
    def effective_status(self):
        """Status uwzględniający datę ważności, także zanim zadanie nocne zapisze zmianę w bazie."""
        if self.status == self.Status.VERIFIED and self.is_expired_by_date:
            return self.Status.EXPIRED
        return self.status

    @property
    def is_valid(self):
        return self.effective_status == self.Status.VERIFIED

    def get_effective_status_display(self):
        return self.Status(self.effective_status).label


def valid_credentials(user, kind):
    today = timezone.localdate()
    return Credential.objects.filter(user=user, kind=kind, status=Credential.Status.VERIFIED).filter(
        models.Q(valid_until__isnull=True) | models.Q(valid_until__gte=today)
    )


def is_range_officer(user):
    return bool(user and user.is_authenticated) and valid_credentials(user, Credential.Kind.RANGE_OFFICER).exists()
