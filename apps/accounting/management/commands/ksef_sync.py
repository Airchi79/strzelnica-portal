from django.core.management.base import BaseCommand

from apps.accounting.ksef_sync import run


class Command(BaseCommand):
    help = "Pobiera nowe faktury z KSeF do skrzynki „Faktury KSeF” (gdy ustawiono token KSeF i włączono pobieranie)."

    def handle(self, *args, **options):
        self.stdout.write(f"KSeF: {run()['message']}")
