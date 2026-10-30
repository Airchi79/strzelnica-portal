import csv
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit.services import log_event
from apps.core.models import AssociationSettings, Notification
from apps.core.permissions import panel_required
from apps.facilities.models import Lane, format_pln

from apps.logbook.views import reservation_qr_context

from apps.payments.services import balance_of

from . import services
from .forms import (
    BlockForm,
    BoardCancelForm,
    BoardReservationForm,
    BookingSettingsForm,
    CancelForm,
    PersonsForm,
    ReservationForm,
    active_lanes,
)
from .models import Block, BookingSettings, Reservation

PERM = services.PERM


def _parse_day(value, default=None):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default or timezone.localdate()


def _day_grid(day, lanes, now=None):
    """Siatka dnia: wiersze = godziny, kolumny = osie. Zwraca (godziny, {oś: {godzina: Slot}})."""
    columns = []
    hours = set()
    for lane in lanes:
        slots = {s.hour: s for s in services.day_slots(lane, day, now)}
        hours.update(slots)
        columns.append((lane, slots))
    ordered = sorted(hours)
    rows = [(h, [(lane, slots.get(h)) for lane, slots in columns]) for h in ordered]
    return [lane for lane, _ in columns], rows


def calendar(request):
    """Publiczny kalendarz zajętości osi — bez danych osobowych."""
    day = _parse_day(request.GET.get("dzien"))
    lanes, rows = _day_grid(day, active_lanes())
    can_book = request.user.is_authenticated and not services.check_can_book(request.user)
    return render(
        request,
        "bookings/calendar.html",
        {"day": day, "prev": day - timedelta(days=1), "next": day + timedelta(days=1), "lanes": lanes, "rows": rows, "can_book": can_book},
    )


def _messages_from(error):
    return error.messages if isinstance(error, ValidationError) else [str(error)]


@login_required
def new(request):
    reasons = services.check_can_book(request.user)
    if reasons:
        return render(request, "bookings/cannot_book.html", {"reasons": reasons})
    initial = {"day": _parse_day(request.GET.get("dzien"))}
    if request.GET.get("os"):
        initial["lane"] = request.GET["os"]
    if request.GET.get("od", "").isdigit():
        initial["start_hour"] = int(request.GET["od"])
        initial["end_hour"] = int(request.GET["od"]) + 1
    form = ReservationForm(request.POST or None, initial=initial)
    preview = None
    if request.method == "POST" and form.is_valid() and request.POST.get("action") != "edit":
        d = form.cleaned_data
        try:
            if request.POST.get("action") == "confirm":
                reservation = services.create_reservation(
                    request.user, d["lane"], d["day"], d["start_hour"], d["end_hour"], d["persons"], d["notes"]
                )
                messages.success(request, f"Zarezerwowano: {reservation.lane}, {timezone.localtime(reservation.starts_at):%d.%m.%Y %H:%M}. Potwierdzenie wysłaliśmy e-mailem.")
                return redirect("bookings:detail", pk=reservation.pk)
            preview = services.quote(d["lane"], d["day"], d["start_hour"], d["end_hour"], d["persons"])
            preview["hours"] = d["end_hour"] - d["start_hour"]
            preview["free_cancel_until"] = preview["starts_at"] - timedelta(hours=BookingSettings.load().free_cancel_hours)
        except ValidationError as e:
            for m in _messages_from(e):
                form.add_error(None, m)
    lane_id = form.data.get("lane") or initial.get("lane") or (active_lanes().values_list("pk", flat=True).first())
    lane = Lane.objects.filter(pk=lane_id).first() if str(lane_id).isdigit() else None
    day = _parse_day(form.data.get("day"), initial["day"])
    slots = services.day_slots(lane, day) if lane else []
    return render(request, "bookings/new.html", {"form": form, "preview": preview, "slots": slots, "lane": lane, "day": day})


@login_required
def my_list(request):
    now = timezone.now()
    qs = request.user.reservations.select_related("lane")
    upcoming = qs.filter(ends_at__gt=now, status=Reservation.Status.CONFIRMED).order_by("starts_at")
    past = qs.exclude(pk__in=upcoming.values("pk")).order_by("-starts_at")
    page = Paginator(past, 20).get_page(request.GET.get("page"))
    return render(request, "bookings/my_list.html", {"upcoming": upcoming, "page": page})


