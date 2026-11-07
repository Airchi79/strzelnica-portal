from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
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
from .forms import AnnouncementForm, AssociationSettingsForm, LogoForm, UserPermissionsForm
from .models import Announcement, AssociationSettings, Branding
from .permissions import PANEL_PERMISSIONS, ROLE_PRESETS, panel_required

User = get_user_model()


def home(request):
    lanes = Lane.objects.filter(is_active=True, facility__is_active=True).select_related("facility")
    return render(request, "core/home.html", {"lanes": lanes, "association": AssociationSettings.load()})


@login_required
def push_subscribe(request):
    """Zapis / usunięcie subskrypcji push tego urządzenia (JSON z przeglądarki: endpoint, keys.p256dh, keys.auth)."""
    import json

    from django.http import JsonResponse

    from . import webpush
    from .models import PushSubscription

    if request.method != "POST":
        return JsonResponse({"key": webpush.public_key_b64()})
    try:
        data = json.loads(request.body.decode() or "{}")
        endpoint = str(data.get("endpoint", ""))[:1000]
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Nieprawidłowe dane."}, status=400)
    if data.get("action") == "remove":
        PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
        return JsonResponse({"ok": True})
    keys = data.get("keys") or {}
    p256dh, auth = str(keys.get("p256dh", ""))[:200], str(keys.get("auth", ""))[:60]
    if not webpush.endpoint_allowed(endpoint):
        return JsonResponse({"ok": False, "error": "Ta przeglądarka używa nieobsługiwanej usługi powiadomień."}, status=400)
    try:
        webpush.encrypt(b"test", p256dh, auth)  # sprawdzenie kluczy
    except Exception:  # noqa: BLE001
        return JsonResponse({"ok": False, "error": "Nieprawidłowe klucze powiadomień."}, status=400)
    device = request.headers.get("User-Agent", "")[:120]
    PushSubscription.objects.update_or_create(endpoint=endpoint, defaults={"user": request.user, "p256dh": p256dh, "auth": auth,
                                                                          "device": device})
    extra = PushSubscription.objects.filter(user=request.user).order_by("-created_at").values_list("pk", flat=True)[webpush.MAX_PER_USER:]
    PushSubscription.objects.filter(pk__in=list(extra)).delete()
    log_event("push.wlaczenie", None, f"Powiadomienia push na urządzeniu: {device[:60]}")
    return JsonResponse({"ok": True})


@login_required
def my_notifications(request):
    """Powiadomienia klienta (dzwonek w nagłówku): lista, oznaczanie jako przeczytane."""
    from .models import Notification

    qs = request.user.notifications.filter(channel=Notification.Channel.PORTAL)
    if request.method == "POST":
        qs.filter(read_at__isnull=True).update(read_at=timezone.now())
        return redirect("core:notifications")
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "core/notifications.html", {"page": page})


@login_required
def notification_open(request, pk):
    from django.utils.http import url_has_allowed_host_and_scheme

    from .models import Notification

    n = get_object_or_404(Notification, pk=pk, recipient=request.user, channel=Notification.Channel.PORTAL)
    if n.read_at is None:
        n.read_at = timezone.now()
        n.save(update_fields=["read_at"])
    if n.url.startswith("/") and url_has_allowed_host_and_scheme(n.url, allowed_hosts=None):
        return redirect(n.url)
    return redirect("core:notifications")


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


@panel_required("core.publish_announcement")
def panel_announcements(request):
    from . import announcements

    items = list(Announcement.objects.select_related("created_by")[:100])
    live = set(announcements.active().values_list("pk", flat=True))
    return render(request, "panel/announcements.html", {"items": items, "live": live})


