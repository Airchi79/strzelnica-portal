from .context import client_ip, get_request
from .models import AuditLog, SensitiveAccessLog


def _actor(actor):
    if actor is not None:
        return actor
    request = get_request()
    user = getattr(request, "user", None)
    return user if user is not None and user.is_authenticated else None


def log_event(action, obj=None, summary="", changes=None, actor=None):
    return AuditLog.objects.create(
        actor=_actor(actor),
        action=action,
        object_type=obj._meta.label_lower if obj is not None else "",
        object_id=str(obj.pk) if obj is not None and obj.pk is not None else "",
        summary=summary[:255],
        changes=changes or {},
        ip=client_ip(get_request()),
    )


def log_sensitive_access(subject, resource, reason="", actor=None):
    return SensitiveAccessLog.objects.create(
        actor=_actor(actor), subject=subject, resource=resource[:128], reason=reason[:255], ip=client_ip(get_request())
    )


def diff(old, new, fields):
    """Zwraca słownik {pole: [stara, nowa]} dla zmienionych pól."""
    out = {}
    for f in fields:
        a, b = getattr(old, f, None) if old else None, getattr(new, f, None)
        if a != b:
            out[f] = [None if a is None else str(a), None if b is None else str(b)]
    return out
