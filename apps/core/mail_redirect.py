"""Przekierowanie e-maili w kopii testowej: nikt spoza testów nie dostanie wiadomości.

- Adresy z EMAIL_TEST_RECIPIENTS (testerzy) dostają wiadomość bezpośrednio — temat z przedrostkiem „[TEST]”, na początku
  treści informacja, że to kopia testowa.
- Wszyscy pozostali adresaci — wiadomość trafia na EMAIL_REDIRECT_TO; temat „[TEST → pierwotny adresat]”, na początku
  treści informacja, do kogo była adresowana.
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
        testers = getattr(settings, "EMAIL_TEST_RECIPIENTS", set())
        out = []
        for message in email_messages:
            original = list(message.to) + list(message.cc) + list(message.bcc)
            direct = [a for a in original if _address(a) in testers]
            others = [a for a in original if _address(a) not in testers]
            if direct:
                m = copy.copy(message)
                m.to, m.cc, m.bcc = direct, [], []
                m.subject = f"[TEST] {message.subject}"
                m.body = ("[Kopia testowa systemu strzelnicy — wiadomość z próbnej wersji, nie dotyczy prawdziwych rezerwacji "
                          f"ani płatności]\n\n{message.body}")
                m.extra_headers = {**message.extra_headers, "X-Satok-Test-Copy": "1"}
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


def _address(value):
    """„Jan Kowalski <jan@example.com>” → „jan@example.com” (małe litery)."""
    from email.utils import parseaddr

    return parseaddr(value)[1].strip().lower()
