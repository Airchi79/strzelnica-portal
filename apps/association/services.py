"""Reguły stowarzyszenia (Regulamin z 20.04.2026, decyzje Zarządu z 31.10.2026: składka 200 zł/rok do 31.01,
wpisowe 50 zł, przy przyjęciu w trakcie roku pełna składka)."""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.urls import reverse
from django.utils import timezone

from apps.audit.context import client_ip, get_request
from apps.audit.services import log_event
from apps.core.notifications import notify_board, notify_user
from apps.regulations.models import RegulationDocument

from .models import (
    AccessCode,
    Fee,
    GeneralMeeting,
    Member,
    MembershipApplication,
    MembershipSettings,
    Resolution,
    Vote,
)

User = get_user_model()
PERM_MEMBERS = "association.manage_members"
PERM_RESOLUTIONS = "association.manage_resolutions"

APPLICATION_STATEMENT = (
    "Oświadczam, że chcę przystąpić do Stowarzyszenia „STRZELNICA SĄTOK”, zapoznałem(-am) się z jego Regulaminem, "
    "akceptuję go i zobowiązuję się przestrzegać Regulaminu oraz uchwał władz Stowarzyszenia, terminowo opłacać "
    "składki członkowskie i jednorazową opłatę wpisową."
)


# --- członkowie ----------------------------------------------------------------------


def next_member_number():
    return (Member.objects.aggregate(m=Max("number"))["m"] or 0) + 1


def association_rules():
    doc = RegulationDocument.objects.filter(slug="regulamin-stowarzyszenia").first()
    return doc.current_version() if doc else None


def membership_of(user):
    return Member.objects.filter(user=user).first() if user.is_authenticated else None


@transaction.atomic
def create_account_for_person(email, first_name, last_name, by):
    """Konto dla osoby wprowadzanej przez Zarząd (np. założyciel). Osoba ustawia hasło z linku w e-mailu."""
    email = email.strip().lower()
    if User.objects.filter(email__iexact=email).exists():
        raise ValidationError("Konto z tym adresem e-mail już istnieje — wybierz je z listy.")
    user = User.objects.create_user(email=email, first_name=first_name.strip(), last_name=last_name.strip())
    user.set_unusable_password()
    user.email_verified_at = timezone.now()
    user.save()
    log_event("konto.utworzone_przez_zarzad", user, email, actor=by)
    form = PasswordResetForm({"email": email})
    if form.is_valid():
        from django.conf import settings

        request = get_request()
        transaction.on_commit(lambda: form.save(
            request=request,
            use_https=settings.SITE_URL.startswith("https"),
            domain_override=settings.SITE_URL.split("://", 1)[-1],
            email_template_name="emails/account_created.txt",
            subject_template_name="emails/account_created_subject.txt",
            extra_email_context={"site_url": settings.SITE_URL},
        ))
    return user


@transaction.atomic
def add_member(*, kind, joined_on, by, user=None, first_name="", last_name="", email="", address="",
               founder=False, board_role="", charge_fees=False, note="", number=None):
    """Wprowadzenie członka do rejestru (np. założyciele i członkowie przyjęci przed uruchomieniem systemu).
    Konto w systemie nie jest wymagane — można je połączyć później."""
    if user is not None and Member.objects.filter(user=user).exists():
        raise ValidationError("Ta osoba jest już w rejestrze członków.")
    if board_role and Member.objects.filter(board_role=board_role).exists():
        raise ValidationError("Ta funkcja w Zarządzie jest już przypisana innej osobie.")
    if number is not None and Member.objects.filter(number=number).exists():
        raise ValidationError(f"Numer członkowski {number} jest już zajęty.")
    first_name = (first_name or (user.first_name if user else "")).strip()
    last_name = (last_name or (user.last_name if user else "")).strip()
    if not first_name or not last_name:
        raise ValidationError("Podaj imię i nazwisko członka.")
    member = Member.objects.create(
        user=user, first_name=first_name, last_name=last_name, email=(email or (user.email if user else "")).strip().lower(),
        address=address.strip(), number=number or next_member_number(), kind=kind, joined_on=joined_on, founder=founder,
        board_role=board_role, status_note=note[:255],
    )
    log_event("czlonek.wpis_do_rejestru", member, str(member), changes={"rodzaj": kind, "funkcja": board_role}, actor=by)
    if charge_fees and member.pays_fees:
        charge_admission_fees(member, joined_on)
    return member


