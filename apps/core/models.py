from django.conf import settings
from django.db import models


class AssociationSettings(models.Model):
    """Dane stowarzyszenia (jeden rekord)."""

    name = models.CharField("nazwa", max_length=160, default="Stowarzyszenie „STRZELNICA SĄTOK”")
    address = models.CharField("adres siedziby", max_length=255, blank=True,
                               help_text="Adres z rejestru — na dokumentach (potwierdzenia wpłat, raporty, uchwały).")
    range_place = models.CharField(
        "lokalizacja strzelnicy (dojazd)", max_length=160, blank=True, default="Sątok, 56-420 Bierutów",
        help_text="Opis miejsca strzelnicy dla dojazdu (e-maile, kalendarz). Może się różnić od adresu siedziby.",
    )
    range_lat = models.DecimalField("szerokość geograficzna strzelnicy", max_digits=9, decimal_places=6, null=True, blank=True,
                                    default=51.085222, help_text="Np. 51.085222 — przycisk „Dojazd (mapa)” prowadzi dokładnie w to miejsce.")
    range_lon = models.DecimalField("długość geograficzna strzelnicy", max_digits=9, decimal_places=6, null=True, blank=True,
                                    default=17.448874)
    nip = models.CharField("NIP", max_length=13, blank=True)
    regon = models.CharField("REGON", max_length=14, blank=True)
    bank_account = models.CharField("numer rachunku", max_length=40, blank=True)
    contact_email = models.EmailField("e-mail kontaktowy", blank=True)
    contact_phone = models.CharField("telefon kontaktowy", max_length=32, blank=True)
    notify_email = models.EmailField("e-mail powiadomień zarządu", blank=True)

    class Meta:
        verbose_name = "ustawienia stowarzyszenia"
        verbose_name_plural = "ustawienia stowarzyszenia"
        permissions = [("manage_settings", "Może zmieniać dane stowarzyszenia")]

    def __str__(self):
        return self.name

    @property
    def map_url(self):
        """Link do mapy z dojazdem: współrzędne strzelnicy, a gdy ich brak — opis miejsca albo adres siedziby."""
        from urllib.parse import quote

        if self.range_lat is not None and self.range_lon is not None:
            return f"https://www.google.com/maps/search/?api=1&query={self.range_lat},{self.range_lon}"
        place = self.range_place or self.address
        return f"https://www.google.com/maps/search/?api=1&query={quote(place)}" if place else ""

    @property
    def directions(self):
        """Tekst „dojazd” — opis miejsca strzelnicy (albo adres siedziby, gdy brak)."""
        return self.range_place or self.address

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Notification(models.Model):
    """Powiadomienie w portalu: dla zarządu (panel → Powiadomienia) albo dla strzelca (dzwonek w nagłówku)."""

    class Channel(models.TextChoices):
        PANEL = "panel", "Panel zarządu"
        PORTAL = "portal", "Portal strzelca"

    channel = models.CharField(max_length=8, choices=Channel.choices, default=Channel.PANEL, db_index=True)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=500, blank=True)
    url = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Branding(models.Model):
    """Logo stowarzyszenia i wygenerowane z niego ikony aplikacji (jeden rekord). Brak rekordu — logo domyślne z plików
    statycznych. Trzymane w bazie, więc trafia do kopii zapasowych razem z danymi."""

    logo = models.BinaryField("logo")
    logo_type = models.CharField("typ pliku logo", max_length=32)  # image/svg+xml albo image/png
    icon_192 = models.BinaryField("ikona 192×192 (PNG)")
    icon_512 = models.BinaryField("ikona 512×512 (PNG)")
    icon_maskable = models.BinaryField("ikona maskowalna 512×512 (PNG, z tłem)")
    icon_background = models.CharField("kolor tła ikony", max_length=7, default="#ffffff")
    updated_at = models.DateTimeField("zmieniono", auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = "logo"
        verbose_name_plural = "logo"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class Announcement(models.Model):
    """Ogłoszenie zarządu dla strzelców: pasek na stronie i pulpicie, opcjonalnie e-mail (wysyłany w tle przez harmonogram)."""

    class Level(models.TextChoices):
        INFO = "info", "Informacja"
        WARNING = "uwaga", "Uwaga (np. zmiana godzin)"
        CLOSED = "zamkniete", "Ważne (np. strzelnica zamknięta)"

    class Audience(models.TextChoices):
        NONE = "", "Bez e-maila — tylko na stronie"
        ALL = "wszyscy", "E-mail do wszystkich aktywnych kont"
        BOOKED = "rezerwujacy", "E-mail do osób z rezerwacją w podanym okresie"

    title = models.CharField("tytuł", max_length=120)
    body = models.TextField("treść", max_length=3000, blank=True)
    level = models.CharField("rodzaj", max_length=12, choices=Level.choices, default=Level.INFO)
    visible_from = models.DateTimeField("widoczne od")
    visible_until = models.DateTimeField("widoczne do", null=True, blank=True, help_text="Puste — do odwołania.")
    email_audience = models.CharField("e-mail", max_length=12, choices=Audience.choices, blank=True)
    booked_from = models.DateField("rezerwacje od dnia", null=True, blank=True)
    booked_to = models.DateField("rezerwacje do dnia", null=True, blank=True)
    email_queued = models.BooleanField("e-mail czeka na wysłanie", default=False)
    emailed_at = models.DateTimeField("e-mail wysłany", null=True, blank=True)
    emailed_count = models.PositiveIntegerField("wysłano do", default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-visible_from"]
        verbose_name = "ogłoszenie"
        verbose_name_plural = "ogłoszenia"
        permissions = [("publish_announcement", "Może publikować ogłoszenia dla strzelców")]

    def __str__(self):
        return self.title


class AnnouncementDelivery(models.Model):
    """Komu już wysłano e-mail z ogłoszeniem — wysyłka w partiach nie dubluje wiadomości."""

    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="deliveries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["announcement", "user"], name="announcement_delivery_unique")]


class PushConfig(models.Model):
    """Klucz VAPID serwera do powiadomień push (generowany automatycznie przy pierwszym użyciu)."""

    vapid_private_pem = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "konfiguracja powiadomień push"


class PushSubscription(models.Model):
    """Urządzenie (przeglądarka / aplikacja na telefonie), na które użytkownik włączył powiadomienia push."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=60)
    device = models.CharField("urządzenie", max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "subskrypcja push"
        verbose_name_plural = "subskrypcje push"


class FaqItem(models.Model):
    """Pytanie i odpowiedź na publicznej stronie „Częste pytania” (edytowane w panelu)."""

    question = models.CharField("pytanie", max_length=200)
    answer = models.TextField("odpowiedź", max_length=4000,
                              help_text="Akapity oddziel pustą linią. Bez HTML — tekst jest wyświetlany tak, jak go wpiszesz.")
    order = models.PositiveSmallIntegerField("kolejność", default=100, help_text="Mniejsza liczba — wyżej na liście.")
    is_published = models.BooleanField("widoczne na stronie", default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "pk"]
        verbose_name = "częste pytanie"
        verbose_name_plural = "częste pytania"

    def __str__(self):
        return self.question
