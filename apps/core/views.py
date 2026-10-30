from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.cache import cache_control

from apps.bookings.models import Reservation
from apps.payments.services import balance_of
from apps.audit.models import AuditLog, SensitiveAccessLog
from apps.audit.services import diff, log_event, log_sensitive_access
from apps.credentials.models import Credential, is_range_officer
from apps.facilities.models import Lane

from .forms import AssociationSettingsForm, UserPermissionsForm
from .models import AssociationSettings
from .permissions import PANEL_PERMISSIONS, panel_required

User = get_user_model()


def home(request):
    lanes = Lane.objects.filter(is_active=True, facility__is_active=True).select_related("facility")
    return render(request, "core/home.html", {"lanes": lanes, "association": AssociationSettings.load()})


@login_required
def dashboard(request):
    credentials = list(request.user.credentials.all())
    return render(
        request,
        "core/dashboard.html",
        {
            "credentials": credentials,
            "is_ps": is_range_officer(request.user),
            "missing_profile": not (request.user.phone and request.user.city),
            "balance": balance_of(request.user),
            "due_count": request.user.reservations.filter(payment_status="do_zaplaty").count(),
            "upcoming": request.user.reservations.filter(status="potwierdzona", ends_at__gt=timezone.now()).select_related("lane").order_by("starts_at").first(),
        },
    )


@cache_control(max_age=3600)
def manifest(request):
    return HttpResponse(render_to_string("pwa/manifest.webmanifest", request=request), content_type="application/manifest+json")


@cache_control(no_cache=True)
def service_worker(request):
    response = HttpResponse(render_to_string("pwa/sw.js", request=request), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    return response


def offline(request):
    return render(request, "pwa/offline.html")


# --- panel zarządu ---------------------------------------------------------


@panel_required()
def panel_home(request):
    stats = {
        "upcoming_reservations": Reservation.objects.filter(status="potwierdzona", starts_at__gte=timezone.now()).count(),
        "due_reservations": Reservation.objects.filter(payment_status="do_zaplaty").exclude(status="anulowana", late_cancellation=False).count(),
        "pending_credentials": Credential.objects.filter(status=Credential.Status.PENDING).count(),
        "users": User.objects.filter(is_active=True).count(),
        "ps": Credential.objects.filter(kind=Credential.Kind.RANGE_OFFICER, status=Credential.Status.VERIFIED).values("user").distinct().count(),
    }
    return render(request, "panel/home.html", {"stats": stats})


@panel_required("core.manage_settings")
def panel_settings(request):
    obj = AssociationSettings.load()
    before = AssociationSettings.load()
    form = AssociationSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        log_event("ustawienia.zmiana", obj, changes=diff(before, obj, form.Meta.fields))
        messages.success(request, "Zapisano dane stowarzyszenia.")
        return redirect("panel:settings")
    return render(request, "panel/settings.html", {"form": form})


@panel_required("accounts.view_users")
def panel_users(request):
    q = request.GET.get("q", "").strip()
    qs = User.objects.all()
    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "panel/users_list.html", {"page": page, "q": q})


def _perm_objects(codes):
    out = []
    for code in codes:
        app_label, codename = code.split(".")
        out.append(Permission.objects.get(content_type__app_label=app_label, codename=codename))
    return out


@panel_required("accounts.view_users")
def panel_user(request, pk):
    subject = get_object_or_404(User, pk=pk)
    current = [code for code, _ in PANEL_PERMISSIONS if code in {f"{p.content_type.app_label}.{p.codename}" for p in subject.user_permissions.select_related("content_type")}]
    can_manage = (
        request.user.has_perm("accounts.manage_permissions") and subject.pk != request.user.pk and not subject.is_superuser
    )
    form = UserPermissionsForm(request.POST if request.method == "POST" else None, initial={"permissions": current})
    if request.method == "POST":
        if not can_manage:
            raise PermissionDenied
        if form.is_valid():
            chosen = form.cleaned_data["permissions"]
            codes = {code for code, _ in PANEL_PERMISSIONS}
            subject.user_permissions.remove(*[p for p in _perm_objects(codes)])
            subject.user_permissions.add(*_perm_objects(chosen))
            subject.is_staff = bool(chosen)
            subject.save(update_fields=["is_staff"])
            log_event("uprawnienia.zmiana", subject, subject.email, changes={"przed": current, "po": sorted(chosen)})
            messages.success(request, "Zapisano uprawnienia." + (" Osoba skonfiguruje logowanie dwuskładnikowe przy wejściu do panelu." if chosen else ""))
            return redirect("panel:user", pk=subject.pk)
    log_sensitive_access(subject, f"konto:{subject.pk}", "podgląd konta w panelu")
    return render(
        request,
        "panel/user_detail.html",
        {"subject": subject, "credentials": subject.credentials.all(), "form": form, "can_manage": can_manage},
    )


@panel_required("audit.view_auditlog")
def panel_audit(request):
    tab = request.GET.get("tab", "zdarzenia")
    q = request.GET.get("q", "").strip()
    if tab == "wglad":
        qs = SensitiveAccessLog.objects.select_related("actor", "subject")
        if q:
            qs = qs.filter(Q(actor__email__icontains=q) | Q(subject__email__icontains=q) | Q(resource__icontains=q))
    else:
        qs = AuditLog.objects.select_related("actor")
        if q:
            qs = qs.filter(Q(action__icontains=q) | Q(summary__icontains=q) | Q(actor__email__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "panel/audit.html", {"page": page, "tab": tab, "q": q})
