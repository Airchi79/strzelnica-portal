from django.core.management.base import BaseCommand

from apps.logbook.services import settle_finished_reservations


class Command(BaseCommand):
    help = "Rozlicza zakończone rezerwacje według liczby strzelających w książce pobytu (uruchamiać co godzinę)."

    def handle(self, *args, **options):
        self.stdout.write(f"Rezerwacje z dopłatą lub rozbieżnością: {settle_finished_reservations()}")
