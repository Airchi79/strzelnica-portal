"""Saldo strzelca, księgowanie wpłat, automatyczne opłacanie rezerwacji z salda, import wyciągu bankowego.

Zasada (decyzja właściciela): każda wpłata trafia na saldo, a opłaty za rezerwacje są pobierane z salda.
"""

import csv
import hashlib
import io
import re
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from apps.audit.services import log_event
from apps.core.notifications import notify_board, notify_user

from .models import BankImport, LedgerEntry, Payment, PaymentSettings, Wallet

User = get_user_model()

TOPUP_PREFIX = "SATOK-K-"
_CHECK_LETTERS = "ABCDEFGHJKLMNPRSTUWXYZ"  # bez liter łatwych do pomylenia z cyframi
RESERVATION_RE = re.compile(r"SATOK-?R-?(\d{4})-?(\d{4,})")
TOPUP_RE = re.compile(r"SATOK-?K-?(\d{5,})([A-Z])")


# --- kody tytułów przelewów ------------------------------------------------------


def topup_code(user):
    """Osobisty kod do tytułu przelewu zasilającego saldo, z literą kontrolną (literówka nie trafi do innej osoby)."""
    digits = f"{user.pk:05d}"
    check = _CHECK_LETTERS[sum((i + 3) * int(d) for i, d in enumerate(digits)) % len(_CHECK_LETTERS)]
    return f"{TOPUP_PREFIX}{digits}{check}"


def _normalize_title(title):
    return re.sub(r"[\s_.]+", "", (title or "").upper()).replace("—", "-").replace("–", "-")


def match_title(title):
    """Zwraca (użytkownik, rezerwacja) rozpoznane z tytułu przelewu albo (None, None)."""
    from apps.bookings.models import Reservation

    text = _normalize_title(title)
    m = RESERVATION_RE.search(text)
    if m:
        number = f"SATOK-R-{m.group(1)}-{int(m.group(2)):04d}"
        r = Reservation.objects.filter(number=number).select_related("user").first()
        if r:
            return r.user, r
    m = TOPUP_RE.search(text)
    if m:
        user = User.objects.filter(pk=int(m.group(1))).first()
        if user and topup_code(user) == f"{TOPUP_PREFIX}{m.group(1)}{m.group(2)}":
            return user, None
    return None, None


# --- saldo i księga ------------------------------------------------------------------


def _locked_wallet(user):
    Wallet.objects.get_or_create(user=user)
    return Wallet.objects.select_for_update().get(user=user)


def balance_of(user):
    w = Wallet.objects.filter(user=user).first()
    return w.balance if w else 0


@transaction.atomic
def post(user, amount, kind, description, *, payment=None, reservation=None, by=None):
    """Księguje operację na saldzie (wiersz salda blokowany — brak wyścigów przy równoczesnych operacjach)."""
    if amount == 0:
        raise ValueError("Kwota operacji nie może być zerowa")
    wallet = _locked_wallet(user)
    wallet.balance += amount
    wallet.save(update_fields=["balance", "updated_at"])
    return LedgerEntry.objects.create(
        user=user,
        amount=amount,
        kind=kind,
        balance_after=wallet.balance,
        payment=payment,
        reservation=reservation,
        description=description[:255],
        created_by=by,
    )


def wallet_enabled():
    return PaymentSettings.load().wallet_enabled


def operator_commission(total, cfg=None):
    """Prowizja operatora (Tpay) od całej płatności `total` [gr]: procent zaokrąglany w górę do grosza + kwota stała."""
    cfg = cfg or PaymentSettings.load()
    pct = Decimal(cfg.online_fee_percent or 0) / 100
    return int((Decimal(total) * pct).to_integral_value(rounding=ROUND_CEILING)) + (cfg.online_fee_fixed or 0)


