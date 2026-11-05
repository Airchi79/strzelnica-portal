from django.core.management.base import BaseCommand

from apps.core.announcements import send_queued


class Command(BaseCommand):
    help = "Wysyła e-maile z ogłoszeniami w partiach (uruchamiane z harmonogramu co 5 minut)."

    def handle(self, *args, **options):
        self.stdout.write(f"Wysłano e-maili z ogłoszeniami: {send_queued()}")
