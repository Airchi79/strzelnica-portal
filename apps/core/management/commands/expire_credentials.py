from django.core.management.base import BaseCommand

from apps.credentials.services import expire_outdated


class Command(BaseCommand):
    help = "Oznacza jako wygasłe dokumenty po terminie ważności i powiadamia właścicieli (uruchamiać raz dziennie)."

    def handle(self, *args, **options):
        count = expire_outdated()
        self.stdout.write(f"Wygaszono dokumentów: {count}")
