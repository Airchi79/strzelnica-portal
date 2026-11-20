"""Publiczne API danych strzelnicy (JSON, tylko odczyt, bez danych osobowych) — dla strony WordPress i robotów promocji.

    GET /api/publiczne/osie/            osie: nazwa, długość, godziny otwarcia (tydzień), cennik obowiązujący dziś
    GET /api/publiczne/wolne-terminy/   wolne godziny osi w najbliższych dniach (?od=RRRR-MM-DD&dni=1..14&os=<id>)
    GET /api/publiczne/wydarzenia/      najbliższe opublikowane wydarzenia: termin, opłata, wolne miejsca, link do zapisów

Te same dane, które każdy widzi w publicznym kalendarzu i na liście wydarzeń. Odpowiedzi są publiczne (CORS „*”),
z krótką pamięcią podręczną (5 min), więc odpytywanie przez stronę nie obciąża serwera."""

from datetime import date, timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

MAX_DAYS = 14
CACHE_SECONDS = 300
WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]


def _json(data):
    response = JsonResponse(data, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    response["Access-Control-Allow-Origin"] = "*"
    response["X-Robots-Tag"] = "noindex"
    return response


def _zl(grosze):
    return None if grosze is None else round(grosze / 100, 2)


def _lanes(lane_id=None):
    from apps.bookings.forms import active_lanes

    qs = active_lanes().prefetch_related("opening_hours", "price_rules").order_by("facility__order", "order")
    if lane_id:
        qs = qs.filter(pk=lane_id)
    return list(qs)


def _site(path):
    return settings.SITE_URL.rstrip("/") + path


@require_GET
def lanes(request):
    today = timezone.localdate()
    out = []
    for lane in _lanes():
        hours = {oh.weekday: oh for oh in lane.opening_hours.all()}
        rule = lane.price_rule_for(today)
        out.append({
            "id": lane.pk, "nazwa": lane.name, "dlugosc_m": lane.length_m, "opis": lane.description,
            "tylko_na_wylacznosc": lane.exclusive_only,
            "godziny": [{"dzien": WEEKDAYS[w], "od": f"{hours[w].opens:%H:%M}", "do": f"{hours[w].closes:%H:%M}"} if w in hours
                        else {"dzien": WEEKDAYS[w], "zamknieta": True} for w in range(7)],
            "cennik": None if rule is None else {
                "za_osobe_godzina_zl": _zl(rule.per_person_hourly), "wylacznosc_godzina_zl": _zl(rule.flat_hourly),
                "zasada": "opłata za godzinę = mniejsza z kwot: liczba osób × stawka za osobę albo wyłączność",
                "od_dnia": rule.valid_from.isoformat()},
        })
    return _json({"osie": out, "rezerwacja": _site(reverse("bookings:new")), "kalendarz": _site(reverse("bookings:calendar"))})


def _ranges(day, slots):
    """Kolejne wolne godziny → przedziały „od–do”."""
    out, start, prev = [], None, None
    for s in slots:
        if s.state != "wolne":
            continue
        if start is not None and s.hour == prev + 1:
            prev = s.hour
            continue
        if start is not None:
            out.append((start, prev + 1))
        start = prev = s.hour
    if start is not None:
        out.append((start, prev + 1))
    return [{"od": f"{a:02d}:00", "do": f"{b:02d}:00", "godzin": b - a} for a, b in out]


@require_GET
def free_slots(request):
    from apps.bookings import services
    from apps.bookings.models import BookingSettings

    try:
        first = date.fromisoformat(request.GET["od"]) if request.GET.get("od") else timezone.localdate()
        days = min(max(int(request.GET.get("dni", 7)), 1), MAX_DAYS)
        lane_id = int(request.GET["os"]) if request.GET.get("os") else None
    except (ValueError, KeyError):
        return JsonResponse({"error": "Nieprawidłowe parametry: od=RRRR-MM-DD, dni=1–14, os=<numer osi>."}, status=400)
    today = timezone.localdate()
    if first < today or first > today + timedelta(days=90):
        first = today
    lead = BookingSettings.load().min_lead_hours
    out = []
    for lane in _lanes(lane_id):
        lane_days = []
        for i in range(days):
            d = first + timedelta(days=i)
            slots = services.day_slots(lane, d, lead_hours=lead, holds=True)
            if not slots:
                continue
            free = _ranges(d, slots)
            lane_days.append({"data": d.isoformat(), "dzien": WEEKDAYS[d.weekday()], "wolne": free,
                              "wolnych_godzin": sum(r["godzin"] for r in free), "godzin_otwarcia": len(slots)})
        out.append({"id": lane.pk, "nazwa": lane.name, "dni": lane_days})
    return _json({"od": first.isoformat(), "dni": days, "osie": out, "rezerwacja": _site(reverse("bookings:new"))})


@require_GET
def events(request):
    from apps.events.models import Event, Registration

    qs = (Event.objects.filter(status=Event.Status.PUBLISHED, ends_at__gte=timezone.now()).order_by("starts_at")
          .annotate(n_taken=Count("registrations", filter=Q(registrations__status=Registration.Status.REGISTERED)))[:20])
    out = []
    for e in qs:
        free = None if e.capacity is None else max(e.capacity - e.n_taken, 0)
        out.append({
            "id": e.pk, "nazwa": e.title, "opis": e.description,
            "poczatek": timezone.localtime(e.starts_at).isoformat(), "koniec": timezone.localtime(e.ends_at).isoformat(),
            "oplata_zl": _zl(e.fee), "limit": e.capacity, "zapisanych": e.n_taken, "wolnych_miejsc": free,
            "zapisy_do": timezone.localtime(e.registration_until).isoformat(), "zapisy_otwarte": e.registration_open,
            "link": _site(reverse("events:detail", args=[e.pk])),
        })
    return _json({"wydarzenia": out, "lista": _site(reverse("events:list"))})
