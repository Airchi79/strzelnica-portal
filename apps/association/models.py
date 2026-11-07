"""Stowarzyszenie: członkowie, deklaracje, składki, uchwały, Walne Zebranie, kluby i kody dostępu.

Zasady wg Regulaminu Stowarzyszenia „Strzelnica Sątok” (wersja z 20.04.2026):
- władze: Walne Zebranie Członków i Zarząd (2 osoby: Prezes, Wiceprezes) — § 12–13;
- uchwały Zarządu: zwykła większość w obecności całego Zarządu, przy równości decyduje Prezes — § 25;
- uchwały WZC: jawnie, ważoną większością (zwyczajny 1, nadzwyczajny 50), przy obecności Prezesa — § 19;
- pełnomocnictwa na WZC: zwyczajny najwyżej za 2 członków, nadzwyczajny bez limitu — § 21;
- członkowie zwyczajni i nadzwyczajni; nadzwyczajni zwolnieni ze składek — § 30–31;
- przyjęcie uchwałą Zarządu w ciągu 2 miesięcy od deklaracji — § 31–32;
- zmiana rodzaju członkostwa uchwałą członków nadzwyczajnych (większość, quorum ≥ połowa) — § 31 pkt 4;
- zawieszenie / wykluczenie uchwałą Zarządu, odwołanie do WZC w 7 dni — § 37–38.
"""

import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.credentials.models import private_storage


