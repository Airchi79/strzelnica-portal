from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse
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

from . import branding
from .forms import AssociationSettingsForm, LogoForm, UserPermissionsForm
from .models import AssociationSettings, Branding
from .permissions import PANEL_PERMISSIONS, ROLE_PRESETS, panel_required

User = get_user_model()


def home(request):
    lanes = Lane.objects.filter(is_active=True, facility__is_active=True).select_related("facility")
    return render(request, "core/home.html", {"lanes": lanes, "association": AssociationSettings.load()})


@login_required
def dashboard(request):
    credentials = list(request.user.credentials.all())
    from apps.bookings.services import booking_readiness
    from apps.clients.services import pass_uses

    passes = list(request.user.client_passes.filter(is_active=True, valid_until__gte=timezone.localdate()))
    for p in passes:
        p.remaining = None if p.entries_limit is None else max(0, p.entries_limit - pass_uses(p))
    return render(
        request,
        "core/dashboard.html",
        {
            "credentials": credentials,
            "passes": passes,
            "readiness": booking_readiness(request.user),
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
    from .overview import FEED_GROUPS, attention, feed

    group = request.GET.get("grupa", "")
    return render(request, "panel/home.html", {
        "tiles": attention(request.user),
        "feed": feed(group if group in FEED_GROUPS else ""),
        "group": group,
        "groups": [("", "Wszystko"), ("rezerwacje", "Rezerwacje"), ("platnosci", "Płatności"), ("ksiega", "Księga"), ("klienci", "Klienci")],
    })


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
    current = Branding.objects.filter(pk=1).only("updated_at", "logo_type", "icon_background", "updated_by").first()
    logo_form = LogoForm(initial={"icon_background": current.icon_background} if current else None)
    return render(request, "panel/settings.html", {"form": form, "logo_form": logo_form, "branding": current})


@panel_required("core.manage_settings")
def panel_logo(request):
    if request.method != "POST":
        return redirect("panel:settings")
    if request.POST.get("action") == "reset":
        deleted, _ = Branding.objects.filter(pk=1).delete()
        if deleted:
            log_event("ustawienia.logo", None, "Przywrócono logo domyślne")
        messages.success(request, "Przywrócono logo domyślne.")
        return redirect("panel:settings")
    form = LogoForm(request.POST, request.FILES)
    if form.is_valid():
        d = form.cleaned_data
        Branding(
            logo=d["logo"]["data"], logo_type=d["logo"]["type"], icon_background=d["icon_background"], updated_by=request.user,
            **d["icons"],
        ).save()
        log_event("ustawienia.logo", None, f"Nowe logo ({d['logo']['type']}, {len(d['logo']['data']) // 1024} kB)")
        messages.success(request, "Zapisano nowe logo. Ikona aplikacji na telefonach odświeży się przy następnym otwarciu (na iPhonie — po ponownym dodaniu do ekranu początkowego).")
        return redirect("panel:settings")
    obj = AssociationSettings.load()
    return render(request, "panel/settings.html", {"form": AssociationSettingsForm(instance=obj), "logo_form": form,
                                                   "branding": Branding.objects.filter(pk=1).only("updated_at", "logo_type", "icon_background").first()})


def _brand_response(data, content_type, request):
    response = HttpResponse(bytes(data), content_type=content_type)
    # Adres z ?v= zmienia się przy każdej zmianie logo — można go trzymać długo w pamięci podręcznej.
    response["Cache-Control"] = "public, max-age=31536000, immutable" if request.GET.get("v") else "public, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    if content_type == "image/svg+xml":
        response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
    return response


def brand_logo(request):
    row = Branding.objects.filter(pk=1).values_list("logo", "logo_type").first()
    if not row:
        return redirect(branding.brand_urls()["logo"])
    return _brand_response(row[0], row[1], request)


def brand_icon(request, size):
    field = {"192": "icon_192", "512": "icon_512", "maskable": "icon_maskable"}.get(size)
    if field is None:
        raise Http404
    data = Branding.objects.filter(pk=1).values_list(field, flat=True).first()
    if not data:
        return redirect(branding.brand_urls()[field])
    return _brand_response(data, "image/png", request)


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
        preset = ROLE_PRESETS.get(request.POST.get("preset", ""))
        if preset is not None:
            # Gotowy zestaw zastępuje dotychczasowe uprawnienia (z wyjątkiem właściciela — tego nie da się tu zmienić).
            form = UserPermissionsForm({"permissions": preset["permissions"]})
        if form.is_valid():
            chosen = form.cleaned_data["permissions"]
            codes = {code for code, _ in PANEL_PERMISSIONS}
            subject.user_permissions.remove(*[p for p in _perm_objects(codes)])
            subject.user_permissions.add(*_perm_objects(chosen))
            subject.is_staff = bool(chosen)
            subject.save(update_fields=["is_staff"])
            log_event("uprawnienia.zmiana", subject, subject.email,
                      changes={"przed": current, "po": sorted(chosen), **({"zestaw": preset["label"]} if preset else {})})
            messages.success(request, "Zapisano uprawnienia." + (" Osoba skonfiguruje logowanie dwuskładnikowe przy wejściu do panelu." if chosen else ""))
            return redirect("panel:user", pk=subject.pk)
    log_sensitive_access(subject, f"konto:{subject.pk}", "podgląd konta w panelu")
    from apps.association.models import Member
    from apps.clients import services as client_services
    from apps.clients.forms import PassPaidForm, ProfileForm

    client_services.annotate_clients([subject])
    profile = subject.profile
    passes = list(subject.client_passes.prefetch_related("lanes"))
    for p in passes:
        p.uses = client_services.pass_uses(p)
        p.remaining = None if p.entries_limit is None else max(0, p.entries_limit - p.uses)
        p.expired = p.valid_until < timezone.localdate()
    profile_form = ProfileForm(initial={
        "notes": profile.notes if profile else "", "tags": ", ".join(t.name for t in profile.tags.all()) if profile else "",
    })
    return render(
        request,
        "panel/user_detail.html",
        {
            "subject": subject, "credentials": subject.credentials.all(), "form": form, "can_manage": can_manage,
            "presets": [(k, v, sorted(v["permissions"]) == sorted(current)) for k, v in ROLE_PRESETS.items()],
            "profile": profile, "profile_form": profile_form, "passes": passes, "paid_form": PassPaidForm(),
            "member": Member.objects.filter(user=subject).first(),
            "reservations": subject.reservations.select_related("lane", "client_pass", "access_code__club").order_by("-starts_at")[:30],
            "log_entries": subject.log_entries.select_related("reservation__lane").order_by("-entered_at")[:30],
        },
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


def healthz(request):
    """Kontrola stanu dla Dockera: aplikacja odpowiada i ma połączenie z bazą."""
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")
