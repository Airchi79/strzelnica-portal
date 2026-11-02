from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.audit.services import log_event, log_sensitive_access
from apps.core.permissions import panel_required

from . import services
from .forms import PassForm, PassPaidForm, PersonForm, ProfileForm
from .models import ClientPass, ClientTag, Person

User = get_user_model()
LIST_LIMIT = 5000


@panel_required("accounts.view_users")
def panel_list(request):
    f = {k: request.GET.get(k, "").strip() for k in ("q", "typ", "ps", "tag")}
    f["nieaktywne"] = request.GET.get("nieaktywne") == "1"
    base = User.objects.all() if f["nieaktywne"] else User.objects.filter(is_active=True)
    qs = services.search_users(base, f["q"])
    if f["tag"]:
        qs = qs.filter(client_profile__tags__name=f["tag"])
    users = services.annotate_clients(qs.order_by("last_name", "first_name")[:LIST_LIMIT])
    if f["typ"] in services.TYPE_LABELS:
        users = [u for u in users if u.client_type == f["typ"]]
    if f["ps"]:
        users = [u for u in users if u.ps_status == f["ps"]]
    page = Paginator(users, 30).get_page(request.GET.get("page"))
    return render(request, "panel/users_list.html", {
        "page": page, "f": f, "types": services.TYPE_LABELS.items(), "tags": ClientTag.objects.all(),
        "ps_choices": [("wazne", "ważne"), ("do_weryfikacji", "do weryfikacji"), ("wygasle", "wygasłe"), ("brak", "brak")],
    })


def _back(pk, anchor=""):
    response = redirect("panel:user", pk=pk)
    response["Location"] += anchor
    return response


@panel_required("clients.manage_clients")
@require_POST
def profile_update(request, pk):
    subject = get_object_or_404(User, pk=pk)
    form = ProfileForm(request.POST)
    if form.is_valid():
        services.update_profile(subject, notes=form.cleaned_data["notes"], tag_names=form.cleaned_data["tags"], by=request.user)
        messages.success(request, "Zapisano notatki i tagi.")
    else:
        for errs in form.errors.values():
            messages.error(request, errs[0])
    return _back(pk, "#kartoteka")


@panel_required("clients.manage_clients")
@require_POST
def approve(request, pk):
    subject = get_object_or_404(User, pk=pk)
    approved = request.POST.get("approved") == "1"
    services.set_approved(subject, approved, request.user)
    messages.success(request, "Klient zatwierdzony jako stały." if approved else "Cofnięto zatwierdzenie klienta.")
    return _back(pk, "#kartoteka")


@panel_required("clients.manage_clients")
def pass_new(request, pk):
    subject = get_object_or_404(User, pk=pk)
    form = PassForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            p = services.issue_pass(
                subject, kind=d["kind"], valid_from=d["valid_from"], valid_until=d["valid_until"], by=request.user,
                entries_limit=d.get("entries_limit"), max_hours=d["max_hours"], max_persons=d["max_persons"], lanes=d["lanes"],
                price=d["price_gr"], paid_with=d.get("paid_with", ""), note=d.get("note", ""),
            )
        except ValidationError as e:
            for m in e.messages:
                form.add_error(None, m)
        else:
            messages.success(request, f"Wydano kod wejścia {p.code}. Przekaż go klientowi — zobaczy go też na swoim pulpicie.")
            return _back(pk, "#kody")
    from apps.bookings.models import GroupRule

    group = services.client_type(subject)
    rule = GroupRule.for_group(group)
    return render(request, "panel/client_pass_new.html", {
        "subject": subject, "form": form, "group_label": services.TYPE_LABELS[group], "pass_allowed": rule.pay_pass,
    })


@panel_required("clients.manage_clients")
@require_POST
def pass_action(request, pk):
    p = get_object_or_404(ClientPass, pk=pk)
    try:
        if request.POST.get("action") == "paid":
            form = PassPaidForm(request.POST)
            if not form.is_valid():
                raise ValidationError("Wybierz sposób zapłaty.")
            services.mark_pass_paid(p, form.cleaned_data["paid_with"], request.user)
            messages.success(request, f"Kod {p.code} oznaczono jako opłacony.")
        elif request.POST.get("action") == "deactivate":
            services.deactivate_pass(p, request.user)
            messages.success(request, f"Kod {p.code} wyłączono.")
        else:
            raise ValidationError("Nieznana operacja.")
    except ValidationError as e:
        messages.error(request, e.messages[0])
    return _back(p.user_id, "#kody")


# --- osoby z kartoteki bez konta (np. z GunTrackera) ------------------------------------------------


@panel_required("accounts.view_users")
def persons_list(request):
    from apps.logbook.services import mask

    params = request.POST if request.method == "POST" else request.GET
    q = params.get("q", "").strip()
    qs = Person.objects.all()
    if q:
        qs = qs.filter(Q(last_name__icontains=q) | Q(first_name__icontains=q) | Q(permit_number__icontains=q.replace(" ", "").upper()))
    if params.get("niepelne") == "1":
        qs = qs.filter((Q(permit_number="") & Q(address="")) | (~Q(permit_number="") & Q(permit_issuer="")))
    page = Paginator(qs, 50).get_page(params.get("page"))
    reveal = request.method == "POST" and request.POST.get("pokaz") == "1"
    people = list(page)
    for p in people:
        p.masked = f"pozwolenie {mask(p.permit_number)}" if p.permit_number else (f"adres {mask(p.address, 3, 0)}" if p.address else "—")
        if reveal:
            log_sensitive_access(None, f"kartoteka:osoba:{p.pk}", "odkrycie danych osoby z kartoteki")
    return render(request, "panel/persons_list.html", {
        "page": page, "people": people, "q": q, "reveal": reveal, "niepelne": params.get("niepelne") == "1", "total": Person.objects.count(),
    })


@panel_required("clients.manage_clients")
def person_edit(request, pk=None):
    person = get_object_or_404(Person, pk=pk) if pk else None
    form = PersonForm(request.POST or None, instance=person)
    if request.method == "POST":
        if request.POST.get("action") == "delete" and person:
            log_event("klient.usuniecie_osoby", person, person.full_name, actor=request.user)
            person.delete()
            messages.success(request, "Usunięto osobę z kartoteki.")
            return redirect("panel:persons")
        if form.is_valid():
            obj = form.save(commit=False)
            if not pk:
                obj.created_by = request.user
            obj.save()
            log_event("klient.osoba_zapis", obj, obj.full_name, actor=request.user)
            messages.success(request, "Zapisano.")
            return redirect("panel:persons")
    elif person:
        log_sensitive_access(person.user, f"kartoteka:osoba:{person.pk}", "edycja osoby z kartoteki")
    return render(request, "panel/person_edit.html", {"form": form, "person": person})


@panel_required("clients.manage_clients")
def persons_import(request):
    from . import importer

    report = preview = None
    if request.method == "POST":
        uploaded = request.FILES.get("plik")
        preview = request.POST.get("tryb") != "importuj"
        if not uploaded:
            messages.error(request, "Wybierz plik XLSX.")
        else:
            try:
                report = importer.run(uploaded, by=request.user, preview=preview)
            except ValidationError as e:
                messages.error(request, " ".join(e.messages))
            else:
                if not preview:
                    messages.success(request, "Import zakończony.")
    counts = {}
    for status, _ in report or []:
        counts[status] = counts.get(status, 0) + 1
    return render(request, "panel/persons_import.html", {"report": report, "preview": preview, "counts": counts})
