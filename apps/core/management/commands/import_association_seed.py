"""Import danych stowarzyszenia z pakietu (katalog z plikiem dane.json i skanami PDF).

Pakiet przygotowuje się poza repozytorium (zawiera dane osobowe). Import jest idempotentny: istniejące wpisy
(członkowie po numerze, uchwały po organie/roku/numerze) są pomijane.

    python manage.py import_association_seed /sciezka/do/pakietu
"""

import json
from datetime import date
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.association import services
from apps.association.models import Member, Resolution
from apps.core.models import AssociationSettings
from apps.regulations.models import RegulationDocument, RegulationVersion


class Command(BaseCommand):
    help = "Wczytuje członków, uchwały historyczne, dane stowarzyszenia i regulamin z pakietu danych."

    def add_arguments(self, parser):
        parser.add_argument("package", help="Katalog z plikiem dane.json (i ewentualnie skanami PDF)")

    @transaction.atomic
    def handle(self, *args, package, **options):
        root = Path(package)
        data_file = root / "dane.json"
        if not data_file.exists():
            raise CommandError(f"Brak pliku {data_file}")
        data = json.loads(data_file.read_text(encoding="utf-8"))

        settings_data = data.get("stowarzyszenie") or {}
        if settings_data:
            s = AssociationSettings.load()
            for field in ("name", "address", "nip", "regon", "contact_email", "contact_phone", "notify_email"):
                if settings_data.get(field) and not getattr(s, field):
                    setattr(s, field, settings_data[field])
            s.save()
            self.stdout.write("Dane stowarzyszenia: uzupełniono puste pola")

        rules = data.get("regulamin")
        if rules:
            doc = RegulationDocument.objects.get(slug="regulamin-stowarzyszenia")
            if not doc.versions.exists():
                text = (root / rules["plik"]).read_text(encoding="utf-8")
                effective = timezone.make_aware(timezone.datetime.fromisoformat(rules["obowiazuje_od"]))
                RegulationVersion.objects.create(
                    document=doc, number=1, content=text, change_note=rules.get("opis", ""), effective_from=effective,
                    published_at=timezone.now(),
                )
                self.stdout.write("Regulamin Stowarzyszenia: wprowadzono wersję 1")
            else:
                self.stdout.write("Regulamin Stowarzyszenia: już istnieje — pominięto")

        for m in data.get("czlonkowie", []):
            if Member.objects.filter(number=m["numer"]).exists():
                self.stdout.write(f"Członek nr {m['numer']}: już w rejestrze — pominięto")
                continue
            services.add_member(
                number=m["numer"], first_name=m["imie"], last_name=m["nazwisko"], email=m.get("email", ""),
                address=m.get("adres", ""), kind=m["rodzaj"], joined_on=date.fromisoformat(m["data_przyjecia"]),
                founder=m.get("zalozyciel", False), board_role=m.get("funkcja", ""), by=None,
            )
            self.stdout.write(f"Członek nr {m['numer']}: {m['imie']} {m['nazwisko']} — wpisano")

        for r in data.get("uchwaly", []):
            if Resolution.objects.filter(organ=r["organ"], year=r["rok"], number=r["numer"]).exists():
                self.stdout.write(f"Uchwała {r['numer']}/{r['rok']} ({r['organ']}): już w rejestrze — pominięto")
                continue
            attachment = None
            if r.get("skan"):
                path = root / r["skan"]
                if path.exists():
                    attachment = File(path.open("rb"), name=path.name)
            res = services.add_historical(
                organ=r["organ"], year=r["rok"], number=r["numer"], adopted_on=date.fromisoformat(r["data"]),
                title=r["tytul"], content=r["tresc"], attachment=attachment, by=None,
            )
            if r.get("glosy"):
                res.votes_for, res.votes_against, res.votes_abstain = r["glosy"]
                res.save(update_fields=["votes_for", "votes_against", "votes_abstain"])
            self.stdout.write(f"{res.display_number}: wprowadzono")
        self.stdout.write(self.style.SUCCESS("Import zakończony."))
