"""Lista kontrolna miesiąca: czy ewidencja za miesiąc jest kompletna (faktury KSeF, faktura prowizyjna, Stripe, wyciąg,
operacje do decyzji, zwroty, uzgodnienie salda). Każdy krok ma stan i link do miejsca, w którym się go załatwia."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Max, Sum
from django.urls import reverse
from django.utils import timezone

from apps.facilities.models import format_pln

from .models import PAYMENT_OFFSET, BankOperation, ChecklistExplanation, KsefInvoice, KsefSync, StripePayout, StripePosting, StripeTransaction

OK, TODO, WARN, LATER = "ok", "todo", "warn", "later"
LABELS = {OK: "gotowe", TODO: "do zrobienia", WARN: "sprawdź", LATER: "jeszcze nie teraz"}
BADGES = {OK: "ok", TODO: "bad", WARN: "warn", LATER: ""}


@dataclass
class Step:
    title: str
    state: str
    detail: str
    url: str = ""
    action: str = ""
    explain: str = ""  # krok z różnicą, którą można oznaczyć jako wyjaśnioną (klucz ChecklistExplanation.Step)
    explained: object = None  # ChecklistExplanation, gdy różnicę wyjaśniono

    @property
    def label(self):
        return LABELS[self.state]

    @property
    def badge(self):
        return BADGES[self.state]


def _next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def month_steps(year, month, today=None):
    today = today or timezone.localdate()
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    finished = today > end
    last_day = min(end, today - timedelta(days=1))  # ostatni zakończony dzień okresu
    steps = []

    # 1. faktury KSeF do decyzji
    sync = KsefSync.load()
    pending = KsefInvoice.objects.filter(status=KsefInvoice.Status.NEW, issue_date__lte=end)
    n = pending.count()
    sync_note = (f"Automatyczne pobieranie: ostatni udany przebieg {timezone.localtime(sync.last_success_at):%d.%m.%Y %H:%M}."
                 if sync.enabled and sync.last_success_at else "Automatyczne pobieranie z KSeF jest wyłączone — wgraj faktury ręcznie.")
    steps.append(Step(
        "Faktury KSeF przyjęte lub odrzucone", TODO if n else (OK if sync.enabled else WARN),
        (f"Do decyzji: {n} (wystawione do {end:%d.%m.%Y}). " if n else "Brak faktur czekających na decyzję. ") + sync_note,
        reverse("panel:accounting_ksef"), "Przyjmij faktury" if n else "Faktury KSeF"))

    # 2. faktura prowizyjna platformy za miesiąc (wystawiana w następnym miesiącu)
    _, fee = commission_amounts(start)
    nxt = _next_month(start)
    commission = list(KsefInvoice.objects.filter(payment_method=PAYMENT_OFFSET, issue_date__gte=nxt,
                                                 issue_date__lt=_next_month(nxt)).exclude(status=KsefInvoice.Status.REJECTED))
    fees_posted = StripePosting.objects.filter(kind=StripePosting.Kind.FEES, period_start=start, record__removed_at__isnull=True).exists()
    invoiced = sum(i.gross for i in commission)
    if not fee and not commission:
        steps.append(Step("Faktura prowizyjna za płatności online", OK, "W tym miesiącu Stripe nie pobrał prowizji."))
    elif fees_posted:
        steps.append(Step("Faktura prowizyjna za płatności online", OK,
                          f"Prowizje {format_pln(fee)} ujęte z eksportu Stripe (bez faktury).", reverse("panel:accounting_stripe"), "Stripe"))
    elif not commission:
        state = WARN if today >= nxt + timedelta(days=14) else LATER
        steps.append(Step("Faktura prowizyjna za płatności online", state,
                          f"Prowizje Stripe za miesiąc: {format_pln(fee)}. Faktura platformy przychodzi zwykle na początku "
                          f"{nxt:%m.%Y} przez KSeF" + (" — jeszcze jej nie ma, sprawdź z wystawcą." if state == WARN else "."),
                          reverse("panel:accounting_ksef"), "Faktury KSeF"))
    else:
        not_accepted = [i for i in commission if i.status != KsefInvoice.Status.ACCEPTED]
        numbers = ", ".join(i.invoice_number for i in commission)
        if not_accepted:
            steps.append(Step("Faktura prowizyjna za płatności online", TODO, f"Faktura {numbers} czeka na przyjęcie do ewidencji.",
                              reverse("panel:accounting_ksef_invoice", args=[not_accepted[0].pk]), "Otwórz fakturę"))
        elif invoiced != fee:
            explained = ChecklistExplanation.objects.filter(month=start, step=ChecklistExplanation.Step.COMMISSION,
                                                            amount_a=invoiced, amount_b=fee).select_related("created_by").first()
            if explained:
                who = explained.created_by.full_name or explained.created_by.email if explained.created_by else "—"
                steps.append(Step("Faktura prowizyjna za płatności online", OK,
                                  f"Faktura {numbers}: {format_pln(invoiced)}, prowizje w eksporcie Stripe: {format_pln(fee)} — "
                                  f"różnica {format_pln(invoiced - fee)} wyjaśniona: {explained.note} "
                                  f"({who}, {timezone.localtime(explained.created_at):%d.%m.%Y}).",
                                  explain=ChecklistExplanation.Step.COMMISSION, explained=explained))
            else:
                steps.append(Step("Faktura prowizyjna za płatności online", WARN,
                                  f"Faktura {numbers}: {format_pln(invoiced)}, a prowizje w eksporcie Stripe: {format_pln(fee)} — "
                                  "sprawdź, czy eksport Stripe obejmuje cały miesiąc i czy faktura dotyczy tego okresu. "
                                  "Jeśli przyczyna jest znana, oznacz różnicę jako wyjaśnioną.",
                                  reverse("panel:accounting_stripe"), "Stripe", explain=ChecklistExplanation.Step.COMMISSION))
        else:
            steps.append(Step("Faktura prowizyjna za płatności online", OK,
                              f"Faktura {numbers} przyjęta, kwota zgodna z prowizjami Stripe ({format_pln(fee)})."))

    # 3. Stripe: eksport obejmuje miesiąc, dni ujęte w ewidencji
    last_tx = StripeTransaction.objects.aggregate(m=Max("day"))["m"]
    days = set(StripeTransaction.objects.filter(day__gte=start, day__lte=last_day)
               .exclude(type="payout").values_list("day", flat=True))
    posted = set(StripePosting.objects.filter(kind=StripePosting.Kind.REVENUE, period_start__gte=start, period_start__lte=end,
                                              record__removed_at__isnull=True).values_list("period_start", flat=True))
    missing = sorted(days - posted)
    if last_tx is None:
        steps.append(Step("Płatności online (Stripe) wgrane i ujęte", WARN, "Nie wgrano jeszcze eksportu ze Stripe.",
                          reverse("panel:accounting_stripe"), "Wgraj eksport"))
    elif last_tx < last_day:
        steps.append(Step("Płatności online (Stripe) wgrane i ujęte", TODO,
                          f"Eksport kończy się na {last_tx:%d.%m.%Y} — wgraj świeży eksport (historia salda i wypłaty).",
                          reverse("panel:accounting_stripe"), "Wgraj eksport"))
    elif missing:
        steps.append(Step("Płatności online (Stripe) wgrane i ujęte", TODO,
                          f"Dni z płatnościami jeszcze nieujęte w ewidencji: {len(missing)} (od {missing[0]:%d.%m}).",
                          reverse("panel:accounting_stripe"), "Ujmij w ewidencji"))
    else:
        steps.append(Step("Płatności online (Stripe) wgrane i ujęte", OK if finished else LATER,
                          f"Ujęte dni z płatnościami: {len(posted)}." + ("" if finished else " Miesiąc jeszcze trwa.")))

    # 4. wyciąg bankowy obejmuje miesiąc (kontrola: wypłaty Stripe z miesiąca mają swoje wpływy w wyciągu)
    last_op = BankOperation.objects.aggregate(m=Max("booked_on"))["m"]
    unmatched = StripePayout.objects.filter(bank_operations__isnull=True, arrival_date__gte=start, arrival_date__lte=min(end, today - timedelta(days=4)))
    if last_op is None:
        steps.append(Step("Wyciąg bankowy za cały miesiąc", WARN, "Nie wgrano jeszcze wyciągu z BNP Paribas.",
                          reverse("panel:accounting_bank"), "Wgraj wyciąg"))
    elif unmatched.exists() or (finished and last_op < end):
        reasons = []
        if last_op < end:
            reasons.append(f"ostatnia operacja w systemie: {last_op:%d.%m.%Y}")
        if unmatched.exists():
            reasons.append(f"wypłaty ze Stripe bez wpływu w wyciągu: {unmatched.count()}")
        steps.append(Step("Wyciąg bankowy za cały miesiąc", TODO,
                          "Wgraj zestawienie operacji za cały miesiąc (" + "; ".join(reasons) + "). Jeśli w końcówce miesiąca "
                          "nie było operacji, a wypłaty się zgadzają — wszystko w porządku.", reverse("panel:accounting_bank"), "Wgraj wyciąg"))
    else:
        steps.append(Step("Wyciąg bankowy za cały miesiąc", OK if finished else LATER,
                          f"Ostatnia operacja: {last_op:%d.%m.%Y}." + ("" if finished else " Miesiąc jeszcze trwa.")))

    # 5. operacje z wyciągu do decyzji
    n = BankOperation.objects.filter(status=BankOperation.Status.NEW, booked_on__gte=start, booked_on__lte=end).count()
    steps.append(Step("Operacje z wyciągu rozstrzygnięte", TODO if n else OK,
                      f"Do decyzji: {n} — zaksięguj, połącz z fakturą lub zwrotem albo pomiń." if n else "Nic nie czeka na decyzję.",
                      reverse("panel:accounting_bank"), "Wyciąg bankowy" if n else ""))

    # 6. zwroty za faktury zapłacone prywatnie (niezależnie od miesiąca — przypomnienie)
    owed = KsefInvoice.objects.filter(payment_method="prywatnie", reimbursement="do_zwrotu").exclude(status=KsefInvoice.Status.REJECTED)
    total = owed.aggregate(s=Sum("gross"))["s"] or 0
    steps.append(Step("Zwroty za faktury zapłacone prywatnie", WARN if total else OK,
                      f"Do zwrotu: {format_pln(total)} ({owed.count()} faktur). Po przelewie połącz go w wyciągu jako zwrot."
                      if total else "Nic nie czeka na zwrot.", reverse("panel:accounting_reconciliation") + f"?rok={year}#g-private"
                      if total else "", "Szczegóły" if total else ""))

    # 7. uzgodnienie salda bez niewyjaśnionej różnicy i bez faktur o nieznanej zapłacie
    from . import reconciliation

    r = reconciliation.reconcile(year, end)
    unpaid = next((g for g in r["groups"] if g.key == "unpaid"), None)
    url = reverse("panel:accounting_reconciliation") + f"?rok={year}&do={end:%Y-%m-%d}"
    if not r["ops_count"]:
        steps.append(Step("Saldo rachunku uzgodnione z ewidencją", LATER, "Brak operacji z wyciągu w tym roku.", url, "Uzgodnienie salda"))
    elif r["unexplained"]:
        steps.append(Step("Saldo rachunku uzgodnione z ewidencją", WARN, f"Różnica niewyjaśniona: {format_pln(r['unexplained'])}.",
                          url, "Uzgodnienie salda"))
    elif unpaid:
        steps.append(Step("Saldo rachunku uzgodnione z ewidencją", WARN,
                          f"Faktury bez zapłaty z rachunku: {unpaid.count} ({format_pln(unpaid.total)}) — zapłacone później, "
                          "inaczej (wskaż sposób na karcie faktury) albo jeszcze niezapłacone.", url + "#g-unpaid", "Uzgodnienie salda"))
    else:
        steps.append(Step("Saldo rachunku uzgodnione z ewidencją", OK,
                          f"Saldo na {end:%d.%m.%Y}: {format_pln(r['closing'])} — wszystkie różnice wyjaśnione.", url, "Uzgodnienie salda"))
    return steps


# --- lista kontrolna zamknięcia roku --------------------------------------------------------------------------------------

MONTH_NAMES = ["", "styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień", "wrzesień", "październik",
               "listopad", "grudzień"]


def year_steps(year, today=None):
    """Kroki od grudnia do terminu CIT-8 (31 marca roku następnego): miesiące rozliczone, środki trwałe i odpisy,
    forma działalności (zestawienie uzupełniające), saldo na 31.12, przepływy, zamknięcie roku, uchwała o dochodzie, CIT-8."""
    from apps.association.models import Resolution

    from . import assets, reconciliation
    from .models import AccountingYear, DepreciationPosting, GeneratedDocument, Record

    today = today or timezone.localdate()
    end = date(year, 12, 31)
    finished = today > end
    cit_deadline = date(year + 1, 3, 31)
    steps = []

    # 1. miesiące rozliczone
    open_months = []
    for m in range(1, 13):
        if date(year, m, 1) > today:
            break
        bad = [s for s in month_steps(year, m, today) if s.state in (TODO, WARN)]
        if bad:
            open_months.append((m, bad))
    todo = any(s.state == TODO for _, bad in open_months for s in bad)
    steps.append(Step(
        "Wszystkie miesiące rozliczone (lista miesiąca)", TODO if todo else (WARN if open_months else (OK if finished else LATER)),
        ("Do uzupełnienia: " + ", ".join(f"{MONTH_NAMES[m]} ({len(bad)})" for m, bad in open_months) + ".") if open_months
        else ("Każdy miesiąc rozliczony." if finished else "Dotychczasowe miesiące rozliczone — grudzień po jego zakończeniu."),
        reverse("panel:accounting_month") + (f"?rok={year}&miesiac={open_months[0][0]}" if open_months else f"?rok={year}&miesiac=12"),
        "Otwórz miesiąc" if open_months else "Lista miesiąca"))

    # 2. środki trwałe i odpisy amortyzacyjne
    items = assets.active_in(year)
    expected = sum(assets.year_figures(a, year)["year"] for a in items)
    posted = sum(DepreciationPosting.objects.filter(year=year, record__removed_at__isnull=True).values_list("amount", flat=True))
    url = reverse("panel:accounting_assets") + f"?rok={year}"
    if not items:
        steps.append(Step("Wykaz środków trwałych i odpisy", OK,
                          "Brak środków trwałych w wykazie. Sprawdź, czy w roku nie kupiono sprzętu o wartości ponad 10 000 zł "
                          "(środek trwały) — wtedy wpisz go do wykazu.", url, "Wykaz"))
    elif expected and posted < expected:
        steps.append(Step("Wykaz środków trwałych i odpisy", TODO if finished else LATER,
                          f"Odpisy za rok według wykazu: {format_pln(expected)}, ujęte w ewidencji: {format_pln(posted)}. "
                          "Zaksięguj brakujące odpisy (miesięcznie albo za cały rok po jego zakończeniu).", url, "Zaksięguj odpisy"))
    else:
        steps.append(Step("Wykaz środków trwałych i odpisy", OK,
                          f"Pozycji w wykazie: {len(items)}; odpisy za rok ujęte ({format_pln(posted)}).", url, "Wykaz"))

    # 3. forma działalności — zestawienie uzupełniające (§ 4)
    records = Record.objects.filter(year=year, removed_at__isnull=True)
    missing = records.filter(activity="").count()
    url = reverse("panel:accounting_supplementary") + f"?rok={year}"
    steps.append(Step("Forma działalności przypisana zapisom (zestawienie uzupełniające)", TODO if missing else OK,
                      f"Zapisy bez formy działalności: {missing} — przypisz je (np. „pozostałe przychody i koszty”), inaczej "
                      "zestawienie uzupełniające będzie niepełne." if missing else
                      f"Wszystkie zapisy ({records.count()}) mają formę działalności. Pobierz zestawienie uzupełniające (PDF/XLSX).",
                      reverse("panel:accounting") + f"?rok={year}&miesiac=0" if missing else url,
                      "Zestawienie" if missing else "Zestawienie uzupełniające"))

    # 4. saldo rachunku na 31.12 uzgodnione
    r = reconciliation.reconcile(year, end)
    url = reverse("panel:accounting_reconciliation") + f"?rok={year}&do={end:%Y-%m-%d}"
    pending = next((g for g in r["groups"] if g.key == "pending"), None)
    if not r["ops_count"]:
        steps.append(Step("Saldo rachunku na 31.12 uzgodnione", WARN, "Brak operacji z wyciągu w tym roku.", url, "Uzgodnienie salda"))
    elif r["unexplained"] or pending:
        steps.append(Step("Saldo rachunku na 31.12 uzgodnione", TODO if finished else LATER,
                          (f"Różnica niewyjaśniona: {format_pln(r['unexplained'])}. " if r["unexplained"] else "")
                          + (f"Operacje do decyzji: {pending.count}." if pending else ""), url, "Uzgodnienie salda"))
    else:
        steps.append(Step("Saldo rachunku na 31.12 uzgodnione", OK if finished else LATER,
                          f"Saldo z wyciągu: {format_pln(r['closing'])}, różnice wyjaśnione." + ("" if finished else
                                                                                                  " Sprawdź ponownie po wgraniu wyciągu za grudzień."),
                          url, "Uzgodnienie salda"))

    # 5. zestawienie przepływów (załącznik nr 2) — kol. 4–6
    obj = AccountingYear.objects.filter(year=year).first()
    flows_done = obj is not None and obj.free_income is not None
    steps.append(Step("Zestawienie przepływów finansowych (załącznik nr 2)", OK if flows_done else (TODO if finished else LATER),
                      "Kolumny 4–6 uzupełnione — zestawienie w generatorze dokumentów." if flows_done else
                      "Po zakończeniu roku: wygeneruj zestawienie (kol. 4 — dochód wolny przeznaczony na cele statutowe, "
                      "kol. 6 — wydatki z dochodu wolnego). Formularz podpowiada wartości z ewidencji.",
                      reverse("panel:accounting_doc_new", args=["przeplywy"]), "Generuj" if not flows_done else "Dokumenty"))

    # 6. zamknięcie roku w ewidencji
    closed = obj is not None and obj.closed_at is not None
    steps.append(Step("Zamknięcie roku w ewidencji", OK if closed else (TODO if finished else LATER),
                      f"Rok zamknięty {timezone.localtime(obj.closed_at):%d.%m.%Y} — zapisy są zablokowane." if closed else
                      "Po zakończeniu roku i uzgodnieniu wszystkiego: pobierz zestawienie przychodów i kosztów za rok (PDF/XLSX), "
                      "potem zamknij rok — blokuje to dalsze zmiany zapisów (§ 7).",
                      reverse("panel:accounting_years"), "Lata i zamknięcia"))

    # 7. uchwała o przeznaczeniu dochodu na cele statutowe (zalecane — dokumentuje zwolnienie z art. 17 ust. 1 pkt 4)
    resolution = (Resolution.objects.filter(title__icontains="dochod", status=Resolution.Status.ADOPTED,
                                            adopted_on__gt=date(year, 1, 1), adopted_on__lte=cit_deadline)
                  .order_by("-adopted_on").first())
    draft = GeneratedDocument.objects.filter(kind="uchwala_dochod", data__rok=year).order_by("-created_at").first()
    if resolution:
        state, detail = OK, f"Przyjęta {resolution.adopted_on:%d.%m.%Y}: „{resolution.title}”."
        url, action = reverse("panel:resolutions"), "Uchwały"
    elif draft:
        state = WARN if finished else LATER
        detail = (f"Uchwała przygotowana w generatorze {timezone.localtime(draft.created_at):%d.%m.%Y} — po podjęciu wprowadź ją do "
                  f"rejestru uchwał („Wprowadź uchwałę z dokumentu”), do {cit_deadline:%d.%m.%Y}.")
        url, action = reverse("panel:resolution_historical"), "Wprowadź do rejestru"
    else:
        state = WARN if finished else LATER
        detail = (f"Dochód zwolniony z CIT (art. 17 ust. 1 pkt 4) musi być przeznaczony na cele statutowe — najlepiej "
                  f"udokumentować to uchwałą przed złożeniem CIT-8, do {cit_deadline:%d.%m.%Y}. Wzór z kwotami z ewidencji jest w generatorze.")
        url, action = reverse("panel:accounting_doc_new", args=["uchwala_dochod"]), "Generuj uchwałę"
    steps.append(Step("Uchwała o przeznaczeniu dochodu na cele statutowe (zalecane)", state, detail, url, action))

    # 8. CIT-8 z CIT-8/O
    sheet = GeneratedDocument.objects.filter(kind="cit8", data__rok=year).order_by("-created_at").first()
    days_left = (cit_deadline - today).days
    if not finished:
        state, detail = LATER, f"Po zakończeniu roku: arkusz danych do CIT-8 i CIT-8/O, zeznanie do {cit_deadline:%d.%m.%Y}."
    elif sheet:
        state = OK
        detail = (f"Arkusz przygotowany {timezone.localtime(sheet.created_at):%d.%m.%Y}. Złóż zeznanie elektronicznie do "
                  f"{cit_deadline:%d.%m.%Y} i zachowaj UPO.")
    else:
        state = TODO if days_left >= 0 else WARN
        detail = (f"Termin złożenia: {cit_deadline:%d.%m.%Y} (zostało {days_left} dni)." if days_left >= 0 else
                  f"Termin minął {cit_deadline:%d.%m.%Y} — złóż zeznanie jak najszybciej (rozważ czynny żal).")
    steps.append(Step("Zeznanie CIT-8 z załącznikiem CIT-8/O", state, detail,
                      reverse("panel:accounting_doc_new", args=["cit8"]), "Arkusz CIT-8"))
    return steps


def commission_amounts(start):
    """(kwota faktur prowizyjnych, prowizje netto z eksportu Stripe) za miesiąc — w groszach."""
    end = date(start.year, start.month, monthrange(start.year, start.month)[1])
    fee = 0
    for t in StripeTransaction.objects.filter(day__gte=start, day__lte=end).exclude(type="payout"):
        fee += t.fee if t.type in ("payment", "charge", "payment_refund", "refund") else -t.amount
    nxt = _next_month(start)
    invoiced = sum(i.gross for i in KsefInvoice.objects.filter(payment_method=PAYMENT_OFFSET, issue_date__gte=nxt,
                                                               issue_date__lt=_next_month(nxt))
                   .exclude(status=KsefInvoice.Status.REJECTED))
    return invoiced, fee


def explain(start, step, note, *, by):
    """Oznacza różnicę jako wyjaśnioną (z kwotami z tej chwili). Pusty `note` — cofa wyjaśnienie."""
    from django.core.exceptions import ValidationError

    from apps.audit.services import log_event

    if step != ChecklistExplanation.Step.COMMISSION:
        raise ValidationError("Nieznany krok listy kontrolnej.")
    note = (note or "").strip()
    current = ChecklistExplanation.objects.filter(month=start, step=step).first()
    if not note:
        if current:
            current.delete()
            log_event("ksiegowosc.roznica_cofnieta", None, f"{start:%m.%Y}: cofnięto wyjaśnienie różnicy ({current.note})", actor=by)
        return None
    invoiced, fee = commission_amounts(start)
    if invoiced == fee:
        raise ValidationError("Kwoty są zgodne — nie ma różnicy do wyjaśnienia.")
    if current:
        current.delete()
    obj = ChecklistExplanation.objects.create(month=start, step=step, note=note[:255], amount_a=invoiced, amount_b=fee,
                                              created_by=by)
    log_event("ksiegowosc.roznica_wyjasniona", obj,
              f"{start:%m.%Y}: faktura {format_pln(invoiced)} vs Stripe {format_pln(fee)} — {obj.note}", actor=by)
    return obj
