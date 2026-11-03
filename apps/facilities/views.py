from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

from apps.audit.services import diff, log_event
from apps.core.permissions import panel_required

from .forms import FacilityForm, HoursFormSet, LaneForm, NewLaneForm, PriceRuleForm
from .models import Facility, Lane, OpeningHours, PriceRule


def public_lanes(request):
    today = timezone.localdate()
    lanes = [(lane, lane.price_rule_for(today)) for lane in Lane.objects.filter(is_active=True, facility__is_active=True)
             .select_related("facility").prefetch_related("opening_hours")]
    return render(request, "facilities/public_lanes.html", {"lanes": lanes})


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
        messages.success(request, "Dodano cennik. Wcześniejsze rezerwacje zachowują swoją cenę.")
        return redirect("panel:lane", pk=lane.pk)
    return render(request, "panel/price_add.html", {"lane": lane, "form": form})


# --- obiekty (strzelnice) i osie: dodawanie, edycja, usuwanie ----------------------------------------


def _future_reservations(lane):
    from apps.bookings.models import Reservation

    return lane.reservations.filter(ends_at__gt=timezone.now(), status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING_APPROVAL])


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
        messages.success(request, f"Dodano oś {lane}. Sprawdź godziny otwarcia i cennik.")
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
