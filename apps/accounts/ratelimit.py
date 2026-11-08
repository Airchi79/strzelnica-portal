"""Limity prób (logowanie, kody 2FA, e-maile z formularzy publicznych) w współdzielonej pamięci podręcznej (tabela w bazie).

Licznik przechowuje parę (liczba, koniec okna). Okno liczy się od pierwszej próby i nie przesuwa się przy kolejnych —
`cache.incr()` w Django zapisywał wartość z domyślnym czasem życia 300 s, przez co limity „na godzinę” działały jak
„na 5 minut”."""

import hashlib
import time

from django.conf import settings
from django.core.cache import cache


def _key(kind, value):
    return f"login-rl:{kind}:{hashlib.sha256((value or '').lower().encode()).hexdigest()}"


def _keys(email, ip):
    return [_key("email", email), _key("ip", ip)]


def _now():
    return time.time()


def _read(key, now):
    """(liczba, koniec_okna) albo (0, None), gdy brak licznika lub okno minęło."""
    data = cache.get(key)
    if isinstance(data, (tuple, list)) and len(data) == 2 and data[1] > now:
        return int(data[0]), float(data[1])
    if isinstance(data, int):  # licznik zapisany przez starszą wersję — liczba bez końca okna
        return data, None
    return 0, None


def _hit(key, window, now=None):
    """Zalicza jedną próbę; okno zaczyna się przy pierwszej próbie. Zwraca liczbę prób w oknie."""
    now = now or _now()
    count, ends = _read(key, now)
    ends = ends or now + window
    count += 1
    cache.set(key, (count, ends), max(1, int(ends - now) + 1))
    return count


def count(key, now=None):
    return _read(key, now or _now())[0]


def is_blocked(email, ip):
    limit = settings.LOGIN_RATE_LIMIT_ATTEMPTS
    # Limit na IP jest luźniejszy, bo z jednego adresu może logować się kilka osób (np. sieć strzelnicy).
    return count(_key("email", email)) >= limit or count(_key("ip", ip)) >= limit * 4


def register_failure(email, ip):
    for key in _keys(email, ip):
        _hit(key, settings.LOGIN_RATE_LIMIT_WINDOW)


def reset(email, ip):
    cache.delete(_key("email", email))


def allow(kind, value, limit, window=3600):
    """Ogólny licznik: True, jeśli w oknie czasu wykonano mniej niż `limit` operacji (i zalicza tę operację)."""
    key = _key(kind, value)
    if count(key) >= limit:
        return False
    _hit(key, window)
    return True


# Limity wysyłki e-maili z formularzy publicznych (ochrona skrzynek przed zalewem i domeny przed oznaczeniem jako spam).
MAIL_PER_ADDRESS_PER_HOUR = 3
MAIL_PER_IP_PER_HOUR = 20
REGISTRATIONS_PER_IP_PER_HOUR = 10
