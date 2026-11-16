from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from apps.audit.models import AuditLog
from apps.audit.services import log_event, log_sensitive_access
from apps.core.permissions import panel_required

from . import services
from .forms import CredentialForm, RevokeForm, ReviewForm
from .models import Credential


OCR_LIMIT = 10  # odczytów zdjęcia na osobę w ciągu OCR_WINDOW (odczyt obciąża serwer)
OCR_WINDOW = 600


@login_required
@require_POST
def my_read_photo(request):
    """Odczyt danych z wgranego zdjęcia albo PDF legitymacji do formularza (to samo rozpoznawanie tekstu co w kiosku).
    Zwraca tylko pola dokumentu — odczytany tekst (z PESEL) nie jest zapisywany ani odsyłany; zdjęcie nie jest zapisywane."""
    from django.core.cache import cache
    from django.http import JsonResponse

    from apps.kiosk import ocr

    from .forms import ALLOWED_SIGNATURES

    key = f"ocr-legitymacja:{request.user.pk}"
    used = cache.get(key, 0)
    if used >= OCR_LIMIT:
        return JsonResponse({"ok": False, "error": "Za dużo prób odczytu — wpisz dane ręcznie albo spróbuj za kilka minut."}, status=429)
    cache.set(key, used + 1, OCR_WINDOW)
    photo = request.FILES.get("scan")
    if photo is None or photo.size > settings.MAX_UPLOAD_BYTES:
        return JsonResponse({"ok": False, "error": "Wybierz zdjęcie legitymacji (do 5 MB)."}, status=400)
    data = photo.read()
    kind = next((ext for sig, ext in ALLOWED_SIGNATURES.items() if data.startswith(sig)), None)
    if kind is None:
        return JsonResponse({"ok": False, "error": "Wybierz zdjęcie (JPG/PNG) albo plik PDF legitymacji."}, status=400)
    if kind == ".pdf":
        text = ocr.read_pdf_text(data)  # PDF z warstwą tekstową (np. z portalu PZSS) — bez programu do odczytu obrazu
    elif not ocr.available():
        return JsonResponse({"ok": False, "error": "Odczyt zdjęć jest chwilowo niedostępny — wpisz dane ręcznie."})
    else:
        text = ocr.read_text(data)
    fields = ocr.extract(text)
    del text, data  # tekst legitymacji (PESEL) nie wychodzi poza tę funkcję
    found = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in fields.items() if v}
    return JsonResponse({"ok": True, "fields": found})


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
            "soon": timezone.localdate() + timedelta(days=30),
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
        saved = form.save(commit=False)
        services.submit(saved, form.changed_data)
        if saved.status == Credential.Status.VERIFIED:
            messages.success(request, "Zapisano. Twoje uprawnienia zostały zweryfikowane wcześniej — możesz od razu rezerwować.")
        else:
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


# (krótka etykieta przycisku, tekst wstawiany do uwag dla strzelca)
REJECT_REASONS = [
    ("Nieczytelne zdjęcie", "Zdjęcie jest nieczytelne — dodaj wyraźniejsze zdjęcie dokumentu."),
    ("Zły numer", "Numer nie zgadza się z dokumentem."),
    ("Inne nazwisko", "Imię i nazwisko na dokumencie nie zgadza się z danymi konta."),
    ("Dokument nieważny", "Dokument jest nieważny."),
    ("Brak zdjęcia", "Brak zdjęcia — dodaj zdjęcie dokumentu albo okaż go na strzelnicy."),
]


EXPIRING = "wygasa"  # zakładka: zweryfikowane, ważne jeszcze najwyżej 30 dni
AUTO = "automatycznie"  # zakładka: zatwierdzone automatycznie w kiosku — do przejrzenia przez obsługę


