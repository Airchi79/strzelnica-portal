import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import Func, Q
from django.utils import timezone

from apps.facilities.models import Lane, PriceRule


class TsTzRange(Func):
    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class BookingSettings(models.Model):
    """Ustawienia rezerwacji (jeden rekord)."""

    free_cancel_hours = models.PositiveIntegerField(
        "strzelec może sam anulować do [h] przed terminem", default=24,
        help_text="Później anulować może tylko obsługa.",
    )
    hold_minutes = models.PositiveIntegerField(
        "czas na wybór płatności [min]", default=5,
        help_text="Po kliknięciu „Rezerwuję” termin jest trzymany dla strzelca przez ten czas (inni widzą go jako zajęty). "
                  "Bez wyboru płatności — termin wraca do puli.",
    )
    transfer_days = models.PositiveIntegerField(
        "przelew tradycyjny: czas na zaksięgowanie [dni]", default=3,
        help_text="Rezerwacja „oczekuje na przelew” tyle dni (dzień przed terminem strzelec dostaje przypomnienie). Po terminie "
                  "zarząd dostaje powiadomienie do decyzji; bez decyzji rezerwacja zwalnia się w chwili rozpoczęcia.",
    )
    transfer_cutoff_hours = models.PositiveIntegerField(
        "przelew tradycyjny: najpóźniej [h] przed terminem", default=24,
        help_text="Termin zaksięgowania przelewu nie później niż tyle godzin przed strzelaniem. Przelew jest dostępny tylko, "
                  "gdy do terminu zostało co najmniej „dni na zaksięgowanie” + te godziny.",
    )
    max_unpaid_holds = models.PositiveIntegerField(
        "nieopłacone rezerwacje jednej osoby naraz", default=3,
        help_text="Ile rezerwacji „oczekuje na płatność” (wybór płatności, płatność online, przelew) może mieć jednocześnie jeden "
                  "strzelec — przelew trzyma termin kilka dni bez zapłaty. 0 — bez limitu. Nie dotyczy rezerwacji przez obsługę.",
    )
    online_payment_minutes = models.PositiveIntegerField(
        "czas na płatność online [min]", default=15,
        help_text="Po wejściu do płatności online termin jest trzymany tyle minut od jej rozpoczęcia (BLIK, logowanie do banku). "
                  "Także: rezerwacje grup „tylko online” utworzone przez obsługę zwalniają się po tym czasie. 0 — bez limitu.",
    )
    approval_payment_hours = models.PositiveIntegerField(
        "czas na płatność po zatwierdzeniu [h]", default=24,
        help_text="Gdy rezerwację zatwierdziła obsługa — liczony od zatwierdzenia (strzelec dostaje e-mail).",
    )
    offline_instructions = models.TextField(
        "instrukcja płatności offline", blank=True, max_length=2000,
        default="Zapłać przelewem na konto Stowarzyszenia, w tytule wpisz numer rezerwacji, albo gotówką na miejscu przed strzelaniem.",
        help_text="Pokazywana strzelcom, którzy mogą płacić offline (przelew, gotówka na miejscu).",
    )
    min_lead_hours = models.PositiveIntegerField(
        "rezerwacja online najpóźniej [h] przed rozpoczęciem", default=0,
        help_text="Dotyczy tylko rezerwacji przez stronę / aplikację. Np. 2 = dwie godziny, 24 = dzień, 48 = dwa dni wcześniej; "
                  "0 — do rozpoczęcia godziny. Bez limitu: obsługa w panelu oraz rezerwacja na miejscu w panelu na strzelnicy.",
    )
    prepay_before_approval = models.BooleanField(
        "nowy strzelec może zapłacić online przed zatwierdzeniem", default=True,
        help_text="Po odrzuceniu wpłata wraca automatycznie na rachunek / kartę strzelca (Tpay). Wyłączone: płatność dopiero po zatwierdzeniu.",
    )
    waitlist_hold_minutes = models.PositiveIntegerField(
        "lista oczekujących: termin trzymany [min]", default=30,
        help_text="Po zwolnieniu terminu pierwsza osoba z listy dostaje e-mail; przez ten czas tylko ona może go zarezerwować.",
    )
    officer_service = models.BooleanField(
        "prowadzący od strzelnicy dla osób bez uprawnień PS", default=False,
        help_text="Osoba bez zweryfikowanych uprawnień PS może zarezerwować oś z prowadzącym zapewnionym przez strzelnicę. "
                  "Taka rezerwacja zawsze czeka na zatwierdzenie — obsługa przydziela prowadzącego.",
    )
    officer_fee_hourly = models.PositiveIntegerField("dopłata za prowadzącego od strzelnicy [gr / h]", default=0)
    max_advance_days = models.PositiveIntegerField("rezerwacja najwyżej [dni] naprzód", default=180)
    max_persons = models.PositiveIntegerField("maksymalna liczba osób w rezerwacji", default=50)

    class Meta:
        verbose_name = "ustawienia rezerwacji"
        verbose_name_plural = "ustawienia rezerwacji"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class GroupRule(models.Model):
    """Zasady rezerwowania dla grupy strzelców (członek, powracający, nowy)."""

    class Group(models.TextChoices):
        MEMBER = "czlonek", "Członkowie stowarzyszenia"
        RETURNING = "powracajacy", "Strzelcy powracający"
        NEW = "nowy", "Nowi strzelcy"

    group = models.CharField("grupa", max_length=12, choices=Group.choices, unique=True)
    requires_approval = models.BooleanField("rezerwacja wymaga zatwierdzenia przez obsługę", default=False)
    pay_online = models.BooleanField("płatność online (Tpay)", default=True)
    pay_transfer = models.BooleanField("przelew tradycyjny", default=True)
    pay_cash = models.BooleanField("gotówka na miejscu", default=True)
    pay_pass = models.BooleanField("kody wejścia (karnety, abonamenty)", default=False)

    class Meta:
        ordering = ["id"]
        verbose_name = "zasady grupy strzelców"
        verbose_name_plural = "zasady grup strzelców"
        constraints = [models.CheckConstraint(condition=Q(pay_online=True) | Q(pay_transfer=True) | Q(pay_cash=True) | Q(pay_pass=True),
                                              name="group_some_payment")]

    def __str__(self):
        return self.get_group_display()

    @property
    def online_only(self):
        return self.pay_online and not self.pay_offline

    @property
    def pay_offline(self):
        """Przelew tradycyjny albo gotówka na miejscu."""
        return self.pay_transfer or self.pay_cash

    @classmethod
    def for_group(cls, group):
        defaults = DEFAULT_GROUP_RULES.get(group, DEFAULT_GROUP_RULES[cls.Group.NEW])
        obj, _ = cls.objects.get_or_create(group=group, defaults=defaults)
        return obj


