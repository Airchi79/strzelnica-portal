from django.core.management.base import BaseCommand

from apps.credentials.services import expire_outdated, remind_expiring


class Command(BaseCommand):
    help = ("Przypomina o dokumentach wygasających za 30 i 7 dni, oznacza jako wygasłe dokumenty po terminie ważności "
            "i powiadamia właścicieli (uruchamiane z harmonogramu; każde powiadomienie wysyłane raz).")

    def handle(self, *args, **options):
        reminded = remind_expiring()
        count = expire_outdated()
        self.stdout.write(f"Przypomnienia o wygasaniu: {reminded}; wygaszono dokumentów: {count}")