def online_fee(amount, cfg=None):
    """Opłata za płatność online doliczana do kwoty `amount` [gr] — bezzwrotna.

    Prowizję Tpay pobiera od CAŁEJ płatności (kwota + opłata), więc opłata jest wyliczana „w górę” (gross-up): najmniejsza
    kwota płatności T, przy której po potrąceniu prowizji (od T, zaokrąglonej w górę) zostaje co najmniej `amount`.
    Dzięki temu zwrot ceny rezerwacji zawsze ma pokrycie w środkach z tej płatności."""
    if not amount or amount <= 0:
        return 0
    cfg = cfg or PaymentSettings.load()
    pct = Decimal(cfg.online_fee_percent or 0) / 100
    fixed = cfg.online_fee_fixed or 0
    if not pct and not fixed:
        return 0
    total = int(((Decimal(amount) + fixed) / (1 - pct)).to_integral_value(rounding=ROUND_CEILING))
    while total - operator_commission(total, cfg) < amount:  # zaokrąglenie prowizji w górę — dobijamy po groszu
        total += 1
    return total - amount


_FEE_IN_RULES = re.compile(r"opłat\w*[^.]{0,80}płatno\w*\s+online|płatno\w*\s+online[^.]{0,80}opłat\w*", re.I)


def online_fee_rule_text(cfg=None):
    """Proponowany zapis do regulaminu z aktualną wysokością opłaty za płatność online ('' — opłata wyłączona)."""
    from apps.facilities.models import format_pln

    cfg = cfg or PaymentSettings.load()
    parts = []
    if cfg.online_fee_percent:
        parts.append(f"{cfg.online_fee_percent.normalize():f}".replace(".", ",") + "% kwoty płatności")
    if cfg.online_fee_fixed:
        parts.append(format_pln(cfg.online_fee_fixed))
    if not parts:
        return ""
    return ("Do płatności online (Tpay: BLIK, przelew online, karta) doliczana jest opłata za obsługę płatności odpowiadająca "
            f"prowizji operatora płatności (obecnie {' + '.join(parts)}), widoczna przed zapłatą. Opłata ta nie podlega zwrotowi przy anulowaniu, wycofaniu lub odrzuceniu "
            "rezerwacji albo zapisu — zwracana jest opłata za rezerwację lub opłata startowa. Płatność przelewem tradycyjnym "
            "lub na miejscu jest wolna od tej opłaty.")


def online_fee_missing_in_rules(cfg=None):
    """True, gdy opłata za płatność online jest włączona, a żaden obowiązujący regulamin o niej nie wspomina."""
    from apps.regulations.models import RegulationDocument

    cfg = cfg or PaymentSettings.load()
    if not (cfg.online_fee_percent or cfg.online_fee_fixed):
        return False
    for doc in RegulationDocument.objects.all():
        version = doc.current_version()
        if version and _FEE_IN_RULES.search(version.content or ""):
            return False
    return True


def _due_items(user):
    """Należności użytkownika (rezerwacje i składki) w kolejności terminów."""
    from apps.association.models import Fee
    from apps.bookings.models import Reservation

    items = [(timezone.localtime(r.starts_at).date(), 1, r) for r in Reservation.objects.filter(user=user, payment_status=Reservation.Payment.DUE)]
    items += [(f.due_on, 0, f) for f in Fee.objects.filter(member__user=user, status=Fee.Status.DUE)]
    return [obj for _, _, obj in sorted(items, key=lambda x: (x[0], x[1], x[2].pk))]


