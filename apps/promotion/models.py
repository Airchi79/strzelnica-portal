"""Promocja strzelnicy: publikacje przygotowywane przez system (teksty na Facebook, Instagram, TikTok i stronę) i ustawienia.

Każda publikacja zaczyna jako szkic; trafia do mediów dopiero po zatwierdzeniu przez osobę z uprawnieniem „Promocja”."""

from django.conf import settings
from django.db import models

from apps.credentials.models import get_private_storage

def promotion_photo_upload_to(instance, filename):
    import uuid

    return f"promocja/{uuid.uuid4().hex}.jpg"


GUNTRACKER_URL = "https://www.guntracker.pl/p/strzelnica-satok"
DEFAULT_TONE = ("Rzeczowo i życzliwie, sportowo, bez przesady i wykrzykników w każdym zdaniu. Zwracamy się do czytelnika "
                "na „Ty”. Podkreślamy bezpieczeństwo, dobrą organizację i przyjazną atmosferę strzelnicy.")
DEFAULT_HASHTAGS = "#StrzelnicaSątok #strzelectwo #strzelnica #sportstrzelecki #Bierutów #Oleśnica #Wrocław"


class PromotionSettings(models.Model):
    class Target(models.TextChoices):
        GUNTRACKER = "guntracker", "GunTracker (do dnia uruchomienia naszego systemu)"
        SYSTEM = "system", "Nasz system rezerwacji"

    booking_target = models.CharField(
        "rezerwacje w publikacjach prowadzą do", max_length=12, choices=Target.choices, default=Target.GUNTRACKER,
        help_text="Do dnia uruchomienia nowego systemu dla wszystkich — GunTracker; potem przełącz na nasz system.")
    guntracker_url = models.URLField("adres strzelnicy w GunTrackerze", default=GUNTRACKER_URL)
    tone = models.TextField("styl i ton wypowiedzi", default=DEFAULT_TONE)
    hashtags = models.CharField("stałe hasztagi", max_length=300, default=DEFAULT_HASHTAGS)
    facts = models.TextField(
        "stałe informacje o strzelnicy", blank=True,
        help_text="Np. dojazd, parking, co zabrać, zasady dla osób bez pozwolenia. System używa ich w tekstach — nie wymyśla faktów.")

    class Meta:
        verbose_name = "ustawienia promocji"
        permissions = [("manage_promotion", "Może przygotowywać i zatwierdzać publikacje promocyjne")]

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def booking_url(self):
        if self.booking_target == self.Target.SYSTEM:
            from django.urls import reverse

            return settings.SITE_URL.rstrip("/") + reverse("bookings:new")
        return self.guntracker_url


class Post(models.Model):
    class Kind(models.TextChoices):
        MANUAL = "polecenie", "Na polecenie"
        EVENT = "wydarzenie", "Wydarzenie"
        FREE_SLOTS = "wolne_terminy", "Wolne terminy"

    class Status(models.TextChoices):
        DRAFT = "szkic", "Szkic — do sprawdzenia"
        APPROVED = "zatwierdzony", "Zatwierdzony"
        PUBLISHED = "opublikowany", "Opublikowany"
        REJECTED = "odrzucony", "Odrzucony"

    kind = models.CharField("rodzaj", max_length=16, choices=Kind.choices, default=Kind.MANUAL)
    brief = models.TextField("polecenie", max_length=2000)
    event = models.ForeignKey("events.Event", verbose_name="wydarzenie", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    status = models.CharField("status", max_length=14, choices=Status.choices, default=Status.DRAFT, db_index=True)
    title = models.CharField("tytuł roboczy", max_length=160, blank=True)
    facebook = models.TextField("Facebook", blank=True)
    instagram = models.TextField("Instagram", blank=True)
    tiktok = models.TextField("TikTok (scenariusz i opis filmu)", blank=True)
    website_title = models.CharField("strona — tytuł wpisu", max_length=200, blank=True)
    website_body = models.TextField("strona — treść wpisu", blank=True)
    hashtags = models.CharField("hasztagi", max_length=400, blank=True)
    graphic_headline = models.CharField("grafika — nagłówek", max_length=70, blank=True)
    graphic_subtitle = models.CharField("grafika — podtytuł", max_length=110, blank=True)
    graphic_details = models.TextField("grafika — szczegóły (każdy w osobnym wierszu, najwyżej 4)", max_length=400, blank=True)
    photo = models.FileField("zdjęcie w tle grafiki", upload_to=promotion_photo_upload_to, blank=True, storage=get_private_storage)
    model = models.CharField("model", max_length=60, blank=True)
    usage = models.JSONField("zużycie (tokeny)", default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField("uwagi", max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "publikacja"
        verbose_name_plural = "publikacje"

    def __str__(self):
        return self.title or self.brief[:60]

    @property
    def tiktok_caption(self):
        """Opis filmu (część po „OPIS FILMU”/„OPIS”), bez scenariusza ujęć — do podglądu TikToka."""
        import re

        m = re.search(r"OPIS(?: FILMU)?\s*:?\s*(.+)", self.tiktok or "", re.S | re.I)
        return (m.group(1) if m else self.tiktok or "").strip()
