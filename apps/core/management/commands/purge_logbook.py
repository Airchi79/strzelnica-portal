from django.core.management.base import BaseCommand

from apps.logbook.services import purge_expired


class Command(BaseCommand):
    help = "Usuwa wpisy książki pobytu starsze niż okres przechowywania z ustawień (uruchamiać raz dziennie)."

    def handle(self, *args, **options):
        self.stdout.write(f"Usunięto wpisów: {purge_expired()}")
