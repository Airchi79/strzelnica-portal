"""Eksport książki pobytu do arkusza XLSX (pełne dane — dostęp rejestrowany w widoku)."""

from io import BytesIO

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

HEADERS = [
    "Nr wpisu", "Wejście", "Wyjście", "Wyjście automatyczne", "Imię", "Nazwisko", "Nr pozwolenia na broń", "Organ wydający",
    "Adres", "Cel pobytu", "Opis celu", "Rezerwacja", "Oś", "Prowadzący strzelanie", "Sposób wpisu", "Wpisał (obsługa)",
    "Wersja regulaminu", "Oświadczenie i podpis", "Integralność",
]


def safe(value):
    """Chroni przed wstrzyknięciem formuły (CSV/XLSX injection) w polach wpisanych przez użytkownika."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _local(dt):
    return timezone.localtime(dt).replace(tzinfo=None) if dt else None


def entries_xlsx(entries):
    wb = Workbook()
    ws = wb.active
    ws.title = "Książka pobytu"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for e in entries:
        r = e.reservation
        ws.append([safe(v) for v in [
            e.number, _local(e.entered_at), _local(e.exited_at), "tak" if e.exit_auto else "", e.first_name, e.last_name,
            e.permit_number, e.permit_issuer, e.address, e.get_purpose_display(), e.purpose_note,
            r.number if r else "", str(r.lane) if r else "", e.range_officer.full_name if e.range_officer else "",
            e.get_source_display(), e.created_by.full_name if e.created_by else "",
            e.regulation_version.number if e.regulation_version else "", "złożone (podpis w systemie)",
            "zgodna" if e.integrity_ok else "NIEZGODNOŚĆ",
        ]])
    for row in ws.iter_rows(min_row=2, min_col=2, max_col=3):
        for cell in row:
            cell.number_format = "yyyy-mm-dd hh:mm"
    widths = [16, 17, 17, 10, 14, 18, 18, 30, 34, 22, 24, 20, 14, 22, 26, 20, 10, 24, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
