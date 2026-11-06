"""Widżet na zewnętrzną stronę internetową: zajętość osi, „teraz na strzelnicy”, najbliższe wydarzenia.
Strony widżetu zawierają tylko dane publiczne (bez formularzy), więc wolno je osadzać w ramce na dowolnej stronie."""

from datetime import timedelta

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.cache import cache_control
from django.views.decorators.clickjacking import xframe_options_exempt

KINDS = {"zajetosc": "Zajętość osi", "teraz": "Teraz na strzelnicy", "wydarzenia": "Najbliższe wydarzenia"}


def _theme(request):
    return "dark" if request.GET.get("motyw") in ("ciemny", "dark") else "light"


@xframe_options_exempt
@cache_control(public=True, max_age=60)
def widget_page(request, kind):
    from apps.bookings import views as bookings_views
    from apps.bookings.forms import active_lanes
    from apps.bookings.models import BookingSettings

    if kind not in KINDS:
        from django.http import Http404

        raise Http404
    ctx = {"kind": kind, "theme": _theme(request), "site": settings.SITE_URL}
    if kind == "zajetosc":
        lanes = list(active_lanes().order_by("facility__order", "order"))
        lane = next((x for x in lanes if str(x.pk) == request.GET.get("os", "")), lanes[0] if lanes else None)
        day = bookings_views._parse_day(request.GET.get("dzien"))
        start = day - timedelta(days=day.weekday())
        lead = BookingSettings.load().min_lead_hours
        days, rows = bookings_views._week_grid(lane, start, lead_hours=lead, holds=True) if lane else ([], [])
        ctx.update({"lanes": lanes, "lane": lane, "week_days": days, "rows": rows, "week_start": start, "week_end": start + timedelta(days=6), "today": timezone.localdate(),
                    "prev": start - timedelta(days=7), "next": start + timedelta(days=7)})
    elif kind == "teraz":
        from apps.logbook.now import lanes_now

        ctx["now_lanes"] = lanes_now()
        ctx["now"] = timezone.now()
    else:
        from apps.events.models import Event

        try:
            n = max(1, min(int(request.GET.get("liczba", 5)), 20))
        except ValueError:
            n = 5
        ctx["events"] = Event.objects.filter(status=Event.Status.PUBLISHED, ends_at__gte=timezone.now()).order_by("starts_at")[:n]
    return render(request, "widget/page.html", ctx)


@cache_control(public=True, max_age=300)
def embed_js(request):
    """Skrypt dla zewnętrznej strony: zamienia <div data-satok-widget="…"> w ramkę z widżetem i dopasowuje jej wysokość."""
    js = render_to_string("widget/embed.js", {"site": settings.SITE_URL})
    response = HttpResponse(js, content_type="application/javascript; charset=utf-8")
    response["Access-Control-Allow-Origin"] = "*"
    return response
