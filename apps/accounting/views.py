from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit.services import log_event, log_sensitive_access
from apps.core.models import AssociationSettings
from apps.core.permissions import panel_required

from . import documents, exports, ksef, services
from apps.audit.models import AuditLog

from .forms import (DESCRIPTION_HINTS, AssetForm, DisposeForm, KsefAcceptForm, KsefImportForm, PostingForm, ReasonForm, RecordForm,
                    SettingsForm, format_amount)
from .models import AccountingSettings, AccountingYear, DepreciationPosting, FixedAsset, GeneratedDocument, KsefInvoice, Record

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


def _error(request, e):
    messages.error(request, e.messages[0] if hasattr(e, "messages") else str(e))


# --- zestawienie i zapisy ---------------------------------------------------------------


@panel_required(PERM)
def panel_list(request):
    year, month = _period(request)
    st = services.statement(year, month)
    return render(request, "panel/accounting.html", {
        "st": st, "years": services.years(), "months": list(enumerate(exports.MONTHS))[1:], "period_label": exports.period_label(st),
        "settings_obj": AccountingSettings.load(), "kinds": Record.Kind,
        "ksef_new": KsefInvoice.objects.filter(status=KsefInvoice.Status.NEW).count(),
    })


@panel_required(PERM)
def panel_add(request, kind):
    kinds = {"przychod": Record.Kind.REVENUE, "koszt": Record.Kind.COST}
    if kind not in kinds:
        raise Http404
    kind = kinds[kind]
    # podpowiedź z generatora dokumentów (dowód wewnętrzny) — tylko wartości początkowe formularza
    prefill = {k: request.GET[k][:255] for k in ("date", "document", "description", "amount", "notes") if request.GET.get(k)}
    form = RecordForm(request.POST or None, request.FILES or None, kind=kind,
                      initial={"date": timezone.localdate(), **prefill})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            record = services.add_record(
                kind=kind, day=d["date"], document=d["document"], description=d["description"], amount=d["amount"],
                column=form.column_value, by=request.user, counterparty=d["counterparty"], notes=d["notes"],
                attachment=d.get("attachment"), ksef_number=d.get("ksef_number", ""))
        except ValidationError as e:
            form.add_error(None, e.messages[0])
        else:
            messages.success(request, f"Zapisano pod Lp. {record.lp}. Oznacz tym numerem dowód księgowy.")
            if "jeszcze" in request.POST:
                return redirect(request.path)
            return redirect(_list_url(record.year, record.date.month))
    return render(request, "panel/accounting_form.html", {
        "form": form, "kind": kind, "hints": DESCRIPTION_HINTS[kind],
        "title": "Nowy przychód" if kind == Record.Kind.REVENUE else "Nowy koszt",
    })


@panel_required(PERM)
def panel_detail(request, pk):
    record = get_object_or_404(Record.objects.select_related("created_by", "sales_report", "removed_by", "updated_by"), pk=pk)
    return render(request, "panel/accounting_record.html", {
        "r": record, "history": services.history(record), "closed": AccountingYear.is_closed(record.year),
        "invoices": record.ksef_invoices.all(), "remove_form": ReasonForm(),
    })


@panel_required(PERM)
def panel_edit(request, pk):
    record = get_object_or_404(Record, pk=pk)
    if record.is_removed or AccountingYear.is_closed(record.year):
        messages.error(request, "Tego zapisu nie można zmienić (usunięty albo rok zamknięty).")
        return redirect("panel:accounting_record", pk=pk)
    form = RecordForm(request.POST or None, request.FILES or None, kind=record.kind, editing=True,
                      initial=RecordForm.initial_for(record))
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            services.update_record(record, by=request.user, reason=d["reason"], day=d["date"], document=d["document"],
                                   description=d["description"], amount=d["amount"], column=form.column_value,
                                   counterparty=d["counterparty"], notes=d["notes"], ksef_number=d.get("ksef_number", ""),
                                   attachment=d.get("attachment"))
        except ValidationError as e:
            form.add_error(None, e.messages[0])
        else:
            messages.success(request, f"Zmieniono zapis Lp. {record.lp}. Poprzednia treść jest w historii zapisu.")
            return redirect("panel:accounting_record", pk=pk)
    return render(request, "panel/accounting_form.html", {"form": form, "kind": record.kind, "record": record,
                                                          "hints": DESCRIPTION_HINTS.get(record.kind, []),
                                                          "title": f"Zmiana zapisu Lp. {record.lp}"})


