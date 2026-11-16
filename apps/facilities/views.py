from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

from apps.audit.services import diff, log_event
from apps.core.permissions import panel_required

from .forms import FacilityForm, HoursFormSet, LaneForm, NewLaneForm, PriceRuleForm, WeekHoursForm
from .models import Facility, Lane, OpeningHours, PriceRule


def public_lanes(request):
    today = timezone.localdate()
    lanes = [(lane, lane.price_rule_for(today)) for lane in Lane.objects.filter(is_active=True, facility__is_active=True)
             .select_related("facility").prefetch_related("opening_hours")]
    from apps.bookings.models import BookingSettings

    return render(request, "facilities/public_lanes.html", {"lanes": lanes, "booking_cfg": BookingSettings.load()})


@panel_required("facilities.manage_facilities")
def panel_list(request):
    today = timezone.localdate()
    facilities = []
    for facility in Facility.objects.prefetch_related("lanes__opening_hours"):
        facilities.append((facility, [(lane, lane.price_rule_for(today)) for lane in facility.lanes.all()]))
    return render(request, "panel/lanes_list.html", {"facilities": facilities})


@panel_required("facilities.manage_facilities")
def panel_lane(request, pk):
    lane = get_object_or_404(Lane, pk=pk)
    before = Lane.objects.get(pk=pk)
    form = LaneForm(request.POST or None, instance=lane, prefix="lane")
    hours = HoursFormSet(request.POST or None, instance=lane, prefix="hours")
    if request.method == "POST" and form.is_valid() and hours.is_valid():
        with transaction.atomic():
            lane = form.save()
            # Najpierw usunięcia, potem zapis — unikalność (oś, dzień) przy zamianie dni.
            hours.save(commit=False)
            for obj in hours.deleted_objects:
                obj.delete()
            for obj in hours.new_objects + [o for o, _ in hours.changed_objects]:
                obj.save()
        log_event(
            "os.zmiana",
            lane,
            str(lane),
            changes={**diff(before, lane, LaneForm.Meta.fields), "godziny": [str(h) for h in lane.opening_hours.all()]},
        )
        messages.success(request, "Zapisano oś.")
        if before.is_active and not lane.is_active:
            future = _future_reservations(lane).count()
            if future:
                messages.warning(request, f"Oś wyłączona, ale ma {future} przyszłych rezerwacji — zostają ważne. "
                                          "Anuluj je albo przenieś w panelu, jeśli oś nie będzie dostępna.")
        return redirect("panel:lanes")
    return render(
        request, "panel/lane_edit.html",
        {"lane": lane, "form": form, "hours": hours, "rules": lane.price_rules.all(), "can_delete": _lane_deletable(lane),
         "future_count": _future_reservations(lane).count()},
    )


def _common_week(lanes):
    """Najczęstsze godziny każdego dnia wśród osi — propozycja do formularza zmiany zbiorczej."""
    from collections import Counter

    week = {}
    for day, _ in OpeningHours.WEEKDAYS:
        spans = Counter()
        for lane in lanes:
            oh = next((h for h in lane.opening_hours.all() if h.weekday == day), None)
            spans[(oh.opens, oh.closes) if oh else None] += 1
        week[day] = spans.most_common(1)[0][0] if spans else None
    return week


def _outside_hours(lanes):
    """Przyszłe rezerwacje tych osi, które po zmianie wypadają (choć częściowo) poza godzinami otwarcia."""
    out = []
    for lane in lanes:
        hours = {h.weekday: (h.opens, h.closes) for h in lane.opening_hours.all()}
        for r in _future_reservations(lane).select_related("lane"):
            s, e = timezone.localtime(r.starts_at), timezone.localtime(r.ends_at)
            span = hours.get(s.weekday())
            end_time = e.time() if e.date() == s.date() else None
            if not span or s.time() < span[0] or end_time is None or end_time > span[1]:
                out.append(r)
    return sorted(out, key=lambda r: r.starts_at)


