import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class PaymentSettings(models.Model):
    """Tryb rozliczeń. Domyślnie (decyzja właściciela 31.10.2026) każda rezerwacja i składka jest opłacana bezpośrednio;
    saldo z przedpłatami i automatycznym pobieraniem opłat można włączyć później."""

    wallet_enabled = models.BooleanField(
        "saldo strzelca (przedpłaty, automatyczne pobieranie opłat)", default=False,
        help_text="Wyłączone: wpłata opłaca tylko wskazaną rezerwację lub składkę; nadpłaty widzi zarząd jako kwoty do zwrotu.",
    )
    online_fee_percent = models.DecimalField(
        "opłata za płatność online — prowizja operatora [%]", max_digits=4, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
        help_text="Wpisz prowizję Tpay z umowy (np. 0,99). Opłata jest wyliczana tak, żeby po potrąceniu prowizji od całej płatności "
                  "stowarzyszeniu została pełna cena — wtedy zwrot zawsze ma pokrycie. Nie wraca przy anulowaniu; przelewem lub "
                  "na miejscu bez opłaty. 0 = bez opłaty.",
    )
    online_fee_fixed = models.PositiveIntegerField("opłata za płatność online — stała część prowizji operatora [gr]", default=0)
    overpayment_pays_reservations = models.BooleanField(
        "nadpłata opłaca kolejne rezerwacje", default=False,
        help_text="Przy wyłączonym saldzie: nadpłata strzelca (np. zwrot za anulowaną rezerwację) sama opłaca jego "
                  "nieopłacone rezerwacje, od najwcześniejszej. Składek nie opłaca.",
    )

    class Meta:
        verbose_name = "ustawienia płatności"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Wallet(models.Model):
    """Saldo strzelca. Saldo jest zawsze równe sumie operacji w `LedgerEntry` (sprawdzane w testach)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="wallet")
    balance = models.IntegerField("saldo [gr]", default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "saldo"
        verbose_name_plural = "salda"

    def __str__(self):
        return f"{self.user} — {self.balance / 100:.2f} zł"


class Payment(models.Model):
    """Wpłata (pieniądze przychodzące): przelew z wyciągu, Tpay, gotówka, BLIK na telefon."""

    class Method(models.TextChoices):
        TRANSFER = "przelew", "Przelew na rachunek"
        TPAY = "tpay", "Płatność online"  # operator (Tpay / Axepta) — z prefiksu external_id (apps/payments/gateway.py)
        CASH = "gotowka", "Gotówka"
        BLIK_PHONE = "blik", "BLIK na telefon"

    class Status(models.TextChoices):
        PENDING = "oczekuje", "Oczekuje"
        COMPLETED = "zaksiegowana", "Zaksięgowana"
        FAILED = "nieudana", "Nieudana / anulowana"
        UNMATCHED = "nieprzypisana", "Nieprzypisana (do decyzji)"
        IGNORED = "pominieta", "Pominięta (nie dotyczy)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="strzelec", null=True, blank=True, on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.PositiveIntegerField("kwota [gr]")
    method = models.CharField("sposób", max_length=16, choices=Method.choices)
    status = models.CharField("status", max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    title = models.CharField("tytuł", max_length=255, blank=True)
    payer_name = models.CharField("wpłacający", max_length=255, blank=True)
    booked_on = models.DateField("data operacji", null=True, blank=True)
    external_id = models.CharField("identyfikator zewnętrzny", max_length=128, null=True, blank=True, unique=True)
    reservation = models.ForeignKey(
        "bookings.Reservation", verbose_name="za rezerwację", null=True, blank=True, on_delete=models.PROTECT, related_name="payments"
    )
    event_registration = models.ForeignKey(
        "events.Registration", verbose_name="za zapis na wydarzenie", null=True, blank=True, on_delete=models.PROTECT, related_name="payments"
    )
    fee = models.ForeignKey(
        "association.Fee", verbose_name="za składkę / wpisowe", null=True, blank=True, on_delete=models.PROTECT, related_name="payments"
    )
    note = models.CharField("uwagi", max_length=255, blank=True)
    surcharge = models.PositiveIntegerField("w tym opłata za płatność online [gr]", default=0)
    raw = models.JSONField("dane źródłowe", default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField("utworzono", auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField("zaksięgowano", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "wpłata"
        verbose_name_plural = "wpłaty"
        permissions = [
            ("manage_payments", "Może przyjmować wpłaty, importować wyciągi i korygować salda"),
            ("accept_onsite", "Może przyjmować wpłaty na miejscu (gotówka, BLIK) za rezerwacje"),
        ]

    def __str__(self):
        return f"{self.get_method_display()} {self.amount / 100:.2f} zł ({self.get_status_display()})"

    @property
    def provider_name(self):
        """Operator płatności online (np. „Tpay”, „Axepta BNP Paribas”) albo "" dla innych wpłat."""
        from .gateway import NAMES, provider_of

        return NAMES.get(provider_of(self), "") if self.method == self.Method.TPAY else ""

    @property
    def is_test(self):
        """Płatność w środowisku testowym operatora (sandbox Tpay / Axepty) — bez prawdziwych pieniędzy; nie wchodzi do raportów."""
        raw = self.raw or {}
        return self.method == self.Method.TPAY and (raw.get("sandbox") is True or (raw.get("powiadomienie") or {}).get("test_mode") == "1")


class LedgerEntry(models.Model):
    """Operacja na saldzie (dodatnia = uznanie, ujemna = obciążenie). Wpisów nie można zmieniać ani usuwać."""

    class Kind(models.TextChoices):
        DEPOSIT = "wplata", "Wpłata"
        RESERVATION_FEE = "oplata", "Opłata za rezerwację"
        MEMBERSHIP_FEE = "skladka", "Składka / wpisowe"
        EVENT_FEE = "startowe", "Opłata startowa (wydarzenie)"
        REFUND = "zwrot", "Zwrot za rezerwację"
        CHARGEBACK = "zwrot_tpay", "Zwrot płatności do wpłacającego"
        CORRECTION = "korekta", "Korekta zarządu"
        ONLINE_FEE = "oplata_online", "Opłata za płatność online"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="ledger")
    amount = models.IntegerField("kwota [gr]")
    kind = models.CharField("rodzaj", max_length=16, choices=Kind.choices)
    balance_after = models.IntegerField("saldo po operacji [gr]")
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.PROTECT, related_name="ledger_entries")
    reservation = models.ForeignKey(
        "bookings.Reservation", null=True, blank=True, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    description = models.CharField("opis", max_length=255)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "operacja na saldzie"
        verbose_name_plural = "operacje na saldach"
        constraints = [models.CheckConstraint(condition=~models.Q(amount=0), name="ledger_nonzero")]


class BankImport(models.Model):
    """Wgrany plik wyciągu bankowego (do rozliczalności: kto, kiedy, ile wierszy)."""

    filename = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    rows_total = models.PositiveIntegerField(default=0)
    rows_new = models.PositiveIntegerField(default=0)
    rows_matched = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class TpayRefund(models.Model):
    """Zlecenie zwrotu przez Tpay (na kartę / rachunek płacącego).

    Zlecenie powstaje w transakcji operacji (anulowanie, odrzucenie, zwrot nadpłaty) razem z obciążeniem salda; wywołanie
    API Tpay następuje dopiero po zatwierdzeniu transakcji — wycofanie operacji nie zostawi zwrotu bez śladu w systemie.
    Przed wywołaniem zlecenie dostaje status „wysyłane” (zapisany osobno): przerwanie w trakcie nie spowoduje
    automatycznego ponowienia (ryzyko podwójnego zwrotu) — zarząd sprawdza w panelu Tpay i oznacza wynik."""

    class Status(models.TextChoices):
        QUEUED = "zlecony", "Zlecony"
        SENDING = "wysylany", "Wysyłany do Tpay"
        DONE = "wykonany", "Przyjęty przez Tpay"
        FAILED = "nieudany", "Nieudany (kwota wróciła na saldo)"
        UNKNOWN = "do_sprawdzenia", "Do sprawdzenia w panelu Tpay"

    ACTIVE = (Status.QUEUED, Status.SENDING, Status.DONE, Status.UNKNOWN)

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="tpay_refunds", verbose_name="wpłata")
    amount = models.PositiveIntegerField("kwota [gr]")
    reason = models.CharField("powód", max_length=255)
    status = models.CharField("status", max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    response = models.JSONField("odpowiedź Tpay", default=dict, blank=True)
    error = models.CharField("błąd", max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField("zlecono", auto_now_add=True)
    sent_at = models.DateTimeField("wysłano", null=True, blank=True)
    finished_at = models.DateTimeField("zakończono", null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
                                    verbose_name="wynik oznaczył (zarząd)")

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "zwrot Tpay"
        verbose_name_plural = "zwroty Tpay"
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="tpay_refund_positive"),
            # jeden aktywny (niezakończony porażką) zwrot na wpłatę — ochrona przed podwójnym zwrotem
            models.UniqueConstraint(fields=["payment"], condition=~models.Q(status="nieudany"), name="tpay_refund_one_active"),
        ]

    def __str__(self):
        return f"Zwrot {self.amount / 100:.2f} zł · {self.get_status_display()}"
