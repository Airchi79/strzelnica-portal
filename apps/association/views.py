import csv
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import FileResponse, Http404, HttpResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit.services import log_event, log_sensitive_access
from apps.bookings.models import Reservation
from apps.core.models import AssociationSettings
from apps.core.permissions import panel_required
from apps.facilities.models import Lane
from apps.payments.models import Payment

from . import services
from .forms import (
    AccessCodeForm,
    ApplicationForm,
    BoardRoleForm,
    ClubForm,
    CreateAccountForm,
    FeePaymentForm,
    HistoricalResolutionForm,
    LinkAccountForm,
    MeetingForm,
    MemberEditForm,
    MemberForm,
    ResolutionForm,
    VoteForm,
)
from .models import (
    AccessCode,
    Attendance,
    Club,
    Fee,
    GeneralMeeting,
    Member,
    MembershipApplication,
    MembershipSettings,
    Resolution,
    Vote,
)

PERM_M = services.PERM_MEMBERS
PERM_R = services.PERM_RESOLUTIONS
PERM_C = "association.manage_clubs"
PERM_REP = "association.view_reports"


def _err(request, e):
    for m in (e.messages if isinstance(e, ValidationError) else [str(e)]):
        messages.error(request, m)


# --- strona członka ---------------------------------------------------------------------


@login_required
def membership(request):
    member = services.membership_of(request.user)
    pending = MembershipApplication.objects.filter(user=request.user, status=MembershipApplication.Status.PENDING).first()
    form = ApplicationForm(request.POST or None)
    can_apply = member is None or member.status == Member.Status.REMOVED
    if request.method == "POST" and can_apply:
        if request.POST.get("action") == "withdraw" and pending:
            services.withdraw_application(pending, request.user)
            messages.info(request, "Deklaracja wycofana.")
            return redirect("association:membership")
        elif form.is_valid():
            try:
                services.submit_application(request.user, form.cleaned_data["motivation"])
            except ValidationError as e:
                _err(request, e)
            else:
                messages.success(request, "Deklaracja złożona. Zarząd rozpatrzy ją w ciągu 2 miesięcy (§ 32 Regulaminu).")
            return redirect("association:membership")
    rules = services.association_rules()
    ctx = {"member": member, "pending": pending, "form": form, "rules": rules, "cfg": MembershipSettings.load(), "can_apply": can_apply}
    if member and member.status != Member.Status.REMOVED:
        ctx["fees"] = member.fees.all().order_by("-due_on")
        ctx["resolutions"] = Resolution.objects.filter(status__in=[Resolution.Status.ADOPTED, Resolution.Status.REPEALED]).order_by("-adopted_on")[:30]
        ctx["meetings"] = GeneralMeeting.objects.all()[:10]
    return render(request, "association/membership.html", ctx)


@login_required
def resolution_public(request, pk):
    """Przyjęte uchwały są dostępne dla członków Stowarzyszenia (aktywnych i zawieszonych)."""
    member = services.membership_of(request.user)
    res = get_object_or_404(Resolution, pk=pk, status__in=[Resolution.Status.ADOPTED, Resolution.Status.REPEALED])
    if not (request.user.is_staff or (member and member.status != Member.Status.REMOVED)):
        raise PermissionDenied
    return render(request, "association/resolution_print.html", {"r": res, "association": AssociationSettings.load(), "public": True})


# --- panel: członkowie ---------------------------------------------------------------------


@panel_required(PERM_M)
def panel_members(request):
    qs = Member.objects.select_related("user")
    f = {"status": request.GET.get("status", ""), "kind": request.GET.get("kind", ""), "q": request.GET.get("q", "").strip()}
    if f["status"] in Member.Status.values:
        qs = qs.filter(status=f["status"])
    if f["kind"] in Member.Kind.values:
        qs = qs.filter(kind=f["kind"])
    if f["q"]:
        qs = qs.filter(Q(last_name__icontains=f["q"]) | Q(first_name__icontains=f["q"]) | Q(email__icontains=f["q"]) | Q(user__email__icontains=f["q"]))
    if request.GET.get("format") == "csv":
        log_sensitive_access(None, "rejestr_czlonkow:eksport", "eksport rejestru członków")
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="czlonkowie-{timezone.localdate():%Y%m%d}.csv"'
        response.write("﻿")
        w = csv.writer(response, delimiter=";")
        w.writerow(["Nr", "Imię", "Nazwisko", "E-mail", "Rodzaj", "Status", "Funkcja", "Data przyjęcia", "Założyciel"])
        for m in qs:
            w.writerow([m.number, m.first_name, m.last_name, m.contact_email, m.get_kind_display(), m.get_status_display(),
                        m.get_board_role_display() if m.board_role else "", m.joined_on, "tak" if m.founder else ""])
        return response
    stats = services.membership_stats()
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    pending_apps = MembershipApplication.objects.filter(status=MembershipApplication.Status.PENDING).count()
    return render(request, "panel/members.html", {"page": page, "f": f, "stats": stats, "pending_apps": pending_apps,
                                                  "statuses": Member.Status.choices, "kinds": Member.Kind.choices})


