"""Prosty limit prób logowania oparty o współdzieloną pamięć podręczną (tabela w bazie)."""

import hashlib

from django.conf import settings
from django.core.cache import cache


def _key(kind, value):
    return f"login-rl:{kind}:{hashlib.sha256((value or '').lower().encode()).hexdigest()}"


def _keys(email, ip):
    return [_key("email", email), _key("ip", ip)]


def is_blocked(email, ip):
    limit = settings.LOGIN_RATE_LIMIT_ATTEMPTS
    # Limit na IP jest luźniejszy, bo z jednego adresu może logować się kilka osób (np. sieć strzelnicy).
    return (cache.get(_key("email", email), 0) >= limit) or (cache.get(_key("ip", ip), 0) >= limit * 4)


def register_failure(email, ip):
    for key in _keys(email, ip):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, settings.LOGIN_RATE_LIMIT_WINDOW)


def reset(email, ip):
    cache.delete(_key("email", email))


def allow(kind, value, limit, window=3600):
    """Ogólny licznik: True, jeśli w oknie czasu wykonano mniej niż `limit` operacji (i zalicza tę operację)."""
    key = _key(kind, value)
    count = cache.get(key, 0)
    if count >= limit:
        return False
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, window)
    return True


# Limity wysyłki e-maili z formularzy publicznych (ochrona skrzynek przed zalewem i domeny przed oznaczeniem jako spam).
MAIL_PER_ADDRESS_PER_HOUR = 3
MAIL_PER_IP_PER_HOUR = 20
REGISTRATIONS_PER_IP_PER_HOUR = 10
