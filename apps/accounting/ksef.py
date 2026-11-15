"""Import faktur ustrukturyzowanych z Krajowego Systemu e-Faktur (KSeF): pliki XML w schemacie FA(2) / FA(3) pobrane
z KSeF (Aplikacja Podatnika KSeF, program księgowy, e-mail od kontrahenta) — pojedynczo albo w archiwum ZIP.

Parser jest odporny na wersję schematu (szuka elementów po nazwie lokalnej, bez przestrzeni nazw) i bezpieczny
(defusedxml — bez encji zewnętrznych i „bomb” XML; limity rozmiaru plików i archiwów).

Bezpośrednie pobieranie z API KSeF (token / certyfikat KSeF) — kolejny etap."""

import hashlib
import io
import re
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction

from apps.audit.services import log_event

from .models import KsefInvoice

MAX_XML_BYTES = 5 * 1024 * 1024
MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_ZIP_FILES = 300
MAX_ZIP_TOTAL = 100 * 1024 * 1024
KSEF_NUMBER = re.compile(r"\b(\d{10}-\d{8}-[0-9A-F]{12}-[0-9A-F]{2})\b", re.IGNORECASE)
NET_FIELDS = ["P_13_1", "P_13_2", "P_13_3", "P_13_4", "P_13_5", "P_13_6_1", "P_13_6_2", "P_13_6_3", "P_13_7", "P_13_8",
              "P_13_9", "P_13_10", "P_13_11"]
VAT_FIELDS = ["P_14_1", "P_14_2", "P_14_3", "P_14_4", "P_14_5"]


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, *path):
    for name in path:
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == name), None)
    return el


def _text(el, *path):
    found = _child(el, *path)
    return " ".join((found.text or "").split()) if found is not None and found.text else ""


def _grosze(value):
    if not value:
        return 0
    try:
        return int((Decimal(value.replace(",", ".")) * 100).quantize(Decimal(1)))
    except InvalidOperation as e:
        raise ValidationError(f"Niepoprawna kwota w fakturze: {value}") from e


def _date(value):
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def parse(content, filename=""):
    """Dane faktury z pliku XML KSeF. Zwraca słownik pól modelu KsefInvoice (bez kierunku i pliku)."""
    from defusedxml import ElementTree
    from defusedxml.common import DefusedXmlException

    if len(content) > MAX_XML_BYTES:
        raise ValidationError(f"{filename or 'plik'}: plik XML jest za duży.")
    try:
        root = ElementTree.fromstring(content)
    except (ElementTree.ParseError, DefusedXmlException, ValueError) as e:
        raise ValidationError(f"{filename or 'plik'}: to nie jest poprawny plik XML.") from e
    if _local(root.tag) != "Faktura":
        raise ValidationError(f"{filename or 'plik'}: to nie jest faktura ustrukturyzowana KSeF (brak elementu „Faktura”).")
    fa = _child(root, "Fa")
    if fa is None:
        raise ValidationError(f"{filename or 'plik'}: brak sekcji „Fa” z danymi faktury.")
    issue = _date(_text(fa, "P_1"))
    number = _text(fa, "P_2")
    if not issue or not number:
        raise ValidationError(f"{filename or 'plik'}: brak daty wystawienia (P_1) lub numeru faktury (P_2).")
    items = []
    for row in (c for c in fa if _local(c.tag) == "FaWiersz"):
        items.append({"name": _text(row, "P_7")[:200], "qty": _text(row, "P_8B"), "unit": _text(row, "P_8A"),
                      "net": _grosze(_text(row, "P_11")), "gross": _grosze(_text(row, "P_11A"))})
    seller, buyer = _child(root, "Podmiot1"), _child(root, "Podmiot2")
    ksef = KSEF_NUMBER.search(filename or "")
    return {
        "ksef_number": ksef.group(1).upper() if ksef else "",
        "invoice_number": number[:256],
        "kind": _text(fa, "RodzajFaktury")[:16],
        "issue_date": issue,
        "sale_date": _date(_text(fa, "P_6")),
        "seller_nip": _text(seller, "DaneIdentyfikacyjne", "NIP")[:16],
        "seller_name": _text(seller, "DaneIdentyfikacyjne", "Nazwa")[:255],
        "buyer_nip": _text(buyer, "DaneIdentyfikacyjne", "NIP")[:16],
        "buyer_name": _text(buyer, "DaneIdentyfikacyjne", "Nazwa")[:255],
        "currency": (_text(fa, "KodWaluty") or "PLN")[:3],
        "net": sum(_grosze(_text(fa, f)) for f in NET_FIELDS),
        "vat": sum(_grosze(_text(fa, f)) for f in VAT_FIELDS),
        "gross": _grosze(_text(fa, "P_15")),
        "items": items[:200],
    }