@transaction.atomic
def set_board_role(member, role, by):
    if role and Member.objects.filter(board_role=role).exclude(pk=member.pk).exists():
        raise ValidationError("Ta funkcja w Zarządzie jest już przypisana innej osobie.")
    before = member.board_role
    member.board_role = role
    member.save(update_fields=["board_role"])
    log_event("czlonek.funkcja_zarzad", member, str(member), changes={"funkcja": [before, role]}, actor=by)
    return member


def board_members():
    return Member.objects.filter(status=Member.Status.ACTIVE).exclude(board_role="").select_related("user")


# --- deklaracje -------------------------------------------------------------------------


@transaction.atomic
def submit_application(user, motivation=""):
    if not user.email_verified:
        raise ValidationError("Najpierw potwierdź adres e-mail.")
    member = Member.objects.filter(user=user).first()
    if member and member.status != Member.Status.REMOVED:
        raise ValidationError("Jesteś już członkiem Stowarzyszenia.")
    if MembershipApplication.objects.filter(user=user, status=MembershipApplication.Status.PENDING).exists():
        raise ValidationError("Twoja deklaracja czeka już na decyzję Zarządu.")
    app = MembershipApplication.objects.create(
        user=user,
        statement=APPLICATION_STATEMENT,
        regulation_version=association_rules(),
        motivation=motivation.strip()[:500],
        ip=client_ip(get_request()),
    )
    log_event("deklaracja.zlozona", app, user.email, actor=user)
    notify_user(user, "Deklaracja członkowska przyjęta do rozpatrzenia", "emails/application_submitted.txt", application=app)
    notify_board(
        "Nowa deklaracja członkowska",
        "emails/application_board.txt",
        PERM_MEMBERS,
        url=reverse("panel:applications"),
        body=f"{user.full_name} — decyzja do {app.deadline:%d.%m.%Y}",
        application=app,
    )
    return app


@transaction.atomic
def withdraw_application(app, user):
    app = MembershipApplication.objects.select_for_update().get(pk=app.pk)
    if app.user_id != user.pk or app.status != MembershipApplication.Status.PENDING:
        raise ValidationError("Tej deklaracji nie można wycofać.")
    app.status = MembershipApplication.Status.WITHDRAWN
    app.decided_at = timezone.now()
    app.save()
    log_event("deklaracja.wycofana", app, user.email, actor=user)
    return app


def remind_board_about_applications(today=None):
    """Przypomnienie Zarządowi o deklaracjach, którym zbliża się 2-miesięczny termin (§ 32)."""
    today = today or timezone.localdate()
    soon = [a for a in MembershipApplication.objects.filter(status=MembershipApplication.Status.PENDING).select_related("user")
            if a.deadline - timedelta(days=14) <= today]
    if soon:
        notify_board(
            f"Deklaracje członkowskie z bliskim terminem ({len(soon)})",
            "emails/applications_deadline.txt",
            PERM_MEMBERS,
            url=reverse("panel:applications"),
            body=", ".join(f"{a.user.full_name} do {a.deadline:%d.%m}" for a in soon)[:500],
            applications=soon,
        )
    return len(soon)


# --- składki --------------------------------------------------------------------------


def due_date_for(year, cfg=None):
    cfg = cfg or MembershipSettings.load()
    return date(year, cfg.due_month, cfg.due_day)