@panel_required("facilities.manage_facilities")
def panel_hours_bulk(request):
    """Godziny otwarcia całego tygodnia dla wielu osi naraz."""
    lanes = list(Lane.objects.select_related("facility").prefetch_related("opening_hours").order_by("facility__order", "order"))
    active = [lane for lane in lanes if lane.is_active and lane.facility.is_active]
    if request.method == "POST":
        form = WeekHoursForm(request.POST)
        if form.is_valid():
            chosen, week = list(form.cleaned_data["lanes"]), form.cleaned_data["week"]
            with transaction.atomic():
                for lane in chosen:
                    before = [str(h) for h in lane.opening_hours.all()]
                    lane.opening_hours.all().delete()
                    OpeningHours.objects.bulk_create([OpeningHours(lane=lane, weekday=d, opens=span[0], closes=span[1])
                                                      for d, span in week.items() if span])
                    log_event("os.godziny_zbiorczo", lane, str(lane),
                              changes={"przed": before, "po": [str(h) for h in lane.opening_hours.all()]}, actor=request.user)
            messages.success(request, f"Zapisano godziny otwarcia dla osi: {len(chosen)}.")
            outside = _outside_hours(Lane.objects.filter(pk__in=[lane.pk for lane in chosen]).prefetch_related("opening_hours"))
            if outside:
                listing = "; ".join(f"{r.number} ({timezone.localtime(r.starts_at):%d.%m %H:%M}, {r.lane})" for r in outside[:10])
                messages.warning(request, f"Przyszłe rezerwacje poza nowymi godzinami: {len(outside)} — zostają ważne, sprawdź je: "
                                          f"{listing}{' …' if len(outside) > 10 else ''}")
            return redirect("panel:lanes")
    else:
        week = _common_week(active)
        initial = {"lanes": [lane.pk for lane in active]}
        for day, span in week.items():
            initial[f"open_{day}"] = span is not None
            if span:
                initial[f"opens_{day}"], initial[f"closes_{day}"] = span
        form = WeekHoursForm(initial=initial)
    return render(request, "panel/lanes_hours_bulk.html", {"form": form, "lanes": lanes})


