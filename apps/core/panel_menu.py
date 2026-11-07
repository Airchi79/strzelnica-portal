"""Menu boczne panelu: sekcje i pozycje widoczne zależnie od uprawnień. Aktywna pozycja wg nazwy widoku."""

from django.urls import reverse

# (etykieta, nazwa widoku, ikona, uprawnienie lub None, widoki, przy których pozycja jest aktywna; „*” na końcu = prefiks)
SECTIONS = [
    (None, [
        ("Przegląd", "panel:home", "home", None, ["home"]),
        ("Teraz na strzelnicy", "panel:now", "target", "logbook.view_logbook", ["now"]),
        ("Dziś na strzelnicy", "panel:today", "calendar", "bookings.manage_reservations", ["today"]),
        ("Powiadomienia", "panel:notifications", "bell", None, ["notification*"]),
    ]),
    ("Rezerwacje", [
        ("Wszystkie rezerwacje", "panel:reservations_days", "list", "bookings.manage_reservations",
         ["reservations_days", "reservation", "reservation_new", "reservations"]),
        ("Kalendarz osi", "panel:bookings", "grid", "bookings.manage_reservations", ["bookings"]),
        ("Blokady osi", "panel:blocks", "lock", "bookings.manage_reservations", ["block*"]),
        ("Wydarzenia i zawody", "panel:events", "calendar", "events.manage_events", ["event*"]),
        ("Archiwum GunTracker", "panel:reservations_archive", "archive", "bookings.manage_reservations", ["reservations_archive*"]),
        ("Ustawienia rezerwacji", "panel:booking_settings", "sliders", "bookings.manage_booking_settings", ["booking_settings"]),
    ]),
    ("Strzelnica", [
        ("Książka pobytu", "panel:logbook", "book", "logbook.view_logbook",
         ["logbook", "logbook_entry", "logbook_add", "logbook_print", "logbook_export"]),
        ("Archiwum książki", "panel:logbook_archive", "archive", "logbook.view_logbook", ["logbook_archive*"]),
        ("Kioski", "panel:kiosks", "tablet", "logbook.manage_logbook", ["kiosks"]),
        ("Ustawienia książki", "panel:logbook_settings", "sliders", "logbook.manage_logbook",
         ["logbook_settings", "logbook_rotate", "logbook_poster"]),
        ("Osie i opłaty", "panel:lanes", "target", "facilities.manage_facilities", ["lane*", "facility*", "price_add"]),
        ("Usterki", "panel:issues", "alert", "facilities.manage_facilities", ["issue*"]),
        ("Regulaminy", "panel:regulations", "file", "regulations.publish_regulation", ["regulation*"]),
        ("Ogłoszenia", "panel:announcements", "bell", "core.publish_announcement", ["announcement*"]),
    ]),
    ("Klienci", [
        ("Klienci z kontem", "panel:users", "users", "accounts.view_users", ["users", "user", "client_*"]),
        ("Osoby bez konta", "panel:persons", "user", "accounts.view_users", ["person*"]),
        ("Weryfikacja dokumentów", "panel:credentials", "shield", "credentials.review_credential", ["credential*"]),
    ]),
    ("Płatności", [
        ("Wpłaty", "panel:payments", "receipt", "payments.manage_payments", ["payment*"]),
        ("Salda", "panel:wallets", "wallet", "payments.manage_payments", ["wallet*"]),
        ("Raporty wpływów", "panel:sales_reports", "chart", "reports.view_salesreport", ["sales_report*"]),
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
        ("Kopie zapasowe", "panel:backups", "archive", "core.manage_settings", ["backups"]),
        ("Widżet na stronę", "panel:widget", "external", "core.manage_settings", ["widget"]),
        ("RODO", "panel:gdpr", "shield", "accounts.view_users", ["gdpr*"]),
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
    if user.has_perm("facilities.manage_facilities"):
        from apps.facilities.models import Issue

        counts["panel:issues"] = Issue.objects.filter(status=Issue.Status.NEW).count()
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


# Kotwica w „Pomocy” dla bieżącej sekcji panelu (przycisk „?” w górnym pasku).
HELP_ANCHORS = [
    (("today",), "dzien"), (("reservation", "reservations", "bookings"), "rezerwacje"), (("block",), "blokady"),
    (("waitlist",), "oczekujacy"), (("payment_manual",), "gotowka"), (("payment", "wallet", "sales_report"), "platnosci"),
    (("credential",), "weryfikacja"), (("logbook", "kiosk"), "ksiazka"), (("event",), "zawody"), (("announcement",), "ogloszenia"),
    (("issue", "lane", "facility", "price"), "usterki"), (("user",), "uprawnienia"), (("gdpr",), "rodo"), (("backups",), "kopie"),
    (("settings",), "ustawienia"),
]


def help_anchor(url_name):
    for prefixes, anchor in HELP_ANCHORS:
        if any(url_name == p or url_name.startswith(p) for p in prefixes):
            return anchor
    return ""
