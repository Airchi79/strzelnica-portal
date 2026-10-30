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