@login_required
def detail(request, pk):
    reservation = get_object_or_404(Reservation.objects.select_related("lane"), pk=pk, user=request.user)
    form = CancelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            reservation = services.cancel_reservation(reservation, request.user, form.cleaned_data["reason"])
        except ValidationError as e:
            messages.error(request, _messages_from(e)[0])
        else:
            if reservation.late_cancellation:
                messages.warning(request, f"Rezerwacja anulowana po terminie bezpłatnego anulowania — należność {format_pln(reservation.amount)} pozostaje do zapłaty.")
            else:
                messages.success(request, "Rezerwacja anulowana bez opłat.")
        return redirect("bookings:detail", pk=reservation.pk)
    return render(
        request,
        "bookings/detail.html",
        {
            "r": reservation,
            "form": form,
            "late": timezone.now() > reservation.free_cancel_deadline,
            "association": AssociationSettings.load(),
            "log_entries": reservation.log_entries.order_by("entered_at"),
            "tpay_enabled": settings.TPAY_ENABLED,
            **reservation_qr_context(request, reservation),
        },
    )


# --- panel zarządu ---------------------------------------------------------


@panel_required(PERM)
def panel_calendar(request):
    day = _parse_day(request.GET.get("dzien"))
    lanes, rows = _day_grid(day, Lane.objects.filter(is_active=True).select_related("facility"))
    return render(
        request,
        "panel/bookings_calendar.html",
        {"day": day, "prev": day - timedelta(days=1), "next": day + timedelta(days=1), "lanes": lanes, "rows": rows},
    )


