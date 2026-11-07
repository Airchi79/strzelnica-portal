from datetime import date, timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit.services import log_sensitive_access
from apps.core.models import AssociationSettings
from apps.core.permissions import panel_required

from . import services
from .models import SalesReport

PERM = "reports.view_salesreport"


@panel_required(PERM)
def panel_list(request):
    kind = request.GET.get("rodzaj", "")
    qs = SalesReport.objects.all()
    if kind in SalesReport.Kind.values:
        qs = qs.filter(kind=kind)
    page = Paginator(qs.select_related("generated_by"), 40).get_page(request.GET.get("page"))
    today = timezone.localdate()
    return render(request, "panel/sales_reports.html", {
        "page": page, "kind": kind, "yesterday": today - timedelta(days=1),
        "last_month": (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
    })


@panel_required(PERM)
@require_POST
def panel_generate(request):
    try:
        if request.POST.get("rodzaj") == SalesReport.Kind.MONTHLY:
            year, month = (int(x) for x in request.POST.get("miesiac", "").split("-"))
            report, new = services.generate(SalesReport.Kind.MONTHLY, date(year, month, 1), by=request.user)
        else:
            report, new = services.generate(SalesReport.Kind.DAILY, date.fromisoformat(request.POST.get("dzien", "")), by=request.user)
    except (ValueError, TypeError):
        messages.error(request, "Podaj poprawną datę.")
        return redirect("panel:sales_reports")
    except ValidationError as e:
        messages.error(request, e.messages[0])
        return redirect("panel:sales_reports")
    messages.success(request, f"Wygenerowano raport {report.number}." if new else f"Raport za ten okres już istnieje: {report.number}.")
    return redirect("panel:sales_report", pk=report.pk)


@panel_required(PERM)
def panel_detail(request, pk):
    report = get_object_or_404(SalesReport, pk=pk)
    rows = [{**r, "category_label": services.CATEGORY_LABELS.get(r["category"], r["category"]),
             "method_label": services.METHOD_LABELS.get(r["method"], r["method"])} for r in report.data["rows"]]
    s = report.data["summary"]
    return render(request, "panel/sales_report.html", {
        "r": report, "rows": rows, "s": s,
        "categories": [(services.CATEGORY_LABELS.get(k, k), v) for k, v in sorted(s["by_category"].items())],
        "methods": [(services.METHOD_LABELS.get(k, k), v) for k, v in sorted(s["by_method"].items())],
    })


def _filename(report, ext):
    return report.number.replace("/", "-") + "." + ext


@panel_required(PERM)
def panel_csv(request, pk):
    report = get_object_or_404(SalesReport, pk=pk)
    log_sensitive_access(None, f"raport:{report.number}:csv", "pobranie raportu wpływów")
    response = HttpResponse(services.to_csv(report), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{_filename(report, "csv")}"'
    return response


@panel_required(PERM)
def panel_pdf(request, pk):
    report = get_object_or_404(SalesReport, pk=pk)
    log_sensitive_access(None, f"raport:{report.number}:pdf", "pobranie raportu wpływów")
    response = HttpResponse(services.to_pdf(report, AssociationSettings.load()), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{_filename(report, "pdf")}"'
    return response
