from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.audit.services import diff, log_event
from apps.core.permissions import panel_required

from .forms import HoursFormSet, LaneForm, PriceRuleForm
from .models import Facility, Lane


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
        return redirect("panel:lanes")
    return render(
        request, "panel/lane_edit.html", {"lane": lane, "form": form, "hours": hours, "rules": lane.price_rules.all()}
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
