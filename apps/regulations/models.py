from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class RegulationDocument(models.Model):
    """Rodzaj dokumentu (np. regulamin strzelnicy). Treść żyje w kolejnych wersjach."""

    slug = models.SlugField("kod", max_length=40, unique=True)
    title = models.CharField("tytuł", max_length=160)
    requires_acceptance = models.BooleanField("wymaga akceptacji", default=True)
    order = models.PositiveSmallIntegerField("kolejność", default=0)

    class Meta:
        ordering = ["order", "title"]
        verbose_name = "dokument regulaminowy"
        verbose_name_plural = "dokumenty regulaminowe"
        permissions = [("publish_regulation", "Może publikować wersje regulaminów")]

    def __str__(self):
        return self.title

    def current_version(self, at=None):
        at = at or timezone.now()
        return (
            self.versions.filter(published_at__isnull=False, effective_from__lte=at)
            .order_by("-effective_from", "-number")
            .first()
        )


class RegulationVersion(models.Model):
    document = models.ForeignKey(RegulationDocument, verbose_name="dokument", on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField("numer wersji")
    content = models.TextField("treść")
    change_note = models.CharField("opis zmian", max_length=255, blank=True)
    effective_from = models.DateTimeField("obowiązuje od")
    published_at = models.DateTimeField("opublikowano", null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document", "-number"]
        verbose_name = "wersja regulaminu"
        verbose_name_plural = "wersje regulaminów"
        constraints = [models.UniqueConstraint(fields=["document", "number"], name="regulation_version_unique")]

    def __str__(self):
        return f"{self.document.title} — wersja {self.number}"

    @property
    def is_published(self):
        return self.published_at is not None

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.only("published_at").get(pk=self.pk)
            if original.published_at is not None:
                raise ValidationError("Opublikowanej wersji nie można zmieniać. Utwórz nową wersję.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.published_at is not None:
            raise ValidationError("Opublikowanej wersji nie można usunąć.")
        return super().delete(*args, **kwargs)


class Acceptance(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="acceptances")
    version = models.ForeignKey(RegulationVersion, on_delete=models.PROTECT, related_name="acceptances")
    accepted_at = models.DateTimeField("zaakceptowano", auto_now_add=True)
    ip = models.GenericIPAddressField("adres IP", null=True, blank=True)

    class Meta:
        ordering = ["-accepted_at"]
        verbose_name = "akceptacja regulaminu"
        verbose_name_plural = "akceptacje regulaminów"
        constraints = [models.UniqueConstraint(fields=["user", "version"], name="acceptance_unique")]


def current_versions_requiring_acceptance():
    out = []
    for doc in RegulationDocument.objects.filter(requires_acceptance=True):
        version = doc.current_version()
        if version:
            out.append(version)
    return out


def pending_acceptances(user):
    """Obowiązujące wersje regulaminów, których użytkownik jeszcze nie zaakceptował."""
    versions = current_versions_requiring_acceptance()
    if not versions:
        return []
    accepted = set(Acceptance.objects.filter(user=user, version__in=versions).values_list("version_id", flat=True))
    return [v for v in versions if v.pk not in accepted]