@panel_required(PERM_M)
def panel_member_new(request):
    form = MemberForm(request.POST or None, initial={"joined_on": timezone.localdate(), "kind": Member.Kind.ORDINARY})
    account_form = CreateAccountForm(prefix="acc")
    if request.method == "POST" and request.POST.get("action") == "account":
        account_form = CreateAccountForm(request.POST, prefix="acc")
        if account_form.is_valid():
            try:
                user = services.create_account_for_person(**account_form.cleaned_data, by=request.user)
            except ValidationError as e:
                _err(request, e)
            else:
                messages.success(request, f"Utworzono konto {user.email}. Osoba dostanie e-mail z linkiem do ustawienia hasła.")
                return redirect("panel:member_new")
        form = MemberForm(initial={"joined_on": timezone.localdate()})
    elif request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            m = services.add_member(
                user=d["user"], first_name=d["first_name"], last_name=d["last_name"], email=d["email"], address=d["address"],
                kind=d["kind"], joined_on=d["joined_on"], founder=d["founder"], board_role=d["board_role"] or "",
                charge_fees=d["charge_fees"], number=d["number"], by=request.user,
            )
        except ValidationError as e:
            _err(request, e)
        else:
            messages.success(request, f"Wpisano do rejestru: {m}.")
            return redirect("panel:member", pk=m.pk)
    return render(request, "panel/member_new.html", {"form": form, "account_form": account_form})


@panel_required(PERM_M)
def panel_member(request, pk):
    m = get_object_or_404(Member.objects.select_related("user"), pk=pk)
    edit = MemberEditForm(instance=m, prefix="e")
    link = LinkAccountForm(prefix="l")
    role = BoardRoleForm(prefix="r", initial={"board_role": m.board_role})
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "edit":
                edit = MemberEditForm(request.POST, instance=m, prefix="e")
                if edit.is_valid():
                    edit.save()
                    log_event("czlonek.dane", m, str(m), changes={"pola": edit.changed_data})
                    messages.success(request, "Zapisano dane członka.")
                    return redirect("panel:member", pk=m.pk)
            elif action == "link":
                link = LinkAccountForm(request.POST, prefix="l")
                if link.is_valid():
                    services.link_account(m, link.cleaned_data["user"], request.user)
                    messages.success(request, "Połączono z kontem.")
                    return redirect("panel:member", pk=m.pk)
            elif action == "role":
                role = BoardRoleForm(request.POST, prefix="r")
                if role.is_valid():
                    services.set_board_role(m, role.cleaned_data["board_role"] or "", request.user)
                    messages.success(request, "Zapisano funkcję w Zarządzie.")
                    return redirect("panel:member", pk=m.pk)
            elif action == "fees":
                services.charge_admission_fees(m, m.joined_on if m.joined_on.year == timezone.localdate().year else timezone.localdate())
                messages.success(request, "Naliczono wpisowe i składkę za bieżący rok (jeśli nie były naliczone).")
                return redirect("panel:member", pk=m.pk)
        except ValidationError as e:
            _err(request, e)
    log_sensitive_access(m.user, f"czlonek:{m.number}", "podgląd karty członka")
    return render(request, "panel/member_detail.html", {
        "m": m, "edit": edit, "link": link, "role": role, "fees": m.fees.all().order_by("-due_on"),
        "resolutions": m.resolutions.order_by("-adopted_on"), "pay_form": FeePaymentForm(prefix="p"),
    })


@panel_required(PERM_M)
@require_POST
def panel_fee_pay(request, pk):
    fee = get_object_or_404(Fee, pk=pk)
    form = FeePaymentForm(request.POST, prefix="p")
    if form.is_valid():
        try:
            services.mark_fee_paid(fee, form.cleaned_data["method"], request.user, form.cleaned_data["note"])
        except ValidationError as e:
            _err(request, e)
        else:
            messages.success(request, f"Zaksięgowano: {fee.label}.")
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect("panel:fees")


