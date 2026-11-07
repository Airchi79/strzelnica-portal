import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from apps.facilities.models import format_pln

register = template.Library()


@register.filter
def pln(grosze):
    if grosze is None or grosze == "":
        return "—"
    return format_pln(grosze)


_URL = re.compile(r"https://[^\s<>\"']+")


def _linkify(escaped_line):
    """Adresy https:// w już escapowanym tekście → klikalne linki (bez kropki / przecinka / nawiasu na końcu)."""
    def repl(m):
        url = m.group(0)
        tail = ""
        while url and url[-1] in ".,;:)!?":
            tail = url[-1] + tail
            url = url[:-1]
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>{tail}'

    return _URL.sub(repl, escaped_line)


@register.filter
def regulation_text(text):
    """Prosty format treści regulaminu: akapity, wyróżnione paragrafy (§) i klikalne adresy https://.
    Treść jest zawsze escapowana — HTML wpisany w treść nie działa."""
    parts = []
    for block in (text or "").replace("\r\n", "\n").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        html = "<br>".join(_linkify(str(escape(line))) for line in block.split("\n"))
        tag = "h3" if block.startswith("§") and "\n" not in block else "p"
        parts.append(f"<{tag}>{html}</{tag}>")
    return mark_safe("\n".join(parts))


@register.filter
def status_class(status):
    return {"zweryfikowany": "ok", "oczekuje": "warn", "odrzucony": "bad", "wygasly": "bad"}.get(status, "")


@register.filter
def sub(value, arg):
    try:
        return int(value) - int(arg)
    except (TypeError, ValueError):
        return value


@register.filter
def get(mapping, key):
    try:
        return mapping.get(key, "")
    except AttributeError:
        return ""


# --- ikony panelu (proste linie SVG 24×24, kolor z CSS: currentColor) ---------------------------------
ICONS = {
    "home": "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
    "bell": "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0",
    "list": "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
    "grid": "M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z",
    "lock": "M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4",
    "archive": "M3 4h18v4H3zM5 8v12h14V8M10 12h4",
    "sliders": "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
    "book": "M4 19.5A2.5 2.5 0 0 1 6.5 17H20V2H6.5A2.5 2.5 0 0 0 4 4.5zM20 17v5H6.5A2.5 2.5 0 0 1 4 19.5",
    "tablet": "M6 2h12a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM12 18h.01",
    "target": "M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20zM12 6a6 6 0 1 0 0 12a6 6 0 1 0 0-12zM12 10a2 2 0 1 0 0 4a2 2 0 1 0 0-4z",
    "file": "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8M10 9H8",
    "filecheck": "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M9 15l2 2 4-4",
    "users": "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8a4 4 0 0 0 0 8zM22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75",
    "user": "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8a4 4 0 0 0 0 8z",
    "shield": "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM9 12l2 2 4-4",
    "receipt": "M4 2v20l3-2 3 2 3-2 3 2 3-2 1 1V2l-1 1-3-2-3 2-3-2-3 2-3-2zM8 8h8M8 12h8M8 16h5",
    "wallet": "M20 7H5a2 2 0 0 1 0-4h13v4M3 5v14a2 2 0 0 0 2 2h15V7M16 14h.01",
    "chart": "M3 3v18h18M7 15v2M11 11v6M15 7v10M19 13v4",
    "landmark": "M3 22h18M6 18v-7M10 18v-7M14 18v-7M18 18v-7M12 2l9 5H3z",
    "inbox": "M22 12h-6l-2 3h-4l-2-3H2M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z",
    "coins": "M8 14a6 6 0 1 0 0-12a6 6 0 0 0 0 12zM18.09 10.37A6 6 0 1 1 10.34 18M7 6h1v4M16.71 13.88l.7.71-2.82 2.82",
    "alert": "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01",
    "calendar": "M8 2v4M16 2v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z",
    "key": "M15.5 7.5 19 4M21 2l-2 2 3 3-3.5 3.5-3-3M11.39 11.61a5.5 5.5 0 1 1-7.78 7.78a5.5 5.5 0 0 1 7.78-7.78z",
    "building": "M4 22V4a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v18M2 22h20M9 6h2M13 6h2M9 10h2M13 10h2M9 14h2M13 14h2M10 22v-4h4v4",
    "history": "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l4 2",
    "external": "M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6",
    "moon": "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
    "sun": "M12 17a5 5 0 1 0 0-10a5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
    "menu": "M3 6h18M3 12h18M3 18h18",
    "logout": "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
}


@register.filter
def online_fee(grosze):
    """Opłata za płatność online doliczana do kwoty (wg ustawień płatności); 0 = brak opłaty."""
    from apps.payments.services import online_fee as calc

    try:
        return calc(int(grosze))
    except (TypeError, ValueError):
        return 0


@register.simple_tag
def icon(name, size=16):
    path = ICONS.get(name, ICONS["file"])
    return mark_safe(
        f'<svg class="ico" width="{int(size)}" height="{int(size)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="{path}"/></svg>'
    )


@register.simple_tag(takes_context=True)
def panel_menu(context):
    from apps.core.panel_menu import build

    request = context.get("request")
    return build(request) if request is not None and request.user.is_authenticated else []


@register.simple_tag(takes_context=True)
def panel_help_anchor(context):
    from apps.core.panel_menu import help_anchor

    request = context.get("request")
    match = getattr(request, "resolver_match", None) if request is not None else None
    return help_anchor(match.url_name or "") if match else ""


DAY_SHORT = ["pon", "wt", "śr", "czw", "pt", "sob", "ndz"]


@register.filter
def hours_summary(hours):
    """Godziny otwarcia skrótowo: kolejne dni z tymi samymi godzinami łączone w zakres („pon–pt 09:00–18:00”)."""
    rows = sorted(hours, key=lambda h: h.weekday)
    groups = []
    for h in rows:
        span = f"{h.opens:%H:%M}–{h.closes:%H:%M}"
        if groups and groups[-1][2] == span and groups[-1][1] == h.weekday - 1:
            groups[-1][1] = h.weekday
        else:
            groups.append([h.weekday, h.weekday, span])
    parts = []
    for start, end, span in groups:
        days = DAY_SHORT[start] if start == end else f"{DAY_SHORT[start]}–{DAY_SHORT[end]}"
        parts.append(f"{days} {span}")
    return ", ".join(parts)
