from django.core.management.base import BaseCommand

from apps.bookings.services import expire_unpaid
from apps.events.services import expire_unpaid_registrations


class Command(BaseCommand):
    help = "Zwalnia rezerwacje nieopłacone w wyznaczonym terminie (grupy „tylko online”). Uruchamiać co kilka minut."

    def handle(self, *args, **options):
        self.stdout.write(f"Zwolniono rezerwacji: {expire_unpaid()}")
        self.stdout.write(f"Wycofano nieopłaconych zapisów na wydarzenia: {expire_unpaid_registrations()}")
