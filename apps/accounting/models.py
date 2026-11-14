"""Uproszczona ewidencja przychodów i kosztów stowarzyszenia — rozporządzenie Ministra Finansów i Gospodarki z dnia
22 grudnia 2025 r. (Dz. U. poz. 1880), wydane na podstawie art. 10a ust. 6 ustawy o działalności pożytku publicznego
i o wolontariacie.

Zestawienie przychodów i kosztów (załącznik nr 1): każdy zapis ma kolejny numer w roku (Lp.), datę, numer dowodu, opis
zdarzenia i kwotę w jednej z kolumn: przychody (kol. 5), koszty uzyskania przychodów (kol. 6), koszty niestanowiące
kosztów uzyskania przychodów (kol. 7). Zapisy są trwałe (§ 7 ust. 1) — baza nie pozwala ich zmienić ani usunąć; błędy
poprawia się nowym zapisem korygującym (§ 7 ust. 2 pkt 2). Każdy zapis ma autora i czas (§ 6 ust. 3 pkt 3)."""

import uuid
from pathlib import Path

from django.conf import settings
from django.db import models

from apps.credentials.models import get_private_storage

# Przychody z systemu (dzienne raporty wpływów) — kategorie i domyślne opisy zdarzeń w ewidencji. Opisy można zmienić
# w ustawieniach księgowości (kwalifikacja przychodów należy do zarządu).
DEFAULT_LABELS = {
    "rezerwacja": "Dochody z majątku stowarzyszenia — udostępnianie strzelnicy (opłaty za korzystanie z osi)",
    "kod_wejscia": "Dochody z majątku stowarzyszenia — udostępnianie strzelnicy (kody wejścia: karnety, abonamenty)",
    "wydarzenie": "Dochody z majątku stowarzyszenia — udostępnianie strzelnicy (opłaty startowe za wydarzenia)",
    "skladka": "Składki członkowskie i wpisowe",
    "inne": "Dochody z majątku stowarzyszenia — udostępnianie strzelnicy (wpłaty na saldo i inne wpłaty)",
    "zwrot": "Zwroty wpłat (zmniejszenie przychodów)",
}
CATEGORY_ORDER = ["rezerwacja", "kod_wejscia", "wydarzenie", "skladka", "inne", "zwrot"]


class AccountingSettings(models.Model):
    auto_from = models.DateField(
        "przychody z systemu od dnia", null=True, blank=True,
        help_text="Od tego dnia przychody z dziennych raportów wpływów trafiają do ewidencji automatycznie (dowody "
                  "zbiorcze). Ustaw na dzień uruchomienia systemu dla strzelców — wcześniejsze przychody (GunTracker, "
                  "wyciągi bankowe) wpisuje się ręcznie. Puste = zasilanie wyłączone.")
    labels = models.JSONField("opisy przychodów z systemu", default=dict, blank=True)

    class Meta:
        verbose_name = "ustawienia księgowości"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def label(self, category):
        return (self.labels or {}).get(category) or DEFAULT_LABELS.get(category, category)


class RecordSequence(models.Model):
    year = models.PositiveIntegerField(unique=True)
    last = models.PositiveIntegerField(default=0)


def attachment_upload_to(instance, filename):
    ext = Path(filename).suffix.lower()[:5]
    return f"ksiegowosc/{instance.date.year}/{uuid.uuid4().hex}{ext}"


class Record(models.Model):
    """Zapis w zestawieniu przychodów i kosztów (kolumny 1–8 wzoru z załącznika nr 1)."""

    class Kind(models.TextChoices):
        REVENUE_AUTO = "przychod_system", "Przychód z systemu (dowód zbiorczy — raport wpływów)"
        REVENUE = "przychod", "Przychód (wpis ręczny)"
        COST = "koszt", "Koszt"
        CORRECTION = "korekta", "Zapis korygujący"

    year = models.PositiveIntegerField("rok", db_index=True)
    number = models.PositiveIntegerField("Lp.")
    date = models.DateField("data zdarzenia", db_index=True)
    document = models.CharField("nr dowodu księgowego", max_length=64)
    description = models.CharField("opis zdarzenia", max_length=255)
    counterparty = models.CharField("kontrahent / wystawca dowodu", max_length=160, blank=True)
    revenue = models.BigIntegerField("przychody [gr] (kol. 5)", default=0)
    cost_deductible = models.BigIntegerField("koszty uzyskania przychodów [gr] (kol. 6)", default=0)
    cost_other = models.BigIntegerField("koszty niestanowiące kosztów uzyskania przychodów [gr] (kol. 7)", default=0)
    notes = models.CharField("uwagi (kol. 8)", max_length=255, blank=True)
    kind = models.CharField("rodzaj zapisu", max_length=16, choices=Kind.choices)
    category = models.CharField("kategoria przychodu z systemu", max_length=16, blank=True)
    sales_report = models.ForeignKey("reports.SalesReport", verbose_name="raport wpływów (dowód zbiorczy)", null=True, blank=True,
                                     on_delete=models.PROTECT, related_name="accounting_records")
    corrects = models.ForeignKey("self", verbose_name="korygowany zapis", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="corrections")
    attachment = models.FileField("skan dowodu", upload_to=attachment_upload_to, storage=get_private_storage, blank=True)
    attachment_sha256 = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField("zapisano", auto_now_add=True)

    class Meta:
        ordering = ["year", "number"]
        verbose_name = "zapis w ewidencji"
        verbose_name_plural = "zapisy w ewidencji"
        permissions = [("manage_accounting", "Księgowość: ewidencja przychodów i kosztów")]
        constraints = [
            models.UniqueConstraint(fields=["year", "number"], name="accounting_record_number_unique"),
            models.UniqueConstraint(fields=["sales_report", "category"], condition=models.Q(sales_report__isnull=False),
                                    name="accounting_record_report_category_unique"),
            # storno: najwyżej jedno na zapis
            models.UniqueConstraint(fields=["corrects"], condition=models.Q(corrects__isnull=False),
                                    name="accounting_record_one_correction"),
        ]

    def __str__(self):
        return f"{self.year}/{self.number} {self.description}"

    @property
    def lp(self):
        return f"{self.number}/{self.year}"