@transaction.atomic
def charge_admission_fees(member, joined_on):
    """Wpisowe + pełna składka za rok przyjęcia (bez proporcji). Termin: 31.01 albo 14 dni od przyjęcia, co później."""
    if not member.pays_fees:
        return []
    cfg = MembershipSettings.load()
    due = max(due_date_for(joined_on.year, cfg), joined_on + timedelta(days=14))
    created = []
    if not Fee.objects.filter(member=member, kind=Fee.Kind.ENTRY).exists():
        created.append(Fee.objects.create(member=member, kind=Fee.Kind.ENTRY, amount=cfg.entry_fee, due_on=due))
    if not Fee.objects.filter(member=member, kind=Fee.Kind.ANNUAL, year=joined_on.year).exists():
        created.append(Fee.objects.create(member=member, kind=Fee.Kind.ANNUAL, year=joined_on.year, amount=cfg.annual_fee, due_on=due))
    _apply_after_commit(member.user)
    return created


def ensure_annual_fees(year=None, today=None):
    """Nalicza składkę roczną członkom zwyczajnym (aktywnym i zawieszonym). Idempotentne — bezpieczne co godzinę."""
    today = today or timezone.localdate()
    year = year or today.year
    cfg = MembershipSettings.load()
    count = 0
    members = Member.objects.filter(kind=Member.Kind.ORDINARY, joined_on__lt=date(year, 1, 1)).exclude(status=Member.Status.REMOVED)
    for m in members.exclude(fees__kind=Fee.Kind.ANNUAL, fees__year=year).select_related("user"):
        with transaction.atomic():
            Fee.objects.create(member=m, kind=Fee.Kind.ANNUAL, year=year, amount=cfg.annual_fee, due_on=due_date_for(year, cfg))
            count += 1
        _apply_after_commit(m.user)
    if count:
        log_event("skladki.naliczenie", summary=f"Naliczono składkę {year} dla {count} członków")
    return count


def _apply_after_commit(user):
    if user is None:
        return
    from apps.payments.services import apply_wallet

    transaction.on_commit(lambda: apply_wallet(user))  # tylko przy włączonym saldzie strzelca


@transaction.atomic
def link_account(member, user, by):
    if member.user_id:
        raise ValidationError("Członek ma już połączone konto.")
    if Member.objects.filter(user=user).exists():
        raise ValidationError("To konto jest już połączone z innym członkiem.")
    member.user = user
    member.email = user.email
    member.save(update_fields=["user", "email"])
    log_event("czlonek.polaczono_konto", member, f"{member} ↔ {user.email}", actor=by)
    return member


@transaction.atomic
def mark_fee_paid(fee, method, by, note=""):
    """Wpłata składki przyjęta przez zarząd (gotówka, BLIK na telefon, przelew zaksięgowany ręcznie)."""
    from apps.payments.models import Payment
    from apps.payments.services import register_manual_payment

    fee = Fee.objects.select_for_update(of=("self",)).select_related("member__user").get(pk=fee.pk)
    if fee.status != Fee.Status.DUE:
        raise ValidationError("Ta należność nie jest do zapłaty.")
    if fee.member.user_id:
        register_manual_payment(fee.member.user, fee.amount, method, by, fee=fee, note=note)
        fee.refresh_from_db()
        return fee
    # Członek bez konta: wpłata rejestrowana bez salda.
    Payment.objects.create(
        user=None, amount=fee.amount, method=method, fee=fee, status=Payment.Status.COMPLETED, completed_at=timezone.now(),
        title=f"{fee.label} — {fee.member.full_name}", payer_name=fee.member.full_name, booked_on=timezone.localdate(),
        created_by=by, note=note[:255],
    )
    fee.status, fee.paid_at = Fee.Status.PAID, timezone.now()
    fee.save(update_fields=["status", "paid_at"])
    log_event("skladka.oplacona", fee, str(fee), changes={"sposob": method}, actor=by)
    return fee