@panel_required(PERM)
@require_POST
def panel_remove(request, pk):
    record = get_object_or_404(Record, pk=pk)
    form = ReasonForm(request.POST)
    try:
        if not form.is_valid():
            raise ValidationError("Podaj przyczynę usunięcia.")
        services.remove_record(record, by=request.user, reason=form.cleaned_data["reason"])
    except ValidationError as e:
        _error(request, e)
    else:
        messages.success(request, f"Usunięto zapis Lp. {record.lp} z ewidencji (numer i historia zostają).")
    return redirect("panel:accounting_record", pk=pk)


@panel_required(PERM)
@require_POST
def panel_restore(request, pk):
    record = get_object_or_404(Record, pk=pk)
    try:
        services.restore_record(record, by=request.user)
    except ValidationError as e:
        _error(request, e)
    else:
        messages.success(request, f"Przywrócono zapis Lp. {record.lp}.")
    return redirect("panel:accounting_record", pk=pk)


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


@panel_required(PERM)
def panel_years(request):
    if request.method == "POST":
        try:
            year = int(request.POST.get("rok", ""))
            if request.POST.get("akcja") == "zamknij":
                services.close_year(year, by=request.user)
                messages.success(request, f"Zamknięto rok {year}.")
            elif request.POST.get("akcja") == "otworz":
                services.reopen_year(year, by=request.user, reason=request.POST.get("powod", ""))
                messages.success(request, f"Rok {year} jest ponownie otwarty.")
        except (ValueError, ValidationError) as e:
            _error(request, e if isinstance(e, ValidationError) else ValidationError("Niepoprawny rok."))
        return redirect("panel:accounting_years")
    rows = []
    for year in services.years():
        obj = AccountingYear.objects.filter(year=year).first()
        rows.append({"year": year, "flows": services.flows(year), "closed": obj.closed_at if obj else None,
                     "closed_by": obj.closed_by if obj else None, "can_close": year < timezone.localdate().year})
    return render(request, "panel/accounting_years.html", {"rows": rows})


# --- KSeF -------------------------------------------------------------------------------


@panel_required(PERM)
def ksef_list(request):
    status = request.GET.get("status", KsefInvoice.Status.NEW)
    qs = KsefInvoice.objects.select_related("record")
    if status in KsefInvoice.Status.values:
        qs = qs.filter(status=status)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    counts = {s: KsefInvoice.objects.filter(status=s).count() for s in KsefInvoice.Status.values}
    return render(request, "panel/accounting_ksef.html", {"page": page, "status": status, "statuses": KsefInvoice.Status.choices,
                                                          "counts": counts})


@panel_required(PERM)
@require_POST
def ksef_import(request):
    form = KsefImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, form.non_field_errors()[0])
        return redirect("panel:accounting_ksef")
    created, duplicates, errors = ksef.import_files(form.cleaned_data["files"], by=request.user,
                                                    own_nip=AssociationSettings.load().nip)
    if created:
        messages.success(request, f"Zaimportowano faktur: {len(created)}.")
    if duplicates:
        messages.info(request, f"Pominięto faktury już zaimportowane: {duplicates}.")
    for err in errors[:10]:
        messages.error(request, err)
    if not created and not duplicates and not errors:
        messages.info(request, "Nie znaleziono faktur w przesłanych plikach.")
    return redirect("panel:accounting_ksef")


@panel_required(PERM)
def ksef_detail(request, pk):
    invoice = get_object_or_404(KsefInvoice.objects.select_related("record", "imported_by"), pk=pk)
    form = KsefAcceptForm(request.POST or None, invoice=invoice)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            record = ksef.accept(invoice, by=request.user, day=d["date"], column=d["column"], description=d["description"],
                                 document=d["document"], notes=d["notes"])
        except ValidationError as e:
            form.add_error(None, e.messages[0])
        else:
            messages.success(request, f"Faktura przyjęta do ewidencji pod Lp. {record.lp}.")
            return redirect("panel:accounting_ksef_invoice", pk=pk)
    return render(request, "panel/accounting_ksef_invoice.html", {
        "inv": invoice, "form": form, "reject_form": ReasonForm(), "gross": format_amount(invoice.gross),
    })