class MembershipSettings(models.Model):
    """Wysokość składek ustala Zarząd uchwałą (§ 27); tu zapisana jest obowiązująca wartość."""

    annual_fee = models.PositiveIntegerField("składka roczna [gr]", default=20000)
    entry_fee = models.PositiveIntegerField("wpisowe [gr]", default=5000)
    due_month = models.PositiveSmallIntegerField("termin płatności — miesiąc", default=1)
    due_day = models.PositiveSmallIntegerField("termin płatności — dzień", default=31)
    reminder_every_days = models.PositiveIntegerField("przypomnienie o zaległości co [dni]", default=30)

    class Meta:
        verbose_name = "ustawienia członkostwa"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Member(models.Model):
    class Kind(models.TextChoices):
        ORDINARY = "zwyczajny", "Członek zwyczajny"
        EXTRAORDINARY = "nadzwyczajny", "Członek nadzwyczajny"

    class Status(models.TextChoices):
        ACTIVE = "aktywny", "Aktywny"
        SUSPENDED = "zawieszony", "Zawieszony w prawach"
        REMOVED = "wykreslony", "Wykreślony"

    class BoardRole(models.TextChoices):
        NONE = "", "—"
        PRESIDENT = "prezes", "Prezes Zarządu"
        VICE = "wiceprezes", "Wiceprezes Zarządu"

    # Członek może być w rejestrze bez konta w systemie (np. założyciele); konto można połączyć później.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="member"
    )
    first_name = models.CharField("imię", max_length=80)
    last_name = models.CharField("nazwisko", max_length=120)
    email = models.EmailField("e-mail", blank=True)
    address = models.CharField("adres zamieszkania", max_length=255, blank=True)
    number = models.PositiveIntegerField("numer członkowski", unique=True)
    kind = models.CharField("rodzaj", max_length=16, choices=Kind.choices, default=Kind.ORDINARY)
    status = models.CharField("status", max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    board_role = models.CharField("funkcja w Zarządzie", max_length=16, choices=BoardRole.choices, blank=True, default="")
    joined_on = models.DateField("data przyjęcia")
    removed_on = models.DateField("data wykreślenia", null=True, blank=True)
    status_note = models.CharField("uzasadnienie statusu", max_length=255, blank=True)
    appeal_until = models.DateField("termin odwołania do WZC", null=True, blank=True)
    founder = models.BooleanField("założyciel", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["number"]
        verbose_name = "członek"
        verbose_name_plural = "członkowie"
        permissions = [
            ("manage_members", "Może prowadzić rejestr członków, deklaracje i składki"),
            ("manage_resolutions", "Może prowadzić rejestr uchwał i protokoły Walnego Zebrania"),
            ("view_reports", "Może przeglądać raporty"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["board_role"], condition=~Q(board_role=""), name="one_person_per_board_role"),
        ]

    def __str__(self):
        return f"{self.number} {self.full_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def contact_email(self):
        return self.user.email if self.user_id else self.email

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def pays_fees(self):
        return self.kind == self.Kind.ORDINARY

    @property
    def vote_weight(self):
        return 50 if self.kind == self.Kind.EXTRAORDINARY else 1

    @property
    def on_board(self):
        return bool(self.board_role)


class MembershipApplication(models.Model):
    class Status(models.TextChoices):
        PENDING = "oczekuje", "Oczekuje na decyzję Zarządu"
        ACCEPTED = "przyjeta", "Przyjęta"
        REJECTED = "odrzucona", "Odrzucona"
        WITHDRAWN = "wycofana", "Wycofana"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="membership_applications")
    submitted_at = models.DateTimeField("złożono", auto_now_add=True)
    statement = models.TextField("treść oświadczenia")
    regulation_version = models.ForeignKey(
        "regulations.RegulationVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    motivation = models.CharField("uzasadnienie / zainteresowania", max_length=500, blank=True)
    status = models.CharField("status", max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    resolution = models.ForeignKey("Resolution", null=True, blank=True, on_delete=models.PROTECT, related_name="applications")
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        verbose_name = "deklaracja członkowska"
        verbose_name_plural = "deklaracje członkowskie"
        constraints = [
            models.UniqueConstraint(fields=["user"], condition=Q(status="oczekuje"), name="one_pending_application"),
        ]

    @property
    def deadline(self):
        from apps.core.dates import add_months

        return add_months(self.submitted_at.date(), 2)


class Fee(models.Model):
    """Należność członkowska: wpisowe albo składka roczna. Opłacana z salda strzelca."""

    class Kind(models.TextChoices):
        ENTRY = "wpisowe", "Wpisowe"
        ANNUAL = "skladka", "Składka roczna"

    class Status(models.TextChoices):
        DUE = "do_zaplaty", "Do zapłaty"
        PAID = "oplacona", "Opłacona"
        WAIVED = "umorzona", "Umorzona"

    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="fees")
    kind = models.CharField("rodzaj", max_length=16, choices=Kind.choices)
    year = models.PositiveIntegerField("rok", null=True, blank=True)
    amount = models.PositiveIntegerField("kwota [gr]")
    due_on = models.DateField("termin płatności")
    status = models.CharField("status", max_length=16, choices=Status.choices, default=Status.DUE, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    last_reminder_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due_on", "pk"]
        verbose_name = "składka / wpisowe"
        verbose_name_plural = "składki i wpisowe"
        constraints = [
            models.UniqueConstraint(fields=["member", "year"], condition=Q(kind="skladka"), name="one_annual_fee_per_year"),
            models.UniqueConstraint(fields=["member"], condition=Q(kind="wpisowe"), name="one_entry_fee"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}{' ' + str(self.year) if self.year else ''} — {self.member}"

    @property
    def label(self):
        return f"{self.get_kind_display()}{' ' + str(self.year) if self.year else ''}"


class Resolution(models.Model):
    class Organ(models.TextChoices):
        BOARD = "zarzad", "Zarząd"
        GENERAL = "wzc", "Walne Zebranie Członków"
        EXTRAORDINARY = "nadzwyczajni", "Członkowie nadzwyczajni"
        FOUNDING = "zalozycielskie", "Zebranie założycielskie"

    class Status(models.TextChoices):
        DRAFT = "projekt", "Projekt"
        VOTING = "glosowanie", "Głosowanie"
        ADOPTED = "przyjeta", "Przyjęta"
        REJECTED = "odrzucona", "Odrzucona"
        REPEALED = "uchylona", "Uchylona"

    class Action(models.TextChoices):
        NONE = "", "Brak automatycznego skutku"
        ADMIT = "przyjecie", "Przyjęcie członków (deklaracje)"
        REJECT_APPLICATIONS = "odmowa", "Odmowa przyjęcia (deklaracje)"
        SUSPEND = "zawieszenie", "Zawieszenie członków"
        REINSTATE = "przywrocenie", "Przywrócenie praw członkowskich"
        REMOVE = "wykreslenie", "Wykreślenie / wykluczenie członków"
        CHANGE_KIND = "zmiana_rodzaju", "Zmiana rodzaju członkostwa"
        SET_FEES = "skladki", "Ustalenie wysokości składki i wpisowego"
        WAIVE_FEES = "umorzenie", "Umorzenie należności członkowskich"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organ = models.CharField("organ", max_length=16, choices=Organ.choices)
    year = models.PositiveIntegerField("rok", null=True, blank=True)
    number = models.PositiveIntegerField("numer", null=True, blank=True)
    title = models.CharField("w sprawie", max_length=255)
    content = models.TextField("treść")
    status = models.CharField("status", max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    adopted_on = models.DateField("data podjęcia", null=True, blank=True)
    historical = models.BooleanField("wprowadzona z dokumentu papierowego", default=False)

    action = models.CharField("skutek", max_length=20, choices=Action.choices, blank=True, default="")
    target_members = models.ManyToManyField(Member, blank=True, related_name="resolutions", verbose_name="dotyczy członków")
    target_applications = models.ManyToManyField(MembershipApplication, blank=True, related_name="+", verbose_name="dotyczy deklaracji")
    params = models.JSONField("parametry skutku", default=dict, blank=True)
    action_applied_at = models.DateTimeField(null=True, blank=True)

    votes_for = models.PositiveIntegerField("głosy za (ważone)", default=0)
    votes_against = models.PositiveIntegerField("głosy przeciw (ważone)", default=0)
    votes_abstain = models.PositiveIntegerField("wstrzymujące (ważone)", default=0)
    meeting = models.ForeignKey("GeneralMeeting", null=True, blank=True, on_delete=models.PROTECT, related_name="resolutions")
    attachment = models.FileField("skan / załącznik (PDF)", storage=private_storage, upload_to="uchwaly/", blank=True)
    repealed_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="repeals")

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-year", "-number", "-created_at"]
        verbose_name = "uchwała"
        verbose_name_plural = "uchwały"
        constraints = [
            models.UniqueConstraint(
                fields=["organ", "year", "number"], condition=Q(number__isnull=False), name="resolution_number_unique"
            ),
        ]

    def __str__(self):
        return f"{self.display_number} — {self.title}"

    @property
    def display_number(self):
        prefix = {
            self.Organ.BOARD: "Uchwała Zarządu",
            self.Organ.GENERAL: "Uchwała WZC",
            self.Organ.EXTRAORDINARY: "Uchwała członków nadzwyczajnych",
            self.Organ.FOUNDING: "Uchwała zebrania założycielskiego",
        }[self.organ]
        return f"{prefix} Nr {self.number}/{self.year}" if self.number else f"{prefix} (projekt)"


class Vote(models.Model):
    class Choice(models.TextChoices):
        FOR = "za", "Za"
        AGAINST = "przeciw", "Przeciw"
        ABSTAIN = "wstrzymuje", "Wstrzymuję się"

    resolution = models.ForeignKey(Resolution, on_delete=models.CASCADE, related_name="votes")
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="votes")
    choice = models.CharField(max_length=12, choices=Choice.choices)
    weight = models.PositiveIntegerField(default=1)
    proxy = models.ForeignKey(Member, null=True, blank=True, on_delete=models.PROTECT, related_name="proxy_votes")
    cast_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["resolution", "member"], name="one_vote_per_member")]


class GeneralMeeting(models.Model):
    """Walne Zebranie Członków (lub zebranie członków nadzwyczajnych): lista obecności i pełnomocnictwa."""

    class Kind(models.TextChoices):
        GENERAL = "wzc", "Walne Zebranie Członków"
        EXTRAORDINARY = "nadzwyczajni", "Zebranie członków nadzwyczajnych"

    kind = models.CharField("rodzaj", max_length=16, choices=Kind.choices, default=Kind.GENERAL)
    held_on = models.DateField("data")
    place = models.CharField("miejsce", max_length=160)
    chair = models.CharField("przewodniczący", max_length=120, blank=True)
    secretary = models.CharField("protokolant", max_length=120, blank=True)
    announced_on = models.DateField("ogłoszono (min. 14 dni wcześniej)", null=True, blank=True)
    notes = models.TextField("przebieg / ustalenia", blank=True)
    closed = models.BooleanField("protokół zamknięty", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-held_on"]
        verbose_name = "zebranie członków"
        verbose_name_plural = "zebrania członków"

    def __str__(self):
        return f"{self.get_kind_display()} {self.held_on:%d.%m.%Y}"


class Attendance(models.Model):
    meeting = models.ForeignKey(GeneralMeeting, on_delete=models.CASCADE, related_name="attendance")
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="attendances")
    proxy = models.ForeignKey(
        Member, null=True, blank=True, on_delete=models.PROTECT, related_name="represented",
        help_text="Wypełnij, gdy członek jest reprezentowany przez pełnomocnika (pisemne pełnomocnictwo).",
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["meeting", "member"], name="attendance_unique")]

    @property
    def weight(self):
        return self.member.vote_weight


