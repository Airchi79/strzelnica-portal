"""Czyszczenie danych z okresu testów przed uruchomieniem systemu. Szczegóły: apps/core/go_live.py.

    python manage.py purge_test_data --zostaw wlasciciel@example.pl            # raport: co zostanie usunięte
    python manage.py purge_test_data --zostaw wlasciciel@example.pl --wykonaj  # usunięcie (można powtarzać)
    python manage.py purge_test_data --zostaw wlasciciel@example.pl --wykonaj --koniec-testow
        # ostatnie czyszczenie przed uruchomieniem — potem polecenie jest zablokowane na stałe
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.core import go_live
from apps.core.backups import read_status


class Command(BaseCommand):
    help = "Usuwa dane z okresu testów (rezerwacje, książkę pobytu, płatności, konta poza właścicielem itd.). Bez --wykonaj tylko raport."

    def add_arguments(self, parser):
        parser.add_argument("--zostaw", required=True, help="Adres e-mail konta właściciela, które zostaje")
        parser.add_argument("--wykonaj", action="store_true", help="Usuń dane (bez tej opcji tylko raport)")
        parser.add_argument("--koniec-testow", action="store_true",
                            help="Po usunięciu oznacz koniec testów — kolejne czyszczenie będzie niemożliwe")
        parser.add_argument("--z-prawdziwymi-platnosciami", action="store_true",
                            help="Dopuść usunięcie prawdziwych (nie testowych) płatności Tpay")
        parser.add_argument("--bez-kopii", action="store_true", help="Nie wymagaj świeżej kopii zapasowej (tylko poza serwerem)")

    def handle(self, *args, zostaw, wykonaj, koniec_testow, z_prawdziwymi_platnosciami, bez_kopii, **options):
        user = get_user_model().objects.filter(email__iexact=zostaw.strip()).first()
        if not user:
            raise CommandError(f"Nie ma konta {zostaw}.")
        backup = None if bez_kopii else read_status()["state"]
        self.stdout.write(f"Zostaje konto: {user.email} ({user.first_name} {user.last_name})")
        rows = go_live.counts(user)
        width = max(len(label) for label, _ in rows)
        for label, n in rows:
            self.stdout.write(f"  {label.ljust(width)}  {n:>7}")
        self.stdout.write(f"  {'RAZEM'.ljust(width)}  {sum(n for _, n in rows):>7}")
        errors = go_live.check(user, z_prawdziwymi_platnosciami, backup)
        for e in errors:
            self.stdout.write(self.style.ERROR(f"! {e}"))
        if not wykonaj:
            self.stdout.write("Raport — nic nie usunięto. Aby usunąć, dodaj --wykonaj.")
            if errors:
                raise CommandError("Czyszczenie nie jest teraz możliwe (powody powyżej).")
            return
        try:
            result = go_live.run(user, end_of_tests=koniec_testow, allow_real_tpay=z_prawdziwymi_platnosciami, backup_state=backup)
        except ValidationError as e:
            raise CommandError("Nic nie usunięto: " + " ".join(e.messages)) from e
        self.stdout.write(self.style.SUCCESS(f"Usunięto rekordów: {sum(n for _, n in result)}."))
        if koniec_testow:
            self.stdout.write(self.style.SUCCESS("Oznaczono koniec testów — czyszczenie danych jest teraz zablokowane."))
