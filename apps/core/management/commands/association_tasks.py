from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.association import services


class Command(BaseCommand):
    help = "Zadania stowarzyszenia: naliczenie składek roku, przypomnienia o zaległościach i terminach deklaracji (co godzinę)."

    def handle(self, *args, **options):
        now = timezone.localtime()
        self.stdout.write(f"Naliczone składki: {services.ensure_annual_fees()}")
        self.stdout.write(f"Przypomnienia o zaległościach: {services.send_fee_reminders()}")
        if now.hour == 9:  # raz dziennie
            self.stdout.write(f"Deklaracje z bliskim terminem: {services.remind_board_about_applications()}")
