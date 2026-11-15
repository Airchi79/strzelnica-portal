"""Uproszczona ewidencja przychodów i kosztów stowarzyszenia — rozporządzenie Ministra Finansów i Gospodarki z dnia
22 grudnia 2025 r. (Dz. U. poz. 1880), wydane na podstawie art. 10a ust. 6 ustawy o działalności pożytku publicznego
i o wolontariacie.

Zestawienie przychodów i kosztów (załącznik nr 1): każdy zapis ma kolejny numer w roku (Lp.), datę, numer dowodu, opis
zdarzenia i kwotę w jednej z kolumn: przychody (kol. 5), koszty uzyskania przychodów (kol. 6), koszty niestanowiące
kosztów uzyskania przychodów (kol. 7). Zapisy są trwałe (§ 7 ust. 1) — baza nie pozwala ich zmienić ani usunąć; błędy
poprawia się, wpisując nową treść zapisu (§ 7 ust. 2 pkt 1 — ewidencja w postaci elektronicznej). Każda zmiana
i usunięcie zapisu zostaje w niezmiennej historii wersji: kto, kiedy, jaka treść i z jakiego powodu (§ 6 ust. 3 pkt 3).
Usunięty zapis zachowuje swój numer (Lp.) i jest widoczny na wydruku jako usunięty — bez kwot w sumach.
Po zamknięciu roku zapisów z tego roku nie można zmieniać."""

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


class Activity(models.TextChoices):
    """Formy działalności do rachunkowego wyodrębnienia (art. 10 ust. 1 ustawy o działalności pożytku publicznego) —
    zestawienie uzupełniające (§ 4 ust. 1 pkt 1 rozporządzenia)."""

    UNPAID = "nieodplatna", "Nieodpłatna działalność pożytku publicznego"
    PAID = "odplatna", "Odpłatna działalność pożytku publicznego"
    OTHER = "pozostala", "Pozostałe przychody i koszty (np. dochody z majątku, odsetki)"
    COMMON = "wspolne", "Koszty wspólne / administracyjne"


class AccountingSettings(models.Model):
    auto_from = models.DateField(
        "przychody z systemu od dnia", null=True, blank=True,
        help_text="Od tego dnia przychody z dziennych raportów wpływów trafiają do ewidencji automatycznie (dowody "
                  "zbiorcze). Ustaw na dzień uruchomienia systemu dla strzelców — wcześniejsze przychody (GunTracker, "
                  "wyciągi bankowe) wpisuje się ręcznie. Puste = zasilanie wyłączone.")
    labels = models.JSONField("opisy przychodów z systemu", default=dict, blank=True)
    tax_office = models.TextField("urząd skarbowy (nazwa i adres)", blank=True,
                                  help_text="Naczelnik urzędu skarbowego właściwego w sprawach podatku dochodowego — do zawiadomień.")
    approving_body = models.CharField("organ zatwierdzający", max_length=120, blank=True, default="Zebranie Członków",
                                      help_text="Organ, który wg regulaminu stowarzyszenia zatwierdza sprawozdania i decyduje o "
                                                "uproszczonej ewidencji (art. 10a ust. 1 ustawy o działalności pożytku publicznego).")
    signatories = models.TextField("osoby podpisujące", blank=True,
                                   help_text="Każda osoba w osobnym wierszu, np. „Jan Kowalski — przedstawiciel”.")
    auto_activity = models.CharField(
        "forma działalności dla przychodów z systemu", max_length=12, choices=Activity.choices, default=Activity.OTHER,
        help_text="Do zestawienia uzupełniającego (art. 10 ust. 1 ustawy o działalności pożytku publicznego). Opłaty za "
                  "udostępnianie strzelnicy jako dochody z majątku — „pozostałe”. Stowarzyszenie zwykłe nie może prowadzić "
                  "odpłatnej działalności pożytku publicznego (art. 42 ust. 1 pkt 4 Prawa o stowarzyszeniach).")

    class Meta:
        verbose_name = "ustawienia księgowości"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def label(self, category):
        return (self.labels or {}).get(category) or DEFAULT_LABELS.get(category, category)


