"""Kiosk: strona dla tabletu / Raspberry Pi przy wejściu i jego API (uwierzytelnienie ciasteczkiem urządzenia).

API nie używa tokenu CSRF: wymaga ciasteczka urządzenia (SameSite=Strict, HttpOnly), nagłówka X-Kiosk
i treści JSON — żądanie z obcej strony nie dołączy ciasteczka ani nie przejdzie bez zapytania CORS."""

import json
import uuid
from datetime import datetime, timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.audit.context import client_ip
from apps.audit.services import log_event
from apps.core.permissions import panel_required
from apps.logbook import services as logbook
from apps.logbook.forms import EntryForm
from apps.logbook.models import STATEMENT_TEXT, Entry

from apps.bookings.models import BookingSettings

from . import onsite, services
from .models import KioskDevice

DEVICE_RATE_PER_HOUR = 400


def _set_cookie(response, raw):
    response.set_cookie(
        services.COOKIE, raw, max_age=5 * 365 * 24 * 3600, httponly=True, samesite="Strict",
        secure=getattr(settings, "SESSION_COOKIE_SECURE", False),
    )


def kiosk_home(request):
    device = services.device_from_request(request)
    if device is None:
        return render(request, "kiosk/pair.html", {})
    response = render(request, "kiosk/kiosk.html", {
        "device": device, "statement": STATEMENT_TEXT, "purposes": Entry.Purpose.choices,
        "onsite_statement": onsite.STATEMENT_TEXT, "onsite_minutes": onsite.VERIFY_MINUTES,
    })
    response["X-Kiosk-App"] = "1"
    response["Cache-Control"] = "no-store"
    return response


def kiosk_manifest(request):
    """Osobna aplikacja kiosku (pełny ekran, zakres /kiosk/) — do przypięcia na telefonie lub tablecie."""
    from django.http import HttpResponse
    from django.template.loader import render_to_string

    return HttpResponse(render_to_string("kiosk/manifest.webmanifest", request=request), content_type="application/manifest+json")


@require_POST
def kiosk_pair(request):
    try:
        services.check_pair_rate(client_ip(request))
        device, raw = services.pair(request.POST.get("code", ""), ip=client_ip(request))
    except ValidationError as e:
        return render(request, "kiosk/pair.html", {"error": e.messages[0]}, status=400)
    response = redirect("kiosk:home")
    _set_cookie(response, raw)
    return response


def api(view):
    @csrf_exempt
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.headers.get("X-Kiosk") != "1":
            return JsonResponse({"error": "Brak nagłówka kiosku."}, status=400)
        device = services.device_from_request(request)
        if device is None:
            return JsonResponse({"error": "Urządzenie nie jest sparowane.", "unpaired": True}, status=401)
        key = f"kiosk-rate:{device.pk}"
        count = cache.get(key, 0)
        if count >= DEVICE_RATE_PER_HOUR:
            return JsonResponse({"error": "Zbyt wiele żądań z kiosku. Spróbuj później."}, status=429)
        cache.set(key, count + 1, 3600)
        request.kiosk_device = device
        return view(request, *args, **kwargs)

    return wrapped


def _reservation_payload(r):
    return {
        "id": r.pk, "token": logbook.reservation_token(r),
        "label": f"{r.lane} · {timezone.localtime(r.starts_at):%H:%M}–{timezone.localtime(r.ends_at):%H:%M}",
        "starts": r.starts_at.isoformat(), "ends": r.ends_at.isoformat(),
    }


