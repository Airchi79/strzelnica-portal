from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.urls import reverse

from apps.core.notifications import notify_board
from apps.logbook.daily_print import run, verify_chain


class Command(BaseCommand):
    help = ("Tworzy dzienne wydruki książki pobytu (PDF) za zakończone dni i nowe wersje po zmianach, sprawdza łańcuch "
            "wydruków i alarmuje zarząd przy niezgodności (raz na dobę). Uruchamiać co godzinę.")

    def handle(self, *args, **options):
        created = run()
        self.stdout.write(f"Nowe wydruki dzienne: {len(created)}")
        checked, problems = verify_chain()
        self.stdout.write(f"Sprawdzono wydruków: {checked}, problemów: {len(problems)}")
        if not problems or cache.get("logbook-print-alarm-sent"):
            return
        url = reverse("panel:logbook_daily")
        text = "; ".join(problems[:10]) + (f" (i {len(problems) - 10} więcej)" if len(problems) > 10 else "")
        notify_board("ALARM: niezgodność dziennych wydruków książki pobytu", "emails/board_generic.txt", "logbook.view_logbook",
                     url=url, body=text, event="Niezgodność dziennych wydruków książki pobytu", text=text, url_path=url)
        cache.set("logbook-print-alarm-sent", 1, 24 * 3600)
