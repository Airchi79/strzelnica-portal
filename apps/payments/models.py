import uuid

from django.conf import settings
from django.db import models


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
        TPAY = "tpay", "Płatność online (Tpay)"
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
    note = models.CharField("uwagi", max_length=255, blank=True)
    raw = models.JSONField("dane źródłowe", default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField("utworzono", auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField("zaksięgowano", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "wpłata"
        verbose_name_plural = "wpłaty"
        permissions = [("manage_payments", "Może przyjmować wpłaty, importować wyciągi i korygować salda")]

    def __str__(self):
        return f"{self.get_method_display()} {self.amount / 100:.2f} zł ({self.get_status_display()})"


class LedgerEntry(models.Model):
    """Operacja na saldzie (dodatnia = uznanie, ujemna = obciążenie). Wpisów nie można zmieniać ani usuwać."""

    class Kind(models.TextChoices):
        DEPOSIT = "wplata", "Wpłata"
        RESERVATION_FEE = "oplata", "Opłata za rezerwację"
        REFUND = "zwrot", "Zwrot za rezerwację"
        CHARGEBACK = "zwrot_tpay", "Zwrot płatności do wpłacającego"
        CORRECTION = "korekta", "Korekta zarządu"

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