@require_GET
@api
def api_state(request):
    """Stan dla kiosku: rezerwacje dostępne do wpisu dziś (z tokenami kodów QR — do pracy offline)."""
    now = timezone.now()
    day_end = timezone.make_aware(datetime.combine(timezone.localdate(), datetime.max.time()))
    from apps.bookings.models import Reservation

    window = timedelta(minutes=logbook.LogbookSettings.load().window_before_minutes)
    todays = Reservation.objects.filter(status=Reservation.Status.CONFIRMED, ends_at__gte=now, starts_at__lte=day_end).select_related("lane")
    return JsonResponse({
        "device": request.kiosk_device.name, "now": now.isoformat(), "window_minutes": int(window.total_seconds() // 60),
        "reservations": [_reservation_payload(r) for r in todays.order_by("starts_at", "lane__order")],
    })


def _parse_time(value):
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if timezone.is_aware(dt) else timezone.make_aware(dt)


@require_POST
@api
def api_entry(request):
    try:
        payload = json.loads(request.body or b"{}")
        ref = uuid.UUID(str(payload.get("ref")))
    except (ValueError, TypeError):
        return JsonResponse({"error": "Nieprawidłowe dane."}, status=400)
    existing = Entry.objects.filter(client_ref=ref).first()
    if existing:  # ponowne wysłanie z kolejki — wpis już jest
        return JsonResponse({"ok": True, "number": existing.number, "duplicate": True})
    now = timezone.now()
    entered_at = _parse_time(payload.get("entered_at"))
    if entered_at is None or abs((now - entered_at).total_seconds()) < 120:
        entered_at = None  # wpis na bieżąco — godzina serwera
    at = entered_at or now
    reservations = logbook.open_reservations(now=at)
    data = {k: payload.get(k, "") for k in ("purpose", "purpose_note", "first_name", "last_name", "has_permit", "permit_number",
                                            "permit_issuer", "address", "signature")}
    data["reservation"] = payload.get("reservation") or ""
    data["statement"] = "on" if payload.get("statement") else ""
    form = EntryForm(data, reservations=reservations, shooting_without_reservation=True)
    if not form.is_valid():
        errors = [m for msgs in form.errors.values() for m in msgs]
        return JsonResponse({"error": " ".join(errors), "fields": list(form.errors.keys())}, status=400)
    d = form.cleaned_data
    try:
        user = None
        if payload.get("person"):
            user = logbook.user_from_personal_token(payload["person"])
        entry = logbook.create_entry(
            data=d, signature_png=logbook.decode_signature(d.get("signature")), source=Entry.Source.DEVICE,
            reservation=d.get("reservation"), user=user, ip=client_ip(request), user_agent=request.META.get("HTTP_USER_AGENT", ""),
            now=now, entered_at=entered_at, device=request.kiosk_device, client_ref=ref,
        )
    except ValidationError as e:
        return JsonResponse({"error": " ".join(e.messages)}, status=400)
    except IntegrityError:
        existing = Entry.objects.filter(client_ref=ref).first()
        if existing:
            return JsonResponse({"ok": True, "number": existing.number, "duplicate": True})
        raise
    if entry.purpose == Entry.Purpose.SHOOTING and entry.reservation_id is None:
        from apps.core.notifications import notify_board

        url = reverse("panel:logbook_entry", args=[entry.pk])
        body = f"{entry.full_name}, wejście {timezone.localtime(entry.entered_at):%d.%m %H:%M}, prowadzący: {entry.purpose_note}."
        notify_board(
            f"Strzelanie bez rezerwacji — wpis z kiosku {entry.number}", "emails/board_generic.txt", "logbook.view_logbook",
            url=url, body=body + " Sprawdź i w razie potrzeby rozlicz pobyt.", event="Strzelanie bez rezerwacji (kiosk)",
            url_path=url, text=body,
        )
    return JsonResponse({"ok": True, "number": entry.number, "name": entry.full_name})


@require_GET
@api
def api_person(request, token):
    """Dane do formularza z osobistego kodu QR (osoba pokazuje swój kod przy kiosku)."""
    person = logbook.user_from_personal_token(token)
    if person is None:
        return JsonResponse({"error": "Nieznany kod osobisty."}, status=404)
    return JsonResponse({"prefill": logbook.prefill_for(person)})


@require_GET
@api
def api_exit_search(request):
    q = request.GET.get("nazwisko", "").strip()
    if len(q) < 2:
        return JsonResponse({"entries": []})
    since = timezone.now() - timedelta(hours=24)
    qs = Entry.objects.filter(exited_at__isnull=True, entered_at__gte=since, last_name__istartswith=q).order_by("-entered_at")[:10]
    return JsonResponse({"entries": [
        {"id": e.pk, "name": f"{e.first_name} {e.last_name[:1]}.", "at": timezone.localtime(e.entered_at).strftime("%H:%M")} for e in qs
    ]})


@require_POST
@api
def api_exit(request, pk):
    since = timezone.now() - timedelta(hours=24)
    entry = Entry.objects.filter(pk=pk, exited_at__isnull=True, entered_at__gte=since).first()
    if entry is None:
        return JsonResponse({"error": "Nie znaleziono otwartego pobytu."}, status=404)
    logbook.register_exit(entry)
    return JsonResponse({"ok": True, "at": timezone.localtime(entry.exited_at).strftime("%H:%M")})


@require_POST
@api
def api_report_failed(request):
    """Kiosk zgłasza wpis odrzucony przy synchronizacji (bez danych osobowych) — informacja dla obsługi."""
    try:
        reason = str(json.loads(request.body or b"{}").get("reason", ""))[:200]
    except ValueError:
        reason = ""
    from apps.core.notifications import notify_board

    log_event("kiosk.wpis_odrzucony", request.kiosk_device, f"{request.kiosk_device.name}: {reason}")
    url = reverse("panel:kiosks")
    body = f"Powód: {reason}. Poproś osobę o ponowny wpis albo dodaj wpis przez obsługę."
    notify_board(
        f"Kiosk {request.kiosk_device.name}: wpis z kolejki offline odrzucony", "emails/board_generic.txt", "logbook.manage_logbook",
        url=url, body=body, event="Odrzucony wpis z kiosku (kolejka offline)", url_path=url, text=body,
    )
    return JsonResponse({"ok": True})


# --- rezerwacja na miejscu -------------------------------------------------------------------------------


def _json(request):
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _error(e, status=400):
    return JsonResponse({"error": " ".join(e.messages)}, status=status)


@require_GET
@api
def api_onsite_slots(request):
    return JsonResponse({"lanes": onsite.free_slots(), "max_persons": BookingSettings.load().max_persons})


@require_GET
@api
def api_onsite_price(request):
    g = request.GET
    try:
        return JsonResponse(onsite.price(g.get("os"), g.get("od"), g.get("do"), g.get("osoby")))
    except ValidationError as e:
        return _error(e)


@require_POST
@api
def api_onsite_start(request):
    data = _json(request)
    if data is None:
        return JsonResponse({"error": "Nieprawidłowe dane."}, status=400)
    try:
        flow, secret = onsite.start(request.kiosk_device, data.get("email"), data.get("first_name"), data.get("last_name"),
                                    data.get("phone"))
    except ValidationError as e:
        return _error(e)
    return JsonResponse({"ref": str(flow.ref), "secret": secret, "state": onsite.state(flow)})


@require_POST
@transaction.non_atomic_requests  # krok „legitymacja”: odczyt zdjęcia (do kilkunastu s) poza transakcją — pozostałe kroki niżej w transakcji
@api
def api_onsite_step(request, step):
    """Kolejne kroki przebiegu: kod, regulamin, oswiadczenie, legitymacja, rezerwacja, stan."""
    data = _json(request)
    if data is None:
        return JsonResponse({"error": "Nieprawidłowe dane."}, status=400)
    flow = onsite.get_flow(request.kiosk_device, data.get("ref"), data.get("secret"))
    if flow is None:
        return JsonResponse({"error": "Rezerwacja wygasła albo jest nieznana. Zacznij od nowa.", "restart": True}, status=404)
    if step not in ("kod", "regulamin", "oswiadczenie", "legitymacja", "rezerwacja", "stan"):
        return JsonResponse({"error": "Nieznany krok."}, status=404)
    try:
        if step == "legitymacja":
            # zapis legitymacji w krótkiej transakcji, potem automatyczne porównanie bez otwartej transakcji
            onsite.submit_ps(flow, data.get("number"), data.get("issuer"), data.get("issued_on"), data.get("valid_until"),
                             data.get("photo"))
        else:
            # Jak dotąd (transakcja całego żądania): błąd walidacji nie cofa zapisów sprzed niego, np. licznika prób kodu.
            error = None
            with transaction.atomic():
                try:
                    if step == "kod":
                        flow = onsite.verify_code(flow, data.get("code"))
                    elif step == "regulamin":
                        onsite.accept_regulations(flow, ip=client_ip(request))
                    elif step == "oswiadczenie":
                        if not data.get("accept"):
                            raise ValidationError("Zaznacz oświadczenie.")
                        onsite.accept_statement(flow)
                    elif step == "rezerwacja":
                        onsite.book(flow, data.get("lane"), data.get("start_hour"), data.get("end_hour"), data.get("persons"))
                except ValidationError as e:
                    error = e
            if error is not None:
                raise error
    except ValidationError as e:
        return _error(e)
    flow.refresh_from_db()
    return JsonResponse({"ok": True, "state": onsite.state(flow)})


# --- panel ------------------------------------------------------------------------------------------


@panel_required("logbook.manage_logbook")
def panel_list(request):
    code = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add" and request.POST.get("name", "").strip():
            device, code = services.create_device(request.POST["name"], request.user)
            code = (device, code)
        elif action == "camera":
            device = get_object_or_404(KioskDevice, pk=request.POST.get("device"))
            try:
                services.set_camera(device, request.POST.get("camera", ""), request.user)
                messages.success(request, f"{device.name}: kamera {device.get_camera_display()}. Zmiana działa po odświeżeniu strony kiosku (najpóźniej po ponownym otwarciu).")
            except ValidationError as e:
                messages.error(request, e.messages[0])
            return redirect("panel:kiosks")
        elif action in ("code", "revoke"):
            device = get_object_or_404(KioskDevice, pk=request.POST.get("device"))
            if action == "code":
                code = (device, services.new_pairing_code(device, request.user))
            else:
                services.revoke(device, request.user)
                messages.success(request, f"Wyłączono urządzenie {device.name}.")
                return redirect("panel:kiosks")
    return render(request, "panel/kiosks.html", {
        "devices": KioskDevice.objects.all(), "code": code, "cameras": KioskDevice.Camera.choices, "minutes": services.PAIRING_MINUTES,
        "kiosk_url": settings.SITE_URL + reverse("kiosk:home"),
    })
