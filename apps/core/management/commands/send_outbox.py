from django.core.management.base import BaseCommand

from apps.core.notifications import send_outbox


class Command(BaseCommand):
    help = "Ponawia wysyłkę e-maili, których serwer poczty nie przyjął, i usuwa wysłane starsze niż 30 dni (co minutę)."

    def handle(self, *args, **options):
        sent, failed = send_outbox()
        self.stdout.write(f"Poczta: wysłano {sent}, nieudane próby {failed}")
