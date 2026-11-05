from django.core.management.base import BaseCommand

from apps.bookings.waitlist import process


class Command(BaseCommand):
    help = "Lista oczekujących: wygasza nieodebrane oferty i proponuje termin kolejnym osobom (co 5 minut)."

    def handle(self, *args, **options):
        self.stdout.write(f"Wygasłe oferty z listy oczekujących: {process()}")
