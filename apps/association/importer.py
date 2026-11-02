"""Import danych stowarzyszenia z pakietu (dane.json, regulamin .txt, skany PDF) — z katalogu (polecenie)
albo z pliku ZIP wgranego w panelu. Import jest idempotentny: istniejące wpisy są pomijane.
Tryb podglądu wykonuje wszystko w transakcji i ją wycofuje — nic nie zostaje zapisane."""

import io
import json
import posixpath
import zipfile
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from . import services
from .models import Member, Resolution

MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 200


class DirectoryPackage:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _path(self, name):
        path = (self.root / name).resolve()
        if self.root not in path.parents:
            raise ValidationError(f"Nieprawidłowa ścieżka w pakiecie: {name}")
        return path

    def read(self, name):
        path = self._path(name)
        if not path.is_file():
            return None
        return path.read_bytes()


class ZipPackage:
    """Pakiet ZIP; katalog z plikiem dane.json może być zagnieżdżony (np. pakiet-stowarzyszenie/dane.json)."""

    def __init__(self, uploaded):
        if uploaded.size > MAX_ZIP_BYTES:
            raise ValidationError("Plik jest za duży (najwyżej 50 MB).")
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(uploaded.read()))
        except zipfile.BadZipFile as e:
            raise ValidationError("To nie jest poprawny plik ZIP.") from e
        infos = [i for i in self.zip.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise ValidationError("Pakiet zawiera zbyt wiele plików.")
        self.files = {}
        for info in infos:
            name = posixpath.normpath(info.filename.replace("\\", "/"))
            if name.startswith("/") or name.startswith("..") or "/../" in name:
                raise ValidationError(f"Nieprawidłowa ścieżka w pakiecie: {info.filename}")
            self.files[name] = info
        candidates = sorted((n for n in self.files if posixpath.basename(n) == "dane.json"), key=lambda n: n.count("/"))
        if not candidates:
            raise ValidationError("W pakiecie nie ma pliku dane.json.")
        self.base = posixpath.dirname(candidates[0])

    def read(self, name):
        key = posixpath.normpath(posixpath.join(self.base, name)) if self.base else posixpath.normpath(name)
        info = self.files.get(key)
        if info is None:
            return None
        if info.file_size > MAX_FILE_BYTES:
            raise ValidationError(f"Plik {name} jest za duży.")
        return self.zip.read(info)


def run(package, *, by=None, preview=False, can=lambda section: True):
    """Wykonuje import. Zwraca listę wierszy raportu. `can(section)` — czy użytkownik może importować daną część
    (sekcje: stowarzyszenie, regulamin, czlonkowie, uchwaly)."""
    raw = package.read("dane.json")
    if raw is None:
        raise ValidationError("W pakiecie nie ma pliku dane.json.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValidationError(f"Plik dane.json jest uszkodzony: {e}") from e
    if not isinstance(data, dict):
        raise ValidationError("Plik dane.json ma nieprawidłową budowę.")
    report = []
    with transaction.atomic():
        try:
            _import(package, data, by, report, can)
        except (KeyError, TypeError, ValueError) as e:
            raise ValidationError(f"Brak lub błąd pola w dane.json: {e}") from e
        if preview:
            transaction.set_rollback(True)
    return report


def _import(package, data, by, report, can):
    from apps.core.models import AssociationSettings
    from apps.regulations.models import RegulationDocument, RegulationVersion

    settings_data = data.get("stowarzyszenie") or {}
    if settings_data:
        if can("stowarzyszenie"):
            s = AssociationSettings.load()
            filled = []
            for field in ("name", "address", "nip", "regon", "contact_email", "contact_phone", "notify_email"):
                if settings_data.get(field) and not getattr(s, field):
                    setattr(s, field, str(settings_data[field])[:255])
                    filled.append(field)
            s.save()
            if filled:
                report.append(("ok", f"Dane stowarzyszenia: uzupełnienie pól {', '.join(filled)}"))
            else:
                report.append(("pominieto", "Dane stowarzyszenia: pola już wypełnione — bez zmian"))
        else:
            report.append(("pominieto", "Dane stowarzyszenia: brak uprawnienia — pominięto"))

    rules = data.get("regulamin")
    if rules:
        doc = RegulationDocument.objects.filter(slug="regulamin-stowarzyszenia").first()
        if not can("regulamin"):
            report.append(("pominieto", "Regulamin Stowarzyszenia: brak uprawnienia — pominięto"))
        elif doc is None:
            report.append(("blad", "Regulamin Stowarzyszenia: brak dokumentu w systemie"))
        elif doc.versions.exists():
            report.append(("pominieto", "Regulamin Stowarzyszenia: już istnieje — pominięto"))
        else:
            text = package.read(rules["plik"])
            if text is None:
                report.append(("blad", f"Regulamin Stowarzyszenia: brak pliku {rules['plik']}"))
            else:
                effective = timezone.make_aware(timezone.datetime.fromisoformat(rules["obowiazuje_od"]))
                RegulationVersion.objects.create(
                    document=doc, number=1, content=text.decode("utf-8"), change_note=rules.get("opis", ""), effective_from=effective,
                    published_at=timezone.now(),
                )
                report.append(("ok", "Regulamin Stowarzyszenia: wprowadzono wersję 1"))

    for m in data.get("czlonkowie", []):
        label = f"Członek nr {m['numer']}: {m['imie']} {m['nazwisko']}"
        if not can("czlonkowie"):
            report.append(("pominieto", f"{label} — brak uprawnienia"))
        elif Member.objects.filter(number=m["numer"]).exists():
            report.append(("pominieto", f"{label} — już w rejestrze"))
        else:
            services.add_member(
                number=m["numer"], first_name=m["imie"], last_name=m["nazwisko"], email=m.get("email", ""),
                address=m.get("adres", ""), kind=m["rodzaj"], joined_on=date.fromisoformat(m["data_przyjecia"]),
                founder=m.get("zalozyciel", False), board_role=m.get("funkcja", ""), by=by,
            )
            report.append(("ok", f"{label} — wpisano"))

    for r in data.get("uchwaly", []):
        label = f"Uchwała {r['numer']}/{r['rok']} ({r['organ']})"
        if not can("uchwaly"):
            report.append(("pominieto", f"{label} — brak uprawnienia"))
            continue
        if Resolution.objects.filter(organ=r["organ"], year=r["rok"], number=r["numer"]).exists():
            report.append(("pominieto", f"{label} — już w rejestrze"))
            continue
        attachment, note = None, ""
        if r.get("skan"):
            content = package.read(r["skan"])
            if content is None:
                note = f" (brak skanu {r['skan']})"
            elif not content.startswith(b"%PDF"):
                note = f" (pominięto skan {r['skan']} — to nie jest PDF)"
            else:
                attachment = ContentFile(content, name=posixpath.basename(r["skan"]))
        res = services.add_historical(
            organ=r["organ"], year=r["rok"], number=r["numer"], adopted_on=date.fromisoformat(r["data"]),
            title=r["tytul"], content=r["tresc"], attachment=attachment, by=by,
        )
        if r.get("glosy"):
            res.votes_for, res.votes_against, res.votes_abstain = r["glosy"]
            res.save(update_fields=["votes_for", "votes_against", "votes_abstain"])
        report.append(("blad" if note else "ok", f"{res.display_number}: wprowadzono{' ze skanem' if attachment else ''}{note}"))
    return report
