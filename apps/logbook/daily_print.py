"""Dzienne wydruki książki pobytu (PDF) zapisywane w systemie — archiwum do wydruku w każdej chwili.

Zasady:
- dzień D drukuje się po jego zakończeniu, gdy wszystkie pobyty z tego dnia mają zapisane wyjście (albo po czasie
  automatycznego zamknięcia pobytu + 1 h — wtedy otwarty pobyt jest na wydruku bez godziny wyjścia);
- wydruk obejmuje każdy dzień od pierwszego wpisu w książce, także dzień bez wpisów (ciągłość archiwum);
- gdy dane dnia zmienią się później (korekta, zapisane wyjście), powstaje kolejna wersja, poprzednia zostaje;
- każdy wydruk zawiera sumę SHA-256 poprzedniego wydruku (łańcuch), a suma pliku jest zapisana w bazie —
  zmiana, podmiana lub usunięcie wydruku ze środka jest wykrywalne (`verify_chain`);
- plik PDF jest zabezpieczony przed edycją (uprawnienia PDF), a rekord w bazie przed zmianą i usunięciem (trigger).
"""

import hashlib
import io
import json
import secrets
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path

from django.db import connection, transaction
from django.db.models import Min
from django.db.models.expressions import RawSQL
from django.utils import timezone

from apps.audit.services import log_event

from .models import DailyPrint, Entry, EntryCorrection, LogbookSettings

BATCH_DAYS = 60  # najwięcej nowych dni w jednym przebiegu (pierwsze uruchomienie dorabia zaległe dni stopniowo)
RECHECK_DAYS = 45  # tyle ostatnich dni sprawdza się co przebieg pod kątem zmian (starsze — gdy pojawi się korekta)
LOCK_ID = 7_310_501  # pg_advisory_xact_lock — jeden generator naraz


def day_bounds(day):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, time.min), tz)
    return start, timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min), tz)


def day_entries(day):
    from .services import apply_corrections

    start, end = day_bounds(day)
    qs = Entry.objects.filter(entered_at__gte=start, entered_at__lt=end).select_related("reservation__lane", "range_officer")
    return apply_corrections(qs.order_by("entered_at", "id"))


def data_digest(day):
    """Suma danych dnia: oryginały wpisów (ich sumy kontrolne), wyjścia i korekty. Zmiana któregokolwiek → nowa wersja."""
    start, end = day_bounds(day)
    rows = list(Entry.objects.filter(entered_at__gte=start, entered_at__lt=end).order_by("entered_at", "id")
                .values_list("number", "content_sha256", "exited_at", "exit_auto"))
    corrections = list(EntryCorrection.objects.filter(entry__entered_at__gte=start, entry__entered_at__lt=end)
                       .order_by("id").values_list("id", flat=True))
    payload = {"day": day.isoformat(), "entries": [[n, h, e.isoformat() if e else None, a] for n, h, e, a in rows],
               "corrections": corrections}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def day_ready(day, now):
    """Dzień zakończony i pobyty z tego dnia zamknięte (albo minął czas automatycznego zamknięcia + 1 h)."""
    start, end = day_bounds(day)
    if now < end:
        return False
    if not Entry.objects.filter(entered_at__gte=start, entered_at__lt=end, exited_at__isnull=True).exists():
        return True
    hours = LogbookSettings.load().auto_exit_hours or 24
    return now >= end + timedelta(hours=hours + 1)


def _fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    fonts = Path(__file__).resolve().parent.parent / "reports" / "fonts"
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", str(fonts / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(fonts / "DejaVuSans-Bold.ttf")))