def overdue_fees(today=None):
    today = today or timezone.localdate()
    return Fee.objects.filter(status=Fee.Status.DUE, due_on__lt=today).select_related("member__user")


def send_fee_reminders(now=None):
    now = now or timezone.now()
    every = timedelta(days=MembershipSettings.load().reminder_every_days)
    count = 0
    by_member = {}
    for fee in overdue_fees(timezone.localdate(now)).filter(Q(last_reminder_at__isnull=True) | Q(last_reminder_at__lt=now - every)):
        by_member.setdefault(fee.member, []).append(fee)
    for member, fees in by_member.items():
        from apps.payments.services import balance_of, topup_code

        if member.user_id is None:
            continue  # bez konta i e-maila w systemie — zarząd widzi zaległość na liście

        notify_user(member.user, "Zaległe należności członkowskie", "emails/fee_reminder.txt",
                    fees=fees, total=sum(f.amount for f in fees), balance=balance_of(member.user), code=topup_code(member.user))
        Fee.objects.filter(pk__in=[f.pk for f in fees]).update(last_reminder_at=now)
        count += 1
    return count


# --- uchwały -----------------------------------------------------------------------------

ORGAN_ACTIONS = {
    Resolution.Organ.BOARD: {
        Resolution.Action.NONE, Resolution.Action.ADMIT, Resolution.Action.REJECT_APPLICATIONS, Resolution.Action.SUSPEND,
        Resolution.Action.REINSTATE, Resolution.Action.REMOVE, Resolution.Action.SET_FEES, Resolution.Action.WAIVE_FEES,
    },
    Resolution.Organ.GENERAL: {Resolution.Action.NONE, Resolution.Action.REINSTATE},  # rozpatrzenie odwołań (§ 18, § 38)
    Resolution.Organ.EXTRAORDINARY: {Resolution.Action.NONE, Resolution.Action.CHANGE_KIND},  # § 31 pkt 4
    Resolution.Organ.FOUNDING: {Resolution.Action.NONE},
}


def validate_action(organ, action, params):
    if action not in ORGAN_ACTIONS[organ]:
        raise ValidationError("Ten organ nie podejmuje uchwał o takim skutku (zob. Regulamin).")
    if action == Resolution.Action.SET_FEES:
        for key in ("annual_fee", "entry_fee"):
            if not isinstance(params.get(key), int) or params[key] < 0:
                raise ValidationError("Podaj wysokość składki rocznej i wpisowego.")
    if action == Resolution.Action.CHANGE_KIND and params.get("kind") not in Member.Kind.values:
        raise ValidationError("Wskaż nowy rodzaj członkostwa.")


@transaction.atomic
def create_resolution(*, organ, title, content, action="", params=None, members=(), applications=(), by, meeting=None):
    params = params or {}
    validate_action(organ, action, params)
    res = Resolution.objects.create(
        organ=organ, title=title.strip(), content=content.strip(), action=action, params=params, created_by=by, meeting=meeting
    )
    res.target_members.set(members)
    res.target_applications.set(applications)
    log_event("uchwala.projekt", res, f"{res.get_organ_display()}: {res.title}", actor=by)
    return res


@transaction.atomic
def add_historical(*, organ, year, number, adopted_on, title, content, attachment=None, by):
    """Uchwała z dokumentu papierowego (np. 1–4/2025 z zebrania założycielskiego). Skutki nie są wykonywane."""
    if Resolution.objects.filter(organ=organ, year=year, number=number).exists():
        raise ValidationError("Uchwała o tym numerze już istnieje w rejestrze.")
    res = Resolution.objects.create(
        organ=organ, year=year, number=number, adopted_on=adopted_on, title=title.strip(), content=content.strip(),
        status=Resolution.Status.ADOPTED, historical=True, created_by=by,
    )
    if attachment:
        res.attachment = attachment
        res.save(update_fields=["attachment"])
    log_event("uchwala.historyczna", res, str(res), actor=by)
    return res


