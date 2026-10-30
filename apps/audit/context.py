"""Kontekst bieżącego żądania (użytkownik, IP) dostępny dla dziennika zdarzeń."""

from contextvars import ContextVar

from django.conf import settings

_current_request = ContextVar("current_request", default=None)


def set_request(request):
    return _current_request.set(request)


def reset_request(token):
    _current_request.reset(token)


def get_request():
    return _current_request.get()


def client_ip(request):
    if request is None:
        return None
    # Tylko za zaufanym proxy (nginx/caddy) adres klienta jest w X-Forwarded-For; bierzemy ostatni wpis dodany przez proxy.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded and settings.TRUST_X_FORWARDED_FOR:
        return forwarded.split(",")[-1].strip() or None
    return request.META.get("REMOTE_ADDR")