@panel_required(PERM_M)
def panel_applications(request):
    apps = MembershipApplication.objects.select_related("user", "resolution")
    pending = [a for a in apps if a.status == MembershipApplication.Status.PENDING]
    decided = [a for a in apps if a.status != MembershipApplication.Status.PENDING][:50]
    return render(request, "panel/applications.html", {"pending": pending, "decided": decided, "today": timezone.localdate()})


@panel_required(PERM_M)
def panel_fees(request):
    year = int(request.GET.get("rok") or timezone.localdate().year)
    status = request.GET.get("status", "")
    qs = Fee.objects.select_related("member__user").filter(Q(year=year) | Q(kind=Fee.Kind.ENTRY, due_on__year=year))
    if status in Fee.Status.values:
        qs = qs.filter(status=status)
    if request.method == "POST" and request.POST.get("action") == "ensure":
        n = services.ensure_annual_fees(year)
        messages.success(request, f"Naliczono składkę {year} dla {n} członków (pozostali mieli ją już naliczoną lub są zwolnieni).")
        return redirect(f"{request.path}?rok={year}")
    return render(request, "panel/fees.html", {
        "fees": qs.order_by("status", "due_on", "member__number"), "year": year, "status": status, "statuses": Fee.Status.choices,
        "summary": services.fees_summary(year), "overdue": services.overdue_fees().count(), "cfg": MembershipSettings.load(),
        "pay_form": FeePaymentForm(prefix="p"), "today": timezone.localdate(),
    })


# --- panel: uchwały ---------------------------------------------------------------------------


@panel_required(PERM_R)
def panel_resolutions(request):
    qs = Resolution.objects.select_related("meeting")
    organ = request.GET.get("organ", "")
    if organ in Resolution.Organ.values:
        qs = qs.filter(organ=organ)
    voting = Resolution.objects.filter(status=Resolution.Status.VOTING)
    return render(request, "panel/resolutions.html", {"resolutions": qs, "organ": organ, "organs": Resolution.Organ.choices, "voting": voting})


@panel_required(PERM_R)
def panel_resolution_new(request):
    initial = {"organ": request.GET.get("organ", Resolution.Organ.BOARD), "action": request.GET.get("skutek", "")}
    if request.GET.get("deklaracje"):
        initial["applications"] = [int(x) for x in request.GET["deklaracje"].split(",") if x.isdigit()]
    form = ResolutionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            res = services.create_resolution(
                organ=d["organ"], title=d["title"], content=d["content"], action=d["action"] or "", params=d["params"],
                members=d["members"], applications=d["applications"], by=request.user, meeting=d["meeting"],
            )
        except ValidationError as e:
            _err(request, e)
        else:
            messages.success(request, "Zapisano projekt uchwały.")
            return redirect("panel:resolution", pk=res.pk)
    return render(request, "panel/resolution_new.html", {"form": form})


@panel_required(PERM_R)
def panel_resolution_historical(request):
    form = HistoricalResolutionForm(request.POST or None, request.FILES or None,
                                    initial={"organ": Resolution.Organ.BOARD, "year": timezone.localdate().year})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            res = services.add_historical(organ=d["organ"], year=d["year"], number=d["number"], adopted_on=d["adopted_on"],
                                          title=d["title"], content=d["content"], attachment=d["attachment"], by=request.user)
        except ValidationError as e:
            _err(request, e)
        else:
            messages.success(request, f"Wprowadzono {res.display_number}.")
            return redirect("panel:resolution", pk=res.pk)
    return render(request, "panel/resolution_historical.html", {"form": form})