def _next_number(organ, year):
    return (Resolution.objects.filter(organ=organ, year=year).aggregate(m=Max("number"))["m"] or 0) + 1


@transaction.atomic
def start_board_voting(res, by):
    res = Resolution.objects.select_for_update().get(pk=res.pk)
    if res.organ != Resolution.Organ.BOARD or res.status != Resolution.Status.DRAFT:
        raise ValidationError("Głosowanie online dotyczy projektów uchwał Zarządu.")
    if board_members().count() < 2:
        raise ValidationError("W rejestrze członków muszą być oznaczeni Prezes i Wiceprezes Zarządu.")
    res.status = Resolution.Status.VOTING
    res.save(update_fields=["status"])
    log_event("uchwala.glosowanie_start", res, res.title, actor=by)
    notify_board(f"Głosowanie: {res.title}", "emails/resolution_vote.txt", PERM_RESOLUTIONS,
                 url=reverse("panel:resolution", args=[res.pk]), body="Oddaj głos w panelu zarządu.", resolution=res)
    return res


@transaction.atomic
def cast_board_vote(res, user, choice, now=None):
    """Głos członka Zarządu. Po głosach całego Zarządu uchwała jest rozstrzygana (§ 25)."""
    res = Resolution.objects.select_for_update().get(pk=res.pk)
    member = Member.objects.filter(user=user, status=Member.Status.ACTIVE).exclude(board_role="").first()
    if member is None:
        raise ValidationError("Głosować mogą tylko członkowie Zarządu.")
    if res.status != Resolution.Status.VOTING:
        raise ValidationError("Głosowanie nad tą uchwałą nie trwa.")
    if choice not in Vote.Choice.values:
        raise ValidationError("Nieprawidłowy głos.")
    Vote.objects.update_or_create(resolution=res, member=member, defaults={"choice": choice, "weight": 1})
    log_event("uchwala.glos", res, f"{member.full_name}: {choice}", actor=user)
    board = list(board_members())
    board_ids = {m.pk for m in board}
    # liczą się głosy obecnego składu Zarządu (osoba, która w trakcie głosowania przestała być w Zarządzie, nie decyduje)
    votes = {v.member_id: v.choice for v in res.votes.all() if v.member_id in board_ids}
    if all(m.pk in votes for m in board):
        f = sum(1 for c in votes.values() if c == Vote.Choice.FOR)
        a = sum(1 for c in votes.values() if c == Vote.Choice.AGAINST)
        president = next((m for m in board if m.board_role == Member.BoardRole.PRESIDENT), None)
        adopted = f > a or (f == a and f > 0 and president is not None and votes.get(president.pk) == Vote.Choice.FOR)
        res.votes_for, res.votes_against = f, a
        res.votes_abstain = sum(1 for c in votes.values() if c == Vote.Choice.ABSTAIN)
        res.save()
        finalize(res, adopted, (now or timezone.now()).date(), by=user)
    return res


def _eligible_attendance(meeting):
    return meeting.attendance.select_related("member__user", "proxy").filter(member__status=Member.Status.ACTIVE)


def validate_meeting(meeting):
    """Pełnomocnictwa (§ 21): pełnomocnik obecny osobiście, członek w pełni praw; zwyczajny — najwyżej za 2 osoby."""
    att = list(meeting.attendance.select_related("member", "proxy"))
    present_in_person = {a.member_id for a in att if a.proxy_id is None}
    errors = []
    load = {}
    for a in att:
        if a.proxy_id:
            if a.proxy_id not in present_in_person:
                errors.append(f"Pełnomocnik {a.proxy} musi być obecny osobiście.")
            if a.proxy.status != Member.Status.ACTIVE:
                errors.append(f"Pełnomocnik {a.proxy} nie jest członkiem w pełni praw.")
            load[a.proxy_id] = load.get(a.proxy_id, 0) + 1
    for proxy_id, n in load.items():
        proxy = Member.objects.get(pk=proxy_id)
        if proxy.kind == Member.Kind.ORDINARY and n > 2:
            errors.append(f"Członek zwyczajny {proxy} może reprezentować najwyżej 2 osoby (reprezentuje {n}).")
    if errors:
        raise ValidationError(errors)