def _pay_item(user, item, by):
    """Pokrywa należność ze środków na saldzie (jeśli wystarczą). Zwraca True, gdy opłacono."""
    from apps.association.models import Fee
    from apps.bookings.models import Reservation

    wallet = _locked_wallet(user)
    if isinstance(item, Reservation):
        r = Reservation.objects.select_for_update().get(pk=item.pk)
        if r.payment_status != Reservation.Payment.DUE:
            return False
        remaining = r.amount - r.paid_amount
        if remaining > 0:
            if wallet.balance < remaining:
                return False
            post(user, -remaining, LedgerEntry.Kind.RESERVATION_FEE, f"Opłata za rezerwację {r.number}", reservation=r, by=by)
            r.paid_amount += remaining
        r.payment_status = Reservation.Payment.PAID
        r.payment_marked_at = timezone.now()
        r.payment_marked_by = by
        r.save(update_fields=["paid_amount", "payment_status", "payment_marked_at", "payment_marked_by", "updated_at"])
        log_event("rezerwacja.oplacona", r, f"{r.number}: {remaining / 100:.2f} zł", actor=by)
        if r.status == Reservation.Status.HOLD:  # rezerwacja ze strony czekała na płatność → potwierdzona
            from apps.bookings.services import confirm_after_payment

            confirm_after_payment(r, by=by)
        return True
    from apps.events.models import Registration

    if isinstance(item, Registration):
        reg = Registration.objects.select_for_update().select_related("event").get(pk=item.pk)
        if reg.payment_status != Registration.Payment.DUE or reg.status != Registration.Status.REGISTERED:
            return False
        remaining = reg.amount - reg.paid_amount
        if remaining > 0:
            if wallet.balance < remaining:
                return False
            post(user, -remaining, LedgerEntry.Kind.EVENT_FEE, reg.label, by=by)
            reg.paid_amount += remaining
        reg.payment_status = Registration.Payment.PAID
        reg.save(update_fields=["paid_amount", "payment_status"])
        log_event("wydarzenie.oplacone", reg, str(reg), actor=by)
        return True
    fee = Fee.objects.select_for_update().get(pk=item.pk)
    if fee.status != Fee.Status.DUE or wallet.balance < fee.amount:
        return False
    post(user, -fee.amount, LedgerEntry.Kind.MEMBERSHIP_FEE, f"{fee.label} — członek nr {fee.member.number}", by=by)
    fee.status = Fee.Status.PAID
    fee.paid_at = timezone.now()
    fee.save(update_fields=["status", "paid_at"])
    log_event("skladka.oplacona", fee, str(fee), actor=by)
    return True


@transaction.atomic
def use_wallet_for_reservation(reservation, by=None):
    """Saldo najpierw: pobiera z salda strzelca tyle, ile się da (także część należności). Reszta zostaje do zapłaty
    online / przelewem / na miejscu. Tylko przy włączonym saldzie strzelca. Zwraca pobraną kwotę [gr]."""
    if not wallet_enabled():
        return 0
    return _apply_to_reservation(reservation, by, "(z salda)", "rezerwacja.z_salda")


@transaction.atomic
def _apply_to_reservation(reservation, by, label, event):
    """Zalicza środki ze salda strzelca na należność rezerwacji — także częściowo (np. przelew na część kwoty).
    Opłacona w całości rezerwacja „oczekuje na płatność” zostaje potwierdzona. Zwraca zaliczoną kwotę [gr].
    Kolejność blokad: rezerwacja, potem saldo (jak przy anulowaniu)."""
    from apps.bookings.models import Reservation

    r = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if r.payment_status != Reservation.Payment.DUE or r.status not in (Reservation.Status.CONFIRMED, Reservation.Status.PENDING_APPROVAL,
                                                                         Reservation.Status.HOLD):
        return 0
    wallet = _locked_wallet(r.user)
    part = min(max(wallet.balance, 0), max(r.amount - r.paid_amount, 0))
    if part > 0:
        post(r.user, -part, LedgerEntry.Kind.RESERVATION_FEE, f"Opłata za rezerwację {r.number} {label}".strip(), reservation=r, by=by)
        r.paid_amount += part
    elif r.paid_amount < r.amount:
        return 0
    fields = ["paid_amount", "updated_at"]
    if r.paid_amount >= r.amount:
        r.payment_status = Reservation.Payment.PAID
        r.payment_marked_at, r.payment_marked_by = timezone.now(), by
        fields += ["payment_status", "payment_marked_at", "payment_marked_by"]
    r.save(update_fields=fields)
    log_event(event, r, f"{r.number}: {part / 100:.2f} zł" + ("" if r.payment_status == Reservation.Payment.PAID
                                                              else f" (zapłacono {r.paid_amount / 100:.2f} z {r.amount / 100:.2f} zł)"), actor=by)
    if r.payment_status == Reservation.Payment.PAID and r.status == Reservation.Status.HOLD:
        from apps.bookings.services import confirm_after_payment

        confirm_after_payment(r, by=by)
    return part


