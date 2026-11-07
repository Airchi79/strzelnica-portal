from django.core.management.base import BaseCommand

from apps.reports.services import generate_missing


class Command(BaseCommand):
    help = "Generuje brakujące numerowane raporty wpływów: dzienne (dni z operacjami) i miesięczne (zakończone miesiące)."

    def handle(self, *args, **options):
        created = generate_missing()
        self.stdout.write(f"Nowe raporty: {', '.join(r.number for r in created) or 'brak'}")
