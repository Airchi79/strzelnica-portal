from django.core.management.base import BaseCommand

from apps.accounting.services import sync_from_sales_reports


class Command(BaseCommand):
    help = "Dopisuje do ewidencji przychodów i kosztów przychody z dziennych raportów wpływów (od dnia z ustawień księgowości)."

    def handle(self, *args, **options):
        created = sync_from_sales_reports()
        self.stdout.write(f"Nowe zapisy w ewidencji: {', '.join(r.lp for r in created) or 'brak'}")