@transaction.atomic
def record_meeting_votes(res, meeting, choices, by, adopted_on=None):
    """Głosowanie na zebraniu: choices = {member_id: 'za'|'przeciw'|'wstrzymuje'} dla obecnych (osobiście lub przez pełnomocnika).
    WZC: wagi 1/50, wymagana obecność Prezesa (§ 19). Zebranie nadzwyczajnych: waga 1, quorum ≥ połowa uprawnionych (§ 31 pkt 4)."""
    res = Resolution.objects.select_for_update().get(pk=res.pk)
    if res.status not in (Resolution.Status.DRAFT, Resolution.Status.VOTING):
        raise ValidationError("Ta uchwała została już rozstrzygnięta.")
    expected_organ = Resolution.Organ.GENERAL if meeting.kind == GeneralMeeting.Kind.GENERAL else Resolution.Organ.EXTRAORDINARY
    if res.organ != expected_organ:
        raise ValidationError("Uchwała nie należy do tego rodzaju zebrania.")
    validate_meeting(meeting)
    attendance = {a.member_id: a for a in _eligible_attendance(meeting)}
    if meeting.kind == GeneralMeeting.Kind.GENERAL:
        president = Member.objects.filter(board_role=Member.BoardRole.PRESIDENT, status=Member.Status.ACTIVE).first()
        if president is None or president.pk not in attendance or attendance[president.pk].proxy_id:
            raise ValidationError("Uchwały WZC wymagają osobistej obecności Prezesa Zarządu (§ 19).")
    else:
        entitled = Member.objects.filter(kind=Member.Kind.EXTRAORDINARY, status=Member.Status.ACTIVE).count()
        present = sum(1 for a in attendance.values() if a.member.kind == Member.Kind.EXTRAORDINARY)
        if present * 2 < entitled:
            raise ValidationError(f"Brak quorum: obecnych {present} z {entitled} członków nadzwyczajnych (wymagana połowa).")
    res.votes.all().delete()
    totals = {Vote.Choice.FOR: 0, Vote.Choice.AGAINST: 0, Vote.Choice.ABSTAIN: 0}
    for member_id, choice in choices.items():
        if choice not in Vote.Choice.values:
            continue
        a = attendance.get(int(member_id))
        if a is None:
            raise ValidationError("Głosować mogą tylko obecni członkowie w pełni praw.")
        if meeting.kind == GeneralMeeting.Kind.EXTRAORDINARY and a.member.kind != Member.Kind.EXTRAORDINARY:
            raise ValidationError("W tym głosowaniu biorą udział tylko członkowie nadzwyczajni.")
        weight = a.member.vote_weight if meeting.kind == GeneralMeeting.Kind.GENERAL else 1
        Vote.objects.create(resolution=res, member=a.member, choice=choice, weight=weight, proxy=a.proxy)
        totals[choice] += weight
    res.meeting = meeting
    res.votes_for, res.votes_against, res.votes_abstain = totals[Vote.Choice.FOR], totals[Vote.Choice.AGAINST], totals[Vote.Choice.ABSTAIN]
    res.save()
    log_event("uchwala.glosowanie_zebranie", res, res.title, changes={"za": res.votes_for, "przeciw": res.votes_against, "wstrz": res.votes_abstain}, actor=by)
    return finalize(res, res.votes_for > res.votes_against, adopted_on or meeting.held_on, by=by)


@transaction.atomic
def finalize(res, adopted, adopted_on, by):
    res = Resolution.objects.select_for_update().get(pk=res.pk)
    res.adopted_on = adopted_on
    if adopted:
        res.status = Resolution.Status.ADOPTED
        res.year = adopted_on.year
        res.number = _next_number(res.organ, res.year)
    else:
        res.status = Resolution.Status.REJECTED
    res.save()
    log_event("uchwala.rozstrzygnieta", res, f"{res}: {res.get_status_display()}", actor=by)
    if adopted:
        apply_action(res, by)
    return res


