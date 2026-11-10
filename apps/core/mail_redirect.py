"""Przekierowanie wszystkich e-maili na jeden adres (kopia testowa): nikt spoza testów nie dostanie wiadomości.

Temat dostaje przedrostek „[TEST → pierwotny adresat]”, a na początku treści jest informacja, do kogo była adresowana.
Wysyłka idzie przez właściwy backend (EMAIL_INNER_BACKEND, np. SMTP).
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
        redirected = []
        for message in email_messages:
            original = list(message.to) + list(message.cc) + list(message.bcc)
            m = copy.copy(message)
            m.to, m.cc, m.bcc = [target], [], []
            m.reply_to = []
            m.subject = f"[TEST → {', '.join(original)[:120]}] {message.subject}"
            m.body = f"[Kopia testowa — wiadomość adresowana do: {', '.join(original)}]\n\n{message.body}"
            m.extra_headers = {**message.extra_headers, "X-Satok-Original-To": ", ".join(original)[:500]}
            redirected.append(m)
        return self.inner.send_messages(redirected)
