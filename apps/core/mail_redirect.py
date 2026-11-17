"""Przekierowanie e-maili w kopii testowej: nikt spoza testów nie dostanie wiadomości.

- Bezpośrednio (jak na produkcji; temat z przedrostkiem „[TEST]”, na początku treści informacja o kopii testowej):
  • osoby, które korzystają z testu — konto założone albo zalogowane w kopii po jej przygotowaniu (prepare_test_copy),
  • adresy z EMAIL_TEST_RECIPIENTS,
  • reset hasła (nagłówek X-Satok-Direct) — na adres podany w formularzu.
- Wszyscy pozostali (osoby skopiowane z produkcji, które z testu nie korzystają) — na EMAIL_REDIRECT_TO; temat
  „[TEST → pierwotny adresat]”. Dzięki temu przypomnienia i ogłoszenia z testu nie trafiają do wszystkich członków.
Wiadomość z adresatami obu rodzajów jest dzielona na dwie. Wysyłka przez właściwy backend (EMAIL_INNER_BACKEND, np. SMTP).
"""

import copy

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend


class RedirectingEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self.inner = get_connection(settings.EMAIL_INNER_BACKEND, fail_silently=fail_silently, **kwargs)

    def open(self):
        return self.inner.open()

    def close(self):
        return self.inner.close()

    def send_messages(self, email_messages):
        target = settings.EMAIL_REDIRECT_TO
        out = []
        for message in email_messages:
            original = list(message.to) + list(message.cc) + list(message.bcc)
            if message.extra_headers.get(DIRECT_HEADER):
                direct, others = original, []
            else:
                allowed = _direct_addresses({_address(a) for a in original})
                direct = [a for a in original if _address(a) in allowed]
                others = [a for a in original if _address(a) not in allowed]
            if direct:
                m = copy.copy(message)
                m.to, m.cc, m.bcc = direct, [], []
                m.subject = f"[TEST] {message.subject}"
                m.body = ("[Kopia testowa systemu strzelnicy — wiadomość z próbnej wersji, nie dotyczy prawdziwych rezerwacji "
                          f"ani płatności]\n\n{message.body}")
                m.extra_headers = {**{k: v for k, v in message.extra_headers.items() if k != DIRECT_HEADER}, "X-Satok-Test-Copy": "1"}
                out.append(m)
            if others:
                m = copy.copy(message)
                m.to, m.cc, m.bcc = [target], [], []
                m.reply_to = []
                m.subject = f"[TEST → {', '.join(others)[:120]}] {message.subject}"
                m.body = f"[Kopia testowa — wiadomość adresowana do: {', '.join(others)}]\n\n{message.body}"
                m.extra_headers = {**message.extra_headers, "X-Satok-Original-To": ", ".join(others)[:500]}
                out.append(m)
        return self.inner.send_messages(out)


DIRECT_HEADER = "X-Satok-Direct"


def _direct_addresses(addresses):
    """Adresy (spośród `addresses`), które w kopii testowej dostają wiadomość bezpośrednio."""
    allowed = set(getattr(settings, "EMAIL_TEST_RECIPIENTS", set())) & addresses
    rest = addresses - allowed
    if rest:
        from django.contrib.auth import get_user_model
        from django.db.models import Q

        from apps.audit.models import AuditLog

        prepared = AuditLog.objects.filter(action="system.kopia_testowa").order_by("-at").values_list("at", flat=True).first()
        if prepared:  # kopia nieprzygotowana — nikt poza listą (dane prosto z produkcji)
            active = get_user_model().objects.filter(Q(date_joined__gt=prepared) | Q(last_login__gt=prepared), is_active=True)
            allowed |= {e.lower() for e in active.filter(email__in=list(rest)).values_list("email", flat=True)}
            # e-mail zapisany z inną wielkością liter
            allowed |= {a for a in rest if active.filter(email__iexact=a).exists()} if rest - allowed else set()
    return allowed


def _address(value):
    """„Jan Kowalski <jan@example.com>” → „jan@example.com” (małe litery)."""
    from email.utils import parseaddr

    return parseaddr(value)[1].strip().lower()
