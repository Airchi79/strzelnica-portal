"""Wspólny, bezpieczny odczyt arkuszy XLSX z importów (m.in. eksporty GunTrackera)."""

import io

from django.core.exceptions import ValidationError

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50000


def read_table(uploaded, required, max_bytes=MAX_BYTES):
    """Zwraca (nagłówki, [(nr_wiersza, {nagłówek: wartość})]) z pierwszego arkusza. Nagłówki porównywane bez wielkości liter."""
    from openpyxl import load_workbook

    if not (uploaded.name or "").lower().endswith(".xlsx"):
        raise ValidationError("Wybierz plik XLSX.")
    if uploaded.size > max_bytes:
        raise ValidationError("Plik jest za duży.")
    try:
        wb = load_workbook(io.BytesIO(uploaded.read()), read_only=True, data_only=True)
    except Exception as e:  # uszkodzony lub nie-XLSX plik — openpyxl zgłasza różne wyjątki
        raise ValidationError("To nie jest poprawny plik Excel (XLSX).") from e
    ws = wb.worksheets[0]
    ws.reset_dimensions()  # GunTracker zapisuje w pliku błędny rozmiar arkusza („A1:A1”)
    rows = ws.iter_rows(values_only=True)
    header = [str(v or "").strip() for v in next(rows, [])]
    lower = [h.lower() for h in header]
    missing = [c for c in required if c.lower() not in lower]
    if missing:
        raise ValidationError(f"Brak kolumn: {', '.join(missing)}. Użyj eksportu z GunTrackera bez zmian w nagłówkach.")
    out = []
    for row_no, values in enumerate(rows, start=2):
        if row_no > MAX_ROWS:
            raise ValidationError("Arkusz ma zbyt wiele wierszy.")
        if not any(v not in (None, "") for v in values):
            continue
        out.append((row_no, {lower[i]: values[i] for i in range(min(len(values), len(lower))) if lower[i]}))
    return header, out


def text(value, limit):
    return " ".join(str(value if value is not None else "").split())[:limit]