@transaction.atomic
def apply_wallet(user, by=None, targets=None):
    """Opłaca należności ze środków na saldzie.

    targets — konkretne należności (rezerwacje / składki), np. te, których dotyczy wpłata; opłacane zawsze.
    Bez targets: automatyczne pobieranie od najstarszej należności — gdy saldo strzelca jest włączone, albo (tylko
    rezerwacje) gdy włączono „nadpłata opłaca kolejne rezerwacje”.
    Zwraca liczbę opłaconych należności."""
    if user is None:
        return 0
    if targets is None:
        cfg = PaymentSettings.load()
        if cfg.wallet_enabled:
            targets = _due_items(user)
        elif cfg.overpayment_pays_reservations:
            from apps.bookings.models import Reservation

            targets = [x for x in _due_items(user) if isinstance(x, Reservation)]
        else:
            return 0
    paid = 0
    for item in targets:
        if item is not None and _pay_item(user, item, by):
            paid += 1
    return paid


@transaction.atomic
def refund_registration(registration, by=None, reason="Zwrot opłaty startowej"):
    """Zwraca na saldo (jako nadpłatę) wszystko, co pobrano za zapis na wydarzenie."""
    from apps.events.models import Registration

    r = Registration.objects.select_for_update().select_related("event").get(pk=registration.pk)
    if r.paid_amount > 0:
        post(r.user, r.paid_amount, LedgerEntry.Kind.REFUND, f"{reason}: {r.event.title}", by=by)
        r.paid_amount = 0
        r.save(update_fields=["paid_amount"])
    return r


@transaction.atomic
def refund_reservation(reservation, by=None, reason="Zwrot za anulowaną rezerwację"):
    """Zwraca na saldo wszystko, co pobrano za rezerwację."""
    from apps.bookings.models import Reservation

    r = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if r.paid_amount > 0:
        post(r.user, r.paid_amount, LedgerEntry.Kind.REFUND, f"{reason} {r.number}", reservation=r, by=by)
        r.paid_amount = 0
        r.save(update_fields=["paid_amount", "updated_at"])
    return r


@transaction.atomic
def reconcile_reservation(reservation, by=None):
    """Po zmianie należności: nadpłata wraca na saldo, niedopłata staje się należnością (opłacaną z salda, jeśli się da)."""
    from apps.bookings.models import Reservation

    r = Reservation.objects.select_for_update().get(pk=reservation.pk)
    if r.payment_status in (Reservation.Payment.WAIVED, Reservation.Payment.NONE):
        return r
    if r.paid_amount > r.amount:
        excess = r.paid_amount - r.amount
        post(r.user, excess, LedgerEntry.Kind.REFUND, f"Zwrot nadpłaty za rezerwację {r.number}", reservation=r, by=by)
        r.paid_amount = r.amount
    r.payment_status = Reservation.Payment.PAID if r.paid_amount >= r.amount else Reservation.Payment.DUE
    r.save(update_fields=["paid_amount", "payment_status", "updated_at"])
    apply_wallet(r.user, by=by, targets=[r])
    r.refresh_from_db()
    return r


# --- wpłaty ------------------------------------------------------------------------------