def _pending_queue():
    """Dokumenty do weryfikacji — najpierw legitymacje osób czekających przy kiosku (rezerwacja na miejscu)."""
    from django.db.models import Exists, OuterRef

    from apps.kiosk.models import OnsiteBooking

    waiting = OnsiteBooking.objects.filter(credential=OuterRef("pk"), status=OnsiteBooking.Status.AWAITING)
    return (Credential.objects.filter(status=Credential.Status.PENDING).annotate(at_kiosk=Exists(waiting))
            .order_by("-at_kiosk", "created_at", "pk"))


@panel_required("credentials.review_credential")
def panel_list(request):
    status = request.GET.get("status", Credential.Status.PENDING)
    kind = request.GET.get("rodzaj", "")
    q = request.GET.get("q", "").strip()
    qs = Credential.objects.select_related("user", "reviewed_by")
    if q:
        qs = qs.filter(
            Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q)
            | Q(user__email__icontains=q) | Q(number__icontains=q)
        )
    if kind in Credential.Kind.values:
        qs = qs.filter(kind=kind)
    counts = {r["status"]: r["n"] for r in qs.order_by().values("status").annotate(n=Count("pk"))}
    today = timezone.localdate()
    expiring = qs.filter(status=Credential.Status.VERIFIED, valid_until__gte=today, valid_until__lte=today + timedelta(days=30))
    from apps.kiosk.onsite import AUTO_NOTE

    auto = qs.filter(status=Credential.Status.VERIFIED, reviewed_by__isnull=True).filter(
        Q(review_note=AUTO_NOTE) | Q(review_note__contains="lista PS zweryfikowanych"))
    if status == AUTO:
        qs = auto.order_by("-reviewed_at")
    elif status == EXPIRING:
        qs = expiring.order_by("valid_until")  # najbliższy koniec ważności na górze
    elif status == Credential.Status.PENDING:
        qs = qs.filter(status=status).order_by("created_at", "pk")  # najdłużej czekające na górze
    elif status in Credential.Status.values:
        qs = qs.filter(status=status).order_by("-reviewed_at", "-updated_at")
    else:
        qs = qs.order_by("-reviewed_at", "-updated_at")
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    tabs = [(value, label, counts.get(value, 0)) for value, label in Credential.Status.choices]
    tabs.insert(2, (EXPIRING, "Wygasa w 30 dni", expiring.count()))
    tabs.insert(3, (AUTO, "Zatwierdzone automatycznie", auto.count()))
    return render(
        request,
        "panel/credentials_list.html",
        {"page": page, "status": status, "tabs": tabs, "kind": kind, "q": q, "kinds": Credential.Kind.choices,
         "stale_before": timezone.now() - timedelta(days=2)},
    )


@panel_required("credentials.review_credential")
def panel_detail(request, pk):
    credential = get_object_or_404(Credential.objects.select_related("user", "reviewed_by"), pk=pk)
    form = ReviewForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.review(credential, request.user, form.cleaned_data["decision"] == "approve", form.cleaned_data["note"])
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("panel:credentials")
        messages.success(request, f"Zapisano decyzję ({credential.user.full_name}). Strzelec otrzyma powiadomienie e-mail.")
        nxt = _pending_queue().exclude(pk=pk).first()
        if nxt and request.POST.get("then") == "next":
            return redirect("panel:credential", nxt.pk)
        return redirect("panel:credentials")
    if request.method == "GET":
        log_sensitive_access(credential.user, f"dokument:{credential.pk}", "weryfikacja dokumentu")
    queue = list(_pending_queue().values_list("pk", flat=True))
    others = credential.user.credentials.exclude(pk=pk).select_related("reviewed_by")
    history = AuditLog.objects.filter(object_type=Credential._meta.label_lower, object_id=str(pk)).select_related("actor")[:20]
    upcoming = []
    if credential.status == Credential.Status.VERIFIED:
        from apps.bookings.models import Reservation

        upcoming = Reservation.objects.filter(
            user=credential.user, starts_at__gte=timezone.now(),
            status__in=[Reservation.Status.CONFIRMED, Reservation.Status.PENDING_APPROVAL, Reservation.Status.HOLD],
        ).select_related("lane").order_by("starts_at")[:10]
    from apps.kiosk.models import OnsiteBooking

    onsite = (OnsiteBooking.objects.filter(credential=credential, status=OnsiteBooking.Status.AWAITING)
              .select_related("reservation__lane", "device").first())
    scan_kind = ""
    if credential.scan:
        scan_kind = "pdf" if credential.scan.name.lower().endswith(".pdf") else "image"
    return render(
        request,
        "panel/credential_detail.html",
        {
            "credential": credential,
            "form": form,
            "others": others,
            "history": history,
            "scan_kind": scan_kind,
            "reject_reasons": REJECT_REASONS,
            "queue_pos": queue.index(pk) + 1 if pk in queue else None,
            "queue_len": len(queue),
            "has_next": any(x != pk for x in queue),
            "revoke_form": RevokeForm(),
            "upcoming": upcoming,
            "onsite": onsite,
        },
    )


