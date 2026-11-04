import csv
from urllib.parse import quote, urlencode
from datetime import date, timedelta
from datetime import timezone as dt_timezone

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
    GroupRuleForm,
    CancelForm,
    PersonsForm,
    ReservationForm,
    active_lanes,
)
from .models import Block, BookingSettings, GroupRule, Reservation

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


def slots_json(request):
    """Zajętość jednej osi w danym dniu i cena — dla formularza rezerwacji (bez danych osobowych, jak publiczny kalendarz)."""
    from django.http import JsonResponse

    lane_id = request.GET.get("os", "")
    lane = active_lanes().filter(pk=int(lane_id)).first() if lane_id.isdigit() else None
    if lane is None:
        return JsonResponse({"error": "Nieznana oś."}, status=404)
    day = _parse_day(request.GET.get("dzien"))
    slots = services.day_slots(lane, day)
    rule = lane.price_rule_for(day)
    return JsonResponse({
        "lane": lane.pk, "day": day.isoformat(),
        "slots": [{"hour": s.hour, "state": s.state} for s in slots],
        "price": {"per_person": rule.per_person_hourly, "flat": rule.flat_hourly} if rule else None,
        "exclusive_only": lane.exclusive_only,
        "max_persons": BookingSettings.load().max_persons,
    })


def _google_calendar_url(r, request):
    fmt = "%Y%m%dT%H%M%SZ"
    dates = f"{r.starts_at.astimezone(dt_timezone.utc).strftime(fmt)}/{r.ends_at.astimezone(dt_timezone.utc).strftime(fmt)}"
    link = settings.SITE_URL + reverse("bookings:detail", args=[r.pk])
    params = {"action": "TEMPLATE", "text": f"Strzelnica — {r.lane}", "dates": dates,
              "details": f"Rezerwacja {r.number}, {r.persons} os. {link}", "location": AssociationSettings.load().address}
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _messages_from(error):
    return error.messages if isinstance(error, ValidationError) else [str(error)]


@login_required
def new(request):
    reasons = services.check_can_book(request.user)
    if reasons:
        return render(request, "bookings/cannot_book.html", {"reasons": reasons, "readiness": services.booking_readiness(request.user)})
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
                    request.user, d["lane"], d["day"], d["start_hour"], d["end_hour"], d["persons"], d["notes"],
                    access_code=d["access_code"],
                )
                messages.success(request, f"Zarezerwowano: {reservation.lane}, {timezone.localtime(reservation.starts_at):%d.%m.%Y %H:%M}. Potwierdzenie wysłaliśmy e-mailem.")
                return redirect("bookings:detail", pk=reservation.pk)
            preview = services.quote(
                d["lane"], d["day"], d["start_hour"], d["end_hour"], d["persons"], access_code=d["access_code"], user=request.user
            )
            preview["hours"] = d["end_hour"] - d["start_hour"]
            preview["free_cancel_until"] = preview["starts_at"] - timedelta(hours=BookingSettings.load().free_cancel_hours)
            from apps.clients.services import TYPE_LABELS, client_type

            group = client_type(request.user)
            preview["rule"] = GroupRule.for_group(group)
            preview["group_label"] = TYPE_LABELS[group]
        except ValidationError as e:
            for m in _messages_from(e):
                form.add_error(None, m)
    lane_id = form.data.get("lane") or initial.get("lane") or (active_lanes().values_list("pk", flat=True).first())
    lane = Lane.objects.filter(pk=lane_id).first() if str(lane_id).isdigit() else None
    day = _parse_day(form.data.get("day"), initial["day"])
    slots = services.day_slots(lane, day) if lane else []
    lane_cards = [(l, l.price_rule_for(day)) for l in active_lanes().order_by("facility__order", "order")]
    week = [timezone.localdate() + timedelta(days=i) for i in range(7)]
    return render(request, "bookings/new.html", {"form": form, "preview": preview, "slots": slots, "lane": lane, "day": day,
                                                 "lane_cards": lane_cards, "week": week})


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
            messages.success(request, "Rezerwacja anulowana bez opłat.")
        return redirect("bookings:detail", pk=reservation.pk)
    return render(
        request,
        "bookings/detail.html",
        {
            "r": reservation,
            "form": form,
            "association": AssociationSettings.load(),
            "pay_options": services.payment_options(reservation),
            "gcal_url": _google_calendar_url(reservation, request),
            "map_url": ("https://www.google.com/maps/search/?api=1&query=" + quote(AssociationSettings.load().address))
            if AssociationSettings.load().address else "",
            "cancel_hours": BookingSettings.load().free_cancel_hours,
            "log_entries": reservation.log_entries.order_by("entered_at"),
            "tpay_enabled": settings.TPAY_ENABLED,
            **reservation_qr_context(request, reservation),
        },
    )


