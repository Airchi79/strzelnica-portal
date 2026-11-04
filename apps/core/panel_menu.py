"""Menu boczne panelu: sekcje i pozycje widoczne zależnie od uprawnień. Aktywna pozycja wg nazwy widoku."""

from django.urls import reverse

# (etykieta, nazwa widoku, ikona, uprawnienie lub None, widoki, przy których pozycja jest aktywna; „*” na końcu = prefiks)
SECTIONS = [
    (None, [
        ("Przegląd", "panel:home", "home", None, ["home"]),
        ("Dziś na strzelnicy", "panel:today", "calendar", "bookings.manage_reservations", ["today"]),
        ("Powiadomienia", "panel:notifications", "bell", None, ["notification*"]),
    ]),
    ("Rezerwacje", [
        ("Wszystkie rezerwacje", "panel:reservations_days", "list", "bookings.manage_reservations",
         ["reservations_days", "reservation", "reservation_new", "reservations"]),
        ("Kalendarz osi", "panel:bookings", "grid", "bookings.manage_reservations", ["bookings"]),
        ("Blokady osi", "panel:blocks", "lock", "bookings.manage_reservations", ["block*"]),
        ("Archiwum GunTracker", "panel:reservations_archive", "archive", "bookings.manage_reservations", ["reservations_archive*"]),
        ("Ustawienia rezerwacji", "panel:booking_settings", "sliders", "bookings.manage_reservations", ["booking_settings"]),
    ]),
    ("Strzelnica", [
        ("Książka pobytu", "panel:logbook", "book", "logbook.view_logbook",
         ["logbook", "logbook_entry", "logbook_add", "logbook_print", "logbook_export"]),
        ("Archiwum książki", "panel:logbook_archive", "archive", "logbook.view_logbook", ["logbook_archive*"]),
        ("Kioski", "panel:kiosks", "tablet", "logbook.manage_logbook", ["kiosks"]),
        ("Ustawienia książki", "panel:logbook_settings", "sliders", "logbook.manage_logbook",
         ["logbook_settings", "logbook_rotate", "logbook_poster"]),
        ("Osie i cennik", "panel:lanes", "target", "facilities.manage_facilities", ["lane*", "facility*", "price_add"]),
        ("Regulaminy", "panel:regulations", "file", "regulations.publish_regulation", ["regulation*"]),
    ]),
    ("Klienci", [
        ("Klienci z kontem", "panel:users", "users", "accounts.view_users", ["users", "user", "client_*"]),
        ("Osoby bez konta", "panel:persons", "user", "accounts.view_users", ["person*"]),
        ("Weryfikacja dokumentów", "panel:credentials", "shield", "credentials.review_credential", ["credential*"]),
    ]),
    ("Płatności", [
        ("Wpłaty", "panel:payments", "receipt", "payments.manage_payments", ["payment*"]),
        ("Salda", "panel:wallets", "wallet", "payments.manage_payments", ["wallet*"]),
        ("Raporty sprzedaży", "panel:sales_reports", "chart", "reports.view_salesreport", ["sales_report*"]),
    ]),
    ("Stowarzyszenie", [
        ("Członkowie", "panel:members", "landmark", "association.manage_members", ["member*", "association_import*"]),
        ("Deklaracje", "panel:applications", "inbox", "association.manage_members", ["applications"]),
        ("Składki", "panel:fees", "coins", "association.manage_members", ["fee*"]),
        ("Uchwały", "panel:resolutions", "filecheck", "association.manage_resolutions", ["resolution*"]),
        ("Zebrania", "panel:meetings", "calendar", "association.manage_resolutions", ["meeting*"]),
        ("Kluby i kody", "panel:clubs", "key", "association.manage_clubs", ["club*"]),
        ("Raporty", "panel:reports", "chart", "association.view_reports", ["reports"]),
    ]),
    ("System", [
        ("Dane stowarzyszenia", "panel:settings", "building", "core.manage_settings", ["settings"]),
        ("Dziennik zdarzeń", "panel:audit", "history", "audit.view_auditlog", ["audit"]),
    ]),
]


def _matches(current, patterns):
    return any(current.startswith(p[:-1]) if p.endswith("*") else current == p for p in patterns)


def _counts(user):
    """Liczniki spraw czekających na zarząd, pokazywane przy pozycjach menu."""
    counts = {}
    if user.has_perm("credentials.review_credential"):
        from apps.credentials.models import Credential

        counts["panel:credentials"] = Credential.objects.filter(status=Credential.Status.PENDING).count()
    return counts


def build(request):
    user = request.user
    current = getattr(request.resolver_match, "url_name", "") or ""
    sections = []
    counts = _counts(user)
    for title, items in SECTIONS:
        visible = []
        for label, name, icon, perm, patterns in items:
            if perm and not user.has_perm(perm):
                continue
            visible.append({"label": label, "url": reverse(name), "icon": icon, "active": _matches(current, patterns),
                            "badge": "notifications" if name == "panel:notifications" else "", "count": counts.get(name, 0)})
        if visible:
            sections.append({"title": title, "items": visible})
    return sections