@panel_required()
def panel_resolution(request, pk):
    res = get_object_or_404(Resolution.objects.select_related("meeting", "repealed_by"), pk=pk)
    me = Member.objects.filter(user=request.user, status=Member.Status.ACTIVE).exclude(board_role="").first()
    if not (request.user.has_perm(PERM_R) or me):
        raise PermissionDenied
    vote_form = VoteForm(request.POST or None, prefix="v")
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "start" and request.user.has_perm(PERM_R):
                services.start_board_voting(res, request.user)
                messages.success(request, "Rozpoczęto głosowanie. Członkowie Zarządu otrzymali powiadomienie.")
            elif action == "vote" and vote_form.is_valid():
                res = services.cast_board_vote(res, request.user, vote_form.cleaned_data["choice"])
                messages.success(request, "Głos oddany." + (f" Uchwała: {res.get_status_display().lower()}." if res.status != Resolution.Status.VOTING else ""))
            elif action == "repeal" and request.user.has_perm(PERM_R):
                repealing = get_object_or_404(Resolution, pk=request.POST.get("repealing"))
                services.repeal(res, repealing, request.user)
                messages.success(request, "Oznaczono uchwałę jako uchyloną.")
            elif action == "delete" and request.user.has_perm(PERM_R) and res.status == Resolution.Status.DRAFT:
                log_event("uchwala.projekt_usuniety", summary=res.title)
                res.delete()
                messages.success(request, "Usunięto projekt uchwały.")
                return redirect("panel:resolutions")
        except ValidationError as e:
            _err(request, e)
        return redirect("panel:resolution", pk=pk)
    votes = res.votes.select_related("member", "proxy")
    return render(request, "panel/resolution_detail.html", {
        "r": res, "votes": votes, "me": me, "my_vote": votes.filter(member=me).first() if me else None, "vote_form": vote_form,
        "board": services.board_members(), "adopted_others": Resolution.objects.filter(status=Resolution.Status.ADOPTED).exclude(pk=res.pk)[:100],
    })


@panel_required()
def panel_resolution_print(request, pk):
    res = get_object_or_404(Resolution, pk=pk)
    if not request.user.has_perm(PERM_R) and not Member.objects.filter(user=request.user).exclude(board_role="").exists():
        raise PermissionDenied
    return render(request, "association/resolution_print.html", {"r": res, "votes": res.votes.select_related("member", "proxy"),
                                                                 "association": AssociationSettings.load(), "board": services.board_members()})


@panel_required(PERM_R)
def panel_resolution_attachment(request, pk):
    res = get_object_or_404(Resolution, pk=pk)
    if not res.attachment:
        raise Http404
    log_sensitive_access(None, f"uchwala:{res.pk}:skan", "podgląd skanu uchwały")
    response = FileResponse(res.attachment.open("rb"), content_type="application/pdf")
    response["Content-Disposition"] = "inline"
    response["Cache-Control"] = "no-store"
    return response


# --- panel: zebrania ---------------------------------------------------------------------------


@panel_required(PERM_R)
def panel_meetings(request):
    form = MeetingForm(request.POST or None, initial={"place": AssociationSettings.load().address or "Sątok 4, 56-420 Bierutów"})
    if request.method == "POST" and form.is_valid():
        meeting = form.save()
        log_event("zebranie.utworzone", meeting, str(meeting))
        messages.success(request, "Utworzono zebranie. Uzupełnij listę obecności.")
        return redirect("panel:meeting", pk=meeting.pk)
    return render(request, "panel/meetings.html", {"meetings": GeneralMeeting.objects.annotate(n=Count("attendance")), "form": form})


@panel_required(PERM_R)
def panel_meeting(request, pk):
    meeting = get_object_or_404(GeneralMeeting, pk=pk)
    members = Member.objects.filter(status=Member.Status.ACTIVE)
    if meeting.kind == GeneralMeeting.Kind.EXTRAORDINARY:
        members = members.filter(kind=Member.Kind.EXTRAORDINARY)
    attendance = {a.member_id: a for a in meeting.attendance.select_related("proxy")}
    if request.method == "POST" and not meeting.closed:
        action = request.POST.get("action")
        try:
            if action == "attendance":
                meeting.attendance.all().delete()
                for m in members:
                    state = request.POST.get(f"att_{m.pk}", "")
                    if state == "osobiscie":
                        Attendance.objects.create(meeting=meeting, member=m)
                    elif state.startswith("przez_") and state[6:].isdigit():
                        Attendance.objects.create(meeting=meeting, member=m, proxy_id=int(state[6:]))
                services.validate_meeting(meeting)
                log_event("zebranie.obecnosc", meeting, str(meeting), changes={"obecnych": meeting.attendance.count()})
                messages.success(request, "Zapisano listę obecności i pełnomocnictwa.")
            elif action == "vote":
                res = get_object_or_404(Resolution, pk=request.POST.get("resolution"), meeting=meeting)
                choices = {k[2:]: v for k, v in request.POST.items() if k.startswith("v_") and v}
                res = services.record_meeting_votes(res, meeting, choices, request.user)
                messages.success(request, f"{res.title}: {res.get_status_display().lower()} (za {res.votes_for}, przeciw {res.votes_against}, wstrzymało się {res.votes_abstain}).")
            elif action == "close":
                meeting.closed = True
                meeting.save(update_fields=["closed"])
                log_event("zebranie.zamkniete", meeting, str(meeting))
                messages.success(request, "Protokół zamknięty.")
        except ValidationError as e:
            _err(request, e)
        return redirect("panel:meeting", pk=pk)
    present = [a for a in meeting.attendance.select_related("member", "proxy")]
    total_weight = sum(a.member.vote_weight for a in present) if meeting.kind == GeneralMeeting.Kind.GENERAL else len(present)
    return render(request, "panel/meeting_detail.html", {
        "meeting": meeting, "members": members, "attendance": attendance, "present": present, "total_weight": total_weight,
        "resolutions": meeting.resolutions.all(), "choices": Vote.Choice.choices,
    })