def _ics_escape(text):
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@login_required
def ics(request, pk):
    """Plik kalendarza (iCalendar) dla własnej rezerwacji — „Dodaj do kalendarza” w telefonie."""
    r = get_object_or_404(Reservation.objects.select_related("lane"), pk=pk, user=request.user)
    association = AssociationSettings.load()
    fmt = "%Y%m%dT%H%M%SZ"
    utc = lambda dt: dt.astimezone(dt_timezone.utc).strftime(fmt)
    link = settings.SITE_URL + reverse("bookings:detail", args=[r.pk])
    host = settings.SITE_URL.split("//")[-1].split("/")[0] or "strzelnica"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Strzelnica Satok//Rezerwacje//PL", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT", f"UID:{r.number}@{host}", f"DTSTAMP:{utc(timezone.now())}", f"DTSTART:{utc(r.starts_at)}", f"DTEND:{utc(r.ends_at)}",
        f"SUMMARY:{_ics_escape(f'Strzelnica — {r.lane}')}",
        f"LOCATION:{_ics_escape(association.address or association.name)}",
        f"DESCRIPTION:{_ics_escape(f'Rezerwacja {r.number}, {r.persons} os. Kod QR do książki pobytu: {link}')}",
        f"URL:{link}",
        "STATUS:CANCELLED" if r.status == Reservation.Status.CANCELLED else "STATUS:CONFIRMED",
        "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Strzelanie za 2 godziny", "TRIGGER:-PT2H", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR",
    ]
    response = HttpResponse("\r\n".join(lines) + "\r\n", content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{r.number}.ics"'
    return response


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
                    keep = cancel_form.cleaned_data["keep_fee"]
                    services.cancel_reservation(r, request.user, cancel_form.cleaned_data["reason"], as_board=True, keep_fee=keep)
                    messages.success(request, "Rezerwacja anulowana" + (" z zachowaniem należności" if keep else "") + ". Klient otrzyma e-mail.")
                    return redirect("panel:reservation", pk=r.pk)
            elif action == "approve":
                services.approve_reservation(r, request.user)
                messages.success(request, "Rezerwacja zatwierdzona. Klient otrzyma e-mail z informacją o płatności.")
                return redirect("panel:reservation", pk=r.pk)
            elif action == "reject":
                cancel_form = BoardCancelForm(request.POST, prefix="c")
                if cancel_form.is_valid():
                    services.reject_reservation(r, request.user, cancel_form.cleaned_data["reason"])
                    messages.success(request, "Rezerwacja odrzucona. Klient otrzyma e-mail.")
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
            "log_entries": list(r.log_entries.order_by("entered_at")) if request.user.has_perm("logbook.view_logbook") else None,
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
                d["user"], d["lane"], d["day"], d["start_hour"], d["end_hour"], d["persons"], d["notes"], created_by=request.user,
                access_code=d["access_code"],
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
    post = request.POST if request.method == "POST" else None
    form = BookingSettingsForm(post, instance=obj)
    rules = [GroupRule.for_group(g) for g in GroupRule.Group.values]
    rule_forms = [(r, GroupRuleForm(post, instance=r, prefix=r.group)) for r in rules]
    if post is not None and form.is_valid() and all(f.is_valid() for _, f in rule_forms):
        obj = form.save()
        changes = {f: [before[f], getattr(obj, f)] for f in before if before[f] != getattr(obj, f)}
        for rule, f in rule_forms:
            if f.has_changed():
                f.save()
                changes[f"grupa:{rule.group}"] = {k: f.cleaned_data[k] for k in f.changed_data}
        log_event("rezerwacje.ustawienia", obj, changes=changes)
        messages.success(request, "Zapisano ustawienia rezerwacji.")
        return redirect("panel:booking_settings")
    return render(request, "panel/booking_settings.html", {"form": form, "rule_forms": rule_forms, "tpay_enabled": settings.TPAY_ENABLED})


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


# --- panel: wszystkie rezerwacje (widok dniami) ---------------------------------------------------

PAYMENT_LABELS = {"tpay": "Online", "gotowka": "Na miejscu", "blik": "BLIK", "przelew": "Przelew"}


@panel_required(PERM)
def panel_days(request):
    """Rezerwacje pogrupowane dniami: godziny, klient, oś, prowadzący, stan książki pobytu, płatność."""
    from django.db.models import Count, Prefetch

    from apps.logbook.models import Entry
    from apps.payments.models import Payment

    today = timezone.localdate()
    f = {
        "q": request.GET.get("q", "").strip(),
        "os": request.GET.get("os", ""),
        "filtr": request.GET.get("filtr", ""),
        "anulowane": request.GET.get("anulowane") == "1",
    }
    start = _parse_day(request.GET.get("od"), today - timedelta(days=2))
    span = 14
    qs = Reservation.objects.select_related("lane", "user", "access_code__club", "client_pass").annotate(
        shooters=Count("log_entries", filter=Q(log_entries__purpose=Entry.Purpose.SHOOTING), distinct=True),
        entries=Count("log_entries", distinct=True),
    ).prefetch_related(Prefetch("payments", queryset=Payment.objects.filter(status=Payment.Status.COMPLETED).order_by("-completed_at"), to_attr="done_payments"))
    if not f["anulowane"]:
        qs = qs.exclude(status=Reservation.Status.CANCELLED, late_cancellation=False)
    if f["os"].isdigit():
        qs = qs.filter(lane_id=int(f["os"]))
    if f["q"]:
        qs = qs.filter(Q(number__icontains=f["q"]) | Q(user__last_name__icontains=f["q"]) | Q(user__first_name__icontains=f["q"])
                       | Q(user__email__icontains=f["q"]) | Q(user__phone__icontains=f["q"]))
    ranged = True
    if f["filtr"] == "do_zaplaty":
        qs, ranged = qs.filter(payment_status=Reservation.Payment.DUE), False
    elif f["filtr"] == "braki_ksiega":
        now = timezone.now()
        qs, ranged = qs.filter(status=Reservation.Status.CONFIRMED, starts_at__lte=now, starts_at__gte=now - timedelta(days=30), shooters=0), False
    elif f["filtr"] == "do_zatwierdzenia":
        qs, ranged = qs.filter(status=Reservation.Status.PENDING_APPROVAL), False
    elif f["q"]:
        ranged = False
    if ranged:
        qs = qs.filter(starts_at__gte=services.local_dt(start, 0), starts_at__lt=services.local_dt(start + timedelta(days=span), 0))
    days = {}
    for r in qs.order_by("starts_at", "lane__order"):
        pay = r.done_payments[0] if r.done_payments else None
        if r.access_code_id:
            r.pay_label = f"Kod klubu {r.access_code.club}"
        elif r.client_pass_id:
            r.pay_label = f"Kod wejścia {r.client_pass.code}"
        elif pay:
            r.pay_label = PAYMENT_LABELS.get(pay.method, pay.get_method_display())
        else:
            r.pay_label = ""
        r.missing_entries = r.status == Reservation.Status.CONFIRMED and r.starts_at <= timezone.now() and r.shooters == 0
        days.setdefault(timezone.localtime(r.starts_at).date(), []).append(r)
    if ranged:
        groups = [(start + timedelta(days=i), days.get(start + timedelta(days=i), [])) for i in range(span)]
    else:
        groups = sorted(days.items())
    return render(request, "panel/bookings_days.html", {
        "groups": groups, "f": f, "today": today, "start": start, "ranged": ranged,
        "earlier": start - timedelta(days=7), "later": start + timedelta(days=7), "lanes": Lane.objects.all(),
        "total": sum(len(rows) for _, rows in groups),
    })


# --- archiwum rezerwacji z GunTrackera ---------------------------------------------------------


@panel_required(PERM)
def panel_archive(request):
    from .models import ArchivedReservation

    q = request.GET.get("q", "").strip()
    qs = ArchivedReservation.objects.select_related("lane", "block")
    if q:
        qs = qs.filter(Q(client__icontains=q) | Q(number__icontains=q) | Q(range_officer__icontains=q) | Q(contact__icontains=q))
    if request.GET.get("przyszle") == "1":
        qs = qs.filter(ends_at__gt=timezone.now()).order_by("starts_at")
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "panel/bookings_archive.html", {"page": page, "q": q, "przyszle": request.GET.get("przyszle") == "1",
                                                            "total": ArchivedReservation.objects.count()})


@panel_required(PERM)
def panel_archive_import(request):
    from . import archive

    result = preview = None
    year = timezone.localdate().year
    if request.method == "POST":
        uploaded = request.FILES.get("plik")
        preview = request.POST.get("tryb") != "importuj"
        try:
            year = int(request.POST.get("rok", year))
            if not 2000 <= year <= 2100:
                raise ValueError
        except (TypeError, ValueError):
            messages.error(request, "Podaj poprawny rok.")
        else:
            if uploaded is None:
                messages.error(request, "Wybierz plik XLSX.")
            else:
                try:
                    result = archive.run(uploaded, year=year, by=request.user, preview=preview,
                                         update_contacts=request.POST.get("kontakty") == "1")
                except ValidationError as e:
                    messages.error(request, " ".join(e.messages))
                else:
                    if not preview:
                        messages.success(request, "Import rezerwacji zakończony.")
    return render(request, "panel/bookings_archive_import.html", {"result": result, "preview": preview, "year": year})
