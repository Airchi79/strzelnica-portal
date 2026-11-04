from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.audit.services import log_event, log_sensitive_access
from apps.core.permissions import panel_required

from . import services
from .forms import CredentialForm, ReviewForm
from .models import Credential


@login_required
def my_list(request):
    credentials = list(request.user.credentials.all())
    ps = [c for c in credentials if c.kind == Credential.Kind.RANGE_OFFICER]
    return render(
        request,
        "credentials/my_list.html",
        {
            "credentials": credentials,
            "ps_valid": any(c.is_valid for c in ps),
            "ps_pending": any(c.effective_status == Credential.Status.PENDING for c in ps),
        },
    )


@login_required
def my_edit(request, pk=None):
    credential = get_object_or_404(Credential, pk=pk, user=request.user) if pk else Credential(user=request.user)
    initial = {}
    if not pk and request.GET.get("rodzaj") in Credential.Kind.values:
        initial["kind"] = request.GET["rodzaj"]  # np. z listy kontrolnej „Dodaj uprawnienia PS”
    elif not pk and not request.user.credentials.filter(kind=Credential.Kind.RANGE_OFFICER).exists():
        initial["kind"] = Credential.Kind.RANGE_OFFICER
    form = CredentialForm(request.POST or None, request.FILES or None, instance=credential, user=request.user, initial=initial)
    if request.method == "POST" and form.is_valid():
        if pk and "scan" in form.changed_data:
            old = Credential.objects.get(pk=pk)
            if old.scan:
                old.scan.delete(save=False)
        services.submit(form.save(commit=False), form.changed_data)
        messages.success(request, "Zapisano. Dokument czeka na weryfikację przez zarząd.")
        return redirect("credentials:my_list")
    return render(
        request, "credentials/my_edit.html", {"form": form, "credential": credential, "kind_hints": form.hints_json(), "max_upload": settings.MAX_UPLOAD_BYTES}
    )


@login_required
@require_POST
def my_delete(request, pk):
    credential = get_object_or_404(Credential, pk=pk, user=request.user)
    if credential.status == Credential.Status.VERIFIED:
        messages.error(request, "Zweryfikowanego dokumentu nie można usunąć. Skontaktuj się z zarządem.")
        return redirect("credentials:my_list")
    services.delete_scan(credential)
    log_event("dokument.usuniecie", credential, str(credential))
    credential.delete()
    messages.success(request, "Usunięto dokument.")
    return redirect("credentials:my_list")


# --- panel zarządu ---------------------------------------------------------


@panel_required("credentials.review_credential")
def panel_list(request):
    status = request.GET.get("status", Credential.Status.PENDING)
    qs = Credential.objects.select_related("user", "reviewed_by")
    if status in Credential.Status.values:
        qs = qs.filter(status=status)
    page = Paginator(qs.order_by("created_at"), 30).get_page(request.GET.get("page"))
    return render(
        request, "panel/credentials_list.html", {"page": page, "status": status, "statuses": Credential.Status.choices}
    )


@panel_required("credentials.review_credential")
def panel_detail(request, pk):
    credential = get_object_or_404(Credential.objects.select_related("user"), pk=pk)
    form = ReviewForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.review(credential, request.user, form.cleaned_data["decision"] == "approve", form.cleaned_data["note"])
        except ValueError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "Zapisano decyzję. Strzelec otrzyma powiadomienie e-mail.")
        return redirect("panel:credentials")
    if request.method == "GET":
        log_sensitive_access(credential.user, f"dokument:{credential.pk}", "weryfikacja dokumentu")
    return render(request, "panel/credential_detail.html", {"credential": credential, "form": form})


@panel_required("credentials.review_credential")
def panel_scan(request, pk):
    credential = get_object_or_404(Credential, pk=pk)
    if not credential.scan:
        raise Http404
    log_sensitive_access(credential.user, f"skan:{credential.pk}", "podgląd skanu do weryfikacji")
    response = FileResponse(credential.scan.open("rb"), filename=credential.scan.name.rsplit("/", 1)[-1])
    response["Content-Disposition"] = response["Content-Disposition"].replace("attachment", "inline")
    response["Cache-Control"] = "no-store"
    return response
