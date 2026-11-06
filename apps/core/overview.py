"""Przegląd panelu zarządu: sprawy wymagające uwagi i strumień ostatnich zdarzeń."""

from datetime import timedelta

from django.db.models import Count, Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.audit.models import AuditLog

# Grupy zdarzeń w strumieniu (prefiks akcji z dziennika → filtr).
FEED_GROUPS = {
    "rezerwacje": ("rezerwacja.", "blokada."),
    "platnosci": ("platnosc.", "platnosci.", "skladka.", "saldo.", "raport."),
    "ksiega": ("ksiazka.",),
    "klienci": ("konto.rejestracja", "dokument.", "deklaracja.", "czlonek.", "klient.", "kod_wejscia."),
}

FEED_LABELS = {
    "rezerwacja.utworzenie": "Nowa rezerwacja",
    "rezerwacja.anulowanie": "Anulowanie rezerwacji",
    "rezerwacja.zatwierdzenie": "Zatwierdzenie rezerwacji",
    "rezerwacja.oplacona": "Rezerwacja opłacona",
    "rezerwacja.zmiana_osob": "Zmiana liczby osób",
    "rezerwacja.rozliczenie_ksiazka": "Rozliczenie wg książki pobytu",
    "rezerwacja.nieodbyta": "Rezerwacja nieodbyta",
    "rezerwacja.platnosc": "Zmiana rozliczenia",
    "blokada.utworzenie": "Blokada osi",
    "platnosc.zaksiegowana": "Wpłata zaksięgowana",
    "platnosc.tpay_start": "Płatność online rozpoczęta",
    "platnosc.zwrot_tpay": "Zwrot płatności",
    "platnosci.import_wyciagu": "Import wyciągu",
    "raport.sprzedaz": "Raport sprzedaży",
    "skladka.oplacona": "Składka opłacona",
    "ksiazka.wpis": "Wpis do książki pobytu",
    "ksiazka.wypis": "Wyjście ze strzelnicy",
    "konto.rejestracja": "Nowe konto",
    "dokument.dodanie": "Dokument do weryfikacji",
    "dokument.zmiana": "Zmieniony dokument",
    "dokument.weryfikacja": "Weryfikacja dokumentu",
    "deklaracja.zlozona": "Deklaracja członkowska",
    "czlonek.przyjety": "Przyjęcie członka",
    "czlonek.wpis_do_rejestru": "Wpis do rejestru członków",
    "czlonek.dane": "Zmiana danych członka",
    "czlonek.funkcja_zarzad": "Zmiana funkcji w Zarządzie",
    "czlonek.polaczono_konto": "Połączenie członka z kontem",
    "czlonek.rodzaj": "Zmiana rodzaju członkostwa",
    "czlonek.status": "Zmiana statusu członka",
    "deklaracja.wycofana": "Wycofana deklaracja",
    "dokument.usuniecie": "Usunięty dokument",
    "dokument.wygasniecie": "Wygasły dokument",
    "blokada.usuniecie": "Usunięta blokada osi",
    "platnosc.pominieta": "Pominięta wpłata",
    "platnosc.przypisanie_reczne": "Ręczne przypisanie wpłaty",
    "saldo.korekta": "Korekta salda",
    "ksiazka.wydruk": "Wydruk / eksport książki pobytu",
    "ksiazka.czyszczenie": "Usunięcie przeterminowanych wpisów",
    "ksiazka.nowy_kod_osobisty": "Nowy kod osobisty",
    "ksiazka.nowy_kod_wejscia": "Nowy kod wejścia",
    "ksiazka.ustawienia": "Zmiana ustawień książki",
    "ksiazka.eksport": "Eksport książki pobytu (XLSX)",
    "ksiazka.wypis_auto": "Automatyczne zamknięcie pobytów",
    "klient.kartoteka": "Zmiana w kartotece klienta",
    "kiosk.wpis_odrzucony": "Odrzucony wpis z kiosku",
    "klient.zatwierdzenie": "Zatwierdzenie klienta",
    "klient.cofniecie_zatwierdzenia": "Cofnięcie zatwierdzenia klienta",
    "kod_wejscia.wydanie": "Wydanie kodu wejścia",
    "kod_wejscia.zaplata": "Opłacenie kodu wejścia",
    "kod_wejscia.dezaktywacja": "Wyłączenie kodu wejścia",
}


def _label(action):
    if action in FEED_LABELS:
        return FEED_LABELS[action]
    # Zdarzenia bez opisu (np. z wcześniejszych wersji) — czytelna forma zamiast technicznej nazwy.
    return action.split(".", 1)[-1].replace("_", " ").capitalize()

_URLS = {
    "bookings.reservation": ("panel:reservation", True),
    "logbook.entry": ("panel:logbook_entry", True),
    "payments.payment": ("panel:payment", True),
    "credentials.credential": ("panel:credential", True),
    "association.membershipapplication": ("panel:applications", False),
    "association.member": ("panel:member", True),
    "association.fee": ("panel:fees", False),
    "accounts.user": ("panel:user", True),
    "reports.salesreport": ("panel:sales_report", True),
}