@panel_required(PERM)
@require_POST
def ksef_decide(request, pk, action):
    invoice = get_object_or_404(KsefInvoice, pk=pk)
    try:
        if action == "odrzuc":
            ksef.reject(invoice, by=request.user, note=request.POST.get("reason", ""))
            messages.success(request, "Faktura oznaczona jako poza ewidencją.")
        elif action == "przywroc":
            ksef.reopen(invoice, by=request.user)
            messages.success(request, "Faktura wraca do decyzji.")
        elif action == "usun-z-ewidencji":
            if not invoice.record_id:
                raise ValidationError("Faktura nie jest w ewidencji.")
            services.remove_record(invoice.record, by=request.user, reason=request.POST.get("reason", ""))
            messages.success(request, "Zapis usunięty z ewidencji — faktura wraca do decyzji.")
        else:
            raise Http404
    except ValidationError as e:
        _error(request, e)
    return redirect("panel:accounting_ksef_invoice", pk=pk)


@panel_required(PERM)
def ksef_xml(request, pk):
    invoice = get_object_or_404(KsefInvoice, pk=pk)
    log_sensitive_access(None, f"ksef:{invoice.pk}:xml", "pobranie pliku faktury KSeF")
    return FileResponse(invoice.xml.open("rb"), filename=f"faktura-{invoice.ksef_number or invoice.pk}.xml", content_type="application/xml")


# --- generator dokumentów ---------------------------------------------------------------


@panel_required(PERM)
def docs_index(request):
    groups = {}
    for doc in documents.DOCS.values():
        groups.setdefault(doc.group, []).append(doc)
    page = Paginator(GeneratedDocument.objects.select_related("created_by"), 30).get_page(request.GET.get("page"))
    return render(request, "panel/accounting_docs.html", {"groups": groups, "page": page, "names": {k: d.title for k, d in documents.DOCS.items()}})


@panel_required(PERM)
def doc_new(request, key):
    doc = documents.DOCS.get(key)
    if doc is None:
        raise Http404
    ctx = documents.context(request.user)
    form = documents.form_class(doc, ctx)(request.POST or None, initial=doc.initial(ctx))
    if request.method == "POST" and form.is_valid():
        obj = documents.create(doc, form.cleaned_data, by=request.user, ctx=ctx)
        messages.success(request, f"Dokument gotowy: {obj.title}.")
        return redirect("panel:accounting_doc", pk=obj.pk)
    return render(request, "panel/accounting_doc_form.html", {"doc": doc, "form": form})


@panel_required(PERM)
def doc_detail(request, pk):
    obj = get_object_or_404(GeneratedDocument.objects.select_related("created_by"), pk=pk)
    doc = documents.DOCS.get(obj.kind)
    if doc is None:
        raise Http404
    add_url = ""
    if obj.kind == "dowod_wewnetrzny":
        d = obj.data
        add_url = reverse("panel:accounting_add", args=["koszt"]) + "?" + urlencode({
            "date": d.get("data_zdarzenia", ""), "document": d.get("_number", ""), "description": d.get("przedmiot", ""),
            "amount": format_amount(d.get("wartosc")), "notes": d.get("podstawa", "")})
    return render(request, "panel/accounting_doc.html", {"obj": obj, "doc": doc, "add_url": add_url})