def _csv_safe(value):
    """Ochrona przed wstrzyknięciem formuł do arkusza (CSV injection)."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _filtered(request):
    qs = Reservation.objects.select_related("lane", "user").order_by("-starts_at")
    f = {
        "od": request.GET.get("od", ""),
        "do": request.GET.get("do", ""),
        "os": request.GET.get("os", ""),
        "status": request.GET.get("status", ""),
        "platnosc": request.GET.get("platnosc", ""),
        "q": request.GET.get("q", "").strip(),
    }
    if f["od"]:
        qs = qs.filter(starts_at__gte=services.local_dt(_parse_day(f["od"]), 0))
    if f["do"]:
        qs = qs.filter(starts_at__lt=services.local_dt(_parse_day(f["do"]) + timedelta(days=1), 0))
    if f["os"].isdigit():
        qs = qs.filter(lane_id=int(f["os"]))
    if f["status"] in Reservation.Status.values:
        qs = qs.filter(status=f["status"])
    if f["platnosc"] in Reservation.Payment.values:
        qs = qs.filter(payment_status=f["platnosc"])
    if f["q"]:
        qs = qs.filter(Q(number__icontains=f["q"]) | Q(user__last_name__icontains=f["q"]) | Q(user__email__icontains=f["q"]))
    return qs, f


@panel_required(PERM)
def panel_list(request):
    qs, f = _filtered(request)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="rezerwacje-{timezone.localdate():%Y%m%d}.csv"'
        response.write("﻿")  # BOM — poprawne polskie znaki w Excelu
        writer = csv.writer(response, delimiter=";")
        writer.writerow(["Numer", "Oś", "Początek", "Koniec", "Godz.", "Osób", "Rezerwujący", "E-mail", "Status", "Płatność", "Należność [zł]", "Anulowana po terminie"])
        for r in qs:
            writer.writerow([_csv_safe(v) for v in [
                r.number, r.lane.name, f"{timezone.localtime(r.starts_at):%Y-%m-%d %H:%M}", f"{timezone.localtime(r.ends_at):%Y-%m-%d %H:%M}",
                r.hours, r.persons, r.user.full_name, r.user.email, r.display_status, r.get_payment_status_display(),
                f"{r.amount / 100:.2f}".replace(".", ","), "tak" if r.late_cancellation else "",
            ]])
        return response
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "panel/bookings_list.html",
        {"page": page, "f": f, "lanes": Lane.objects.all(), "statuses": Reservation.Status.choices, "payments": Reservation.Payment.choices},
    )


@panel_required(PERM)
def panel_detail(request, pk):
    r = get_object_or_404(Reservation.objects.select_related("lane", "user", "cancelled_by", "payment_marked_by"), pk=pk)
    cancel_form = BoardCancelForm(prefix="c")
    persons_form = PersonsForm(prefix="p", initial={"persons": r.persons})
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "cancel":
                cancel_form = BoardCancelForm(request.POST, prefix="c")
                if cancel_form.is_valid():
                    services.cancel_reservation(r, request.user, cancel_form.cleaned_data["reason"], as_board=True)
                    messages.success(request, "Rezerwacja anulowana. Strzelec otrzyma e-mail.")
                    return redirect("panel:reservation", pk=r.pk)
            elif action == "persons":
                persons_form = PersonsForm(request.POST, prefix="p")
                if persons_form.is_valid():
                    services.change_persons(r, persons_form.cleaned_data["persons"], request.user)
                    messages.success(request, "Zmieniono liczbę osób i należność.")
                    return redirect("panel:reservation", pk=r.pk)
            elif action in services.PAYMENT_TRANSITIONS:
                services.set_payment(r, action, request.user)
                messages.success(request, "Zapisano rozliczenie.")
                return redirect("panel:reservation", pk=r.pk)
            elif action == "no_show":
                services.mark_no_show(r, request.user)
                messages.success(request, "Oznaczono jako nieodbytą.")
                return redirect("panel:reservation", pk=r.pk)
            else:
                raise Http404
        except ValidationError as e:
            messages.error(request, _messages_from(e)[0])
            return redirect("panel:reservation", pk=r.pk)
    return render(
        request,
        "panel/booking_detail.html",
        {
            "r": r,
            "cancel_form": cancel_form,
            "persons_form": persons_form,
            "started": r.starts_at <= timezone.now(),
            "wallet_balance": balance_of(r.user),
            "remaining_pln": f"{(r.amount - r.paid_amount) / 100:.2f}".replace(".", ","),
        },
    )


@panel_required(PERM)
def panel_new(request):
    initial = {"day": _parse_day(request.GET.get("dzien"))}
    if request.GET.get("os"):
        initial["lane"] = request.GET["os"]
    if request.GET.get("od", "").isdigit():
        initial["start_hour"] = int(request.GET["od"])
        initial["end_hour"] = int(request.GET["od"]) + 1
    form = BoardReservationForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            r = services.create_reservation(
                d["user"], d["lane"], d["day"], d["start_hour"], d["end_hour"], d["persons"], d["notes"], created_by=request.user
            )
        except ValidationError as e:
            for m in _messages_from(e):
                form.add_error(None, m)
        else:
            messages.success(request, f"Utworzono rezerwację {r.number}.")
            return redirect("panel:reservation", pk=r.pk)
    return render(request, "panel/booking_new.html", {"form": form})


@panel_required(PERM)
def panel_blocks(request):
    upcoming = Block.objects.filter(ends_at__gt=timezone.now()).prefetch_related("lanes")
    return render(request, "panel/blocks_list.html", {"blocks": upcoming})


@panel_required(PERM)
def panel_block_new(request):
    form = BlockForm(request.POST or None, initial={"day_from": timezone.localdate(), "day_to": timezone.localdate()})
    conflicts = None
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        occurrences = services.block_occurrences(d["day_from"], d["day_to"], d["start_hour"], d["end_hour"], d["weekly"])
        conflicts = list(services.block_conflicts(d["lanes"], occurrences))
        decision = request.POST.get("decision")
        if not conflicts or decision in {"cancel", "keep"}:
            blocks, found, cancelled = services.create_blocks(
                d["title"], d["reason"], list(d["lanes"]), occurrences, request.user, cancel_conflicts=decision == "cancel"
            )
            msg = f"Utworzono blokadę ({len(blocks)} termin(y))."
            if cancelled:
                msg += f" Anulowano {len(cancelled)} rezerwacji — strzelcy otrzymali e-mail."
            elif found:
                msg += f" {len(found)} rezerwacji pozostało — strzelcy otrzymali informację o blokadzie."
            messages.success(request, msg)
            return redirect("panel:blocks")
    return render(request, "panel/block_new.html", {"form": form, "conflicts": conflicts})


@panel_required(PERM)
@require_POST
def panel_block_delete(request, pk):
    block = get_object_or_404(Block, pk=pk)
    count = services.delete_block_series(block, request.user, whole_series=request.POST.get("series") == "1")
    messages.success(request, f"Usunięto blokadę ({count} termin(y)).")
    return redirect("panel:blocks")


@panel_required(PERM)
def panel_settings(request):
    obj = BookingSettings.load()
    before = {f: getattr(obj, f) for f in BookingSettingsForm.Meta.fields}
    form = BookingSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        log_event("rezerwacje.ustawienia", obj, changes={f: [before[f], getattr(obj, f)] for f in before if before[f] != getattr(obj, f)})
        messages.success(request, "Zapisano ustawienia rezerwacji.")
        return redirect("panel:booking_settings")
    return render(request, "panel/booking_settings.html", {"form": form})


@panel_required()
def panel_notifications(request):
    qs = request.user.notifications.all()
    if request.method == "POST":
        qs.filter(read_at__isnull=True).update(read_at=timezone.now())
        return redirect("panel:notifications")
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "panel/notifications.html", {"page": page})


@panel_required()
def panel_notification_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if n.read_at is None:
        n.read_at = timezone.now()
        n.save(update_fields=["read_at"])
    return redirect(n.url if n.url.startswith("/") and not n.url.startswith("//") else reverse("panel:notifications"))
