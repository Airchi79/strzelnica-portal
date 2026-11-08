from django.core.management.base import BaseCommand

from apps.payments.services import process_refund_queue


class Command(BaseCommand):
    help = "Wysyła zlecone zwroty Tpay, które nie wyszły po zatwierdzeniu operacji; zawieszone oznacza do sprawdzenia (co minutę)."

    def handle(self, *args, **options):
        sent, stuck = process_refund_queue()
        self.stdout.write(f"Zwroty Tpay: wysłano {sent}, do sprawdzenia {stuck}")
