from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.permissions import panel_required

from . import services
from .forms import PassForm, PassPaidForm, ProfileForm
from .models import ClientPass, ClientTag

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
    return render(request, "panel/client_pass_new.html", {"subject": subject, "form": form})


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
