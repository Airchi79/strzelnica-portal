from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.urls import reverse

from apps.core.backups import read_status
from apps.core.notifications import notify_board


class Command(BaseCommand):
    help = "Sprawdza wynik ostatniej kopii zapasowej i alarmuje zarząd, gdy kopia jest uszkodzona lub zbyt stara (raz na dobę)."

    def handle(self, *args, **options):
        st = read_status()
        self.stdout.write(f"Kopia zapasowa: {st['state']} — {st['message']}")
        if st["state"] == "ok" or cache.get("backup-alarm-sent"):
            return
        url = reverse("panel:backups")
        notify_board("ALARM: problem z kopią zapasową bazy", "emails/board_generic.txt", "core.manage_settings", url=url,
                     body=st["message"], event="Problem z kopią zapasową bazy danych", text=st["message"], url_path=url)
        cache.set("backup-alarm-sent", 1, 24 * 3600)
