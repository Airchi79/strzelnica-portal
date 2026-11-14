from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit.services import log_event, log_sensitive_access
from apps.core.models import AssociationSettings
from apps.core.permissions import panel_required

from . import exports, services
from .forms import DESCRIPTION_HINTS, CorrectionForm, RecordForm, SettingsForm
from .models import AccountingSettings, Record

PERM = "accounting.manage_accounting"


def _period(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get("rok", today.year))
        month = int(request.GET.get("miesiac", today.month))
    except ValueError:
        year, month = today.year, today.month
    if not 2000 <= year <= today.year + 1:
        year = today.year
    if not 0 <= month <= 12:
        month = today.month
    return year, month or None


def _list_url(year, month):
    return reverse("panel:accounting") + f"?rok={year}&miesiac={month or 0}"


@panel_required(PERM)
def panel_list(request):
    year, month = _period(request)
    st = services.statement(year, month)
    return render(request, "panel/accounting.html", {
        "st": st, "years": services.years(), "months": list(enumerate(exports.MONTHS))[1:], "period_label": exports.period_label(st),
        "settings_obj": AccountingSettings.load(), "kinds": Record.Kind,
    })


@panel_required(PERM)
def panel_add(request, kind):
    kinds = {"przychod": Record.Kind.REVENUE, "koszt": Record.Kind.COST}
    if kind not in kinds:
        raise Http404
    kind = kinds[kind]
    form = RecordForm(request.POST or None, request.FILES or None, kind=kind,
                      initial={"date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            record = services.add_record(
                kind=kind, day=d["date"], document=d["document"], description=d["description"], amount=d["amount"],
                column=form.column_value, by=request.user, counterparty=d["counterparty"], notes=d["notes"],
                attachment=d.get("attachment"))
        except ValidationError as e:
            form.add_error(None, e.messages[0])
        else:
            messages.success(request, f"Zapisano pod Lp. {record.lp}.")
            if "jeszcze" in request.POST:
                return redirect(request.path)
            return redirect(_list_url(record.year, record.date.month))
    return render(request, "panel/accounting_form.html", {
        "form": form, "kind": kind, "hints": DESCRIPTION_HINTS[kind],
        "title": "Nowy przychód" if kind == Record.Kind.REVENUE else "Nowy koszt",
    })


@panel_required(PERM)
def panel_detail(request, pk):
    record = get_object_or_404(Record.objects.select_related("corrects", "created_by", "sales_report"), pk=pk)
    correction = record.corrections.first()
    form = None
    if record.kind != Record.Kind.CORRECTION and not correction:
        form = CorrectionForm(initial={"date": max(record.date, timezone.localdate()) if record.year == timezone.localdate().year
                                       else record.date})
    return render(request, "panel/accounting_record.html", {"r": record, "correction": correction, "form": form})


@panel_required(PERM)
@require_POST
def panel_correct(request, pk):
    record = get_object_or_404(Record, pk=pk)
    form = CorrectionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Podaj datę i przyczynę korekty.")
        return redirect("panel:accounting_record", pk=pk)
    try:
        fix = services.correct(record, reason=form.cleaned_data["reason"], day=form.cleaned_data["date"], by=request.user)
    except ValidationError as e:
        messages.error(request, e.messages[0])
        return redirect("panel:accounting_record", pk=pk)
    messages.success(request, f"Dodano zapis korygujący Lp. {fix.lp}. Jeśli trzeba, wprowadź teraz zapis z poprawną treścią.")
    return redirect("panel:accounting_record", pk=fix.pk)


@panel_required(PERM)
def panel_attachment(request, pk):
    record = get_object_or_404(Record, pk=pk)
    if not record.attachment:
        raise Http404
    log_sensitive_access(None, f"ksiegowosc:{record.lp}:dowod", "podgląd skanu dowodu księgowego")
    return FileResponse(record.attachment.open("rb"), filename=f"dowod-{record.number}-{record.year}"
                        + "." + record.attachment.name.rsplit(".", 1)[-1])


@panel_required(PERM)
@require_POST
def panel_sync(request):
    settings_obj = AccountingSettings.load()
    if not settings_obj.auto_from:
        messages.error(request, "Najpierw ustaw w ustawieniach księgowości dzień, od którego przychody z systemu trafiają do ewidencji.")
        return redirect("panel:accounting_settings")
    created = services.sync_from_sales_reports()
    messages.success(request, f"Dopisano zapisów: {len(created)}." if created else "Ewidencja jest aktualna — brak nowych przychodów z systemu.")
    return redirect("panel:accounting")


@panel_required(PERM)
def panel_settings(request):
    obj = AccountingSettings.load()
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        log_event("ksiegowosc.ustawienia", obj, f"Przychody z systemu od: {obj.auto_from or 'wyłączone'}", actor=request.user)
        messages.success(request, "Zapisano ustawienia księgowości.")
        return redirect("panel:accounting_settings")
    return render(request, "panel/accounting_settings.html", {"form": form})


@panel_required(PERM)
def panel_export(request, fmt):
    year, month = _period(request)
    st = services.statement(year, month)
    association = AssociationSettings.load()
    log_sensitive_access(None, f"ksiegowosc:{year}-{month or 0}:{fmt}", "wydruk zestawienia przychodów i kosztów")
    if fmt == "pdf":
        response = HttpResponse(exports.to_pdf(st, association), content_type="application/pdf")
    elif fmt == "xlsx":
        response = HttpResponse(exports.to_xlsx(st, association),
                                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        raise Http404
    response["Content-Disposition"] = f'attachment; filename="{exports.filename(st, fmt)}"'
    return response