def render_pdf(day, entries, version, prev_sha256, generated_at, association):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    _fonts()
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=7.5, leading=9.5, alignment=TA_LEFT)
    small = ParagraphStyle("s", parent=base, fontSize=6.8, leading=8.5, textColor=colors.HexColor("#444444"))
    head = ParagraphStyle("h", parent=base, fontName="DejaVu-Bold", fontSize=13, leading=16)
    buf = io.BytesIO()
    # Uprawnienia PDF: druk i kopiowanie tak, edycja i adnotacje nie (hasło właściciela losowe, nigdzie nie zapisywane).
    enc = StandardEncryption("", ownerPassword=secrets.token_hex(16), canPrint=1, canModify=0, canCopy=1, canAnnotate=0, strength=128)
    title = f"Książka rejestru pobytu na strzelnicy — {day:%d.%m.%Y}"
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm,
                            bottomMargin=14 * mm, title=title, author=association.name or "Strzelnica", encrypt=enc)
    gen = timezone.localtime(generated_at)
    story = [
        Paragraph(escape(title), head),
        Paragraph(escape(f"{association.name}{', ' + association.address if association.address else ''}"), base),
        Paragraph(escape(f"Wydruk dzienny, wersja {version} · wygenerowano automatycznie {gen:%d.%m.%Y %H:%M} · wpisów: {len(entries)}"), base),
        Spacer(1, 3 * mm),
    ]
    if entries:
        story.append(Paragraph(escape(f"Oświadczenie każdej osoby: „{entries[0].statement}”"), small))
        story.append(Spacer(1, 2 * mm))
        data = [["Nr wpisu", "Wejście", "Wyjście", "Imię i nazwisko", "Nr pozwolenia i organ / adres", "Cel pobytu",
                 "Prowadzący / oś", "Podpis"]]
        for e in entries:
            out = timezone.localtime(e.exited_at).strftime("%d.%m %H:%M") if e.exited_at else "—"
            if e.exited_at and e.exit_auto:
                out += " (auto)"
            ident = f"{e.permit_number}, {e.permit_issuer}" if e.has_permit else e.address
            purpose = e.get_purpose_display() + (f": {e.purpose_note}" if e.purpose_note else "")
            officer = ""
            if e.range_officer_id:
                officer = escape(e.range_officer.full_name) + (f"<br/>{escape(str(e.reservation.lane))}" if e.reservation_id else "")
            try:
                sig = Image(io.BytesIO(bytes(e.signature)), width=30 * mm, height=10 * mm, kind="proportional")
            except Exception:  # noqa: BLE001 — uszkodzony obraz nie może zatrzymać wydruku
                sig = Paragraph("podpis zapisany w systemie", small)
            data.append([
                Paragraph(escape(e.number) + ("*" if e.corrected_fields else ""), base),
                Paragraph(f"{timezone.localtime(e.entered_at):%H:%M}", base), Paragraph(escape(out), base),
                Paragraph(escape(e.full_name), base), Paragraph(escape(ident), base), Paragraph(escape(purpose), base),
                Paragraph(officer, base), sig,
            ])
        widths = [24 * mm, 16 * mm, 22 * mm, 40 * mm, 52 * mm, 44 * mm, 44 * mm, 34 * mm]
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, 0), "DejaVu-Bold", 7.5), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#777777")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(t)
        if any(e.corrected_fields for e in entries):
            story += [Spacer(1, 2 * mm), Paragraph("* wpis skorygowany przez obsługę — pokazano wartości po korekcie; oryginał wpisu, "
                                                   "podpis i historia korekt (kto, kiedy, dlaczego) są przechowywane w systemie.", small)]
    else:
        story.append(Paragraph("Brak wpisów w tym dniu.", base))
    story += [Spacer(1, 4 * mm), Paragraph(
        "Wydruk wygenerowany automatycznie z elektronicznej książki rejestru pobytu. Wpisów nie można zmienić ani usunąć; "
        "korekty są zapisywane osobno z powodem i danymi osoby korygującej. Suma SHA-256 tego pliku jest zapisana w systemie, "
        "a każdy wydruk zawiera sumę poprzedniego — zmiana lub usunięcie wydruku jest wykrywalne.", small)]
    footer = f"Książka pobytu {day:%d.%m.%Y} · wersja {version} · poprzedni wydruk SHA-256: {prev_sha256 or '— (pierwszy w archiwum)'}"

    def on_page(canvas, d):
        canvas.saveState()
        canvas.setFont("DejaVu", 6.5)
        canvas.drawString(10 * mm, 7 * mm, footer)
        canvas.drawRightString(landscape(A4)[0] - 10 * mm, 7 * mm, f"strona {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def make_print(day, now=None, reason=None):
    """Tworzy wydruk dnia, jeśli go nie ma albo dane dnia się zmieniły. Zwraca nowy DailyPrint albo None."""
    from apps.core.models import AssociationSettings

    now = now or timezone.now()
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_ID])
        digest = data_digest(day)
        last = DailyPrint.objects.filter(day=day).order_by("-version").defer("pdf").first()
        if last and last.data_sha256 == digest:
            return None
        prev = DailyPrint.objects.order_by("-id").only("pdf_sha256").first()
        version = last.version + 1 if last else 1
        entries = day_entries(day)
        pdf = render_pdf(day, entries, version, prev.pdf_sha256 if prev else "", now, AssociationSettings.load())
        p = DailyPrint.objects.create(
            day=day, version=version, created_at=now, entries_count=len(entries), data_sha256=digest, pdf=pdf,
            pdf_sha256=hashlib.sha256(pdf).hexdigest(), prev_sha256=prev.pdf_sha256 if prev else "",
            reason=reason or ("wydruk dzienny" if version == 1 else "zmiana danych dnia (korekta lub zapisane wyjście)"),
        )
    log_event("ksiazka.wydruk_dzienny", summary=f"{day:%d.%m.%Y} wersja {version}, wpisów: {len(entries)}")
    return p