def _files(uploaded):
    """(nazwa, zawartość) z przesłanego pliku XML albo archiwum ZIP z plikami XML."""
    name = uploaded.name or ""
    if name.lower().endswith(".zip"):
        if uploaded.size > MAX_ZIP_BYTES:
            raise ValidationError("Archiwum ZIP jest za duże.")
        try:
            archive = zipfile.ZipFile(io.BytesIO(uploaded.read()))
        except zipfile.BadZipFile as e:
            raise ValidationError("To nie jest poprawne archiwum ZIP.") from e
        entries = [i for i in archive.infolist() if i.filename.lower().endswith(".xml") and not i.is_dir()]
        if len(entries) > MAX_ZIP_FILES or sum(i.file_size for i in entries) > MAX_ZIP_TOTAL:
            raise ValidationError("Archiwum zawiera zbyt wiele lub zbyt duże pliki.")
        out = []
        for info in entries:
            if info.file_size > MAX_XML_BYTES:
                raise ValidationError(f"{info.filename}: plik XML jest za duży.")
            out.append((info.filename.rsplit("/", 1)[-1], archive.read(info)))
        return out
    if not name.lower().endswith(".xml"):
        raise ValidationError(f"{name}: wybierz plik XML faktury KSeF albo archiwum ZIP.")
    if uploaded.size > MAX_XML_BYTES:
        raise ValidationError(f"{name}: plik XML jest za duży.")
    return [(name, uploaded.read())]


def _digits(nip):
    return re.sub(r"\D", "", nip or "")


def import_content(name, content, *, by, own_nip="", source="plik"):
    """Jedna faktura XML → KsefInvoice. Zwraca fakturę albo None, gdy już jest w systemie (duplikat).
    Błędny plik → ValidationError."""
    data = parse(content, name)
    digest = hashlib.sha256(content).hexdigest()
    if KsefInvoice.objects.filter(sha256=digest).exists() or (
            data["ksef_number"] and KsefInvoice.objects.filter(ksef_number=data["ksef_number"]).exists()):
        return None
    own = _digits(own_nip)
    data["direction"] = (KsefInvoice.Direction.SALE if own and _digits(data["seller_nip"]) == own
                         else KsefInvoice.Direction.PURCHASE)
    with transaction.atomic():
        invoice = KsefInvoice(sha256=digest, imported_by=by, source=source, **data)
        invoice.xml.save("faktura.xml", ContentFile(content), save=False)
        invoice.save()
    return invoice


def import_files(uploads, *, by, own_nip=""):
    """Importuje faktury; zwraca (nowe, pominięte_duplikaty, błędy[str])."""
    created, duplicates, errors = [], 0, []
    for uploaded in uploads:
        try:
            files = _files(uploaded)
        except ValidationError as e:
            errors.append(e.messages[0])
            continue
        for name, content in files:
            try:
                invoice = import_content(name, content, by=by, own_nip=own_nip)
            except ValidationError as e:
                errors.append(e.messages[0])
                continue
            if invoice is None:
                duplicates += 1
            else:
                created.append(invoice)
    if created:
        from .bank import match_pending_invoices

        match_pending_invoices()
        log_event("ksiegowosc.ksef_import", None, f"Zaimportowano faktur KSeF: {len(created)}", actor=by)
    return created, duplicates, errors