@transaction.atomic
def repeal(res, repealing, by):
    res = Resolution.objects.select_for_update().get(pk=res.pk)
    if res.status != Resolution.Status.ADOPTED:
        raise ValidationError("Uchylić można tylko przyjętą uchwałę.")
    if repealing.status != Resolution.Status.ADOPTED:
        raise ValidationError("Uchwała uchylająca musi być przyjęta.")
    res.status = Resolution.Status.REPEALED
    res.repealed_by = repealing
    res.save(update_fields=["status", "repealed_by"])
    log_event("uchwala.uchylona", res, f"{res} uchylona przez {repealing}", actor=by)
    return res


def apply_action(res, by):
    """Wykonanie skutku przyjętej uchwały. Każdy skutek jest zapisywany w dzienniku zdarzeń."""
    if res.action_applied_at or not res.action:
        return
    A = Resolution.Action
    adopted_on = res.adopted_on
    if res.action == A.ADMIT:
        for app in res.target_applications.select_for_update().filter(status=MembershipApplication.Status.PENDING).select_related("user"):
            member = Member.objects.filter(user=app.user).first()
            if member is None:
                member = Member.objects.create(
                    user=app.user, first_name=app.user.first_name, last_name=app.user.last_name, email=app.user.email,
                    address=app.user.address, number=next_member_number(), kind=Member.Kind.ORDINARY, joined_on=adopted_on,
                )
            else:  # ponowne przyjęcie osoby wcześniej wykreślonej
                member.status, member.removed_on, member.joined_on = Member.Status.ACTIVE, None, adopted_on
                member.save()
            app.status, app.decided_at, app.resolution = MembershipApplication.Status.ACCEPTED, timezone.now(), res
            app.save()
            charge_admission_fees(member, adopted_on)
            log_event("czlonek.przyjety", member, str(member), changes={"uchwala": str(res)}, actor=by)
            notify_user(app.user, "Witamy w Stowarzyszeniu „STRZELNICA SĄTOK”", "emails/member_admitted.txt", member=member, resolution=res)
    elif res.action == A.REJECT_APPLICATIONS:
        for app in res.target_applications.filter(status=MembershipApplication.Status.PENDING).select_related("user"):
            app.status, app.decided_at, app.resolution = MembershipApplication.Status.REJECTED, timezone.now(), res
            app.save()
            notify_user(app.user, "Decyzja w sprawie deklaracji członkowskiej", "emails/application_rejected.txt", application=app, resolution=res)
    elif res.action in (A.SUSPEND, A.REMOVE, A.REINSTATE):
        status = {A.SUSPEND: Member.Status.SUSPENDED, A.REMOVE: Member.Status.REMOVED, A.REINSTATE: Member.Status.ACTIVE}[res.action]
        for m in res.target_members.select_related("user"):
            m.status = status
            m.status_note = f"{res.display_number}: {res.title}"[:255]
            m.appeal_until = adopted_on + timedelta(days=7) if res.action != A.REINSTATE else None
            if res.action == A.REMOVE:
                m.removed_on, m.board_role = adopted_on, ""
            if res.action == A.REINSTATE:
                m.removed_on = None
            m.save()
            log_event("czlonek.status", m, f"{m} → {m.get_status_display()}", changes={"uchwala": str(res)}, actor=by)
            if m.user_id:
                notify_user(m.user, f"Zmiana statusu członkostwa: {m.get_status_display()}", "emails/member_status.txt", member=m, resolution=res)
    elif res.action == A.CHANGE_KIND:
        kind = res.params["kind"]
        for m in res.target_members.all():
            before = m.kind
            m.kind = kind
            m.save(update_fields=["kind"])
            if kind == Member.Kind.EXTRAORDINARY:  # nadzwyczajni są zwolnieni ze składek (§ 31 ust. 2)
                m.fees.filter(status=Fee.Status.DUE, kind=Fee.Kind.ANNUAL).update(status=Fee.Status.WAIVED, note=f"Zmiana rodzaju członkostwa — {res.display_number}")
            log_event("czlonek.rodzaj", m, str(m), changes={"rodzaj": [before, kind], "uchwala": str(res)}, actor=by)
    elif res.action == A.SET_FEES:
        cfg = MembershipSettings.load()
        before = (cfg.annual_fee, cfg.entry_fee)
        cfg.annual_fee, cfg.entry_fee = res.params["annual_fee"], res.params["entry_fee"]
        cfg.save()
        log_event("skladki.wysokosc", cfg, str(res), changes={"przed_gr": before, "po_gr": [cfg.annual_fee, cfg.entry_fee]}, actor=by)
    elif res.action == A.WAIVE_FEES:
        for m in res.target_members.all():
            n = m.fees.filter(status=Fee.Status.DUE).update(status=Fee.Status.WAIVED, note=f"Umorzenie — {res.display_number}")
            log_event("skladki.umorzenie", m, f"{m}: {n} należności", changes={"uchwala": str(res)}, actor=by)
    res.action_applied_at = timezone.now()
    res.save(update_fields=["action_applied_at"])