@panel_required("facilities.manage_facilities")
def panel_price_add(request, pk):
    lane = get_object_or_404(Lane, pk=pk)
    form = PriceRuleForm(request.POST or None, lane=lane, initial={"valid_from": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        rule = form.save(commit=False)
        rule.created_by = request.user
        rule.save()
        log_event(
            "cennik.dodanie",
            rule,
            str(rule),
            changes={"za_osobe_gr": rule.per_person_hourly, "ryczalt_gr": rule.flat_hourly, "od": str(rule.valid_from)},
        )
        messages.success(request, "Dodano tabelę opłat. Wcześniejsze rezerwacje zachowują swoje kwoty.")
        return redirect("panel:lane", pk=lane.pk)
    return render(request, "panel/price_add.html", {"lane": lane, "form": form})


# --- obiekty (strzelnice) i osie: dodawanie, edycja, usuwanie ----------------------------------------


def _future_reservations(lane):
    from apps.bookings.models import Reservation

    return lane.reservations.filter(ends_at__gt=timezone.now(), status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING_APPROVAL,
                                                                                      Reservation.Status.HOLD])


def _lane_deletable(lane):
    """Usunąć można tylko oś bez historii (rezerwacji i blokad) — inaczej wyłączamy ją, by zachować rozliczenia."""
    return not lane.reservations.exists() and not lane.blocks.exists()


@panel_required("facilities.manage_facilities")
def panel_facility(request, pk=None):
    facility = get_object_or_404(Facility, pk=pk) if pk else None
    before = Facility.objects.get(pk=pk) if pk else None
    form = FacilityForm(request.POST or None, instance=facility)
    if request.method == "POST":
        if request.POST.get("action") == "delete" and facility:
            if facility.lanes.exists():
                messages.error(request, "Obiekt ma osie — najpierw je usuń albo przenieś do innego obiektu (albo wyłącz obiekt).")
                return redirect("panel:facility", pk=facility.pk)
            log_event("obiekt.usuniecie", facility, str(facility))
            facility.delete()
            messages.success(request, "Usunięto obiekt.")
            return redirect("panel:lanes")
        if form.is_valid():
            obj = form.save()
            if before:
                log_event("obiekt.zmiana", obj, str(obj), changes=diff(before, obj, FacilityForm.Meta.fields))
            else:
                log_event("obiekt.dodanie", obj, str(obj), changes={"rodzaj": obj.kind})
            messages.success(request, "Zapisano obiekt." if before else "Dodano obiekt. Teraz dodaj do niego osie.")
            return redirect("panel:lanes")
    return render(request, "panel/facility_edit.html", {"form": form, "facility": facility,
                                                         "lane_count": facility.lanes.count() if facility else 0})


@panel_required("facilities.manage_facilities")
def panel_lane_new(request):
    initial = {}
    facility_id = request.GET.get("obiekt", "")
    if facility_id.isdigit():
        initial["facility"] = int(facility_id)
    form = NewLaneForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        with transaction.atomic():
            lane = form.save()
            source = d.get("copy_hours_from")
            if source:
                OpeningHours.objects.bulk_create([OpeningHours(lane=lane, weekday=h.weekday, opens=h.opens, closes=h.closes)
                                                  for h in source.opening_hours.all()])
            if d.get("price_flat") is not None:
                to_gr = form.fields["price_flat"].to_grosze
                PriceRule.objects.create(lane=lane, per_person_hourly=to_gr(d.get("price_per_person")), flat_hourly=to_gr(d["price_flat"]),
                                         valid_from=timezone.localdate(), created_by=request.user)
        log_event("os.dodanie", lane, str(lane), changes={"obiekt": str(lane.facility), "godziny_z": str(source) if source else None,
                                                           "cennik": d.get("price_flat") is not None})
        messages.success(request, f"Dodano oś {lane}. Sprawdź godziny otwarcia i opłaty.")
        return redirect("panel:lane", pk=lane.pk)
    return render(request, "panel/lane_new.html", {"form": form})


@panel_required("facilities.manage_facilities")
@require_POST
def panel_lane_delete(request, pk):
    lane = get_object_or_404(Lane, pk=pk)
    if not _lane_deletable(lane):
        messages.error(request, "Tej osi nie można usunąć — ma rezerwacje lub blokady w historii. Wyłącz ją (pole „aktywna”).")
        return redirect("panel:lane", pk=lane.pk)
    name = str(lane)
    log_event("os.usuniecie", lane, name, changes={"obiekt": str(lane.facility)})
    lane.delete()
    messages.success(request, f"Usunięto oś {name}.")
    return redirect("panel:lanes")


# --- zgłoszenia usterek ---------------------------------------------------


@login_required
def my_issues(request):
    from .models import Issue

    return render(request, "facilities/my_issues.html", {"issues": Issue.objects.filter(reported_by=request.user).select_related("lane")[:50]})


@login_required
def issue_new(request):
    from .forms import IssueForm
    from . import issues

    initial = {"lane": request.GET["os"]} if request.GET.get("os", "").isdigit() else {}
    form = IssueForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        from django.core.exceptions import ValidationError

        try:
            issues.report(request.user, d["title"], d["description"], d["lane"], d["urgent"], d["photo"])
        except ValidationError as e:
            form.add_error(None, e.messages[0])
        else:
            messages.success(request, "Dziękujemy — zgłoszenie trafiło do zarządu. O naprawie dostaniesz e-mail.")
            return redirect("core:issues")
    from apps.core.models import AssociationSettings

    return render(request, "facilities/issue_new.html", {"form": form, "association": AssociationSettings.load()})


@panel_required("facilities.manage_facilities")
def panel_issues(request):
    from django.db.models import Count

    from .models import Issue

    status = request.GET.get("status", "otwarte")
    qs = Issue.objects.select_related("lane", "reported_by")
    counts = {r["status"]: r["n"] for r in qs.order_by().values("status").annotate(n=Count("pk"))}
    if status == "otwarte":
        qs = qs.filter(status__in=Issue.OPEN)
    elif status in Issue.Status.values:
        qs = qs.filter(status=status)
    tabs = [("otwarte", "Otwarte", sum(counts.get(s, 0) for s in Issue.OPEN))] + [
        (v, l, counts.get(v, 0)) for v, l in Issue.Status.choices]
    return render(request, "panel/issues.html", {"issues": qs[:200], "status": status, "tabs": tabs})


@panel_required("facilities.manage_facilities")
def panel_issue(request, pk):
    from django.core.exceptions import ValidationError

    from .forms import IssueStatusForm
    from .models import Issue
    from . import issues

    issue = get_object_or_404(Issue.objects.select_related("lane", "reported_by", "resolved_by"), pk=pk)
    form = IssueStatusForm(request.POST or None, initial={"status": issue.status})
    if request.method == "POST" and form.is_valid():
        try:
            issues.change_status(issue, form.cleaned_data["status"], request.user, form.cleaned_data["note"])
        except ValidationError as e:
            form.add_error("note", e.messages[0])
        else:
            messages.success(request, "Zapisano." + (" Zgłaszający dostanie e-mail." if issue.reported_by_id and issue.reported_by_id != request.user.pk else ""))
            return redirect("panel:issue", pk=issue.pk)
    from apps.audit.models import AuditLog

    history = AuditLog.objects.filter(object_type=Issue._meta.label_lower, object_id=str(issue.pk)).select_related("actor")[:20]
    return render(request, "panel/issue_detail.html", {"issue": issue, "form": form, "history": history})


@panel_required("facilities.manage_facilities")
def panel_issue_photo(request, pk):
    from django.http import FileResponse, Http404

    from .models import Issue

    issue = get_object_or_404(Issue, pk=pk)
    if not issue.photo:
        raise Http404
    response = FileResponse(issue.photo.open("rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store"
    return response
