"""Alarm e-mail o błędach serwera (500, wyjątki w zadaniach). Bez bazy danych — przy błędzie transakcja może być zerwana.

Adresy: zmienna środowiskowa ERROR_EMAIL (po przecinku). Pusta — alarm wyłączony (błędy tylko w logu kontenera).
Ograniczenie: ten sam błąd najwyżej raz na 30 min, łącznie najwyżej 10 wiadomości na godzinę (na proces).
Treść: rodzaj błędu, ślad stosu, adres i metoda żądania — bez danych formularzy (dane osobowe)."""

import hashlib
import logging
import threading
import time
import traceback

THROTTLE_SECONDS = 30 * 60
MAX_PER_HOUR = 10


class BoardErrorEmailHandler(logging.Handler):
    def __init__(self, level=logging.ERROR):
        super().__init__(level)
        self._lock = threading.Lock()
        self._last = {}
        self._sent = []

    def _allowed(self, signature, now):
        with self._lock:
            self._sent = [t for t in self._sent if now - t < 3600]
            if len(self._sent) >= MAX_PER_HOUR or now - self._last.get(signature, 0) < THROTTLE_SECONDS:
                return False
            self._last[signature] = now
            self._sent.append(now)
            return True

    def emit(self, record):
        try:
            from django.conf import settings

            recipients = [a.strip() for a in getattr(settings, "ERROR_EMAILS", []) if a.strip()]
            if not recipients:
                return
            exc = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            where = ""
            request = getattr(record, "request", None)
            if request is not None:
                where = f"{getattr(request, 'method', '')} {getattr(request, 'path', '')}"
            last_line = exc.strip().splitlines()[-1] if exc.strip() else record.getMessage()[:200]
            signature = hashlib.sha256(f"{record.name}|{where}|{last_line}".encode()).hexdigest()
            if not self._allowed(signature, time.time()):
                return
            from django.core.mail import get_connection, send_mail

            body = (f"Błąd w systemie rezerwacji ({settings.SITE_URL}).\n\nLogger: {record.name}\n"
                    f"{'Żądanie: ' + where + chr(10) if where else ''}Komunikat: {record.getMessage()[:1000]}\n\n{exc[-8000:]}\n"
                    "Ten sam błąd zostanie zgłoszony ponownie najwcześniej za 30 minut. Pełny log: docker compose logs app.")
            send_mail(f"[Strzelnica Sątok] BŁĄD: {last_line[:120]}", body, settings.DEFAULT_FROM_EMAIL, recipients,
                      connection=get_connection(timeout=10))
        except Exception:  # noqa: BLE001 — alarm nie może wywołać kolejnego błędu
            pass