# --- kody dostępu klubów -------------------------------------------------------------------


def find_access_code(raw, lane, day, hours, exclude_reservation=None):
    """Sprawdza kod klubu dla rezerwacji. Zwraca AccessCode albo zgłasza ValidationError."""
    code = AccessCode.objects.select_related("club").filter(code=(raw or "").strip().upper()).first()
    if code is None or not code.is_active or not code.club.is_active:
        raise ValidationError("Nieprawidłowy lub nieaktywny kod dostępu.")
    if not (code.valid_from <= day <= code.valid_until):
        raise ValidationError(f"Kod jest ważny od {code.valid_from:%d.%m.%Y} do {code.valid_until:%d.%m.%Y}.")
    if code.lanes.exists() and not code.lanes.filter(pk=lane.pk).exists():
        raise ValidationError("Ten kod nie obejmuje wybranej osi.")
    if code.hours_limit is not None:
        used = code_hours_used(code, exclude_reservation)
        if used + hours > code.hours_limit:
            raise ValidationError(f"Przekroczony limit godzin kodu: wykorzystano {used} z {code.hours_limit} h.")
    return code


def code_hours_used(code, exclude_reservation=None):
    from apps.bookings.models import Reservation

    qs = code.reservations.exclude(status=Reservation.Status.CANCELLED, late_cancellation=False)
    if exclude_reservation is not None:
        qs = qs.exclude(pk=exclude_reservation.pk)
    return sum(r.hours for r in qs)


def code_usage(code):
    from apps.bookings.models import Reservation

    qs = code.reservations.exclude(status=Reservation.Status.CANCELLED, late_cancellation=False)
    hours = sum(r.hours for r in qs)
    return {"reservations": qs.count(), "hours": hours, "value": sum(r.amount for r in qs)}


# --- raporty -----------------------------------------------------------------------------


def membership_stats():
    agg = Member.objects.values("status", "kind").annotate(n=Count("pk"))
    out = {f'{r["status"]}:{r["kind"]}': r["n"] for r in agg}
    out["razem_aktywni"] = sum(r["n"] for r in agg if r["status"] == Member.Status.ACTIVE)
    return out


def fees_summary(year):
    qs = Fee.objects.filter(Q(year=year) | Q(kind=Fee.Kind.ENTRY, due_on__year=year))
    out = {}
    for row in qs.values("kind", "status").annotate(n=Count("pk"), s=Sum("amount")):
        out[f'{row["kind"]}:{row["status"]}'] = {"n": row["n"], "s": row["s"] or 0}
    return out