DEFAULT_GROUP_RULES = {
    "czlonek": {"requires_approval": False, "pay_online": True, "pay_transfer": True, "pay_cash": True, "pay_pass": True},
    "powracajacy": {"requires_approval": False, "pay_online": True, "pay_transfer": True, "pay_cash": True, "pay_pass": False},
    "nowy": {"requires_approval": True, "pay_online": True, "pay_transfer": False, "pay_cash": False, "pay_pass": False},
}


class NumberSequence(models.Model):
    """Licznik numerów rezerwacji w danym roku (blokowany wierszowo przy nadawaniu)."""

    year = models.PositiveIntegerField(primary_key=True)
    last = models.PositiveIntegerField(default=0)


class Reservation(models.Model):
    class Status(models.TextChoices):
        HOLD = "oczekuje", "Oczekuje na płatność"
        PENDING_APPROVAL = "do_zatwierdzenia", "Czeka na zatwierdzenie"
        CONFIRMED = "potwierdzona", "Potwierdzona"
        CANCELLED = "anulowana", "Anulowana"
        NO_SHOW = "nieodbyta", "Nieodbyta"

    class Payment(models.TextChoices):
        DUE = "do_zaplaty", "Do zapłaty"
        PAID = "oplacona", "Opłacona"
        WAIVED = "zwolniona", "Zwolniona z opłaty"
        NONE = "brak", "Bez opłaty (anulowana)"
        CONTRACT = "umowa", "Rozliczana wg umowy z klubem"
        PASS = "kod", "Opłacona kodem wejścia"

    number = models.CharField("numer", max_length=24, unique=True)
    lane = models.ForeignKey(Lane, verbose_name="oś", on_delete=models.PROTECT, related_name="reservations")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="rezerwujący (PS)", on_delete=models.PROTECT, related_name="reservations"
    )
    starts_at = models.DateTimeField("początek", db_index=True)
    ends_at = models.DateTimeField("koniec")
    persons = models.PositiveIntegerField("liczba osób")
    notes = models.CharField("uwagi", max_length=500, blank=True)

    price_rule = models.ForeignKey(PriceRule, verbose_name="tabela opłat", on_delete=models.PROTECT, related_name="+")
    hourly_amount = models.PositiveIntegerField("opłata za godzinę [gr]")
    with_officer = models.BooleanField("prowadzący od strzelnicy", default=False)
    officer_hourly = models.PositiveIntegerField("dopłata za prowadzącego [gr / h]", default=0)
    officer = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="przydzielony prowadzący (PS)", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="officer_reservations")
    amount = models.PositiveIntegerField("należność [gr]")
    paid_amount = models.PositiveIntegerField("zapłacono [gr]", default=0)
    access_code = models.ForeignKey(
        "association.AccessCode", verbose_name="kod dostępu klubu", null=True, blank=True,
        on_delete=models.PROTECT, related_name="reservations",
    )
    client_pass = models.ForeignKey(
        "clients.ClientPass", verbose_name="kod wejścia strzelca", null=True, blank=True,
        on_delete=models.PROTECT, related_name="reservations",
    )

    status = models.CharField("status", max_length=16, choices=Status.choices, default=Status.CONFIRMED, db_index=True)
    payment_status = models.CharField("płatność", max_length=16, choices=Payment.choices, default=Payment.DUE, db_index=True)
    late_cancellation = models.BooleanField("anulowana po terminie bezpłatnego anulowania", default=False)
    cancelled_at = models.DateTimeField("anulowano", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="anulował", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cancel_reason = models.CharField("powód anulowania", max_length=255, blank=True)
    payment_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    payment_marked_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    client_group = models.CharField("grupa strzelca przy rezerwacji", max_length=12, blank=True)
    approved_at = models.DateTimeField("zatwierdzono", null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    pay_by = models.DateTimeField("opłać do (inaczej rezerwacja się zwalnia)", null=True, blank=True)

    class PayMethod(models.TextChoices):
        ONLINE = "online", "Online (Tpay)"
        TRANSFER = "przelew", "Przelew tradycyjny"
        CASH = "gotowka", "Gotówka na miejscu"

    pay_method = models.CharField("wybrany sposób płatności", max_length=10, choices=PayMethod.choices, blank=True)
    needs_approval = models.BooleanField("po opłaceniu czeka na zatwierdzenie", default=False)
    # Przelew tradycyjny: przypomnienie strzelcowi przed terminem i powiadomienie zarządu po terminie (decyzja obsługi).
    pay_reminder_sent_at = models.DateTimeField("przypomnienie o przelewie wysłano", null=True, blank=True)
    overdue_notified_at = models.DateTimeField("zarząd powiadomiony o braku przelewu", null=True, blank=True)
    # Rozliczenie po zakończeniu: dopłata, gdy w książce pobytu było więcej strzelających niż zadeklarowano.
    settled_at = models.DateTimeField("rozliczono po terminie", null=True, blank=True)
    declared_persons = models.PositiveIntegerField("zadeklarowana liczba osób", null=True, blank=True)
    series = models.UUIDField("seria (rezerwacja cykliczna)", null=True, blank=True, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField("utworzono", auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at", "lane__order"]
        verbose_name = "rezerwacja"
        verbose_name_plural = "rezerwacje"
        permissions = [
            ("manage_reservations", "Może zarządzać rezerwacjami i blokadami osi"),
            ("manage_booking_settings", "Może zmieniać ustawienia rezerwacji i importować archiwum"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="reservation_positive_length"),
            models.CheckConstraint(condition=Q(persons__gte=1), name="reservation_persons_min"),
            models.CheckConstraint(condition=Q(access_code__isnull=True) | Q(client_pass__isnull=True), name="reservation_one_code"),
            # Gwarancja na poziomie bazy: na jednej osi aktywne rezerwacje nie mogą się nakładać w czasie.
            ExclusionConstraint(
                name="reservation_no_overlap",
                expressions=[
                    ("lane", RangeOperators.EQUAL),
                    (TsTzRange("starts_at", "ends_at"), RangeOperators.OVERLAPS),
                ],
                condition=~Q(status="anulowana"),
            ),
        ]

    def __str__(self):
        return f"{self.number} · {self.lane} · {timezone.localtime(self.starts_at):%d.%m.%Y %H:%M}"

    @property
    def hours(self):
        return int((self.ends_at - self.starts_at).total_seconds() // 3600)

    @property
    def officer_amount(self):
        """Dopłata za prowadzącego od strzelnicy (wliczona w należność)."""
        return self.officer_hourly * self.hours

    def recompute_amount(self):
        """Należność = opłata za oś × godziny + dopłata za prowadzącego × godziny."""
        self.amount = (self.hourly_amount + self.officer_hourly) * self.hours

    @property
    def range_officer_user(self):
        """Kto prowadzi strzelanie: przydzielony prowadzący od strzelnicy albo rezerwujący PS."""
        return self.officer if self.with_officer else self.user

    @property
    def is_active(self):
        return self.status == self.Status.CONFIRMED

    @property
    def is_finished(self):
        return self.status == self.Status.CONFIRMED and self.ends_at <= timezone.now()

    @property
    def display_status(self):
        if self.is_finished and self.status != self.Status.HOLD:
            return "Zakończona"
        if self.status == self.Status.HOLD and self.pay_method == self.PayMethod.TRANSFER:
            return "Przelew po terminie — do decyzji obsługi" if self.transfer_overdue else "Oczekuje na przelew"
        return self.get_status_display()

    @property
    def transfer_overdue(self):
        """Przelew tradycyjny niezaksięgowany w terminie — rezerwacja czeka na decyzję obsługi (import wyciągu / anulowanie)."""
        return (self.status == self.Status.HOLD and self.pay_method == self.PayMethod.TRANSFER
                and self.pay_by is not None and self.pay_by < timezone.now())

    @property
    def free_cancel_deadline(self):
        return self.starts_at - timedelta(hours=BookingSettings.load().free_cancel_hours)

    def can_user_cancel(self, now=None):
        """Strzelec anuluje sam do terminu z ustawień (domyślnie 24 h przed); później tylko obsługa.
        Rezerwację czekającą na zatwierdzenie można wycofać do jej początku."""
        now = now or timezone.now()
        if self.status in (self.Status.PENDING_APPROVAL, self.Status.HOLD):
            return self.starts_at > now
        return self.status == self.Status.CONFIRMED and now <= self.free_cancel_deadline


class Block(models.Model):
    """Blokada administracyjna osi (zawody, prace, awaria). Wpisy cykliczne mają wspólny `series`."""

    title = models.CharField("tytuł", max_length=120)
    reason = models.CharField("opis", max_length=255, blank=True)
    lanes = models.ManyToManyField(Lane, verbose_name="osie", related_name="blocks")
    starts_at = models.DateTimeField("początek", db_index=True)
    ends_at = models.DateTimeField("koniec")
    series = models.UUIDField("seria", default=uuid.uuid4, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["starts_at"]
        verbose_name = "blokada osi"
        verbose_name_plural = "blokady osi"
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="block_positive_length")]

    def __str__(self):
        return f"{self.title} · {timezone.localtime(self.starts_at):%d.%m.%Y %H:%M}–{timezone.localtime(self.ends_at):%H:%M}"


class ArchivedReservation(models.Model):
    """Rezerwacja z poprzedniego systemu (GunTracker) — archiwum do wglądu. Przyszłe terminy dostają blokadę osi."""

    source = models.CharField("system źródłowy", max_length=20, default="guntracker")
    row_hash = models.CharField(max_length=64, unique=True)
    number = models.CharField("numer w GunTrackerze", max_length=40, db_index=True)
    starts_at = models.DateTimeField("początek", db_index=True)
    ends_at = models.DateTimeField("koniec")
    client = models.CharField("strzelec", max_length=160)
    lane_label = models.CharField("oś / instruktor (GunTracker)", max_length=80)
    lane = models.ForeignKey(Lane, verbose_name="oś", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    positions = models.CharField("stanowiska", max_length=40, blank=True)
    range_officer = models.CharField("prowadzący strzelanie", max_length=160, blank=True)
    logbook_info = models.CharField("księga pobytu", max_length=80, blank=True)
    payment = models.CharField("płatność", max_length=80, blank=True)
    contact = models.CharField("kontakt", max_length=40, blank=True)
    block = models.ForeignKey(Block, verbose_name="blokada osi", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]
        verbose_name = "rezerwacja archiwalna (GunTracker)"
        verbose_name_plural = "archiwum rezerwacji"

    def __str__(self):
        return f"{self.number} {self.starts_at:%Y-%m-%d %H:%M}"


class WaitlistEntry(models.Model):
    """Zapis na liście oczekujących na zajęty termin osi. Po zwolnieniu terminu pierwsza osoba dostaje ofertę
    i przez `hold_until` tylko ona może go zarezerwować."""

    class Status(models.TextChoices):
        WAITING = "czeka", "Czeka"
        OFFERED = "oferta", "Termin zwolniony — czeka na rezerwację"
        DONE = "zarezerwowane", "Zarezerwowane"
        EXPIRED = "wygasla", "Oferta wygasła"
        CANCELLED = "anulowany", "Wycofany"

    ACTIVE = (Status.WAITING, Status.OFFERED)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="waitlist")
    lane = models.ForeignKey(Lane, verbose_name="oś", on_delete=models.CASCADE, related_name="waitlist")
    starts_at = models.DateTimeField("od")
    ends_at = models.DateTimeField("do")
    persons = models.PositiveIntegerField("liczba osób", default=1)
    status = models.CharField("status", max_length=14, choices=Status.choices, default=Status.WAITING, db_index=True)
    offered_at = models.DateTimeField(null=True, blank=True)
    hold_until = models.DateTimeField("termin trzymany do", null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name = "lista oczekujących"
        verbose_name_plural = "lista oczekujących"
        constraints = [models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="waitlist_positive_length")]

    def __str__(self):
        return f"{self.user} — {self.lane}, {self.starts_at:%d.%m %H:%M}"


class CalendarFeed(models.Model):
    """Prywatny link do subskrypcji kalendarza zajętości (dla zarządu). Przechowywany tylko skrót tokenu."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_feed")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "link kalendarza"
        verbose_name_plural = "linki kalendarza"
