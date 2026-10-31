import calendar
from datetime import date


def add_months(d: date, months: int) -> date:
    """Dodaje miesiące; dzień przycinany do długości miesiąca (31.12 + 2 mies. = 28/29.02)."""
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))