class AccountingYear(models.Model):
    """Rok podatkowy: zamknięcie (blokada zmian) i dane do zestawienia przepływów finansowych (załącznik nr 2)."""

    year = models.PositiveIntegerField("rok", unique=True)
    closed_at = models.DateTimeField("zamknięto", null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    free_income = models.BigIntegerField("kol. 4 — dochód wolny w roku przeznaczony na cele z art. 17 ust. 1 [gr]", null=True, blank=True)
    free_prior = models.BigIntegerField("kol. 5 — dochód wolny z lat ubiegłych [gr]", null=True, blank=True)
    free_spent = models.BigIntegerField("kol. 6 — wydatki na cele z art. 17 ust. 1 pokryte z dochodu wolnego [gr]", null=True, blank=True)

    class Meta:
        ordering = ["-year"]

    @classmethod
    def get(cls, year):
        return cls.objects.get_or_create(year=year)[0]

    @classmethod
    def is_closed(cls, year):
        return cls.objects.filter(year=year, closed_at__isnull=False).exists()


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
        KSEF = "ksef", "Faktura z KSeF"

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
    ksef_number = models.CharField("numer KSeF", max_length=40, blank=True)
    activity = models.CharField("forma działalności", max_length=12, choices=Activity.choices, blank=True)
    task = models.ForeignKey("PublicTask", verbose_name="umowa o realizację zadania publicznego", null=True, blank=True,
                             on_delete=models.PROTECT, related_name="records")
    version = models.PositiveIntegerField("wersja", default=1)
    updated_at = models.DateTimeField("ostatnia zmiana", null=True, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    removed_at = models.DateTimeField("usunięto", null=True, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    removed_reason = models.CharField("przyczyna usunięcia", max_length=255, blank=True)

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

    @property
    def is_removed(self):
        return self.removed_at is not None

    # pola treści zapisu — zapisywane w historii wersji
    CONTENT_FIELDS = ("date", "document", "description", "counterparty", "revenue", "cost_deductible", "cost_other", "notes",
                      "ksef_number", "attachment_sha256", "activity", "task_id")

    def snapshot(self):
        return {f: (getattr(self, f).isoformat() if f == "date" else getattr(self, f)) for f in self.CONTENT_FIELDS}


class RecordVersion(models.Model):
    """Niezmienna historia zapisu (trigger w bazie): każda wersja treści, usunięcie i przywrócenie."""

    class Action(models.TextChoices):
        CREATE = "utworzenie", "Utworzenie"
        UPDATE = "zmiana", "Zmiana treści"
        REMOVE = "usuniecie", "Usunięcie z ewidencji"
        RESTORE = "przywrocenie", "Przywrócenie"

    record = models.ForeignKey(Record, on_delete=models.PROTECT, related_name="versions")
    version = models.PositiveIntegerField()
    action = models.CharField(max_length=16, choices=Action.choices)
    data = models.JSONField()
    reason = models.CharField("powód", max_length=255, blank=True)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["record", "pk"]


def ksef_upload_to(instance, filename):
    return f"ksef/{uuid.uuid4().hex}.xml"


class KsefInvoice(models.Model):
    """Faktura ustrukturyzowana z Krajowego Systemu e-Faktur (plik XML pobrany z KSeF), czekająca na decyzję:
    przyjęcie do ewidencji (zapis kosztu albo przychodu) lub odrzucenie (nie dotyczy ewidencji)."""

    class Status(models.TextChoices):
        NEW = "nowa", "Do decyzji"
        ACCEPTED = "przyjeta", "Przyjęta do ewidencji"
        REJECTED = "odrzucona", "Odrzucona (poza ewidencją)"

    class Direction(models.TextChoices):
        PURCHASE = "zakup", "Zakup (koszt)"
        SALE = "sprzedaz", "Sprzedaż (przychód)"

    ksef_number = models.CharField("numer KSeF", max_length=40, blank=True)
    invoice_number = models.CharField("numer faktury", max_length=256)
    kind = models.CharField("rodzaj faktury", max_length=16, blank=True)
    issue_date = models.DateField("data wystawienia")
    sale_date = models.DateField("data dostawy / wykonania usługi", null=True, blank=True)
    seller_nip = models.CharField("NIP sprzedawcy", max_length=16, blank=True)
    seller_name = models.CharField("sprzedawca", max_length=255, blank=True)
    buyer_nip = models.CharField("NIP nabywcy", max_length=16, blank=True)
    buyer_name = models.CharField("nabywca", max_length=255, blank=True)
    currency = models.CharField("waluta", max_length=3, default="PLN")
    net = models.BigIntegerField("netto [gr]", default=0)
    vat = models.BigIntegerField("VAT [gr]", default=0)
    gross = models.BigIntegerField("należność ogółem [gr]", default=0)
    items = models.JSONField("pozycje", default=list, blank=True)
    direction = models.CharField("kierunek", max_length=8, choices=Direction.choices)
    status = models.CharField("status", max_length=10, choices=Status.choices, default=Status.NEW)
    status_note = models.CharField("uwagi do decyzji", max_length=255, blank=True)
    record = models.ForeignKey(Record, null=True, blank=True, on_delete=models.SET_NULL, related_name="ksef_invoices")
    xml = models.FileField("plik XML", upload_to=ksef_upload_to, storage=get_private_storage)
    sha256 = models.CharField(max_length=64, unique=True)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issue_date", "-pk"]
        verbose_name = "faktura KSeF"
        verbose_name_plural = "faktury KSeF"
        constraints = [
            models.UniqueConstraint(fields=["ksef_number"], condition=~models.Q(ksef_number=""), name="ksef_invoice_number_unique"),
        ]

    def __str__(self):
        return f"{self.invoice_number} ({self.seller_name})"

    @property
    def items_summary(self):
        names = [i.get("name", "") for i in self.items if i.get("name")]
        return "; ".join(names)[:255]


class GeneratedDocument(models.Model):
    """Dokument z generatora (zawiadomienie, uchwała, zestawienie, dowód wewnętrzny…) — dane zapamiętane, żeby można
    było pobrać ten sam dokument ponownie (PDF albo DOCX do edycji)."""

    kind = models.CharField("rodzaj", max_length=32)
    year = models.PositiveIntegerField("rok")
    number = models.PositiveIntegerField("numer w roku i rodzaju")
    title = models.CharField("tytuł", max_length=255)
    data = models.JSONField("dane")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["kind", "year", "number"], name="generated_document_number_unique")]

    def __str__(self):
        return self.title


class FixedAsset(models.Model):
    """Pozycja wykazu środków trwałych oraz wartości niematerialnych i prawnych (§ 3 rozporządzenia, Dz. U. 2025
    poz. 1880; art. 16a–16m ustawy o CIT). Odpisy liniowe liczone są od miesiąca następującego po miesiącu
    wprowadzenia do wykazu do zrównania z wartością początkową albo do miesiąca likwidacji / zbycia (art. 16h ust. 1 pkt 1)."""

    class Kind(models.TextChoices):
        FIXED = "st", "Środek trwały"
        INTANGIBLE = "wnip", "Wartość niematerialna i prawna"

    class Method(models.TextChoices):
        LINEAR = "liniowa", "Liniowa — stawka z Wykazu stawek amortyzacyjnych (art. 16i)"
        ONE_OFF = "jednorazowo", "Bez odpisów — wydatek w kosztach w miesiącu oddania do używania (art. 16d ust. 1, do 10 000 zł)"
        NONE = "brak", "Nie podlega amortyzacji (art. 16c, np. grunt)"

    class Acquisition(models.TextChoices):
        PURCHASE = "nabycie", "Nabycie"
        PRODUCTION = "wytworzenie", "Wytworzenie we własnym zakresie"

    class Disposal(models.TextChoices):
        LIQUIDATION = "likwidacja", "Likwidacja"
        SALE = "zbycie", "Zbycie"
        SHORTAGE = "niedobor", "Niedobór"

    number = models.PositiveIntegerField("Lp.", unique=True)
    kind = models.CharField("rodzaj", max_length=4, choices=Kind.choices, default=Kind.FIXED)
    name = models.CharField("określenie (§ 3 ust. 1 pkt 5)", max_length=255)
    kst = models.CharField("symbol KŚT (pkt 6)", max_length=20, blank=True)
    acquisition = models.CharField("sposób", max_length=12, choices=Acquisition.choices, default=Acquisition.PURCHASE)
    acquired_on = models.DateField("data nabycia / wytworzenia (pkt 2)")
    in_use_on = models.DateField("data przyjęcia do używania (pkt 3)")
    entered_on = models.DateField("data wprowadzenia do wykazu")
    acquisition_doc = models.CharField("rodzaj i numer dowodu nabycia / wytworzenia (pkt 4 lit. c)", max_length=160, blank=True)
    in_use_doc = models.CharField("numer dowodu przyjęcia do używania (pkt 4 lit. b)", max_length=64, blank=True)
    initial_value = models.BigIntegerField("wartość początkowa [gr] (pkt 7)")
    updated_value = models.BigIntegerField("zaktualizowana wartość początkowa [gr] (pkt 10)", null=True, blank=True)
    method = models.CharField("metoda", max_length=12, choices=Method.choices, default=Method.LINEAR)
    rate = models.DecimalField("roczna stawka amortyzacyjna [%] (pkt 8)", max_digits=5, decimal_places=2, default=0)
    prior_depreciation = models.BigIntegerField("odpisy dokonane przed rokiem prowadzenia wykazu w systemie [gr]", null=True, blank=True)
    prior_until_year = models.PositiveIntegerField("odpisy dokonane do końca roku", null=True, blank=True)
    disposed_on = models.DateField("data likwidacji / zbycia (pkt 11)", null=True, blank=True)
    disposal = models.CharField("rodzaj wykreślenia", max_length=12, choices=Disposal.choices, blank=True)
    disposal_reason = models.CharField("przyczyna likwidacji", max_length=255, blank=True)
    disposal_doc = models.CharField("numer dowodu wykreślenia (pkt 4 lit. b)", max_length=64, blank=True)
    disposal_ksef = models.CharField("numer KSeF faktury zbycia (pkt 4 lit. a)", max_length=40, blank=True)
    notes = models.CharField("uwagi", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["number"]
        verbose_name = "środek trwały / WNiP"
        verbose_name_plural = "wykaz środków trwałych i WNiP"

    def __str__(self):
        return f"{self.number}. {self.name}"

    @property
    def base(self):
        return self.updated_value if self.updated_value is not None else self.initial_value


class DepreciationPosting(models.Model):
    """Ujęcie odpisów amortyzacyjnych z wykazu w zestawieniu przychodów i kosztów (za miesiąc albo cały rok)."""

    year = models.PositiveIntegerField("rok")
    month = models.PositiveSmallIntegerField("miesiąc (puste — cały rok)", null=True, blank=True)
    amount = models.BigIntegerField("kwota [gr]")
    record = models.ForeignKey(Record, on_delete=models.PROTECT, related_name="depreciation_postings")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["year", "month"]


class PublicTask(models.Model):
    """Umowa o wsparcie lub powierzenie realizacji zadania publicznego (art. 16 ustawy o działalności pożytku publicznego) —
    środki z umowy wyodrębnia się w ewidencji (art. 16 ust. 5)."""

    name = models.CharField("nazwa zadania", max_length=255)
    agreement = models.CharField("numer umowy", max_length=80, blank=True)
    grantor = models.CharField("zleceniodawca (organ / jednostka)", max_length=160)
    signed_on = models.DateField("data zawarcia")
    starts_on = models.DateField("realizacja od")
    ends_on = models.DateField("realizacja do")
    grant = models.BigIntegerField("kwota dotacji [gr]")
    notes = models.CharField("uwagi", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_on", "-pk"]
        verbose_name = "umowa o realizację zadania publicznego"

    def __str__(self):
        return f"{self.name}" + (f" (umowa {self.agreement})" if self.agreement else "")


class InKindDonation(models.Model):
    """Darowizna rzeczowa — łączna wartość w roku jest potrzebna do limitu przychodów z art. 10a ust. 1 pkt 5 ustawy
    (§ 4 ust. 1 pkt 2 rozporządzenia). Bez danych osobowych darczyńców (§ 4 ust. 2)."""

    date = models.DateField("data otrzymania")
    description = models.CharField("przedmiot darowizny", max_length=255)
    value = models.BigIntegerField("wartość [gr]")
    document = models.CharField("dowód (np. umowa darowizny, protokół przyjęcia)", max_length=120, blank=True)
    statutory = models.BooleanField("przeznaczona na działalność statutową", default=True)
    record = models.ForeignKey(Record, null=True, blank=True, on_delete=models.SET_NULL, related_name="in_kind_donations",
                               verbose_name="zapis przychodu w zestawieniu")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-pk"]
        verbose_name = "darowizna rzeczowa"
        verbose_name_plural = "darowizny rzeczowe"
