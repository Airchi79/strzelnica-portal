from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.views import _safe_next
from apps.audit.context import client_ip
from apps.audit.services import log_event
from apps.core.permissions import panel_required

from .forms import AcceptForm, VersionForm
from .models import Acceptance, RegulationDocument, RegulationVersion, pending_acceptances


def public_list(request):
    """Lista dokumentów; zalogowany strzelec widzi przy każdym stan: zaakceptowano (✓), do akceptacji (✕),
    do zapoznania (dokumenty bez akceptacji) albo „w przygotowaniu”."""
    docs = [(d, d.current_version()) for d in RegulationDocument.objects.all()]
    accepted = {}
    if request.user.is_authenticated:
        accepted = dict(Acceptance.objects.filter(user=request.user, version__in=[v for _, v in docs if v])
                        .values_list("version_id", "accepted_at"))
    rows = []
    for doc, version in docs:
        if version is None:
            state = "draft"
        elif not request.user.is_authenticated:
            state = ""
        elif not doc.requires_acceptance:
            state = "info"
        else:
            state = "done" if version.pk in accepted else "missing"
        rows.append({"doc": doc, "version": version, "state": state, "accepted_at": accepted.get(version.pk) if version else None})
    return render(request, "regulations/public_list.html", {"rows": rows})


def version_detail(request, pk):
    version = get_object_or_404(RegulationVersion.objects.select_related("document"), pk=pk, published_at__isnull=False)
    return render(request, "regulations/version_detail.html", {"version": version})


@login_required
def accept(request):
    pending = pending_acceptances(request.user)
    if not pending:
        return redirect(_safe_next(request))
    form = AcceptForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        ip = client_ip(request)
        for version in pending:
            Acceptance.objects.get_or_create(user=request.user, version=version, defaults={"ip": ip})
            log_event("regulamin.akceptacja", version, str(version))
        messages.success(request, "Dziękujemy, akceptacja została zapisana.")
        return redirect(_safe_next(request))
    return render(request, "regulations/accept.html", {"pending": pending, "form": form, "next": request.GET.get("next", "")})


# --- panel zarządu ---------------------------------------------------------


@panel_required("regulations.publish_regulation")
def panel_list(request):
    docs = []
    for doc in RegulationDocument.objects.prefetch_related("versions"):
        docs.append((doc, doc.current_version(), doc.versions.all()))
    return render(request, "panel/regulations_list.html", {"docs": docs})


@panel_required("regulations.publish_regulation")
def panel_version_edit(request, doc_pk, pk=None):
    doc = get_object_or_404(RegulationDocument, pk=doc_pk)
    if pk:
        version = get_object_or_404(RegulationVersion, pk=pk, document=doc)
        if version.is_published:
            messages.error(request, "Opublikowanej wersji nie można zmieniać. Utwórz nową wersję.")
            return redirect("panel:regulations")
    else:
        version = RegulationVersion(document=doc, created_by=request.user)
        latest = doc.versions.order_by("-number").first()
        if latest and request.method == "GET":
            version.content = latest.content
    form = VersionForm(request.POST or None, instance=version)
    if request.method == "POST" and form.is_valid():
        version = form.save(commit=False)
        if not version.number:
            with transaction.atomic():
                RegulationDocument.objects.select_for_update().get(pk=doc.pk)
                version.number = (doc.versions.aggregate(m=Max("number"))["m"] or 0) + 1
                version.save()
        else:
            version.save()
        log_event("regulamin.wersja_zapis", version, str(version))
        messages.success(request, "Zapisano wersję roboczą.")
        return redirect("panel:regulations")
    return render(request, "panel/regulation_version_edit.html", {"form": form, "doc": doc, "version": version})


@panel_required("regulations.publish_regulation")
@require_POST
def panel_version_publish(request, pk):
    version = get_object_or_404(RegulationVersion.objects.select_for_update(), pk=pk)
    if version.is_published:
        messages.info(request, "Ta wersja jest już opublikowana.")
    else:
        version.published_at = timezone.now()
        # save() blokuje zmiany wersji opublikowanych, dlatego publikacja idzie aktualizacją wprost.
        RegulationVersion.objects.filter(pk=version.pk, published_at__isnull=True).update(published_at=version.published_at)
        log_event("regulamin.publikacja", version, str(version))
        messages.success(request, f"Opublikowano: {version}. Użytkownicy zostaną poproszeni o akceptację od {timezone.localtime(version.effective_from):%d.%m.%Y %H:%M}.")
    return redirect("panel:regulations")


@panel_required("regulations.publish_regulation")
@require_POST
def panel_version_delete(request, pk):
    version = get_object_or_404(RegulationVersion, pk=pk)
    try:
        version.delete()
    except ValidationError as e:
        messages.error(request, e.messages[0])
    else:
        log_event("regulamin.wersja_usuniecie", summary=str(version))
        messages.success(request, "Usunięto wersję roboczą.")
    return redirect("panel:regulations")
