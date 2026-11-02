"""Import danych stowarzyszenia z pakietu (katalog z plikiem dane.json i skanami PDF). To samo co przycisk
„Import danych” w panelu (Stowarzyszenie → Członkowie).

    python manage.py import_association_seed /sciezka/do/pakietu [--podglad]
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.association.importer import DirectoryPackage, run


class Command(BaseCommand):
    help = "Wczytuje członków, uchwały historyczne, dane stowarzyszenia i regulamin z pakietu danych."

    def add_arguments(self, parser):
        parser.add_argument("package", help="Katalog z plikiem dane.json (i ewentualnie skanami PDF)")
        parser.add_argument("--podglad", action="store_true", help="Pokaż, co zostanie zaimportowane, bez zapisywania")

    def handle(self, *args, package, podglad=False, **options):
        try:
            report = run(DirectoryPackage(package), preview=podglad)
        except ValidationError as e:
            raise CommandError(" ".join(e.messages)) from e
        for status, line in report:
            self.stdout.write(f"[{status}] {line}")
        self.stdout.write(self.style.SUCCESS("Podgląd — nic nie zapisano." if podglad else "Import zakończony."))