class Club(models.Model):
    name = models.CharField("nazwa", max_length=160)
    nip = models.CharField("NIP", max_length=13, blank=True)
    contact_name = models.CharField("osoba kontaktowa", max_length=120, blank=True)
    contact_email = models.EmailField("e-mail", blank=True)
    contact_phone = models.CharField("telefon", max_length=32, blank=True)
    agreement = models.TextField("ustalenia / umowa (warunki rozliczeń)", blank=True)
    is_active = models.BooleanField("aktywny", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "klub"
        verbose_name_plural = "kluby"
        permissions = [("manage_clubs", "Może zarządzać klubami i kodami dostępu")]

    def __str__(self):
        return self.name


def new_access_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


class AccessCode(models.Model):
    """Kod dostępu klubu: rezerwacje z kodem są rozliczane wg umowy z klubem, poza saldem strzelca."""

    club = models.ForeignKey(Club, on_delete=models.PROTECT, related_name="codes")
    code = models.CharField("kod", max_length=16, unique=True, default=new_access_code)
    valid_from = models.DateField("ważny od")
    valid_until = models.DateField("ważny do")
    lanes = models.ManyToManyField("facilities.Lane", verbose_name="dozwolone osie", blank=True,
                                   help_text="Puste = wszystkie osie.")
    hours_limit = models.PositiveIntegerField("limit godzin w okresie", null=True, blank=True)
    contract_hourly = models.PositiveIntegerField(
        "kwota umowna / h [gr] (do zestawień)", null=True, blank=True,
        help_text="Informacyjnie — opłata nie jest pobierana z salda. Puste = rozliczenie ryczałtowe wg umowy.",
    )
    is_active = models.BooleanField("aktywny", default=True)
    note = models.CharField("uwagi", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-valid_until"]
        verbose_name = "kod dostępu"
        verbose_name_plural = "kody dostępu"
        constraints = [models.CheckConstraint(condition=Q(valid_until__gte=models.F("valid_from")), name="code_period_order")]

    def __str__(self):
        return f"{self.code} ({self.club})"
