"""Macierz dostępu: każdy adres panelu (/panel/…) wymaga logowania, a zwykłe konto strzelca nie dostaje żadnej strony panelu.
Adresy z parametrami są wywoływane z wartościami zastępczymi — liczy się tylko to, że nie zwracają treści (200)."""

import uuid

import pytest
from django.urls import URLPattern, URLResolver, get_resolver

pytestmark = pytest.mark.django_db

SAMPLE = {"int": "999999", "uuid": str(uuid.uuid4()), "str": "x", "slug": "x", "path": "x"}


def _walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p


def _concrete(route):
    import re

    route = re.sub(r"<(?:(\w+):)?\w+>", lambda m: SAMPLE.get(m.group(1) or "str", "x"), route)
    return "/" + route.lstrip("^").rstrip("$")


def _panel_urls():
    return sorted({_concrete(route) for route, _ in _walk(get_resolver().url_patterns) if route.startswith("panel/")})


def test_panel_requires_login(client):
    leaks = []
    for url in _panel_urls():
        for method in ("get", "post"):
            r = getattr(client, method)(url)
            if r.status_code == 200:
                leaks.append(f"{method.upper()} {url}")
            elif r.status_code in (301, 302):
                assert "/konto/" in r["Location"] or r["Location"].startswith("/konto"), f"{url} → {r['Location']}"
    assert not leaks, leaks


def test_panel_forbidden_for_shooter_account(client, user):
    client.force_login(user)
    leaks = [url for url in _panel_urls() for method in ("get", "post") if getattr(client, method)(url).status_code == 200]
    assert not leaks, leaks
