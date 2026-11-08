from django.core.management.base import BaseCommand

from apps.bookings.services import send_reminders, send_transfer_reminders
from apps.events.services import send_payment_reminders


class Command(BaseCommand):
    help = "Wysyła przypomnienia o rezerwacjach zaczynających się w ciągu 24 h oraz o przelewach przed terminem (co godzinę)."

    def handle(self, *args, **options):
        self.stdout.write(f"Wysłano przypomnień: {send_reminders()}")
        self.stdout.write(f"Przypomnień o przelewie: {send_transfer_reminders()}")
        self.stdout.write(f"Przypomnień o opłacie startowej: {send_payment_reminders()}")
