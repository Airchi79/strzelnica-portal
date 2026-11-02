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


class JsonPackageMixin:
    def data(self):
        raw = self.read("dane.json")
        if raw is None:
            raise ValidationError("W pakiecie nie ma pliku dane.json.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValidationError(f"Plik dane.json jest uszkodzony: {e}") from e
        if not isinstance(data, dict):
            raise ValidationError("Plik dane.json ma nieprawidłową budowę.")
        return data


class DirectoryPackage(JsonPackageMixin):
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


class ZipPackage(JsonPackageMixin):
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


# --- arkusz XLSX (członkowie i uchwały bez skanów) -------------------------------------------------

MEMBER_COLUMNS = ["Nr", "Imię", "Nazwisko", "E-mail", "Adres", "Rodzaj", "Data przyjęcia", "Założyciel", "Funkcja w Zarządzie"]
RESOLUTION_COLUMNS = ["Organ", "Rok", "Numer", "Data podjęcia", "Tytuł", "Treść", "Głosy za", "Głosy przeciw", "Wstrzymujące się"]
SHEET_MEMBERS, SHEET_RESOLUTIONS = "Członkowie", "Uchwały"


def _norm(value):
    return str(value or "").strip().lower()


def _choice(value, choices, field, row):
    """Przyjmuje kod (np. „nadzwyczajny”) albo etykietę (np. „Członek nadzwyczajny”), bez względu na wielkość liter."""
    v = _norm(value)
    for code, label in choices:
        if v in (code.lower(), str(label).lower()) or (code and v == f"członek {code}"):
            return code
    allowed = ", ".join(c for c, _ in choices if c)
    raise ValidationError(f"Arkusz, wiersz {row}: „{value}” w kolumnie {field} — dozwolone: {allowed}.")


def _date(value, field, row):
    from datetime import datetime

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValidationError(f"Arkusz, wiersz {row}: nieprawidłowa data „{value}” w kolumnie {field} (np. 10.07.2025).")


def _int(value, field, row, required=True):
    if value in (None, "") and not required:
        return None
    try:
        number = int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        raise ValidationError(f"Arkusz, wiersz {row}: w kolumnie {field} wpisz liczbę.") from None
    if number < 0:
        raise ValidationError(f"Arkusz, wiersz {row}: w kolumnie {field} liczba nie może być ujemna.")
    return number


def _rows(ws, columns):
    header = [str(c.value or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]
    index = {}
    for col in columns:
        if col not in header:
            raise ValidationError(f"Arkusz „{ws.title}”: brak kolumny „{col}”. Użyj wzoru pobranego z panelu.")
        index[col] = header.index(col)
    for row_no, values in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not any(v not in (None, "") for v in values):
            continue
        yield row_no, {col: values[i] if i < len(values) else None for col, i in index.items()}


class XlsxPackage:
    """Arkusz Excela z kartami „Członkowie” i/lub „Uchwały” (wzór do pobrania w panelu). Bez plików — skany uchwał
    dodaje się w panelu albo pakietem ZIP."""

    def __init__(self, uploaded):
        from openpyxl import load_workbook

        if uploaded.size > MAX_ZIP_BYTES:
            raise ValidationError("Plik jest za duży (najwyżej 50 MB).")
        try:
            self.wb = load_workbook(io.BytesIO(uploaded.read()), read_only=True, data_only=True)
        except Exception as e:  # uszkodzony lub nie-XLSX plik — openpyxl zgłasza różne wyjątki
            raise ValidationError("To nie jest poprawny plik Excel (XLSX).") from e
        if SHEET_MEMBERS not in self.wb.sheetnames and SHEET_RESOLUTIONS not in self.wb.sheetnames:
            raise ValidationError(f"W arkuszu nie ma karty „{SHEET_MEMBERS}” ani „{SHEET_RESOLUTIONS}”. Użyj wzoru pobranego z panelu.")

    def read(self, name):
        return None

    def data(self):
        data = {"czlonkowie": [], "uchwaly": []}
        if SHEET_MEMBERS in self.wb.sheetnames:
            for row, r in _rows(self.wb[SHEET_MEMBERS], MEMBER_COLUMNS):
                if not str(r["Imię"] or "").strip() or not str(r["Nazwisko"] or "").strip():
                    raise ValidationError(f"Arkusz „{SHEET_MEMBERS}”, wiersz {row}: podaj imię i nazwisko.")
                data["czlonkowie"].append({
                    "numer": _int(r["Nr"], "Nr", row), "imie": str(r["Imię"]).strip()[:80], "nazwisko": str(r["Nazwisko"]).strip()[:120],
                    "email": str(r["E-mail"] or "").strip()[:254], "adres": str(r["Adres"] or "").strip()[:255],
                    "rodzaj": _choice(r["Rodzaj"], Member.Kind.choices, "Rodzaj", row),
                    "data_przyjecia": _date(r["Data przyjęcia"], "Data przyjęcia", row).isoformat(),
                    "zalozyciel": _norm(r["Założyciel"]) in ("tak", "t", "1", "true", "x"),
                    "funkcja": _choice(r["Funkcja w Zarządzie"], Member.BoardRole.choices, "Funkcja w Zarządzie", row)
                    if _norm(r["Funkcja w Zarządzie"]) not in ("", "—", "-", "brak", "nie") else "",
                })
        if SHEET_RESOLUTIONS in self.wb.sheetnames:
            for row, r in _rows(self.wb[SHEET_RESOLUTIONS], RESOLUTION_COLUMNS):
                if not str(r["Tytuł"] or "").strip():
                    raise ValidationError(f"Arkusz „{SHEET_RESOLUTIONS}”, wiersz {row}: podaj tytuł uchwały.")
                votes = [_int(r[c], c, row, required=False) for c in ("Głosy za", "Głosy przeciw", "Wstrzymujące się")]
                item = {
                    "organ": _choice(r["Organ"], Resolution.Organ.choices, "Organ", row), "rok": _int(r["Rok"], "Rok", row),
                    "numer": _int(r["Numer"], "Numer", row), "data": _date(r["Data podjęcia"], "Data podjęcia", row).isoformat(),
                    "tytul": str(r["Tytuł"]).strip()[:255], "tresc": str(r["Treść"] or "").strip(),
                }
                if any(v is not None for v in votes):
                    item["glosy"] = [v or 0 for v in votes]
                data["uchwaly"].append(item)
        return data


def template_xlsx():
    """Wzór arkusza do importu: karty z nagłówkami, listy wyboru i instrukcja."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_MEMBERS
    ws.append(MEMBER_COLUMNS)
    for col, choices in (("F", "zwyczajny,nadzwyczajny"), ("H", "tak,nie"), ("I", "prezes,wiceprezes,brak")):
        dv = DataValidation(type="list", formula1=f'"{choices}"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}1000")
    ws2 = wb.create_sheet(SHEET_RESOLUTIONS)
    ws2.append(RESOLUTION_COLUMNS)
    dv = DataValidation(type="list", formula1='"zarzad,wzc,nadzwyczajni,zalozycielskie"', allow_blank=True)
    ws2.add_data_validation(dv)
    dv.add("A2:A1000")
    for sheet, widths in ((ws, [6, 16, 20, 28, 34, 16, 16, 12, 20]), (ws2, [16, 8, 8, 16, 40, 60, 10, 12, 16])):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for i, w in enumerate(widths):
            sheet.column_dimensions[chr(65 + i)].width = w
        sheet.freeze_panes = "A2"
    info = wb.create_sheet("Instrukcja")
    for line in [
        "Import do rejestru Stowarzyszenia — wypełnij karty „Członkowie” i/lub „Uchwały” od 2. wiersza.",
        "Członkowie: Nr — numer w rejestrze; Rodzaj — zwyczajny lub nadzwyczajny; Data przyjęcia — np. 10.07.2025;",
        "  Założyciel — tak/nie; Funkcja w Zarządzie — prezes, wiceprezes albo puste.",
        "Uchwały (historyczne, z dokumentów papierowych): Organ — zarzad, wzc, nadzwyczajni, zalozycielskie;",
        "  Rok i Numer — jak w numerze uchwały (np. 1/2025); głosy — opcjonalnie.",
        "Wpisy, które już są w rejestrze (ten sam numer członka / ta sama uchwała), są pomijane.",
        "Skany uchwał (PDF) dodaje się pakietem ZIP albo w panelu przy uchwale.",
        "Przed zapisem użyj przycisku „Podgląd” — pokaże, co zostanie wprowadzone.",
    ]:
        info.append([line])
    info.column_dimensions["A"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def open_upload(uploaded):
    name = (uploaded.name or "").lower()
    if name.endswith(".xlsx"):
        return XlsxPackage(uploaded)
    if name.endswith(".zip"):
        return ZipPackage(uploaded)
    raise ValidationError("Wybierz plik ZIP (pakiet ze skanami) albo XLSX (arkusz według wzoru).")


def run(package, *, by=None, preview=False, can=lambda section: True):
    """Wykonuje import. Zwraca listę wierszy raportu. `can(section)` — czy użytkownik może importować daną część
    (sekcje: stowarzyszenie, regulamin, czlonkowie, uchwaly)."""
    data = package.data()
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