@transaction.atomic
def complete_payment(payment, user=None, by=None, note=""):
    """Księguje wpłatę na saldo strzelca i opłaca z salda jego należności."""
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status == Payment.Status.COMPLETED:
        return payment  # idempotentnie (np. powtórzone powiadomienie Tpay)
    if user is not None:
        payment.user = user
    if payment.user is None:
        raise ValidationError("Wskaż strzelca, któremu przypisać wpłatę.")
    payment.status = Payment.Status.COMPLETED
    payment.completed_at = timezone.now()
    if note:
        payment.note = note[:255]
    payment.save()
    post(
        payment.user,
        payment.amount,
        LedgerEntry.Kind.DEPOSIT,
        f"{payment.get_method_display()}{': ' + payment.title if payment.title else ''}",
        payment=payment,
        by=by,
    )
    if payment.surcharge:
        post(payment.user, -payment.surcharge, LedgerEntry.Kind.ONLINE_FEE,
             f"Opłata za płatność online (bezzwrotna){': ' + payment.title if payment.title else ''}", payment=payment, by=by)
    log_event("platnosc.zaksiegowana", payment, str(payment), changes={"dla": payment.user.email}, actor=by)
    if payment.reservation_id and payment.reservation.status == payment.reservation.Status.CANCELLED:
        # Wpłata dotarła po zwolnieniu/anulowaniu rezerwacji — środki zostają na koncie strzelca, decyzję podejmuje obsługa.
        notify_board(
            f"Wpłata za anulowaną rezerwację {payment.reservation.number}", "emails/reservation_board.txt", "payments.manage_payments",
            url=reverse("panel:payment", args=[payment.pk]), reservation=payment.reservation,
            body=f"{payment.user.full_name}: {payment.amount / 100:.2f} zł. Zwróć wpłatę albo utwórz rezerwację ponownie.",
            event="Wpłata za anulowaną rezerwację",
        )
    elif payment.reservation_id:
        # Wpłata za konkretną rezerwację zaliczana na nią także częściowo (przelew na część kwoty) — przy zwolnieniu
        # lub anulowaniu rezerwacji jest wtedy rozliczana (zwrot / saldo / przypomnienie o zwrocie ręcznym).
        _apply_to_reservation(payment.reservation, by, "", "rezerwacja.oplacona")
    elif payment.fee_id or payment.event_registration_id:
        apply_wallet(payment.user, by=by, targets=[payment.fee or payment.event_registration])
    elif wallet_enabled():
        apply_wallet(payment.user, by=by)
    else:
        # Wpłata bez wskazania (np. przelew z osobistym kodem SATOK-K): pokrywa zaległe składki członkowskie.
        from apps.association.models import Fee

        apply_wallet(payment.user, by=by, targets=list(Fee.objects.filter(member__user=payment.user, status=Fee.Status.DUE).order_by("due_on")))
    notify_user(payment.user, "Wpłata zaksięgowana", "emails/payment_completed.txt", payment=payment, balance=balance_of(payment.user))
    return payment


@transaction.atomic
def register_manual_payment(user, amount, method, by, reservation=None, note="", fee=None, registration=None):
    """Gotówka lub BLIK na telefon przyjęte przez zarząd/obsługę."""
    if method not in (Payment.Method.CASH, Payment.Method.BLIK_PHONE, Payment.Method.TRANSFER):
        raise ValidationError("Nieprawidłowy sposób płatności.")
    if amount <= 0:
        raise ValidationError("Kwota musi być dodatnia.")
    payment = Payment.objects.create(
        user=user, amount=amount, method=method, reservation=reservation, fee=fee, event_registration=registration, created_by=by,
        note=note[:255], booked_on=timezone.localdate(),
        title=reservation.number if reservation else (fee.label if fee else (registration.label if registration else "")),
    )
    return complete_payment(payment, by=by)


@transaction.atomic
def correction(user, amount, reason, by):
    if not reason.strip():
        raise ValidationError("Podaj powód korekty.")
    entry = post(user, amount, LedgerEntry.Kind.CORRECTION, f"Korekta: {reason.strip()}", by=by)
    log_event("saldo.korekta", entry, reason, changes={"kwota_gr": amount, "dla": user.email}, actor=by)
    if amount > 0:
        apply_wallet(user, by=by)  # tylko przy włączonym saldzie
    return entry


MANUAL_REFUND_METHODS = {"przelew": "przelew", "gotowka": "gotówka", "tpay_panel": "panel Tpay", "blik": "BLIK na telefon"}


@transaction.atomic
def manual_refund(user, amount, method, reason, by):
    """Zwrot wykonany poza systemem (przelew, gotówka, zwrot ręczny w panelu Tpay) — księgowany jako „zwrot do
    wpłacającego”, więc trafia do raportu wpływów jako zwrot. Najwyżej do wysokości nadpłaty."""
    if method not in MANUAL_REFUND_METHODS:
        raise ValidationError("Wybierz sposób zwrotu.")
    if not reason.strip():
        raise ValidationError("Podaj powód zwrotu.")
    if amount <= 0:
        raise ValidationError("Kwota musi być dodatnia.")
    _locked_wallet(user)
    if amount > balance_of(user):
        raise ValidationError(f"Zwrot nie może przekroczyć nadpłaty strzelca ({balance_of(user) / 100:.2f} zł).")
    entry = post(user, -amount, LedgerEntry.Kind.CHARGEBACK, f"Zwrot ręczny ({MANUAL_REFUND_METHODS[method]}): {reason.strip()}", by=by)
    log_event("platnosc.zwrot_reczny", entry, f"{user.full_name}: {amount / 100:.2f} zł ({MANUAL_REFUND_METHODS[method]})",
              changes={"kwota_gr": amount, "sposob": method, "powod": reason.strip()}, actor=by)
    return entry