@panel_required("core.publish_announcement")
def panel_announcement(request, pk=None):
    from . import announcements

    obj = get_object_or_404(Announcement, pk=pk) if pk else Announcement(visible_from=timezone.now().replace(second=0, microsecond=0))
    if request.method == "POST" and request.POST.get("action") == "end" and obj.pk:
        obj.visible_until = timezone.now()
        obj.save(update_fields=["visible_until"])
        log_event("ogloszenie.zakonczenie", obj, obj.title)
        messages.success(request, "Ogłoszenie zdjęte ze strony.")
        return redirect("panel:announcements")
    form = AnnouncementForm(request.POST or None, instance=obj)
    preview_count = None
    if request.method == "POST" and form.is_valid():
        a = form.save(commit=False)
        is_new = a.pk is None
        if is_new:
            a.created_by = request.user
        if a.email_audience and not a.emailed_at and not a.email_queued:
            if request.POST.get("action") != "confirm_email":
                preview_count = announcements.recipients(a).count()
                return render(request, "panel/announcement_edit.html", {"form": form, "obj": obj, "preview_count": preview_count})
            a.email_queued = True
        a.save()
        log_event("ogloszenie.zapis", a, a.title, changes={"email": a.email_audience, "nowe": is_new})
        msg = "Zapisano ogłoszenie."
        if a.email_queued:
            msg += " E-maile wyślą się w tle w ciągu kilku–kilkunastu minut."
        messages.success(request, msg)
        return redirect("panel:announcements")
    return render(request, "panel/announcement_edit.html", {"form": form, "obj": obj, "preview_count": preview_count})


@panel_required("core.manage_settings")
def panel_backups(request):
    from .backups import read_status

    return render(request, "panel/backups.html", {"st": read_status()})


@panel_required("accounts.view_users")
def panel_gdpr(request, pk=None):
    from apps.audit.models import AuditLog

    from apps.accounts import gdpr

    if pk is None:
        pending = User.objects.filter(is_active=False, anonymized_at__isnull=True).order_by("-deactivated_at")
        for u in pending:
            u.blockers = gdpr.anonymize_blockers(u)
        log = AuditLog.objects.filter(action__in=["rodo.eksport", "rodo.anonimizacja", "konto.dezaktywacja"]).select_related("actor")[:50]
        return render(request, "panel/gdpr.html", {"pending": pending, "log": log})
    subject = get_object_or_404(User, pk=pk)
    can_act = request.user.has_perm("accounts.manage_permissions") and subject.pk != request.user.pk
    if request.method == "POST":
        if not can_act:
            raise PermissionDenied
        action = request.POST.get("action")
        if action == "deactivate" and subject.is_active and not subject.is_superuser:
            subject.is_active = False
            subject.deactivated_at = timezone.now()
            subject.save(update_fields=["is_active", "deactivated_at"])
            log_event("konto.dezaktywacja", subject, f"Dezaktywacja przez zarząd: {request.POST.get('reason', '')[:120]}")
            messages.success(request, "Konto dezaktywowane — osoba nie może się zalogować.")
        elif action == "anonymize":
            if request.POST.get("confirm") != f"USUŃ {subject.pk}":
                messages.error(request, f"Wpisz dokładnie: USUŃ {subject.pk}")
            else:
                try:
                    gdpr.anonymize(subject, request.user)
                except ValidationError as e:
                    messages.error(request, " ".join(e.messages))
                else:
                    messages.success(request, "Dane osobowe usunięte. Zostały tylko dane, które przepisy każą przechowywać.")
        return redirect("panel:gdpr_user", pk=subject.pk)
    log_sensitive_access(subject, f"rodo:{subject.pk}", "obsługa żądania RODO")
    return render(request, "panel/gdpr_user.html", {"subject": subject, "blockers": gdpr.anonymize_blockers(subject), "can_act": can_act})


@panel_required()
def panel_help(request):
    return render(request, "panel/help.html")


@panel_required("core.manage_settings")
def panel_widget(request):
    from django.conf import settings as dj_settings

    from apps.bookings.forms import active_lanes

    from .widget import KINDS

    return render(request, "panel/widget.html", {"kinds": KINDS, "site": dj_settings.SITE_URL, "lanes": active_lanes()})
