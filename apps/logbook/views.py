import base64
from datetime import date, datetime, time, timedelta

from django.conf import settings

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit.context import client_ip
from apps.audit.services import log_event, log_sensitive_access
from apps.bookings.models import Reservation
from apps.core.models import AssociationSettings
from apps.core.permissions import panel_required
from apps.core.qr import qr_svg_data_uri

from . import services
from .forms import EntryForm, LogbookSettingsForm, StaffEntryForm
from .models import Entry, LogbookSettings

SESSION_RECENT = "logbook_recent_entries"


def _absolute(request, path):
    # Adres kanoniczny z ustawień — kody QR są drukowane, nie mogą zależeć od nagłówka Host żądania.
    return settings.SITE_URL + path


def _remember(request, entry):
    recent = request.session.get(SESSION_RECENT, [])[-9:]
    recent.append(entry.pk)
    request.session[SESSION_RECENT] = recent


def _handle_entry(request, *, form, source, reservation=None, person=None, template_ctx):
    """Wspólna obsługa formularza wpisu (kod rezerwacji, kod wejścia, dopisanie przez prowadzącego)."""
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            services.check_rate_limit(client_ip(request))
            signature = services.decode_signature(d.get("signature"))
            if source == Entry.Source.PERSONAL_QR:
                account, author = person, request.user
            else:
                account, author = (request.user if request.user.is_authenticated else None), None
            entry = services.create_entry(
                data=d,
                signature_png=signature,
                source=source,
                reservation=reservation or d.get("reservation"),
                user=account,
                created_by=author,
                ip=client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except ValidationError as e:
            for m in e.messages:
                form.add_error(None, m)
        else:
            if source != Entry.Source.PERSONAL_QR:
                _remember(request, entry)
                return redirect("logbook:done", pk=entry.pk)
            messages.success(request, f"Dopisano {entry.full_name} ({entry.number}).")
            return redirect("bookings:detail", pk=entry.reservation_id)
    return render(request, "logbook/entry_form.html", {"form": form, **template_ctx})


def _initial(request, person=None):
    who = person or (request.user if request.user.is_authenticated else None)
    return services.prefill_for(who) if who else {}


def entry_by_reservation(request, token):
    reservation = services.reservation_from_token(token)
    if reservation is None:
        raise Http404
    if not services.reservation_open_for_entry(reservation):
        return render(request, "logbook/closed.html", {"reservation": reservation}, status=410)
    purposes = [Entry.Purpose.SHOOTING, Entry.Purpose.OBSERVER]
    initial = {"purpose": Entry.Purpose.SHOOTING, **_initial(request)}
    form = EntryForm(request.POST or None, initial=initial, fixed_reservation=reservation, purposes=purposes)
    return _handle_entry(
        request, form=form, source=Entry.Source.RESERVATION_QR, reservation=reservation, template_ctx={"reservation": reservation}
    )


def entry_by_gate(request, token):
    if not services.gate_token_valid(token):
        return render(request, "logbook/closed.html", {"gate_invalid": True}, status=404)
    form = EntryForm(request.POST or None, initial=_initial(request), reservations=services.open_reservations())
    return _handle_entry(request, form=form, source=Entry.Source.GATE_QR, template_ctx={"gate": True})


def done(request, pk):
    if pk not in request.session.get(SESSION_RECENT, []):
        raise Http404
    entry = get_object_or_404(Entry, pk=pk)
    return render(request, "logbook/done.html", {"entry": entry, "exit_token": services.exit_token(entry)})


def exit_view(request, token):
    entry = services.entry_from_exit_token(token)
    if entry is None:
        raise Http404
    if request.method == "POST":
        try:
            services.register_exit(entry)
            messages.success(request, f"Zapisano wyjście ({timezone.localtime(entry.exited_at):%H:%M}). Do zobaczenia!")
        except ValidationError as e:
            messages.info(request, e.messages[0])
    return render(request, "logbook/exit.html", {"entry": entry})


@login_required
def my_qr(request):
    if request.method == "POST":
        from apps.accounts.models import new_qr_secret

        request.user.qr_secret = new_qr_secret()
        request.user.save(update_fields=["qr_secret"])
        log_event("ksiazka.nowy_kod_osobisty", request.user)
        messages.success(request, "Wygenerowano nowy kod. Poprzedni przestał działać.")
        return redirect("logbook:my_qr")
    url = _absolute(request, reverse("logbook:personal", args=[services.personal_token(request.user)]))
    entries = request.user.log_entries.select_related("reservation__lane").order_by("-entered_at")[:50]
    return render(request, "logbook/my_qr.html", {"qr": qr_svg_data_uri(url, box_size=10), "entries": entries})


@login_required
def personal_add(request, token):
    """Prowadzący strzelanie skanuje stały kod QR strzelca i dopisuje go do swojej trwającej rezerwacji."""
    person = services.user_from_personal_token(token)
    if person is None:
        raise Http404
    own_open = services.open_reservations().filter(user=request.user)
    if not own_open.exists():
        return render(request, "logbook/closed.html", {"no_own_reservation": True, "person": person}, status=403)
    form = EntryForm(
        request.POST or None,
        initial={"purpose": Entry.Purpose.SHOOTING, "reservation": own_open.first().pk, **_initial(request, person)},
        reservations=own_open,
        purposes=[Entry.Purpose.SHOOTING, Entry.Purpose.OBSERVER],
    )
    if request.method == "POST" and form.is_valid() and form.cleaned_data.get("reservation") is None:
        form.add_error("reservation", "Wybierz swoją rezerwację.")
    return _handle_entry(request, form=form, source=Entry.Source.PERSONAL_QR, person=person, template_ctx={"person": person})


def reservation_qr_context(request, reservation):
    """Dane do pokazania kodu QR rezerwacji prowadzącemu (na stronie szczegółów rezerwacji)."""
    if reservation.status != Reservation.Status.CONFIRMED or reservation.ends_at < timezone.now():
        return {}
    url = _absolute(request, reverse("logbook:by_reservation", args=[services.reservation_token(reservation)]))
    return {
        "entry_qr": qr_svg_data_uri(url, box_size=10),
        "entry_url": url,
        "entry_open": services.reservation_open_for_entry(reservation),
        "entry_opens_at": reservation.starts_at - timedelta(minutes=LogbookSettings.load().window_before_minutes),
    }


# --- panel zarządu ---------------------------------------------------------------


def _parse_day(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _filtered(params):
    today = timezone.localdate()
    f = {
        "od": _parse_day(params.get("od"), today - timedelta(days=30)),
        "do": _parse_day(params.get("do"), today),
        "cel": params.get("cel", ""),
        "q": params.get("q", "").strip(),
        "otwarte": params.get("otwarte") == "1",
        "grupuj": params.get("grupuj", "1") != "0",
    }
    start = timezone.make_aware(datetime.combine(f["od"], time.min))
    end = timezone.make_aware(datetime.combine(f["do"] + timedelta(days=1), time.min))
    qs = Entry.objects.filter(entered_at__gte=start, entered_at__lt=end).select_related("reservation__lane", "range_officer", "created_by")
    if f["cel"] in Entry.Purpose.values:
        qs = qs.filter(purpose=f["cel"])
    if f["otwarte"]:
        qs = qs.filter(exited_at__isnull=True)
    if f["q"]:
        qs = qs.filter(Q(last_name__icontains=f["q"]) | Q(first_name__icontains=f["q"]) | Q(number__icontains=f["q"]) | Q(permit_number__icontains=f["q"]) | Q(reservation__number__iexact=f["q"]))
    return qs, f


def _group(entries):
    """Grupuje wpisy strony wg rezerwacji (kolejność pierwszego wystąpienia); wpisy bez rezerwacji — wg dnia."""
    groups, index = [], {}
    for e in entries:
        key = ("r", e.reservation_id) if e.reservation_id else ("d", timezone.localdate(e.entered_at))
        if key not in index:
            index[key] = {"reservation": e.reservation, "day": key[1] if key[0] == "d" else None, "entries": []}
            groups.append(index[key])
        index[key]["entries"].append(e)
    return groups


@panel_required("logbook.view_logbook")
def panel_list(request):
    params = request.POST if request.method == "POST" else request.GET
    qs, f = _filtered(params)
    page = Paginator(qs, 50).get_page(params.get("page"))
    reveal = request.method == "POST" and request.POST.get("pokaz") == "1"
    entries = list(page)
    for e in entries:
        e.identity_masked = services.masked_identity(e)
    if reveal:
        for e in entries:
            log_sensitive_access(e.user, f"ksiazka:wpis:{e.number}", "odkrycie danych na liście książki pobytu")
    else:
        log_sensitive_access(None, f"ksiazka:lista:{f['od']}..{f['do']}", "przegląd książki pobytu (dane ukryte)")
    query = params.copy()
    for k in ("csrfmiddlewaretoken", "pokaz", "page"):
        query.pop(k, None)
    ctx = {
        "page": page, "entries": entries, "groups": _group(entries) if f["grupuj"] else None, "f": f, "reveal": reveal,
        "purposes": Entry.Purpose.choices, "query": query.urlencode(),
        "open_count": Entry.objects.filter(exited_at__isnull=True).count(),
    }
    return render(request, "panel/logbook_list.html", ctx)


@panel_required("logbook.view_logbook")
def panel_detail(request, pk):
    entry = get_object_or_404(Entry.objects.select_related("reservation__lane", "range_officer", "user", "created_by", "regulation_version"), pk=pk)
    reveal = request.method == "POST" and request.POST.get("pokaz") == "1"
    ctx = {"e": entry, "reveal": reveal, "identity_masked": services.masked_identity(entry)}
    if reveal:
        log_sensitive_access(entry.user, f"ksiazka:wpis:{entry.number}", "podgląd danych wpisu w książce pobytu")
        ctx["signature"] = "data:image/png;base64," + base64.b64encode(bytes(entry.signature)).decode()
    return render(request, "panel/logbook_detail.html", ctx)


@panel_required("logbook.view_logbook")
def panel_print(request):
    qs, f = _filtered(request.GET)
    entries = list(qs.order_by("entered_at", "id")[:2000])
    rows = [(e, "data:image/png;base64," + base64.b64encode(bytes(e.signature)).decode()) for e in entries]
    log_sensitive_access(None, f"ksiazka:wydruk:{f['od']}..{f['do']}", "wydruk książki pobytu")
    log_event("ksiazka.wydruk", summary=f"{f['od']}–{f['do']}, wpisów: {len(rows)}")
    return render(request, "logbook/print.html", {"rows": rows, "f": f, "association": AssociationSettings.load(), "truncated": len(entries) == 2000})


EXPORT_LIMIT = 20000


@panel_required("logbook.view_logbook")
def panel_export(request):
    from .export import entries_xlsx

    qs, f = _filtered(request.GET)
    entries = list(qs.select_related("regulation_version").order_by("entered_at", "id")[:EXPORT_LIMIT])
    log_sensitive_access(None, f"ksiazka:eksport:{f['od']}..{f['do']}", "eksport książki pobytu do XLSX")
    log_event("ksiazka.eksport", summary=f"{f['od']}–{f['do']}, wpisów: {len(entries)}")
    response = HttpResponse(entries_xlsx(entries), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="ksiazka-pobytu-{f["od"]}-{f["do"]}.xlsx"'
    response["Cache-Control"] = "no-store"
    return response


def _staff_reservations(now):
    """Rezerwacje, do których obsługa może dopisać osobę: potwierdzone, trwające w ostatnich 24 h."""
    return (
        Reservation.objects.filter(
            status=Reservation.Status.CONFIRMED, ends_at__gte=now - timedelta(hours=services.STAFF_BACKDATE_HOURS), starts_at__lte=now + timedelta(hours=1)
        ).select_related("lane", "user").order_by("starts_at", "lane__order")
    )


@panel_required("logbook.staff_entry")
def panel_add(request):
    now = timezone.now()
    initial = {"entered_at": timezone.localtime(now).replace(second=0, microsecond=0)}
    if request.GET.get("rezerwacja", "").isdigit():
        initial.update(purpose=Entry.Purpose.SHOOTING, reservation=int(request.GET["rezerwacja"]))
    form = StaffEntryForm(request.POST or None, initial=initial, reservations=_staff_reservations(now))
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            entry = services.create_entry(
                data=d, signature_png=services.decode_signature(d.get("signature")), source=Entry.Source.STAFF,
                reservation=d.get("reservation"), created_by=request.user, ip=client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""), entered_at=d["entered_at"], now=now,
            )
        except ValidationError as e:
            for m in e.messages:
                form.add_error(None, m)
        else:
            messages.success(request, f"Dodano wpis {entry.number}: {entry.full_name}.")
            if "kolejny" in request.POST:
                url = reverse("panel:logbook_add")
                return redirect(f"{url}?rezerwacja={entry.reservation_id}" if entry.reservation_id else url)
            return redirect("panel:logbook_entry", pk=entry.pk)
    return render(request, "panel/logbook_add.html", {"form": form})


@panel_required("logbook.manage_logbook")
def panel_settings(request):
    cfg = LogbookSettings.load()
    before = {k: getattr(cfg, k) for k in LogbookSettingsForm.Meta.fields}
    form = LogbookSettingsForm(request.POST or None, instance=cfg)
    if request.method == "POST" and form.is_valid():
        cfg = form.save()
        log_event("ksiazka.ustawienia", cfg, changes={k: [before[k], getattr(cfg, k)] for k in before if before[k] != getattr(cfg, k)})
        messages.success(request, "Zapisano ustawienia książki pobytu.")
        return redirect("panel:logbook_settings")
    return render(request, "panel/logbook_settings.html", {"form": form, "cfg": cfg})


@panel_required("logbook.manage_logbook")
@require_POST
def panel_rotate_gate(request):
    services.rotate_gate_token(request.user)
    messages.success(request, "Wygenerowano nowy kod QR wejścia. Wydrukuj i wywieś nowy plakat — stary kod już nie działa.")
    return redirect("panel:logbook_settings")


@panel_required("logbook.manage_logbook")
def panel_gate_poster(request):
    url = _absolute(request, reverse("logbook:by_gate", args=[LogbookSettings.load().gate_token]))
    return render(request, "logbook/gate_poster.html", {"qr": qr_svg_data_uri(url, box_size=14), "url": url})