def accept(invoice, *, by, day, column, description, document=None, notes="", activity="", task=None):
    """Przyjęcie faktury do ewidencji: zapis kosztu (zakup) albo przychodu (sprzedaż), kwota = należność ogółem (P_15)."""
    from . import services
    from .models import Record

    if invoice.status == KsefInvoice.Status.ACCEPTED and invoice.record_id:
        raise ValidationError("Faktura jest już w ewidencji.")
    from .bank import booked_payment_for

    duplicate = booked_payment_for(invoice)
    if duplicate:
        raise ValidationError(f"Ten wydatek jest już zaksięgowany z wyciągu bankowego (Lp. {duplicate.record.lp}) — przyjęcie faktury "
                              "podwoiłoby koszt. Usuń tamten zapis i połącz operację z wyciągu z tą fakturą.")
    if invoice.currency != "PLN":
        raise ValidationError("Faktura w walucie obcej — przelicz kwotę na złote i wprowadź zapis ręcznie (§ 7 ust. 5).")
    expected = ("revenue",) if invoice.direction == KsefInvoice.Direction.SALE else ("cost_deductible", "cost_other")
    if column not in expected:
        raise ValidationError("Wybierz właściwą kolumnę zestawienia.")
    counterparty = invoice.seller_name if invoice.direction == KsefInvoice.Direction.PURCHASE else invoice.buyer_name
    with transaction.atomic():
        record = services.add_record(
            kind=Record.Kind.KSEF, day=day, document=(document or invoice.invoice_number)[:64], description=description,
            amount=invoice.gross, column=column, by=by, counterparty=counterparty[:160], notes=notes,
            ksef_number=invoice.ksef_number, activity=activity, task=task)
        invoice.status, invoice.record, invoice.status_note = KsefInvoice.Status.ACCEPTED, record, ""
        invoice.save(update_fields=["status", "record", "status_note"])
    return record


def reject(invoice, *, by, note):
    note = " ".join((note or "").split())
    if not note:
        raise ValidationError("Podaj, dlaczego faktura nie trafia do ewidencji.")
    if invoice.status == KsefInvoice.Status.ACCEPTED:
        raise ValidationError("Faktura jest w ewidencji — najpierw usuń zapis z ewidencji.")
    invoice.status, invoice.status_note = KsefInvoice.Status.REJECTED, note[:255]
    invoice.save(update_fields=["status", "status_note"])
    log_event("ksiegowosc.ksef_odrzucenie", None, f"Faktura {invoice.invoice_number} poza ewidencją: {note}", actor=by)


def reopen(invoice, *, by):
    if invoice.status != KsefInvoice.Status.REJECTED:
        raise ValidationError("Faktura nie jest odrzucona.")
    invoice.status, invoice.status_note = KsefInvoice.Status.NEW, ""
    invoice.save(update_fields=["status", "status_note"])
    log_event("ksiegowosc.ksef_przywrocenie", None, f"Faktura {invoice.invoice_number} wraca do decyzji", actor=by)


def default_description(invoice):
    return (invoice.items_summary or invoice.seller_name or invoice.invoice_number)[:255]


def accept_many(invoices, *, by, description="", cost_column="cost_deductible", activity="", task=None):
    """Przyjęcie wielu faktur naraz: data zdarzenia = data wystawienia, nr dowodu = numer faktury, wspólny opis
    (albo opis z pozycji każdej faktury). Zwraca (przyjęte zapisy, błędy[str])."""
    from django.utils import timezone

    records, errors = [], []
    for invoice in invoices:
        column = "revenue" if invoice.direction == KsefInvoice.Direction.SALE else cost_column
        try:
            if invoice.issue_date > timezone.localdate():
                raise ValidationError("data wystawienia jest z przyszłości")
            records.append(accept(invoice, by=by, day=invoice.issue_date, column=column,
                                  description=(description or default_description(invoice))[:255], document=invoice.invoice_number[:64],
                                  notes=f"KSeF {invoice.ksef_number}"[:255] if invoice.ksef_number else "", activity=activity, task=task))
        except ValidationError as e:
            errors.append(f"{invoice.invoice_number}: {e.messages[0]}")
    return records, errors
