from django.core.management.base import BaseCommand

from apps.logbook.services import close_stale_entries, purge_expired


class Command(BaseCommand):
    help = "Zamyka pobyty bez zapisanego wyjścia i usuwa wpisy starsze niż okres przechowywania (uruchamiać co godzinę)."

    def handle(self, *args, **options):
        self.stdout.write(f"Zamknięto automatycznie pobytów: {close_stale_entries()}")
        self.stdout.write(f"Usunięto wpisów: {purge_expired()}")