def run(now=None):
    """Przebieg okresowy (co godzinę): dorabia brakujące dni i nowe wersje dni, w których zmieniły się dane."""
    now = now or timezone.now()
    first = Entry.objects.aggregate(m=Min("entered_at"))["m"]
    if not first:
        return []
    today = timezone.localdate(now)
    start = timezone.localdate(first)
    printed = set(DailyPrint.objects.filter(day__gte=start).values_list("day", flat=True).distinct())
    created, new_days = [], 0
    day = start
    while day < today and new_days < BATCH_DAYS:
        if day not in printed and day_ready(day, now):
            created.append(make_print(day, now))
            new_days += 1
        day += timedelta(days=1)
    # Zmiany w dniach już wydrukowanych: ostatnie tygodnie zawsze, starsze — gdy pojawiła się korekta ich wpisu.
    recheck = {d for d in printed if d >= today - timedelta(days=RECHECK_DAYS)}
    recent = EntryCorrection.objects.filter(created_at__gte=now - timedelta(days=2)).values_list("entry__entered_at", flat=True)
    recheck |= {timezone.localdate(t) for t in recent} & printed
    for d in sorted(recheck):
        if day_ready(d, now):
            p = make_print(d, now)
            if p:
                created.append(p)
    return [p for p in created if p]


def verify_chain():
    """Sprawdza każdy wydruk: suma pliku zgodna z zapisaną i łańcuch nieprzerwany. Sumy liczy baza (bez pobierania plików).
    Zwraca (liczba sprawdzonych, lista problemów)."""
    rows = DailyPrint.objects.order_by("id").annotate(actual=RawSQL("encode(sha256(pdf), 'hex')", [])) \
        .values_list("id", "day", "version", "pdf_sha256", "prev_sha256", "actual")
    problems, prev, n = [], None, 0
    for _, day, version, stored, prev_sha, actual in rows.iterator():
        n += 1
        label = f"{day:%d.%m.%Y} v{version}"
        if actual != stored:
            problems.append(f"{label}: plik niezgodny z zapisaną sumą")
        if prev is not None and prev_sha != prev:
            problems.append(f"{label}: przerwany łańcuch (brak lub zmiana poprzedniego wydruku)")
        prev = stored
    return n, problems


def purge_before(cutoff_day):
    """Usuwa wydruki dni sprzed końca okresu przechowywania (wywoływane w czyszczeniu książki, z flagą purge)."""
    deleted, _ = DailyPrint.objects.filter(day__lt=cutoff_day).delete()
    return deleted