def ledger_consistent(user):
    total = LedgerEntry.objects.filter(user=user).aggregate(s=Sum("amount"))["s"] or 0
    return total == balance_of(user)


# --- import wyciągu bankowego (CSV) ------------------------------------------------------

DATE_KEYS = ["data operacji", "data księgowania", "data ksiegowania", "data transakcji", "data waluty", "data"]
AMOUNT_KEYS = ["kwota operacji", "kwota transakcji", "kwota"]
TITLE_KEYS = ["tytuł", "tytul", "opis operacji", "opis", "szczegóły", "szczegoly"]
SENDER_KEYS = ["nadawca", "kontrahent", "dane kontrahenta", "nazwa kontrahenta", "odbiorca/nadawca", "nazwa"]


def _decode(raw: bytes):
    for enc in ("utf-8-sig", "cp1250", "iso-8859-2"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValidationError("Nie rozpoznano kodowania pliku. Zapisz wyciąg jako CSV (UTF-8 lub Windows-1250).")


def _find_column(header, keys):
    lowered = [h.strip().lower().strip('"#') for h in header]
    for key in keys:
        for i, h in enumerate(lowered):
            if h == key:
                return i
    for key in keys:
        for i, h in enumerate(lowered):
            if key in h:
                return i
    return None


def _parse_amount(text):
    cleaned = re.sub(r"[^\d,.\-+]", "", (text or "").replace(" ", "").replace(" ", ""))
    if cleaned.count(",") == 1 and cleaned.count(".") >= 1:
        cleaned = cleaned.replace(".", "")  # 1.234,56
    cleaned = cleaned.replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return int((value * 100).to_integral_value())


def _parse_date(text):
    text = (text or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y.%m.%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_bank_csv(raw: bytes):
    """Zwraca listę słowników {booked_on, amount, title, payer_name} dla wpływów (kwota > 0)."""
    text = _decode(raw)
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";"
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    header_idx = None
    for i, row in enumerate(rows[:40]):
        joined = " ".join(c.lower() for c in row)
        if "kwota" in joined and "data" in joined:
            header_idx = i
            break
    if header_idx is None:
        raise ValidationError("Nie znaleziono nagłówka z kolumnami „Data” i „Kwota”. Prześlij wyciąg w formacie CSV z banku.")
    header = rows[header_idx]
    cols = {
        "date": _find_column(header, DATE_KEYS),
        "amount": _find_column(header, AMOUNT_KEYS),
        "title": _find_column(header, TITLE_KEYS),
        "sender": _find_column(header, SENDER_KEYS),
    }
    if cols["date"] is None or cols["amount"] is None or cols["title"] is None:
        raise ValidationError("Brakuje kolumny daty, kwoty lub tytułu w nagłówku wyciągu.")
    out = []
    for row in rows[header_idx + 1:]:
        if len(row) <= max(c for c in cols.values() if c is not None):
            continue
        amount = _parse_amount(row[cols["amount"]])
        booked_on = _parse_date(row[cols["date"]])
        if amount is None or booked_on is None or amount <= 0:
            continue  # pomijamy obciążenia, podsumowania i puste wiersze
        out.append({
            "booked_on": booked_on,
            "amount": amount,
            "title": row[cols["title"]].strip()[:255],
            "payer_name": row[cols["sender"]].strip()[:255] if cols["sender"] is not None else "",
        })
    return out


@transaction.atomic
def import_bank_csv(raw: bytes, filename: str, by):
    rows = parse_bank_csv(raw)
    batch = BankImport.objects.create(filename=filename[:255], sha256=hashlib.sha256(raw).hexdigest(), rows_total=len(rows), created_by=by)
    seen = {}
    new = matched = 0
    for row in rows:
        base = f"{row['booked_on']}|{row['amount']}|{row['title']}|{row['payer_name']}"
        seen[base] = seen.get(base, 0) + 1  # identyczne wiersze w jednym pliku to osobne wpłaty
        external_id = "bank:" + hashlib.sha256(f"{base}|{seen[base]}".encode()).hexdigest()
        if Payment.objects.filter(external_id=external_id).exists():
            continue
        user, reservation = match_title(row["title"])
        payment = Payment.objects.create(
            amount=row["amount"],
            method=Payment.Method.TRANSFER,
            status=Payment.Status.PENDING if user else Payment.Status.UNMATCHED,
            title=row["title"],
            payer_name=row["payer_name"],
            booked_on=row["booked_on"],
            external_id=external_id,
            user=user,
            reservation=reservation,
            raw={"plik": filename[:120], "import": batch.pk},
            created_by=by,
        )
        new += 1
        if user:
            complete_payment(payment, by=by)
            matched += 1
    batch.rows_new, batch.rows_matched = new, matched
    batch.save(update_fields=["rows_new", "rows_matched"])
    log_event("platnosci.import_wyciagu", batch, f"{filename}: nowych {new}, przypisanych {matched}", actor=by)
    return batch


@transaction.atomic
def assign_unmatched(payment, user, by):
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.UNMATCHED:
        raise ValidationError("Ta wpłata nie czeka na przypisanie.")
    log_event("platnosc.przypisanie_reczne", payment, str(payment), changes={"dla": user.email}, actor=by)
    return complete_payment(payment, user=user, by=by)


@transaction.atomic
def ignore_unmatched(payment, reason, by):
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.UNMATCHED:
        raise ValidationError("Ta wpłata nie czeka na przypisanie.")
    payment.status = Payment.Status.IGNORED
    payment.note = reason[:255]
    payment.save(update_fields=["status", "note"])
    log_event("platnosc.pominieta", payment, reason, actor=by)
    return payment


def apply_all_overpayments(by=None):
    """Po włączeniu „nadpłata opłaca rezerwacje”: rozlicza istniejące nadpłaty. Zwraca liczbę opłaconych należności."""
    count = 0
    for wallet in Wallet.objects.filter(balance__gt=0).select_related("user"):
        count += apply_wallet(wallet.user, by=by)
    return count


def refund_to_payer(payment, amount, by=None, reason="Zwrot"):
    """Zwrot pieniędzy płacącemu przez Tpay (na kartę / rachunek / BLIK). Księguje operację „zwrot do wpłacającego”
    na saldzie (ta sama, którą dodaje powiadomienie Tpay o zwrocie — nie zostanie zaksięgowana drugi raz).
    Zwraca True, gdy Tpay przyjął zwrot; False (i powiadomienie zarządu), gdy trzeba zwrócić ręcznie."""
    from . import tpay

    if amount <= 0:
        return True
    if payment.method != Payment.Method.TPAY or payment.status != Payment.Status.COMPLETED or not (payment.external_id or "").startswith("tpay:"):
        return False
    with transaction.atomic():
        p = Payment.objects.select_for_update().get(pk=payment.pk)
        if p.ledger_entries.filter(kind=LedgerEntry.Kind.CHARGEBACK).exists() or (p.raw or {}).get("zwrot_api"):
            return True  # już zwrócone
        try:
            resp = tpay.refund_transaction(p.external_id.split(":", 1)[1], None if amount >= p.amount else amount) or {}
        except tpay.TpayError as e:
            log_event("platnosc.zwrot_nieudany", p, f"{p}: {e}", actor=by)
            if wallet_enabled():
                # Przy włączonym saldzie kwota trafia na saldo strzelca bez przypomnienia o zwrocie ręcznym (to wysyłane jest
                # tylko przy wyłączonym saldzie) — zarząd musi wiedzieć, że strzelec czeka na zwrot na kartę / rachunek.
                _notify_tpay_refund_failed(p, amount, str(e))
            return False
        p.raw = {**(p.raw or {}), "zwrot_api": {"kwota_gr": amount, "kiedy": timezone.now().isoformat(), "powod": reason[:120],
                                     "odpowiedz": {k: resp.get(k) for k in ("result", "requestId", "status", "transactionId") if k in resp}}}
        p.save(update_fields=["raw"])
        post(p.user, -amount, LedgerEntry.Kind.CHARGEBACK, f"{reason} — zwrot na rachunek płacącego (Tpay)", payment=p, by=by)
        log_event("platnosc.zwrot_tpay", p, f"{p}: zwrot {amount / 100:.2f} zł", actor=by)
    return True


def _notify_tpay_refund_failed(payment, amount, error):
    user = payment.user
    url = reverse("panel:wallet", args=[user.pk])
    what = f"{user.full_name}: {amount / 100:.2f} zł"
    notify_board(f"Zwrot Tpay nie powiódł się — {user.full_name}", "emails/board_generic.txt", "payments.manage_payments", url=url,
                 body=f"{what} wróciło na saldo strzelca zamiast na kartę / rachunek ({error}). Ponów: Salda → „Zwróć przez Tpay”.",
                 event=f"Zwrot Tpay nie powiódł się — {user.full_name}",
                 text=f"{what} — Tpay odrzucił zwrot ({error}). Kwota jest na saldzie strzelca. Ponów zwrot w panelu: "
                      f"Płatności → Salda → strzelec → „Zwróć przez Tpay” (np. po uzupełnieniu środków na koncie Tpay).",
                 url_path=url)


def refund_to_payers(payments, amount, by, reason):
    """Zwraca `amount` z podanych wpłat (najstarsze najpierw) na rachunek/kartę płacących przez Tpay — bez opłaty za
    płatność online. Wpłaty poza Tpay i odrzucone przez Tpay pomija. Zwraca (zwrócono, pozostało)."""
    refunded, remaining = 0, amount
    for p in payments.filter(status=Payment.Status.COMPLETED).order_by("completed_at"):
        if remaining <= 0:
            break
        part = min(p.amount - p.surcharge, remaining)  # opłata za płatność online nie wraca
        if part > 0 and refund_to_payer(p, part, by=by, reason=reason):
            refunded += part
            remaining -= part
    return refunded, max(remaining, 0)


def remind_manual_refund(user, amount, label):
    """Przypomnienie dla zarządu: kwotę trzeba zwrócić ręcznie (wpłata poza Tpay albo Tpay odrzucił zwrot)."""
    url = reverse("panel:wallet", args=[user.pk])
    notify_board(f"Zwrot do wykonania ręcznie: {label}", "emails/board_generic.txt", "payments.manage_payments", url=url,
                 body=f"{user.full_name}: {amount / 100:.2f} zł (wpłata poza Tpay albo zwrot Tpay nie powiódł się).",
                 event=f"Zwrot do wykonania ręcznie: {label}",
                 text=f"{user.full_name}: {amount / 100:.2f} zł — zwróć przelewem/gotówką albo w panelu Tpay.", url_path=url)


def surcharge_of(payments):
    """Suma opłat za płatność online w zaksięgowanych wpłatach (nie wracają przy zwrocie)."""
    return payments.filter(status=Payment.Status.COMPLETED).aggregate(s=Sum("surcharge"))["s"] or 0


def refundable_overpayments(user):
    """Wpłaty Tpay za anulowane (bez zachowania należności) rezerwacje, które nie zostały jeszcze zwrócone płacącemu —
    nadpłata strzelca, którą zarząd może zwrócić jednym kliknięciem. [(wpłata, kwota_do_zwrotu)]"""
    balance = balance_of(user)
    out = []
    qs = (Payment.objects.filter(user=user, method=Payment.Method.TPAY, status=Payment.Status.COMPLETED, external_id__startswith="tpay:",
                                 reservation__status="anulowana", reservation__late_cancellation=False)
          .exclude(ledger_entries__kind=LedgerEntry.Kind.CHARGEBACK).select_related("reservation").order_by("completed_at"))
    for p in qs:
        if (p.raw or {}).get("zwrot_api") or balance <= 0:
            continue
        amount = min(p.amount - p.surcharge, balance)  # bez opłaty za płatność online
        if amount <= 0:
            continue
        out.append((p, amount))
        balance -= amount
    return out
