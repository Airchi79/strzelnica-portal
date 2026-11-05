import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.permissions import panel_required

from . import services
from .forms import EventForm, RegisterForm
from .models import Event, Registration

PERM = services.PERM


def _annotated(qs):
    return qs.annotate(n_taken=Count("registrations", filter=Q(registrations__status=Registration.Status.REGISTERED)))


def public_list(request):
    events = _annotated(Event.objects.filter(status=Event.Status.PUBLISHED, ends_at__gte=timezone.now()))
    mine = set()
    if request.user.is_authenticated:
        mine = set(Registration.objects.filter(user=request.user).exclude(status=Registration.Status.CANCELLED).values_list("event_id", flat=True))
    return render(request, "events/list.html", {"events": events, "mine": mine})


def detail(request, pk):
    event = get_object_or_404(Event.objects.exclude(status=Event.Status.DRAFT), pk=pk)
    reg = None
    if request.user.is_authenticated:
        reg = Registration.objects.filter(event=event, user=request.user).exclude(status=Registration.Status.CANCELLED).first()
    form = RegisterForm(event=event)
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect(f"{settings_login()}?next={request.path}")
        action = request.POST.get("action")
        try:
            if action == "register":
                form = RegisterForm(request.POST, event=event)
                if form.is_valid():
                    reg = services.register(request.user, event, form.cleaned_data.get("note", ""))
                    messages.success(request, "Zapisano na listę rezerwową." if reg.status == Registration.Status.RESERVE else "Zapisano! Potwierdzenie wysłaliśmy e-mailem.")
                    return redirect("events:detail", pk=event.pk)
            elif action == "cancel" and reg:
                services.cancel_registration(reg, request.user)
                messages.success(request, "Wycofano zapis.")
                return redirect("events:detail", pk=event.pk)
        except ValidationError as e:
            for m in e.messages:
                form.add_error(None, m)
    reasons = services.can_register(request.user, event) if request.user.is_authenticated and not reg else []
    from apps.core.models import AssociationSettings
    from django.conf import settings as dj_settings

    return render(request, "events/detail.html", {
        "event": event, "reg": reg, "form": form, "reasons": reasons, "taken": event.taken(), "free": event.free_places(),
        "tpay_enabled": dj_settings.TPAY_ENABLED, "association": AssociationSettings.load(),
    })


def settings_login():
    from django.conf import settings

    return settings.LOGIN_URL


# --- panel -----------------------------------------------------------------


@panel_required(PERM)
def panel_list(request):
    show = request.GET.get("pokaz", "nadchodzace")
    qs = _annotated(Event.objects.all())
    qs = qs.filter(ends_at__gte=timezone.now()) if show == "nadchodzace" else qs.filter(ends_at__lt=timezone.now()).order_by("-starts_at")
    return render(request, "panel/events.html", {"events": qs, "show": show})


@panel_required(PERM)
def panel_edit(request, pk=None):
    event = get_object_or_404(Event, pk=pk) if pk else None
    form = EventForm(request.POST or None, instance=event)
    conflicts = None
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        obj = form.save(commit=False)
        if obj.status == Event.Status.PUBLISHED and d["lanes"] and request.POST.get("confirm") != "1":
            conflicts = services.conflicts_for(obj, d["lanes"], obj.starts_at, obj.ends_at)
        if conflicts:
            return render(request, "panel/event_edit.html", {"form": form, "event": event, "conflicts": conflicts})
        services.save_event(obj, d["lanes"], request.user, cancel_conflicts=d.get("cancel_conflicts"))
        messages.success(request, "Zapisano wydarzenie." + (" Osie zablokowane na czas wydarzenia." if obj.status == Event.Status.PUBLISHED and d["lanes"] else ""))
        return redirect("panel:event", pk=obj.pk)
    return render(request, "panel/event_edit.html", {"form": form, "event": event, "conflicts": None})


@panel_required(PERM)
def panel_detail(request, pk):
    event = get_object_or_404(Event, pk=pk)
    regs = list(event.registrations.select_related("user").order_by("status", "created_at", "pk"))
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "cancel_event":
                services.cancel_event(event, request.user, request.POST.get("reason", ""))
                messages.success(request, "Wydarzenie odwołane. Uczestnicy dostali e-mail.")
            elif action in ("cash", "blik", "waive", "withdraw"):
                reg = get_object_or_404(Registration, pk=request.POST.get("reg"), event=event)
                if action == "withdraw":
                    services.cancel_registration(reg, request.user, by_staff=True, reason=request.POST.get("reason", ""))
                    messages.success(request, f"Wycofano: {reg.user.full_name}.")
                elif action == "waive":
                    reg.payment_status = Registration.Payment.WAIVED
                    reg.save(update_fields=["payment_status"])
                    messages.success(request, f"Zwolniono z opłaty: {reg.user.full_name}.")
                else:
                    if not (request.user.has_perm("payments.manage_payments") or request.user.has_perm("payments.accept_onsite")):
                        raise ValidationError("Brak uprawnienia do przyjmowania wpłat.")
                    from apps.payments.services import register_manual_payment

                    register_manual_payment(reg.user, reg.amount - reg.paid_amount, "gotowka" if action == "cash" else "blik",
                                            request.user, registration=reg)
                    messages.success(request, f"Przyjęto opłatę: {reg.user.full_name}.")
        except ValidationError as e:
            messages.error(request, e.messages[0])
        return redirect("panel:event", pk=event.pk)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="lista-{event.pk}.csv"'
        response.write("﻿")
        w = csv.writer(response, delimiter=";")
        w.writerow(["Lp.", "Imię i nazwisko", "E-mail", "Telefon", event.note_label or "Uwagi", "Status", "Płatność", "Zapisano"])
        for i, r in enumerate([x for x in regs if x.status != Registration.Status.CANCELLED], 1):
            w.writerow([i, _safe(r.user.full_name), r.user.email, _safe(r.user.phone), _safe(r.note), r.get_status_display(),
                        r.get_payment_status_display(), timezone.localtime(r.created_at).strftime("%d.%m.%Y %H:%M")])
        return response
    return render(request, "panel/event_detail.html", {
        "event": event, "registered": [r for r in regs if r.status == Registration.Status.REGISTERED],
        "reserve": [r for r in regs if r.status == Registration.Status.RESERVE],
        "cancelled": [r for r in regs if r.status == Registration.Status.CANCELLED],
        "print": request.GET.get("druk") == "1",
    })


def _safe(v):
    v = str(v or "")
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v