@panel_required(PERM)
def doc_download(request, pk, fmt):
    obj = get_object_or_404(GeneratedDocument, pk=pk)
    if obj.kind not in documents.DOCS:
        raise Http404
    if fmt == "pdf":
        response = HttpResponse(documents.to_pdf(obj), content_type="application/pdf")
    elif fmt == "docx":
        response = HttpResponse(documents.to_docx(obj),
                                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    else:
        raise Http404
    response["Content-Disposition"] = f'attachment; filename="{documents.filename(obj, fmt)}"'
    return response


# --- wykaz środków trwałych -------------------------------------------------------------


@panel_required(PERM)
def assets_list(request):
    from . import assets

    year, _ = _period(request)
    reg = assets.register(year)
    for r in reg["rows"]:
        r["late"] = assets.late_entry(r["asset"])
    postings = DepreciationPosting.objects.filter(year=year, record__removed_at__isnull=True).select_related("record")
    today = timezone.localdate()
    months = [(m, exports.MONTHS[m], assets.month_total(year, m)) for m in range(1, 13)
              if (year, m) < (today.year, today.month)]
    return render(request, "panel/accounting_assets.html", {
        "reg": reg, "years": services.years(), "postings": postings, "months": months,
        "posted_months": {p.month for p in postings}, "year_posted": any(p.month is None for p in postings),
        "can_post_year": year < today.year,
    })


@panel_required(PERM)
def asset_form(request, pk=None):
    from . import assets

    asset = get_object_or_404(FixedAsset, pk=pk) if pk else None
    initial = AssetForm.initial_for(asset) if asset else {"entered_on": timezone.localdate(), "in_use_on": timezone.localdate(),
                                                           "acquired_on": timezone.localdate()}
    form = AssetForm(request.POST or None, initial=initial, editing=bool(asset))
    if request.method == "POST" and form.is_valid():
        d = dict(form.cleaned_data)
        reason = d.pop("reason", "")
        try:
            if asset:
                posted = assets.posted_months(asset)
                assets.update(asset, d, by=request.user, reason=reason)
                if posted:
                    messages.warning(request, "Odpisy od tej pozycji są już ujęte w zestawieniu przychodów i kosztów — "
                                              "jeśli zmiana wpływa na ich wysokość, popraw te zapisy.")
                messages.success(request, f"Zmieniono pozycję wykazu nr {asset.number}.")
            else:
                asset = assets.create(d, by=request.user)
                messages.success(request, f"Wprowadzono do wykazu pod nr {asset.number}.")
                if assets.late_entry(asset):
                    messages.warning(request, "Wprowadzenie po miesiącu przyjęcia do używania — to ujawnienie środka trwałego "
                                              "(§ 3 ust. 3 rozporządzenia); odpisy liczą się od miesiąca po wprowadzeniu.")
        except ValidationError as e:
            form.add_error(None, e.messages[0])
        else:
            return redirect("panel:accounting_asset", pk=asset.pk)
    return render(request, "panel/accounting_asset_form.html", {"form": form, "asset": asset})


@panel_required(PERM)
def asset_detail(request, pk):
    from . import assets

    asset = get_object_or_404(FixedAsset.objects.select_related("created_by"), pk=pk)
    rows, prior = assets.schedule(asset)
    history = AuditLog.objects.filter(object_type="accounting.fixedasset", object_id=str(asset.pk)).select_related("actor")
    return render(request, "panel/accounting_asset.html", {
        "a": asset, "schedule": rows, "prior": prior, "late": assets.late_entry(asset), "history": history,
        "dispose_form": DisposeForm(initial={"day": timezone.localdate()}), "reason_form": ReasonForm(),
        "closed": AccountingYear.is_closed(asset.entered_on.year),
    })


@panel_required(PERM)
@require_POST
def asset_action(request, pk, action):
    from . import assets

    asset = get_object_or_404(FixedAsset, pk=pk)
    try:
        if action == "wykresl":
            form = DisposeForm(request.POST)
            if not form.is_valid():
                raise ValidationError("Uzupełnij rodzaj i datę wykreślenia.")
            d = form.cleaned_data
            assets.dispose(asset, by=request.user, day=d["day"], kind=d["kind"], reason=d["reason"], doc=d["doc"], ksef_number=d["ksef_number"])
            messages.success(request, "Pozycja wykreślona z wykazu.")
        elif action == "cofnij-wykreslenie":
            assets.cancel_disposal(asset, by=request.user, reason=request.POST.get("reason", ""))
            messages.success(request, "Cofnięto wykreślenie.")
        elif action == "usun":
            assets.delete(asset, by=request.user, reason=request.POST.get("reason", ""))
            messages.success(request, "Pozycję usunięto z wykazu.")
            return redirect("panel:accounting_assets")
        else:
            raise Http404
    except ValidationError as e:
        _error(request, e)
    return redirect("panel:accounting_asset", pk=pk)


@panel_required(PERM)
@require_POST
def assets_post(request):
    from . import assets

    form = PostingForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Niepoprawny okres.")
        return redirect("panel:accounting_assets")
    d = form.cleaned_data
    try:
        record = assets.post_depreciation(d["year"], d["month"] or None, by=request.user, column=d["column"])
    except ValidationError as e:
        _error(request, e)
    else:
        messages.success(request, f"Odpisy ujęte w zestawieniu pod Lp. {record.lp}.")
    return redirect(reverse("panel:accounting_assets") + f"?rok={d['year']}")


@panel_required(PERM)
def assets_export(request, fmt):
    from . import assets

    year, _ = _period(request)
    reg = assets.register(year)
    association = AssociationSettings.load()
    log_sensitive_access(None, f"ksiegowosc:wykaz-st:{year}:{fmt}", "wydruk wykazu środków trwałych")
    if fmt == "pdf":
        response = HttpResponse(assets.to_pdf(reg, association), content_type="application/pdf")
    elif fmt == "xlsx":
        response = HttpResponse(assets.to_xlsx(reg, association),
                                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    else:
        raise Http404
    response["Content-Disposition"] = f'attachment; filename="wykaz-srodkow-trwalych-{year}.{fmt}"'
    return response