# --- panel: kluby i kody -------------------------------------------------------------------------


@panel_required(PERM_C)
def panel_clubs(request):
    form = ClubForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        club = form.save()
        log_event("klub.dodany", club, club.name)
        messages.success(request, "Dodano klub.")
        return redirect("panel:club", pk=club.pk)
    return render(request, "panel/clubs.html", {"clubs": Club.objects.annotate(n=Count("codes")), "form": form})


@panel_required(PERM_C)
def panel_club(request, pk):
    club = get_object_or_404(Club, pk=pk)
    form = ClubForm(instance=club, prefix="c")
    code_form = AccessCodeForm(prefix="k", initial={"valid_from": timezone.localdate(), "valid_until": date(timezone.localdate().year, 12, 31)})
    if request.method == "POST":
        if request.POST.get("action") == "club":
            form = ClubForm(request.POST, instance=club, prefix="c")
            if form.is_valid():
                form.save()
                log_event("klub.zmiana", club, club.name, changes={"pola": form.changed_data})
                messages.success(request, "Zapisano klub.")
                return redirect("panel:club", pk=pk)
        elif request.POST.get("action") == "code":
            code_form = AccessCodeForm(request.POST, prefix="k")
            if code_form.is_valid():
                code = code_form.save(commit=False)
                code.club = club
                code.save()
                code_form.save_m2m()
                log_event("kod.utworzony", code, f"{club.name}: {code.code}")
                messages.success(request, f"Utworzono kod {code.code}. Przekaż go opiekunowi klubu.")
                return redirect("panel:club", pk=pk)
        elif request.POST.get("action") == "toggle":
            code = get_object_or_404(AccessCode, pk=request.POST.get("code"), club=club)
            code.is_active = not code.is_active
            code.save(update_fields=["is_active"])
            log_event("kod.aktywny" if code.is_active else "kod.wylaczony", code, code.code)
            return redirect("panel:club", pk=pk)
    codes = [(c, services.code_usage(c)) for c in club.codes.prefetch_related("lanes")]
    reservations = Reservation.objects.filter(access_code__club=club).select_related("lane", "user", "access_code").order_by("-starts_at")[:100]
    return render(request, "panel/club_detail.html", {"club": club, "form": form, "code_form": code_form, "codes": codes, "reservations": reservations})


# --- panel: raporty ---------------------------------------------------------------------------------


