"""Menu boczne panelu: sekcje i pozycje widoczne zależnie od uprawnień. Aktywna pozycja wg nazwy widoku."""

from django.urls import reverse

# (etykieta, cel, ikona, uprawnienie, widoki, przy których pozycja jest aktywna; „*” na końcu = prefiks).
# Cel: nazwa widoku albo lista par (widok, uprawnienie) — pierwszy dostępny dla osoby (jedna pozycja zamiast kilku).
# Uprawnienie: None, nazwa albo krotka (wystarczy jedno z nich).
SETTINGS_PAGES = [  # strona „Ustawienia” — wszystkie ustawienia w jednym miejscu
    ("panel:settings", "core.manage_settings", "Dane stowarzyszenia", "Nazwa, adres, NIP, rachunek, logo, kontakt, dane do dokumentów."),
    ("panel:booking_settings", "bookings.manage_booking_settings", "Rezerwacje",
     "Anulowanie, terminy płatności, wyprzedzenie, grupy strzelców, prowadzący od strzelnicy."),
    ("panel:logbook_settings", "logbook.manage_logbook", "Książka pobytu", "Okres przechowywania, oświadczenie, kod QR wejścia, plakat."),
    ("panel:kiosks", "logbook.manage_logbook", "Kioski", "Parowanie urządzeń przy wejściu, kamera, wyłączanie."),
    ("panel:payment_settings", "payments.manage_payments", "Płatności", "Opłata za płatność online, saldo, sposoby płatności."),
    ("panel:accounting_settings", "accounting.manage_accounting", "Księgowość",
     "Przychody z systemu od dnia, organ zatwierdzający, urząd skarbowy, koniec płatności przez Stripe."),
    ("panel:lanes", "facilities.manage_facilities", "Osie, godziny i opłaty", "Godziny otwarcia (także zbiorczo), cennik osi."),
    ("panel:promotion_settings", "promotion.manage_promotion", "Promocja",
     "Dokąd prowadzą linki do rezerwacji (GunTracker / nasz system), styl tekstów, hasztagi, stałe informacje."),
]
SETTINGS_PERMS = tuple(sorted({perm for _, perm, _, _ in SETTINGS_PAGES}))

SECTIONS = [
    (None, [
        ("Przegląd", "panel:home", "home", None, ["home"]),
        ("Dziś na strzelnicy", [("panel:today", "bookings.manage_reservations"), ("panel:now", "logbook.view_logbook")], "calendar",
         ("bookings.manage_reservations", "logbook.view_logbook"), ["today", "now"]),
        ("Powiadomienia", "panel:notifications", "bell", None, ["notification*"]),
    ]),
    ("Rezerwacje", [
        ("Wszystkie rezerwacje", "panel:reservations_days", "list", "bookings.manage_reservations",
         ["reservations_days", "reservation", "reservation_new", "reservations"]),
        ("Kalendarz osi", "panel:bookings", "grid", "bookings.manage_reservations", ["bookings"]),
        ("Blokady osi", "panel:blocks", "lock", "bookings.manage_reservations", ["block*"]),
        ("Wydarzenia i zawody", "panel:events", "calendar", "events.manage_events", ["event*"]),
    ]),
    ("Strzelnica", [
        ("Książka pobytu", "panel:logbook", "book", "logbook.view_logbook",
         ["logbook", "logbook_entry", "logbook_add", "logbook_print", "logbook_export"]),
        ("Kioski", "panel:kiosks", "tablet", "logbook.manage_logbook", ["kiosks"]),
        ("Osie i opłaty", "panel:lanes", "target", "facilities.manage_facilities", ["lane*", "facility*", "price_add"]),
        ("Usterki", "panel:issues", "alert", "facilities.manage_facilities", ["issue*"]),
        ("Regulaminy", "panel:regulations", "file", "regulations.publish_regulation", ["regulation*"]),
        ("Ogłoszenia", "panel:announcements", "bell", "core.publish_announcement", ["announcement*"]),
        ("Częste pytania", "panel:faq", "list", "core.publish_announcement", ["faq*"]),
    ]),
    ("Strzelcy", [
        ("Strzelcy z kontem", "panel:users", "users", "accounts.view_users", ["users", "user", "client_*"]),
        ("Osoby bez konta", "panel:persons", "user", "accounts.view_users", ["person*"]),
        ("Weryfikacja dokumentów", "panel:credentials", "shield", "credentials.review_credential", ["credential*"]),
    ]),
    ("Płatności", [
        ("Wpłaty", "panel:payments", "receipt", "payments.manage_payments", ["payment*"]),
        ("Salda", "panel:wallets", "wallet", "payments.manage_payments", ["wallet*"]),
        ("Księgowość", "panel:accounting", "book", "accounting.manage_accounting", ["accounting*"]),
    ]),
    ("Stowarzyszenie", [
        ("Członkowie", "panel:members", "landmark", "association.manage_members", ["member*", "association_import*"]),
        ("Deklaracje", "panel:applications", "inbox", "association.manage_members", ["applications"]),
        ("Składki", "panel:fees", "coins", "association.manage_members", ["fee*"]),
        ("Uchwały", "panel:resolutions", "filecheck", "association.manage_resolutions", ["resolution*"]),
        ("Zebrania", "panel:meetings", "calendar", "association.manage_resolutions", ["meeting*"]),
        ("Kluby i kody", "panel:clubs", "key", "association.manage_clubs", ["club*"]),
    ]),
    ("Raporty", [
        ("Statystyki i raporty", "panel:reports", "chart", "association.view_reports",
         ["reports", "month_stats", "occupancy", "reservations_report"]),
        ("Raporty wpływów", "panel:sales_reports", "receipt", "reports.view_salesreport", ["sales_report*"]),
    ]),
    ("Promocja", [
        ("Publikacje", "panel:promotion", "external", "promotion.manage_promotion", ["promotion", "promotion_post", "promotion_graphic"]),
    ]),
    ("System", [
        ("Ustawienia", "panel:settings_hub", "sliders", SETTINGS_PERMS,
         ["settings_hub", "settings", "booking_settings", "logbook_settings", "logbook_rotate", "logbook_poster", "payment_settings",
          "accounting_settings", "promotion_settings"]),
        ("Archiwum GunTracker", [("panel:reservations_archive", "bookings.manage_reservations"),
                                 ("panel:logbook_archive", "logbook.view_logbook")], "archive",
         ("bookings.manage_reservations", "logbook.view_logbook"), ["reservations_archive*", "logbook_archive*"]),
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


def has_any(user, perm):
    if not perm:
        return True
    return any(user.has_perm(p) for p in (perm if isinstance(perm, (list, tuple)) else [perm]))


def _target(user, target):
    """Nazwa widoku dla pozycji menu: stała albo pierwsza z listy (widok, uprawnienie) dostępna dla osoby."""
    if isinstance(target, str):
        return target
    return next((name for name, perm in target if has_any(user, perm)), None)


def settings_pages(user):
    return [{"url": reverse(name), "title": title, "text": text} for name, perm, title, text in SETTINGS_PAGES
            if has_any(user, perm)]


def build(request):
    user = request.user
    current = getattr(request.resolver_match, "url_name", "") or ""
    sections = []
    counts = _counts(user)
    for title, items in SECTIONS:
        visible = []
        for label, target, icon, perm, patterns in items:
            name = _target(user, target) if has_any(user, perm) else None
            if name is None:
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
