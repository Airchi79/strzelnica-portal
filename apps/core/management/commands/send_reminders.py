from django.core.management.base import BaseCommand

from apps.bookings.services import send_reminders


class Command(BaseCommand):
    help = "Wysyła przypomnienia o rezerwacjach zaczynających się w ciągu 24 h (uruchamiać co godzinę)."

    def handle(self, *args, **options):
        self.stdout.write(f"Wysłano przypomnień: {send_reminders()}")
