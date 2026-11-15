"""Automatyczne pobieranie faktur z KSeF do skrzynki „Faktury KSeF” (przyrostowo, wg daty trwałego zapisu w KSeF).

Przebieg (co godzinę z crona albo przyciskiem w panelu):
1. uwierzytelnienie tokenem KSeF z .env w kontekście NIP stowarzyszenia,
2. zapytania o metadane faktur zakupu (Subject2 — stowarzyszenie jako nabywca), opcjonalnie sprzedaży (Subject1),
   od zapamiętanej daty; nowe numery KSeF trafiają do kolejki,
3. pobranie XML faktur z kolejki i import (ten sam parser co przy wgrywaniu plików), duplikaty pomijane.

Limity API KSeF (OpenAPI MF): metadane 20 zapytań na godzinę, pobranie faktury 64 na godzinę — jeden przebieg wykonuje
najwyżej kilka zapytań i kilkadziesiąt pobrań; resztę kończą kolejne przebiegi. Przebiegi nie nakładają się (blokada)."""

import datetime as dt
import re

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit.services import log_event

from . import ksef, ksef_api
from .models import KsefInvoice, KsefSync

LOCK_KEY = "ksiegowosc:ksef-sync"
LOCK_SECONDS = 15 * 60
MAX_QUERIES_PER_SUBJECT = 3
MAX_DOWNLOADS = 40
MAX_PENDING = 500
PAGE_SIZE = 100
WINDOW = dt.timedelta(days=90)  # KSeF przyjmuje zakres do 100 dni


def _digits(value):
    return re.sub(r"\D", "", value or "")


def _parse(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def subjects(state):
    return ["Subject2"] + (["Subject1"] if state.include_sales else [])


def _start(state, subject):
    if state.hwm.get(subject):
        return _parse(state.hwm[subject])
    return timezone.make_aware(dt.datetime.combine(state.sync_from, dt.time.min))


def _collect(access, state, subject, now):
    """Dopisuje do kolejki nowe numery KSeF; przesuwa znacznik daty. Zwraca liczbę nowych pozycji."""
    known = {p["n"] for p in state.pending}
    added = 0
    date_from, offset = _start(state, subject), 0
    for _ in range(MAX_QUERIES_PER_SUBJECT):
        if date_from >= now or len(state.pending) >= MAX_PENDING:
            break
        date_to = min(date_from + WINDOW, now)
        page = ksef_api.query_metadata(access, subject_type=subject, date_from=date_from, date_to=date_to, page_offset=offset,
                                       page_size=PAGE_SIZE)
        invoices = page.get("invoices") or []
        numbers = [i["ksefNumber"] for i in invoices]
        existing = set(KsefInvoice.objects.filter(ksef_number__in=numbers).values_list("ksef_number", flat=True))
        for inv in invoices:
            n = inv["ksefNumber"]
            if n not in existing and n not in known:
                state.pending.append({"n": n, "s": subject})
                known.add(n)
                added += 1
        last = _parse(invoices[-1]["permanentStorageDate"]) if invoices else None
        if page.get("hasMore") and not page.get("isTruncated"):
            offset += PAGE_SIZE
            if last:
                state.hwm[subject] = last.isoformat()  # na wypadek końca budżetu zapytań — duplikaty odfiltruje kolejka
            continue
        if page.get("hasMore") and page.get("isTruncated"):
            date_from, offset = last, 0
            state.hwm[subject] = last.isoformat()
            continue
        # koniec wyników w oknie: znacznik = koniec okna (ograniczony do daty trwałego zapisu potwierdzonej przez KSeF)
        mark = date_to
        if page.get("permanentStorageHwmDate"):
            mark = min(mark, _parse(page["permanentStorageHwmDate"]))
        state.hwm[subject] = mark.isoformat()
        date_from, offset = mark, 0
        if date_to >= now:
            break
    return added


def run(*, by=None, now=None, force=False):
    """Jeden przebieg pobierania. Zwraca słownik z wynikiem (także gdy pobieranie jest wyłączone)."""
    from apps.core.models import AssociationSettings

    now = now or timezone.now()
    state = KsefSync.load()
    if not settings.KSEF_ENABLED:
        return {"ok": False, "message": "Brak tokena KSeF (KSEF_TOKEN w pliku .env) — pobieranie wyłączone."}
    if not (state.enabled or force):
        return {"ok": False, "message": "Automatyczne pobieranie jest wyłączone w ustawieniach KSeF."}
    if not state.sync_from:
        return {"ok": False, "message": "Ustaw datę, od której pobierać faktury."}
    nip = _digits(AssociationSettings.load().nip)
    if len(nip) != 10:
        return {"ok": False, "message": "Uzupełnij NIP w „Dane stowarzyszenia” — KSeF wymaga kontekstu NIP."}
    if not cache.add(LOCK_KEY, "1", LOCK_SECONDS):
        return {"ok": False, "message": "Pobieranie już trwa — spróbuj za kilka minut."}
    created, failed, queued = [], [], 0
    error = ""
    try:
        access = ksef_api.authenticate(nip)
        for subject in subjects(state):
            queued += _collect(access, state, subject, now)
        for item in list(state.pending[:MAX_DOWNLOADS]):
            try:
                content = ksef_api.download_invoice(access, item["n"])
            except ksef_api.KsefError as e:
                if e.transient:
                    error = str(e)
                    break
                failed.append(f"{item['n']}: {e}")
                state.pending.remove(item)
                continue
            try:
                invoice = ksef.import_content(f"{item['n']}.xml", content, by=by, own_nip=nip, source="api")
            except ValidationError as e:
                failed.append(e.messages[0])
            else:
                if invoice is not None:
                    created.append(invoice)
            state.pending.remove(item)
    except ksef_api.KsefError as e:
        error = str(e)
    finally:
        cache.delete(LOCK_KEY)
    state.last_run_at = now
    parts = [f"nowe faktury: {len(created)}"]
    if queued:
        parts.append(f"znalezione w KSeF: {queued}")
    if state.pending:
        parts.append(f"czeka na pobranie: {len(state.pending)}")
    if failed:
        parts.append(f"błędne: {len(failed)} ({'; '.join(failed)[:200]})")
    state.last_result = ", ".join(parts)[:500]
    state.last_error = error[:500]
    if not error:
        state.last_success_at = now
    state.save()
    if created or failed or error:
        log_event("ksiegowosc.ksef_pobieranie", None, f"KSeF: {state.last_result}" + (f"; błąd: {error}" if error else ""), actor=by)
    return {"ok": not error, "message": state.last_result + (f" — {error}" if error else ""), "created": created}
