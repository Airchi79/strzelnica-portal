"""Operator płatności online (Tpay albo Axepta BNP Paribas).

Wpłata online ma `method = "tpay"` (historyczna nazwa: „płatność online”), a operatora rozpoznaje się po prefiksie
`external_id`: `tpay:<id transakcji>` albo `axepta:<id płatności>`. Nowe płatności idą przez operatora z ustawienia
ONLINE_PAYMENTS_PROVIDER; powiadomienia i zwroty wcześniejszych płatności — przez operatora, który je obsłużył.
"""

from django.conf import settings

NAMES = {"tpay": "Tpay", "axepta": "Axepta BNP Paribas"}


class GatewayError(Exception):
    """Błąd komunikacji z operatorem płatności (wspólny dla Tpay i Axepty)."""


def active():
    return settings.ONLINE_PAYMENTS_PROVIDER


def active_name():
    return NAMES[active()]


def provider_of(payment):
    """Operator, przez którego przeszła wpłata online (z prefiksu external_id), albo None."""
    prefix = (payment.external_id or "").split(":", 1)[0]
    return prefix if prefix in NAMES and ":" in (payment.external_id or "") else None


def configured(provider):
    return {"tpay": settings.TPAY_ENABLED, "axepta": settings.AXEPTA_ENABLED}.get(provider, False)


def online_enabled():
    """Czy nowe płatności online są dostępne (operator z ONLINE_PAYMENTS_PROVIDER ma komplet danych w .env)."""
    return configured(active())


def external_ref(payment):
    """Identyfikator transakcji/płatności u operatora (bez prefiksu)."""
    return (payment.external_id or "").split(":", 1)[-1]