def event_url(entry):
    name, with_pk = _URLS.get(entry.object_type, (None, False))
    if not name or (with_pk and not entry.object_id):
        return ""
    try:
        return reverse(name, args=[entry.object_id]) if with_pk else reverse(name)
    except NoReverseMatch:
        return ""


def feed(group="", limit=60):
    prefixes = FEED_GROUPS.get(group) or tuple(p for ps in FEED_GROUPS.values() for p in ps)
    q = Q()
    for p in prefixes:
        q |= Q(action__startswith=p)
    rows = []
    for e in AuditLog.objects.filter(q).select_related("actor")[:limit]:
        rows.append({"entry": e, "label": _label(e.action), "url": event_url(e)})
    return rows


def attention(user, now=None):
    """Liczniki „wymaga uwagi” — tylko dla sekcji, do których użytkownik ma uprawnienia."""
    from apps.association.models import Member, MembershipApplication, Resolution
    from apps.bookings.models import Reservation
    from apps.credentials.models import Credential
    from apps.logbook.models import Entry
    from apps.payments.models import Payment

    now = now or timezone.now()
    tiles = []
    if user.has_perm("bookings.manage_reservations"):
        pending = Reservation.objects.filter(status=Reservation.Status.PENDING_APPROVAL).count()
        tiles.append(("Czekają na zatwierdzenie", "Rezerwacje nowych klientów", pending, reverse("panel:reservations_days") + "?filtr=do_zatwierdzenia"))
        due = Reservation.objects.filter(payment_status=Reservation.Payment.DUE).exclude(status=Reservation.Status.CANCELLED, late_cancellation=False).count()
        tiles.append(("Płatności", "Rezerwacje do zapłaty", due, reverse("panel:reservations_days") + "?filtr=do_zaplaty"))
        missing = (
            Reservation.objects.filter(status=Reservation.Status.CONFIRMED, starts_at__lte=now, starts_at__gte=now - timedelta(days=30))
            .annotate(n=Count("log_entries", filter=Q(log_entries__purpose=Entry.Purpose.SHOOTING))).filter(n=0).count()
        )
        tiles.append(("Braki w książce pobytu", "Odbyte rezerwacje bez wpisów (30 dni)", missing, reverse("panel:reservations_days") + "?filtr=braki_ksiega"))
    if user.has_perm("credentials.review_credential"):
        tiles.append(("Dokumenty do weryfikacji", "Pozwolenia i legitymacje PS", Credential.objects.filter(status=Credential.Status.PENDING).count(), reverse("panel:credentials")))
        today = timezone.localdate()
        soon = Credential.objects.filter(status=Credential.Status.VERIFIED, valid_until__gte=today, valid_until__lte=today + timedelta(days=30)).count()
        tiles.append(("Dokumenty wygasają", "Ważność kończy się w ciągu 30 dni", soon, reverse("panel:credentials") + "?status=wygasa"))
    if user.has_perm("association.manage_members"):
        tiles.append(("Deklaracje członkowskie", "Czekają na uchwałę Zarządu", MembershipApplication.objects.filter(status=MembershipApplication.Status.PENDING).count(), reverse("panel:applications")))
    me = Member.objects.filter(user=user, status=Member.Status.ACTIVE).exclude(board_role="").first()
    if me:
        to_vote = Resolution.objects.filter(status=Resolution.Status.VOTING).exclude(votes__member=me).count()
        tiles.append(("Uchwały do głosowania", "Twój głos w Zarządzie", to_vote, reverse("panel:resolutions")))
    if user.has_perm("facilities.manage_facilities"):
        from apps.facilities.models import Issue

        open_issues = Issue.objects.filter(status__in=Issue.OPEN)
        urgent = open_issues.filter(urgent=True).count()
        tiles.append(("Usterki", f"Otwarte zgłoszenia{f', pilne: {urgent}' if urgent else ''}", open_issues.count(), reverse("panel:issues")))
    if user.has_perm("core.manage_settings"):
        from .backups import read_status

        st = read_status(now)
        tiles.append(("Kopia zapasowa", "Kopia odtworzona i sprawdzona" if st["state"] == "ok" else st["message"][:80],
                      0 if st["state"] == "ok" else 1, reverse("panel:backups")))
    if user.has_perm("payments.manage_payments"):
        from apps.payments.services import online_fee_missing_in_rules

        if online_fee_missing_in_rules():
            tiles.append(("Regulamin a opłata online", "Opłata za płatność online jest włączona, a regulamin o niej nie wspomina", 1,
                          reverse("panel:payment_settings")))
        tiles.append(("Wpłaty do przypisania", "Przelewy bez rozpoznanego tytułu", Payment.objects.filter(status=Payment.Status.UNMATCHED).count(), reverse("panel:payments") + "?status=nieprzypisana"))
    return tiles