@panel_required(PERM_REP)
def panel_reports(request):
    year = int(request.GET.get("rok") or timezone.localdate().year)
    active = Reservation.objects.filter(starts_at__year=year).exclude(status=Reservation.Status.CANCELLED, late_cancellation=False)
    lanes = Lane.objects.all()
    occupancy = {}
    for r in active.select_related("lane"):
        month = timezone.localtime(r.starts_at).month
        occupancy.setdefault(r.lane_id, {}).setdefault(month, 0)
        occupancy[r.lane_id][month] += r.hours
    occ_rows = [(lane, [occupancy.get(lane.pk, {}).get(m, 0) for m in range(1, 13)], sum(occupancy.get(lane.pk, {}).values())) for lane in lanes]
    income = (
        Payment.objects.filter(status=Payment.Status.COMPLETED, completed_at__year=year)
        .annotate(m=TruncMonth("completed_at")).values("m", "method").annotate(s=Sum("amount"), n=Count("pk")).order_by("m")
    )
    inc = {}
    for row in income:
        inc.setdefault(timezone.localtime(row["m"]).month if timezone.is_aware(row["m"]) else row["m"].month, {})[row["method"]] = row["s"]
    methods = Payment.Method.choices
    inc_rows = [(m, [inc.get(m, {}).get(k, 0) for k, _ in methods], sum(inc.get(m, {}).values())) for m in range(1, 13)]
    club_rows = []
    for code in AccessCode.objects.select_related("club"):
        u = services.code_usage(code)
        if u["reservations"]:
            club_rows.append((code, u))
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="raport-{year}.csv"'
        response.write("﻿")
        w = csv.writer(response, delimiter=";")
        w.writerow([f"Obłożenie osi {year} [godziny]"] + [f"{m:02d}" for m in range(1, 13)] + ["Razem"])
        for lane, months, total in occ_rows:
            w.writerow([lane.name] + months + [total])
        w.writerow([])
        w.writerow([f"Wpływy {year} [zł]"] + [label for _, label in methods] + ["Razem"])
        for m, vals, total in inc_rows:
            w.writerow([f"{m:02d}"] + [f"{v / 100:.2f}".replace(".", ",") for v in vals] + [f"{total / 100:.2f}".replace(".", ",")])
        return response
    return render(request, "panel/reports.html", {
        "year": year, "occ_rows": occ_rows, "inc_rows": inc_rows, "methods": methods, "months": range(1, 13),
        "fees": services.fees_summary(year), "stats": services.membership_stats(), "club_rows": club_rows,
        "inc_total": sum(t for _, _, t in inc_rows), "occ_total": sum(t for _, _, t in occ_rows),
    })


IMPORT_PERMS = {
    "stowarzyszenie": "core.manage_settings",
    "regulamin": "regulations.publish_regulation",
    "czlonkowie": "association.manage_members",
    "uchwaly": "association.manage_resolutions",
}


@panel_required(PERM_M)
def panel_import(request):
    """Import pakietu danych stowarzyszenia (ZIP: dane.json, regulamin, skany PDF) — z podglądem bez zapisu."""
    from .importer import open_upload, run

    report = preview = None
    if request.method == "POST":
        uploaded = request.FILES.get("pakiet")
        preview = request.POST.get("tryb") != "importuj"
        if not uploaded:
            messages.error(request, "Wybierz plik ZIP albo XLSX.")
        else:
            try:
                report = run(open_upload(uploaded), by=request.user, preview=preview,
                             can=lambda section: request.user.has_perm(IMPORT_PERMS[section]))
            except ValidationError as e:
                messages.error(request, " ".join(e.messages))
            else:
                if not preview:
                    log_event("stowarzyszenie.import", summary=f"{uploaded.name}: {sum(1 for s, _ in report if s == 'ok')} pozycji wprowadzonych")
                    messages.success(request, "Import zakończony.")
    return render(request, "panel/association_import.html", {"report": report, "preview": preview})


@panel_required(PERM_M)
def panel_import_template(request):
    from .importer import template_xlsx

    response = HttpResponse(template_xlsx(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="wzor-importu-stowarzyszenie.xlsx"'
    return response


@panel_required(PERM_REP)
def panel_occupancy(request):
    """Obłożenie osi w okresie: mapa dzień tygodnia × godzina, dni tygodnia, osie, najbardziej oblegane terminy."""
    from . import stats

    key = request.GET.get("okres", "90")
    if key not in stats.OCCUPANCY_PERIODS:
        key = "90"
    label, days = stats.OCCUPANCY_PERIODS[key]
    today = timezone.localdate()
    first, nxt = (today + timedelta(days=days), today) if days < 0 else (today, today + timedelta(days=days))
    data = stats.occupancy(first, nxt)
    data.update({"period": key, "period_label": label, "periods": [(k, v[0]) for k, v in stats.OCCUPANCY_PERIODS.items()]})
    return render(request, "panel/occupancy.html", data)


@panel_required(PERM_REP)
def panel_month_stats(request):
    """Statystyki miesiąca z porównaniem do poprzedniego."""
    from . import stats

    today = timezone.localdate()
    default = stats.prev_month(today.year, today.month) if today.day <= 3 else (today.year, today.month)
    try:
        year, month = (int(x) for x in request.GET.get("m", "").split("-"))
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            raise ValueError
    except ValueError:
        year, month = default
    data = stats.with_comparison(year, month)
    py, pm = stats.prev_month(year, month)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    data.update({"prev_m": f"{py}-{pm:02d}", "next_m": f"{ny}-{nm:02d}", "this_m": f"{year}-{month:02d}",
                 "is_future": (ny, nm) > (today.year, today.month)})
    return render(request, "panel/month_stats.html", data)