@panel_required("credentials.review_credential")
@require_POST
def panel_revoke(request, pk):
    credential = get_object_or_404(Credential, pk=pk)
    form = RevokeForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Podaj powód cofnięcia weryfikacji.")
        return redirect("panel:credential", pk)
    try:
        services.revoke(credential, request.user, form.cleaned_data["reason"])
    except ValueError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, "Cofnięto weryfikację. Strzelec otrzyma e-mail z powodem.")
    return redirect("panel:credential", pk)


@panel_required("credentials.review_credential")
@xframe_options_sameorigin  # podgląd PDF osadzony na stronie weryfikacji
def panel_scan(request, pk):
    credential = get_object_or_404(Credential, pk=pk)
    if not credential.scan:
        raise Http404
    log_sensitive_access(credential.user, f"skan:{credential.pk}", "podgląd skanu do weryfikacji")
    response = FileResponse(credential.scan.open("rb"), filename=credential.scan.name.rsplit("/", 1)[-1])
    response["Content-Disposition"] = response["Content-Disposition"].replace("attachment", "inline")
    response["Cache-Control"] = "no-store"
    return response


@panel_required("credentials.review_credential")
def panel_preverified(request):
    """Lista PS zweryfikowanych wcześniej (GunTracker): import z podglądem, stan wykorzystania."""
    from apps.audit.services import log_sensitive_access
    from django.core.exceptions import ValidationError

    from . import preverified
    from .models import PreverifiedOfficer

    report = None
    preview = True
    if request.method == "POST":
        uploaded = request.FILES.get("plik")
        preview = request.POST.get("tryb") != "importuj"
        if not uploaded:
            messages.error(request, "Wybierz plik XLSX.")
        else:
            try:
                report = preverified.run(uploaded, by=request.user, preview=preview,
                                         approve_client=request.POST.get("zatwierdz_klienta") == "1",
                                         fill_emails=request.POST.get("uzupelnij_emaile") == "1")
            except ValidationError as e:
                messages.error(request, " ".join(e.messages))
            else:
                if not preview:
                    messages.success(request, "Lista zapisana.")
    entries = list(PreverifiedOfficer.objects.select_related("applied_user"))
    status = preverified.kartoteka_status(entries)
    for e in entries:
        e.kartoteka_state, e.kartoteka_text = status[e.email]
    summary = {}
    for state, _ in status.values():
        summary[state] = summary.get(state, 0) + 1
    log_sensitive_access(None, "ps-zweryfikowani-wczesniej", "lista PS zweryfikowanych wcześniej")
    counts = {}
    for status, _ in report or []:
        counts[status] = counts.get(status, 0) + 1
    return render(request, "panel/credentials_preverified.html", {
        "entries": entries, "report": report, "preview": preview, "counts": counts, "summary": summary,
        "applied": sum(1 for e in entries if e.applied_user_id)})
