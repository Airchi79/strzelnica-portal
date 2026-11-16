"""Zadania okresowe w jednym stałym procesie (kontener „cron”).

Wcześniej pętla w powłoce uruchamiała co minutę 4 osobne procesy `python manage.py …` (a co godzinę kolejne 10) — każdy
start Django to ok. 0,7 s pracy procesora i 85 MB pamięci, nawet gdy nie ma nic do zrobienia (ok. 5 800 startów na dobę).
Tu Django ładuje się raz, a zadania są wywoływane w tym samym procesie z tym samym harmonogramem:
- co minutę: zwalnianie wygasłych blokad i nieopłaconych rezerwacji, lista oczekujących, zwroty, wysyłka e-maili,
- co 5 minut: ogłoszenia,
- co godzinę: przypomnienia, rozliczenia, wygaszanie dokumentów, książka pobytu, stowarzyszenie, raporty, księgowość,
  KSeF, kontrola kopii zapasowych.

Błąd jednego zadania nie zatrzymuje pozostałych (zapis w logu, kolejne zadanie). Połączenie z bazą jest odświeżane przed
każdym zadaniem (np. po restarcie bazy). Proces kończy się sam po `--max-hours` (domyślnie 24 h) — Docker uruchamia go
od nowa (restart: unless-stopped), co porządkuje pamięć. SIGTERM (zatrzymanie kontenera) kończy pracę po bieżącym zadaniu."""

import logging
import signal
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connections

logger = logging.getLogger(__name__)

EVERY_MINUTE = ["expire_reservations", "process_waitlist", "process_refunds", "send_outbox"]
EVERY_5_MINUTES = ["send_announcements"]
EVERY_HOUR = ["send_reminders", "settle_reservations", "expire_credentials", "purge_logbook", "daily_logbook_print",
              "association_tasks", "sales_reports", "accounting_sync", "ksef_sync", "check_backups"]


def tasks_for(tick):
    """Zadania w danym obiegu pętli (obieg = minuta; obieg 0 — wszystkie, jak w dawnej pętli w powłoce)."""
    out = list(EVERY_MINUTE)
    if tick % 5 == 0:
        out += EVERY_5_MINUTES
    if tick % 60 == 0:
        out += EVERY_HOUR
    return out


class Command(BaseCommand):
    help = "Zadania okresowe w jednym procesie (zamiast osobnego startu Django dla każdego zadania co minutę)."

    def add_arguments(self, parser):
        parser.add_argument("--max-hours", type=float, default=24.0, help="Po tylu godzinach proces kończy się (Docker uruchomi go ponownie).")
        parser.add_argument("--interval", type=float, default=60.0, help="Długość obiegu w sekundach.")
        parser.add_argument("--ticks", type=int, default=0, help="Liczba obiegów (0 = bez końca; do testów).")

    def handle(self, *args, max_hours, interval, ticks, **options):
        self.stopping = False

        def stop(signum, frame):
            self.stopping = True
            logger.info("Harmonogram: zatrzymanie (sygnał %s) — kończę po bieżącym zadaniu.", signum)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        deadline = time.monotonic() + max_hours * 3600
        tick = 0
        while not self.stopping and (not ticks or tick < ticks) and time.monotonic() < deadline:
            started = time.monotonic()
            for name in tasks_for(tick):
                if self.stopping:
                    break
                self.run_task(name)
            tick += 1
            if ticks and tick >= ticks:
                break
            # sen do końca obiegu, w krótkich odcinkach — szybka reakcja na zatrzymanie kontenera
            while not self.stopping and time.monotonic() - started < interval:
                time.sleep(min(1.0, interval - (time.monotonic() - started)))
        connections.close_all()

    def run_task(self, name):
        close_old_connections()  # zerwane / przeterminowane połączenie z bazą → nowe
        try:
            call_command(name, stdout=self.stdout, stderr=self.stderr)
        except Exception:  # noqa: BLE001 — błąd jednego zadania nie zatrzymuje harmonogramu
            logger.exception("Harmonogram: zadanie %s zakończone błędem", name)
            connections.close_all()  # połączenie mogło zostać w stanie błędu — następne zadanie dostanie nowe
        finally:
            close_old_connections()
