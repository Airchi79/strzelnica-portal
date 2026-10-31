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


@register.filter
def regulation_text(text):
    """Prosty format treści regulaminu: akapity i wyróżnione paragrafy (§). Treść jest zawsze escapowana."""
    parts = []
    for block in (text or "").replace("\r\n", "\n").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        html = "<br>".join(escape(line) for line in block.split("\n"))
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
