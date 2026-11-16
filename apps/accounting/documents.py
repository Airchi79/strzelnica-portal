"""Generator dokumentów uproszczonej ewidencji: zawiadomienia do urzędu skarbowego, uchwały, opis zasad prowadzenia
ewidencji, zestawienie przepływów finansowych (załącznik nr 2), arkusz pomocniczy do CIT-8, dowód wewnętrzny.

Każdy dokument to formularz (pola z podpowiedziami z danych stowarzyszenia i ewidencji) oraz treść złożona z bloków,
z których powstaje PDF (do podpisu) albo DOCX (do dalszej edycji w edytorze tekstu). Dane każdego wygenerowanego
dokumentu są zapamiętywane — dokument można pobrać ponownie w tej samej treści.

Podstawy prawne (teksty w brzmieniu od 1.01.2026):
- art. 10a ustawy z dnia 24 kwietnia 2003 r. o działalności pożytku publicznego i o wolontariacie (Dz. U. z 2025 r.
  poz. 1338, z późn. zm.),
- rozporządzenie Ministra Finansów i Gospodarki z dnia 22 grudnia 2025 r. (Dz. U. poz. 1880),
- art. 7, 8, 17 ust. 1 pkt 4 i art. 27 ustawy o podatku dochodowym od osób prawnych (Dz. U. z 2025 r. poz. 278, z późn. zm.)."""

import io
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from django import forms
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from .models import AccountingSettings, AccountingYear, GeneratedDocument

UDPP = ("ustawy z dnia 24 kwietnia 2003 r. o działalności pożytku publicznego i o wolontariacie "
        "(Dz. U. z 2025 r. poz. 1338, z późn. zm.)")
ROZP = ("rozporządzenia Ministra Finansów i Gospodarki z dnia 22 grudnia 2025 r. w sprawie prowadzenia uproszczonej "
        "ewidencji przychodów i kosztów przez niektóre organizacje pozarządowe oraz stowarzyszenia jednostek samorządu "
        "terytorialnego (Dz. U. poz. 1880)")
CIT = "ustawy z dnia 15 lutego 1992 r. o podatku dochodowym od osób prawnych (Dz. U. z 2025 r. poz. 278, z późn. zm.)"
MONTHS_GEN = ["", "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
              "października", "listopada", "grudnia"]


def pl_date(d):
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{d.day} {MONTHS_GEN[d.month]} {d.year} r." if d else ""


def pln(grosze):
    if grosze is None:
        return "—"
    return f"{grosze / 100:,.2f}".replace(",", " ").replace(".", ",") + " zł"


def _lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _date_field(label, required=True):
    return forms.DateField(label=label, required=required, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))


def _amount_field(label, required=False):
    from .forms import AmountField

    return AmountField(label=label, required=required)


# --- definicje dokumentów -------------------------------------------------------------


class Doc:
    key = ""
    title = ""
    group = ""
    legal = ""
    hint = ""
    prefix = ""  # numer własny dokumentu (np. „DW”) — puste: bez numeru
    landscape = False

    def fields(self, ctx):
        return {}

    def initial(self, ctx):
        return {}

    def blocks(self, d, ctx):
        raise NotImplementedError

    def doc_title(self, d):
        return self.title

    def after_create(self, d, ctx):
        """Zapis skutków ubocznych (np. danych do zestawienia przepływów)."""

    def prepare(self, cleaned, ctx):
        """Uzupełnienie danych przed zapisem (np. kwot z ewidencji — dokument ma tę samą treść przy ponownym pobraniu)."""
        return cleaned


def _header(ctx, place, day):
    a = ctx["association"]
    ident = [a["name"], a["address"]]
    if a["nip"]:
        ident.append(f"NIP: {a['nip']}")
    if a["regon"]:
        ident.append(f"REGON: {a['regon']}")
    return [("right", f"{place}, {pl_date(day)}" if place else pl_date(day)), ("left", "\n".join(x for x in ident if x))]


def _signatures(ctx, extra=None):
    names = extra if extra is not None else _lines(ctx["settings"]["signatories"])
    return [("sign", names or ["(imię, nazwisko, funkcja, podpis)"])]


class ElectionResolution(Doc):
    key = "uchwala_wybor"
    title = "Uchwała w sprawie prowadzenia uproszczonej ewidencji przychodów i kosztów"
    group = "Uchwały"
    legal = "art. 10a ust. 1 i 3 ustawy o działalności pożytku publicznego i o wolontariacie"
    hint = "Decyzję o prowadzeniu uproszczonej ewidencji podejmuje organ zatwierdzający (art. 10a ust. 1 in fine)."

    def fields(self, ctx):
        return {
            "numer": forms.CharField(label="Numer uchwały", max_length=40, required=False),
            "data": _date_field("Data podjęcia"),
            "miejscowosc": forms.CharField(label="Miejscowość", max_length=80, required=False),
            "organ": forms.CharField(label="Organ zatwierdzający", max_length=120),
            "rok": forms.IntegerField(label="Pierwszy rok podatkowy prowadzenia ewidencji", min_value=2026, max_value=2100),
            "nowa_jednostka": forms.BooleanField(label="Stowarzyszenie rozpoczęło działalność w tym roku (bez limitu przychodów — art. 10a ust. 2)", required=False),
            "przychody_poprzedni": _amount_field("Przychody w roku poprzedzającym rok wyboru [zł] (bez darowizn rzeczowych)"),
            "zawiadamia": forms.CharField(label="Kto zawiadamia urząd skarbowy", max_length=120, initial="Zarząd"),
            "podpisy": forms.CharField(label="Podpisy (każdy w osobnym wierszu)", widget=forms.Textarea(attrs={"rows": 3}), required=False),
        }

    def initial(self, ctx):
        year = timezone.localdate().year
        return {"data": timezone.localdate(), "organ": ctx["settings"]["approving_body"] or "Zebranie Członków",
                "rok": year, "miejscowosc": ctx["place"], "podpisy": "Przewodniczący zebrania\nProtokolant",
                "przychody_poprzedni": ctx["revenue_by_year"].get(year - 1)}

    def doc_title(self, d):
        return f"Uchwała {d.get('numer') or ''} w sprawie uproszczonej ewidencji od {d['rok']} r.".replace("  ", " ")

    def blocks(self, d, ctx):
        name = ctx["association"]["name"]
        limit = ("rozpoczęło działalność w roku podatkowym, w którym rozpoczyna prowadzenie uproszczonej ewidencji, wobec "
                 "czego warunku dotyczącego wysokości przychodów nie stosuje się (art. 10a ust. 2 ustawy)"
                 if d.get("nowa_jednostka") else
                 f"w roku poprzedzającym rok wyboru prowadzenia uproszczonej ewidencji osiągnęło przychody w wysokości "
                 f"{pln(d.get('przychody_poprzedni'))}, nieprzekraczającej 1 000 000 zł, nie wliczając wartości darowizn "
                 "rzeczowych przeznaczonych na prowadzenie działalności statutowej")
        return [
            ("right", f"{d.get('miejscowosc') or ''}, {pl_date(d['data'])}".lstrip(", ")),
            ("title", f"UCHWAŁA{(' NR ' + d['numer']) if d.get('numer') else ''}"),
            ("subtitle", f"{d['organ']} {name}\nz dnia {pl_date(d['data'])}\nw sprawie prowadzenia uproszczonej ewidencji przychodów i kosztów"),
            ("p", f"Na podstawie art. 10a ust. 1 {UDPP} postanawia się, co następuje:"),
            ("section", "§ 1"),
            ("p", f"Od roku podatkowego {d['rok']} {name} prowadzi uproszczoną ewidencję przychodów i kosztów na zasadach "
                  f"określonych w {ROZP}."),
            ("section", "§ 2"),
            ("p", "Stwierdza się, że stowarzyszenie spełnia warunki określone w art. 10a ust. 1 ustawy, ponieważ:"),
            ("list", ["nie prowadzi działalności gospodarczej w rozumieniu przepisów ustawy z dnia 6 marca 2018 r. – Prawo przedsiębiorców,",
                      "nie posiada statusu organizacji pożytku publicznego,", limit + "."]),
            ("section", "§ 3"),
            ("p", f"{d['zawiadamia']} zawiadomi naczelnika urzędu skarbowego właściwego w sprawach opodatkowania podatkiem "
                  "dochodowym o wyborze prowadzenia uproszczonej ewidencji w terminie do końca trzeciego miesiąca roku "
                  "podatkowego, w którym stowarzyszenie rozpoczyna jej prowadzenie (art. 10a ust. 3 ustawy). Zawiadomienie "
                  "dotyczy także lat następnych (art. 10a ust. 4 ustawy)."),
            ("section", "§ 4"),
            ("p", "Uchwała wchodzi w życie z dniem podjęcia."),
        ] + _signatures(ctx, _lines(d.get("podpisy")))


class ElectionNotice(Doc):
    key = "zawiadomienie_wybor"
    title = "Zawiadomienie urzędu skarbowego o wyborze uproszczonej ewidencji"
    group = "Urząd skarbowy"
    legal = "art. 10a ust. 3 i 4 ustawy o działalności pożytku publicznego i o wolontariacie"
    hint = "Termin: do końca trzeciego miesiąca roku podatkowego, w którym ewidencja jest prowadzona po raz pierwszy " \
           "(nowe stowarzyszenie: 90 dni od rozpoczęcia działalności). Dotyczy także lat następnych."

    def fields(self, ctx):
        return {
            "data": _date_field("Data zawiadomienia"),
            "miejscowosc": forms.CharField(label="Miejscowość", max_length=80, required=False),
            "urzad": forms.CharField(label="Urząd skarbowy (nazwa i adres)", widget=forms.Textarea(attrs={"rows": 3})),
            "rok": forms.IntegerField(label="Pierwszy rok podatkowy prowadzenia ewidencji", min_value=2026, max_value=2100),
            "uchwala": forms.CharField(label="Uchwała organu zatwierdzającego (numer i data)", max_length=160, required=False),
            "podpisy": forms.CharField(label="Podpisy (każdy w osobnym wierszu)", widget=forms.Textarea(attrs={"rows": 3}), required=False),
        }

    def initial(self, ctx):
        return {"data": timezone.localdate(), "miejscowosc": ctx["place"], "urzad": ctx["settings"]["tax_office"],
                "rok": timezone.localdate().year, "podpisy": ctx["settings"]["signatories"]}

    def doc_title(self, d):
        return f"Zawiadomienie US o wyborze uproszczonej ewidencji od {d['rok']} r."

    def blocks(self, d, ctx):
        name = ctx["association"]["name"]
        basis = f" na podstawie uchwały {d['uchwala']}" if d.get("uchwala") else ""
        return _header(ctx, d.get("miejscowosc"), d["data"]) + [
            ("addressee", "Naczelnik\n" + d["urzad"]),
            ("title", "ZAWIADOMIENIE"),
            ("subtitle", "o wyborze prowadzenia uproszczonej ewidencji przychodów i kosztów"),
            ("p", f"Na podstawie art. 10a ust. 3 {UDPP} {name} zawiadamia, że{basis} od roku podatkowego {d['rok']} "
                  f"prowadzi uproszczoną ewidencję przychodów i kosztów, o której mowa w art. 10a ust. 1 tej ustawy, "
                  f"zgodnie z przepisami {ROZP}."),
            ("p", "Stowarzyszenie nie prowadzi działalności gospodarczej, nie posiada statusu organizacji pożytku publicznego "
                  "i spełnia warunek dotyczący wysokości przychodów określony w art. 10a ust. 1 pkt 5 ustawy. "
                  "Zgodnie z art. 10a ust. 4 ustawy zawiadomienie dotyczy także lat następnych."),
        ] + _signatures(ctx, _lines(d.get("podpisy")))


class ResignationNotice(Doc):
    key = "zawiadomienie_rezygnacja"
    title = "Zawiadomienie urzędu skarbowego o rezygnacji z ewidencji / niespełnianiu warunków"
    group = "Urząd skarbowy"
    legal = "art. 10a ust. 4a i 5 ustawy o działalności pożytku publicznego i o wolontariacie"
    hint = "Termin: 90 dni od końca ostatniego miesiąca prowadzenia ewidencji (rezygnacja) albo od końca ostatniego " \
           "miesiąca roku podatkowego (niespełnianie warunków). Rezygnacja w trakcie roku oznacza księgi rachunkowe od początku roku."
    REASONS = [("rezygnacja", "rezygnacja z prowadzenia uproszczonej ewidencji (art. 10a ust. 5 pkt 1)"),
               ("warunki", "niespełnianie warunków z art. 10a ust. 1 (art. 10a ust. 5 pkt 2)")]

    def fields(self, ctx):
        return {
            "powod": forms.ChoiceField(label="Rodzaj zawiadomienia", choices=self.REASONS, widget=forms.RadioSelect),
            "data": _date_field("Data zawiadomienia"),
            "miejscowosc": forms.CharField(label="Miejscowość", max_length=80, required=False),
            "urzad": forms.CharField(label="Urząd skarbowy (nazwa i adres)", widget=forms.Textarea(attrs={"rows": 3})),
            "ostatni_miesiac": forms.CharField(label="Ostatni miesiąc prowadzenia ewidencji / rok podatkowy (np. „czerwiec 2027 r.”)", max_length=60),
            "uchwala": forms.CharField(label="Uchwała organu zatwierdzającego (numer i data) — przy rezygnacji", max_length=160, required=False),
            "podpisy": forms.CharField(label="Podpisy (każdy w osobnym wierszu)", widget=forms.Textarea(attrs={"rows": 3}), required=False),
        }

    def initial(self, ctx):
        return {"powod": "rezygnacja", "data": timezone.localdate(), "miejscowosc": ctx["place"],
                "urzad": ctx["settings"]["tax_office"], "podpisy": ctx["settings"]["signatories"]}

    def doc_title(self, d):
        return "Zawiadomienie US — " + ("rezygnacja z uproszczonej ewidencji" if d["powod"] == "rezygnacja"
                                        else "niespełnianie warunków uproszczonej ewidencji")

    def blocks(self, d, ctx):
        name = ctx["association"]["name"]
        if d["powod"] == "rezygnacja":
            body = (f"Na podstawie art. 10a ust. 5 pkt 1 {UDPP} {name} zawiadamia o rezygnacji z prowadzenia uproszczonej "
                    f"ewidencji przychodów i kosztów{(' na podstawie uchwały ' + d['uchwala']) if d.get('uchwala') else ''}. "
                    f"Ostatnim miesiącem prowadzenia uproszczonej ewidencji jest {d['ostatni_miesiac']}. Zgodnie z art. 10a "
                    "ust. 4a ustawy stowarzyszenie prowadzi księgowość zgodnie z przepisami o rachunkowości od początku roku podatkowego.")
        else:
            body = (f"Na podstawie art. 10a ust. 5 pkt 2 {UDPP} {name} zawiadamia o niespełnianiu warunków, o których mowa "
                    f"w art. 10a ust. 1 tej ustawy. Uproszczona ewidencja przychodów i kosztów była prowadzona w roku "
                    f"podatkowym: {d['ostatni_miesiac']}.")
        return _header(ctx, d.get("miejscowosc"), d["data"]) + [
            ("addressee", "Naczelnik\n" + d["urzad"]), ("title", "ZAWIADOMIENIE"),
            ("subtitle", "w sprawie uproszczonej ewidencji przychodów i kosztów"), ("p", body),
        ] + _signatures(ctx, _lines(d.get("podpisy")))


class Principles(Doc):
    key = "opis_zasad"
    title = "Opis zasad prowadzenia uproszczonej ewidencji (uchwała zarządu z załącznikiem)"
    group = "Uchwały"
    legal = "§ 6 ust. 2 i 3 rozporządzenia (Dz. U. 2025 poz. 1880)"
    hint = "Opis zasad jest częścią uproszczonej ewidencji. Treść jest przygotowana dla ewidencji prowadzonej w tym systemie — " \
           "sprawdź ją i uzupełnij przed podjęciem uchwały (DOCX pozwala na edycję)."

    def fields(self, ctx):
        return {
            "numer": forms.CharField(label="Numer uchwały zarządu", max_length=40, required=False),
            "data": _date_field("Data uchwały"),
            "miejscowosc": forms.CharField(label="Miejscowość", max_length=80, required=False),
            "od_dnia": _date_field("Opis obowiązuje od dnia"),
            "start_programu": _date_field("Data rozpoczęcia eksploatacji programu"),
            "wersja": forms.CharField(label="Wersja oprogramowania", max_length=80),
            "przechowywanie": forms.CharField(label="Miejsce przechowywania dowodów papierowych", max_length=255),
            "osoby": forms.CharField(label="Osoby uprawnione do zapisów (każda w osobnym wierszu)", widget=forms.Textarea(attrs={"rows": 3})),
            "podpisy": forms.CharField(label="Podpisy (każdy w osobnym wierszu)", widget=forms.Textarea(attrs={"rows": 3}), required=False),
        }

    def initial(self, ctx):
        today = timezone.localdate()
        return {"data": today, "miejscowosc": ctx["place"], "od_dnia": date(today.year, 1, 1),
                "start_programu": ctx["auto_from"] or today, "wersja": f"Portal strzelnicy — moduł Księgowość, wydanie z {today:%d.%m.%Y}",
                "przechowywanie": f"siedziba stowarzyszenia: {ctx['association']['address']}".rstrip(": "),
                "osoby": ctx["settings"]["signatories"], "podpisy": ctx["settings"]["signatories"]}

    def doc_title(self, d):
        return f"Opis zasad prowadzenia uproszczonej ewidencji (od {d['od_dnia']})"

    def blocks(self, d, ctx):
        name = ctx["association"]["name"]
        people = _lines(d["osoby"])
        return [
            ("right", f"{d.get('miejscowosc') or ''}, {pl_date(d['data'])}".lstrip(", ")),
            ("title", f"UCHWAŁA{(' NR ' + d['numer']) if d.get('numer') else ''} ZARZĄDU"),
            ("subtitle", f"{name}\nz dnia {pl_date(d['data'])}\nw sprawie opisu zasad prowadzenia uproszczonej ewidencji przychodów i kosztów"),
            ("p", f"Na podstawie § 6 ust. 2 {ROZP} postanawia się, co następuje:"),
            ("section", "§ 1"),
            ("p", f"Przyjmuje się opis zasad prowadzenia uproszczonej ewidencji przychodów i kosztów stanowiący załącznik do "
                  f"uchwały, obowiązujący od dnia {pl_date(d['od_dnia'])}."),
            ("section", "§ 2"),
            ("p", "Uchwała wchodzi w życie z dniem podjęcia."),
        ] + _signatures(ctx, _lines(d.get("podpisy"))) + [
            ("pagebreak", ""),
            ("right", "Załącznik do uchwały zarządu"),
            ("title", "OPIS ZASAD PROWADZENIA UPROSZCZONEJ EWIDENCJI PRZYCHODÓW I KOSZTÓW"),
            ("subtitle", name),
            ("heading", "1. Podstawa i zakres"),
            ("p", f"Stowarzyszenie prowadzi uproszczoną ewidencję przychodów i kosztów na podstawie art. 10a {UDPP} oraz "
                  f"{ROZP}, w języku polskim i w walucie polskiej (§ 6 ust. 1 rozporządzenia). Ewidencja obejmuje: zestawienie "
                  "przychodów i kosztów (załącznik nr 1 do rozporządzenia), zestawienie przepływów finansowych (załącznik nr 2), "
                  "wykaz środków trwałych oraz wartości niematerialnych i prawnych oraz zestawienie uzupełniające."),
            ("heading", "2. Rok podatkowy"),
            ("p", f"Rokiem podatkowym w rozumieniu art. 8 {CIT} jest rok kalendarzowy."),
            ("heading", "3. Zasady dokonywania zapisów"),
            ("list", [
                "Zapisy w zestawieniu przychodów i kosztów mają kolejne numery (Lp.) w danym roku; tym samym numerem oznacza się dowód księgowy.",
                "Przychody z opłat przyjmowanych w systemie (udostępnianie strzelnicy, kody wejścia, składki, wydarzenia) ujmuje się raz "
                "dziennie jednym zapisem na rodzaj przychodu, na podstawie numerowanego dziennego zestawienia wpływów z wykazem "
                "wszystkich operacji (§ 8 pkt 1 i § 13 rozporządzenia). Zwroty wpłat zmniejszają przychody dnia, w którym ich dokonano.",
                "Przychody spoza systemu (darowizny, dotacje, odsetki, składki wpłacone poza systemem) ujmuje się na podstawie wyciągów "
                "bankowych i dowodów wpłat; składki i darowizny mogą być ujmowane dowodem zbiorczym (§ 7 ust. 4).",
                "Koszty ujmuje się na podstawie faktur (w tym faktur z Krajowego Systemu e-Faktur), rachunków, wyciągów bankowych "
                "i dowodów wewnętrznych (§ 9), najpóźniej do 20. dnia miesiąca za miesiąc poprzedni (§ 12 ust. 2). Koszty uzyskania "
                "przychodów ujmuje się w kolumnie 6, a pozostałe koszty (art. 16 ust. 1 ustawy o CIT oraz wydatki pokryte z dochodów "
                "wolnych od podatku) — w kolumnie 7.",
                "Faktury z Krajowego Systemu e-Faktur są importowane do systemu i ujmowane w ewidencji po decyzji osoby uprawnionej; "
                "faktura niedotycząca ewidencji jest oznaczana jako odrzucona z podaniem przyczyny.",
                "Błędy poprawia się przez wpisanie nowej treści zapisu (§ 7 ust. 2 pkt 1) albo usunięcie zapisu z podaniem przyczyny; "
                "poprzednie treści zapisu pozostają w niezmiennej historii zapisu z oznaczeniem osoby i czasu zmiany.",
                "Po zakończeniu okresu sprawozdawczego (miesiąc) dane z kolumn 5–7 są sumowane („Razem w okresie sprawozdawczym”) "
                "oraz podawana jest suma od początku roku.",
                "Po złożeniu zeznania rocznego rok jest zamykany w systemie — zapisy z zamkniętego roku nie mogą być zmieniane.",
            ]),
            ("heading", "4. System przetwarzania danych (§ 6 ust. 2 pkt 2)"),
            ("p", f"Ewidencja jest prowadzona w postaci elektronicznej w programie: {d['wersja']}, eksploatowanym od dnia "
                  f"{pl_date(d['start_programu'])}. Program działa jako aplikacja internetowa stowarzyszenia z bazą danych PostgreSQL."),
            ("p", "Wykaz funkcji programu (moduł Księgowość):"),
            ("list", ["zestawienie przychodów i kosztów — przegląd, dodawanie, zmiana i usuwanie zapisów, historia zmian każdego zapisu;",
                      "automatyczne ujmowanie przychodów z dziennych zestawień wpływów (raz na godzinę oraz na żądanie);",
                      "import faktur z Krajowego Systemu e-Faktur (pliki XML), przyjmowanie do ewidencji i odrzucanie;",
                      "przechowywanie skanów dowodów księgowych z sumą kontrolną SHA-256;",
                      "wykaz środków trwałych oraz wartości niematerialnych i prawnych z planem odpisów amortyzacyjnych (metoda "
                      "liniowa, art. 16h i 16i ustawy o CIT) i ujęciem odpisów w zestawieniu przychodów i kosztów;",
                      "wydruk zestawienia za miesiąc i rok w porządku chronologicznym zapisów (PDF, XLSX) — § 6 ust. 3 pkt 1;",
                      "zestawienie przepływów finansowych, arkusz danych do zeznania CIT-8, generator zawiadomień, uchwał i dowodów wewnętrznych;",
                      "zamknięcie roku podatkowego."]),
            ("heading", "5. Ochrona danych i dostęp (§ 6 ust. 2 pkt 2–3 i ust. 3)"),
            ("list", [
                "Dostęp do ewidencji mają wyłącznie osoby z imiennym kontem, zalogowane hasłem i kodem jednorazowym (logowanie "
                "dwuskładnikowe), którym nadano uprawnienie „Księgowość”. Osoby uprawnione do dokonywania zapisów: "
                + (", ".join(people) if people else "członkowie zarządu wskazani w uprawnieniach systemu") + ".",
                "Każdy zapis, zmiana i usunięcie zapisu są przypisane do osoby (albo procesu automatycznego) i czasu; historia wersji "
                "zapisów jest chroniona w bazie danych przed zmianą i usunięciem. Działania są dodatkowo rejestrowane w dzienniku zdarzeń systemu.",
                "Połączenia z systemem są szyfrowane (HTTPS). Skany dowodów są przechowywane w magazynie plików niedostępnym "
                "pod publicznym adresem.",
                "Baza danych i pliki są objęte codzienną kopią zapasową, której wykonanie system sprawdza i zgłasza zarządowi.",
                "Dowody w postaci papierowej przechowuje się w: " + d["przechowywanie"] + ". Faktury wystawione i otrzymane przy "
                "użyciu Krajowego Systemu e-Faktur są przechowywane w tym systemie (§ 7 ust. 11).",
                "Ewidencję i dowody przechowuje się do upływu okresu przedawnienia zobowiązania podatkowego (art. 86 § 1 Ordynacji podatkowej).",
            ]),
            ("heading", "6. Zestawienie uzupełniające (§ 4)"),
            ("p", "Zestawienie uzupełniające zawiera dane służące wypełnieniu wymagań art. 10 ust. 1 pkt 1 i 2 oraz art. 16 ust. 5 "
                  "ustawy o działalności pożytku publicznego i o wolontariacie oraz łączną wartość darowizn rzeczowych niezbędną "
                  "do ustalenia limitu przychodów z art. 10a ust. 1 pkt 5 tej ustawy. Zestawienie nie zawiera danych osobowych."),
            ("p", "W tym celu każdy zapis zestawienia przychodów i kosztów ma przypisaną formę działalności (nieodpłatna "
                  "działalność pożytku publicznego, odpłatna działalność pożytku publicznego, pozostałe przychody i koszty, "
                  "koszty wspólne), a zapisy dotyczące umów o realizację zadania publicznego — także umowę, z której pochodzą "
                  "środki. Darowizny rzeczowe są ewidencjonowane z datą, przedmiotem, wartością i dowodem."),
        ]


class Flows(Doc):
    key = "przeplywy"
    title = "Zestawienie przepływów finansowych (załącznik nr 2)"
    group = "Zestawienia roczne"
    legal = "§ 2 pkt 2 rozporządzenia, wzór z załącznika nr 2"
    hint = "Kol. 1–3 wylicza system z ewidencji. Kol. 4–6 uzupełnia zarząd — podpowiedzi: kol. 4 = dochód (kol. 3), kol. 6 = suma " \
           "kosztów z kol. 7 ewidencji (wydatki pokryte z dochodu wolnego); kol. 5 domyślnie przenosi kol. 7 z roku poprzedniego. " \
           "Zestawienie sporządza się po zakończeniu roku."
    landscape = True

    def fields(self, ctx):
        return {
            "rok": forms.IntegerField(label="Rok podatkowy", min_value=2026, max_value=2100),
            "kol4": _amount_field("Kol. 4 — dochód (przychód) wolny od podatku w roku, przeznaczony na cele z art. 17 ust. 1 CIT [zł]"),
            "kol5": _amount_field("Kol. 5 — dochód wolny z lat ubiegłych przeznaczony na te cele [zł] (puste = kol. 7 z roku poprzedniego)"),
            "kol6": _amount_field("Kol. 6 — wydatki w roku na cele z art. 17 ust. 1 CIT pokryte z dochodu wolnego [zł]"),
        }

    def initial(self, ctx):
        year = timezone.localdate().year - (1 if timezone.localdate().month <= 3 else 0)
        f = flows_data(year)
        # podpowiedzi: kol. 4 = dochód (kol. 3), kol. 6 = koszty z kol. 7 ewidencji (wydatki pokryte z dochodu wolnego
        # od podatku — objaśnienie 7 do załącznika nr 1); zapisane wcześniej wartości mają pierwszeństwo
        return {"rok": year, "kol4": f["col4"] if f["col4"] is not None else (f["col3"] if f["col3"] > 0 else 0),
                "kol5": AccountingYear.get(year).free_prior, "kol6": f["col6"] if f["col6"] is not None else f["cost_other"]}

    def doc_title(self, d):
        return f"Zestawienie przepływów finansowych za {d['rok']} r."

    def after_create(self, d, ctx):
        obj = AccountingYear.get(d["rok"])
        obj.free_income, obj.free_prior, obj.free_spent = d.get("kol4"), d.get("kol5"), d.get("kol6")
        obj.save(update_fields=["free_income", "free_prior", "free_spent"])

    def blocks(self, d, ctx):
        f = flows_data(d["rok"], d.get("kol4"), d.get("kol5"), d.get("kol6"))
        a = ctx["association"]
        header = ["Przychody", "Koszty uzyskania przychodów", "Dochód w rozumieniu art. 7 ustawy o CIT",
                  "Dochód (przychód) wolny od podatku w roku podatkowym przeznaczony na cele określone w art. 17 ust. 1 ustawy o CIT",
                  "Dochód wolny od podatku z lat ubiegłych przeznaczony na cele określone w art. 17 ust. 1 ustawy o CIT",
                  "Wydatki na cele określone w art. 17 ust. 1 ustawy o CIT pokryte z dochodu wolnego od podatku",
                  "Dochód wolny od podatku niewydatkowany na cele, o których mowa w art. 17 ust. 1 ustawy o CIT – na koniec roku"]
        return [
            ("title", "ZESTAWIENIE PRZEPŁYWÓW FINANSOWYCH"),
            ("subtitle", f"za rok podatkowy {d['rok']}"),
            ("left", f"Nazwa podatnika: {a['name']}\nAdres: {a['address']}" + (f"\nNIP: {a['nip']}" if a["nip"] else "")),
            ("table", {"header": header, "numbers": True, "rows": [[pln(f[f"col{i}"]).replace(" zł", "") for i in range(1, 8)]]}),
            ("note", "Kol. 1 — przychody z kol. 5 zestawienia przychodów i kosztów; kol. 2 — koszty z kol. 6; kol. 3 = kol. 1 − kol. 2; "
                     "kol. 5 — wartość kol. 7 z zestawienia za rok ubiegły; kol. 7 = kol. 4 + kol. 5 − kol. 6. Wzór: załącznik nr 2 "
                     "do rozporządzenia (Dz. U. 2025 poz. 1880)."),
        ] + _signatures(ctx)


class IncomeResolution(Doc):
    key = "uchwala_dochod"
    title = "Uchwała w sprawie przeznaczenia dochodu na cele statutowe"
    group = "Uchwały"
    legal = "art. 17 ust. 1 pkt 4 ustawy o CIT"
    hint = ("Dochód stowarzyszenia jest wolny od podatku w części przeznaczonej na cele statutowe (art. 17 ust. 1 pkt 4 "
            "ustawy o CIT). Uchwała dokumentuje to przeznaczenie — podejmij ją po zakończeniu roku, przed złożeniem CIT-8 "
            "(do 31 marca). Kwoty pochodzą z ewidencji w chwili tworzenia dokumentu.")

    DEFAULT_DIRECTIONS = ("utrzymanie, wyposażenie i rozwój strzelnicy\n"
                          "organizacja treningów, zawodów i szkoleń strzeleckich\n"
                          "pokrycie bieżących kosztów działalności statutowej")

    def fields(self, ctx):
        return {
            "numer": forms.CharField(label="Numer uchwały", max_length=40, required=False),
            "data": _date_field("Data podjęcia"),
            "miejscowosc": forms.CharField(label="Miejscowość", max_length=80, required=False),
            "organ": forms.CharField(label="Organ podejmujący uchwałę", max_length=120),
            "rok": forms.IntegerField(label="Rok podatkowy, którego dotyczy dochód", min_value=2026, max_value=2100),
            "cele": forms.CharField(label="Cel statutowy (art. 17 ust. 1 pkt 4 CIT)", max_length=255),
            "kierunki": forms.CharField(label="Na co zostanie przeznaczony dochód (każdy kierunek w osobnym wierszu)",
                                        widget=forms.Textarea(attrs={"rows": 4})),
            "kwota": _amount_field("Kwota przeznaczona na cele statutowe [zł] (puste = cały dochód)"),
            "podpisy": forms.CharField(label="Podpisy (każdy w osobnym wierszu)", widget=forms.Textarea(attrs={"rows": 3}), required=False),
        }

    def initial(self, ctx):
        today = timezone.localdate()
        return {"data": today, "miejscowosc": ctx["place"], "organ": ctx["settings"]["approving_body"] or "Zebranie Członków",
                "rok": today.year - (1 if today.month <= 3 else 0), "cele": "działalność w zakresie kultury fizycznej i sportu",
                "kierunki": self.DEFAULT_DIRECTIONS, "podpisy": "Przewodniczący zebrania\nProtokolant"}

    def prepare(self, cleaned, ctx):
        f = flows_data(cleaned["rok"])
        cleaned["_ewidencja"] = {"przychody": f["col1"], "koszty": f["col2"], "dochod": f["col3"], "kol7": f["cost_other"]}
        return cleaned

    def doc_title(self, d):
        return f"Uchwała {d.get('numer') or ''} w sprawie przeznaczenia dochodu za {d['rok']} r.".replace("  ", " ")

    def blocks(self, d, ctx):
        name = ctx["association"]["name"]
        e = d.get("_ewidencja") or {}
        if not e:
            f = flows_data(d["rok"])
            e = {"przychody": f["col1"], "koszty": f["col2"], "dochod": f["col3"], "kol7": f["cost_other"]}
        income = e["dochod"]
        out = [
            ("right", f"{d.get('miejscowosc') or ''}, {pl_date(d['data'])}".lstrip(", ")),
            ("title", f"UCHWAŁA{(' NR ' + d['numer']) if d.get('numer') else ''}"),
            ("subtitle", f"{d['organ']} {name}\nz dnia {pl_date(d['data'])}\nw sprawie przeznaczenia dochodu za rok {d['rok']}"),
            ("p", f"Na podstawie art. 17 ust. 1 pkt 4 {CIT} oraz regulaminu stowarzyszenia postanawia się, co następuje:"),
            ("section", "§ 1"),
            ("p", f"Stwierdza się, że według uproszczonej ewidencji przychodów i kosztów prowadzonej na podstawie {ROZP} "
                  f"{name} osiągnęło w roku podatkowym {d['rok']}:"),
            ("list", [f"przychody w wysokości {pln(e['przychody'])},",
                      f"koszty uzyskania przychodów w wysokości {pln(e['koszty'])},",
                      (f"dochód w wysokości {pln(income)}." if income > 0 else
                       f"{'stratę' if income < 0 else 'wynik zerowy'} w wysokości {pln(abs(income))}.")]),
        ]
        if income > 0:
            amount = d.get("kwota") if d.get("kwota") is not None else income
            amount = min(amount, income)
            directions = [x.rstrip(",.;") + "," for x in _lines(d.get("kierunki"))]
            if directions:
                directions[-1] = directions[-1].rstrip(",") + "."
            out += [
                ("section", "§ 2"),
                ("p", f"Dochód za rok {d['rok']} w wysokości {pln(amount)} przeznacza się na cele statutowe stowarzyszenia: "
                      f"{d['cele']}" + (", w szczególności na:" if directions else ".")),
            ]
            if directions:
                out.append(("list", directions))
            if amount < income:
                out.append(("p", f"Pozostała część dochodu w wysokości {pln(income - amount)} nie jest objęta niniejszą uchwałą "
                                 "i podlega opodatkowaniu na zasadach ogólnych."))
            out += [
                ("section", "§ 3"),
                ("p", "Zobowiązuje się Zarząd do wydatkowania dochodu zgodnie z § 2 oraz do wykazania dochodu wolnego od podatku "
                      "w zeznaniu CIT-8 z załącznikiem CIT-8/O i w zestawieniu przepływów finansowych za ten rok."),
                ("section", "§ 4"),
            ]
        else:
            out += [
                ("section", "§ 2"),
                ("p", f"Wobec braku dochodu za rok {d['rok']} nie dokonuje się jego podziału. Zobowiązuje się Zarząd do wykazania "
                      "wyniku w zeznaniu CIT-8 za ten rok."),
                ("section", "§ 3"),
            ]
        out.append(("p", "Uchwała wchodzi w życie z dniem podjęcia."))
        return out + _signatures(ctx, _lines(d.get("podpisy")))


class CitSheet(Doc):
    key = "cit8"
    title = "Arkusz danych do zeznania CIT-8 i załącznika CIT-8/O"
    group = "Zestawienia roczne"
    legal = "art. 7, art. 17 ust. 1 pkt 4 i art. 27 ust. 1 ustawy o CIT"
    hint = "Arkusz zbiera kwoty z ewidencji do przeniesienia na formularz CIT-8 (z załącznikiem CIT-8/O). To nie jest " \
           "deklaracja — numery pól przenieś według wersji formularza obowiązującej za dany rok. Zeznanie składa się " \
           "elektronicznie do końca trzeciego miesiąca roku następnego (art. 27 ust. 1 i 1c)."

    def fields(self, ctx):
        return {
            "rok": forms.IntegerField(label="Rok podatkowy", min_value=2026, max_value=2100),
            "cele": forms.CharField(label="Cel statutowy uzasadniający zwolnienie (art. 17 ust. 1 pkt 4 CIT)", max_length=255,
                                    initial="działalność w zakresie kultury fizycznej i sportu"),
            "wolny": _amount_field("Dochód wolny od podatku — przeznaczony na cele statutowe [zł] (puste = cały dochód)"),
        }

    def initial(self, ctx):
        year = timezone.localdate().year - (1 if timezone.localdate().month <= 3 else 0)
        return {"rok": year, "cele": "działalność w zakresie kultury fizycznej i sportu"}

    def doc_title(self, d):
        return f"Arkusz danych do CIT-8 za {d['rok']} r."

    def blocks(self, d, ctx):
        f = flows_data(d["rok"])
        income = f["col3"]
        free = d.get("wolny") if d.get("wolny") is not None else max(income, 0)
        free = min(free, max(income, 0))
        taxable = max(income - free, 0)
        a = ctx["association"]
        rows = [
            ["Przychody (suma kol. 5 zestawienia przychodów i kosztów)", pln(f["col1"])],
            ["Koszty uzyskania przychodów (suma kol. 6)", pln(f["col2"])],
            ["Dochód (przychody − koszty uzyskania przychodów, art. 7 ust. 2)" if income >= 0 else "Strata (art. 7 ust. 2)", pln(income)],
            ["Dochód wolny od podatku — art. 17 ust. 1 pkt 4 (w części przeznaczonej na cele statutowe)", pln(free)],
            ["Dochód do opodatkowania (przed odliczeniami)", pln(taxable)],
            ["Dla informacji: koszty niestanowiące kosztów uzyskania przychodów (suma kol. 7)", pln(f["cost_other"])],
        ]
        deadline = date(d["rok"] + 1, 3, 31)
        return [
            ("title", "ARKUSZ DANYCH DO ZEZNANIA CIT-8"),
            ("subtitle", f"za rok podatkowy {d['rok']} (od 1 stycznia do 31 grudnia {d['rok']} r.)"),
            ("left", f"Podatnik: {a['name']}\nAdres: {a['address']}" + (f"\nNIP: {a['nip']}" if a["nip"] else "")),
            ("table", {"header": ["Pozycja", "Kwota"], "rows": rows, "widths": [0.75, 0.25]}),
            ("heading", "Załącznik CIT-8/O"),
            ("p", f"Dochód wolny od podatku na podstawie art. 17 ust. 1 pkt 4 {CIT}: {pln(free)} — cel statutowy: {d['cele']}. "
                  "Zwolnienie przysługuje w części dochodu przeznaczonej na cele statutowe; kwoty przeznaczone i wydatkowane "
                  "wynikają z zestawienia przepływów finansowych za ten rok."),
            ("heading", "Termin i sposób złożenia"),
            ("p", f"Zeznanie CIT-8 składa się do naczelnika urzędu skarbowego w terminie do {pl_date(deadline)} (koniec trzeciego "
                  "miesiąca roku następnego — art. 27 ust. 1 ustawy o CIT), za pomocą środków komunikacji elektronicznej "
                  "(art. 27 ust. 1c). W tym terminie wpłaca się podatek należny, jeżeli wystąpi."),
            ("note", "Arkusz pomocniczy z systemu ewidencji — nie zastępuje formularza. Przed złożeniem sprawdź kwoty i aktualną "
                     "wersję formularza CIT-8 oraz CIT-8/O."),
        ] + _signatures(ctx)


class InternalProof(Doc):
    key = "dowod_wewnetrzny"
    title = "Dowód wewnętrzny (§ 9)"
    group = "Dowody księgowe"
    legal = "§ 7 ust. 3 pkt 2 i § 9 rozporządzenia (Dz. U. 2025 poz. 1880)"
    hint = "Wyłącznie dla: zakupów materiałów w handlu detalicznym, kosztów podróży służbowych, opłat sądowych i notarialnych, " \
           "parkowania i przejazdów transportem publicznym. Po wygenerowaniu można od razu wpisać koszt do ewidencji."
    prefix = "DW"
    KINDS = [("detal", "zakup materiałów w jednostce handlu detalicznego (§ 9 ust. 2 pkt 1)"),
             ("podroz", "koszty podróży służbowej (§ 9 ust. 2 pkt 2)"),
             ("oplaty", "opłaty sądowe i notarialne (§ 9 ust. 2 pkt 3)"),
             ("parking", "parkowanie / przejazd transportem publicznym (§ 9 ust. 2 pkt 4)")]

    def fields(self, ctx):
        return {
            "rodzaj": forms.ChoiceField(label="Rodzaj wydatku", choices=self.KINDS),
            "data": _date_field("Data wystawienia"),
            "data_zdarzenia": _date_field("Data zdarzenia (wydatku)"),
            "przedmiot": forms.CharField(label="Przedmiot zdarzenia / nazwa towaru", max_length=200),
            "ilosc": forms.CharField(label="Ilość i jednostka (przy zakupie, np. „10 szt.”)", max_length=40, required=False),
            "cena": _amount_field("Cena jednostkowa [zł] (przy zakupie)"),
            "wartosc": _amount_field("Wartość wydatku [zł]", required=True),
            "podstawa": forms.CharField(label="Podstawa (np. nr paragonu, bilet, potwierdzenie z aplikacji)", max_length=200, required=False),
            "podroz": forms.CharField(label="Podróż: imię i nazwisko, cel i miejscowość, wyjazd–powrót (data, godzina), stawka i wartość diet",
                                      widget=forms.Textarea(attrs={"rows": 3}), required=False),
            "sporzadzil": forms.CharField(label="Sporządził(a)", max_length=120),
            "zatwierdzil": forms.CharField(label="Zatwierdził(a)", max_length=120, required=False),
        }

    def initial(self, ctx):
        today = timezone.localdate()
        return {"rodzaj": "detal", "data": today, "data_zdarzenia": today, "sporzadzil": ctx["user_name"]}

    def doc_title(self, d):
        return f"Dowód wewnętrzny — {d['przedmiot']}"[:255]

    def blocks(self, d, ctx):
        a = ctx["association"]
        kind = dict(self.KINDS)[d["rodzaj"]]
        rows = [["Rodzaj wydatku", kind], ["Data zdarzenia", pl_date(d["data_zdarzenia"])], ["Przedmiot zdarzenia", d["przedmiot"]]]
        if d.get("ilosc"):
            rows.append(["Ilość", d["ilosc"]])
        if d.get("cena") is not None:
            rows.append(["Cena jednostkowa", pln(d["cena"])])
        rows.append(["Wartość", pln(d["wartosc"])])
        if d.get("podstawa"):
            rows.append(["Podstawa (załączony dokument)", d["podstawa"]])
        if d.get("podroz"):
            rows.append(["Podróż służbowa", d["podroz"]])
        return [
            ("right", pl_date(d["data"])),
            ("left", f"{a['name']}\n{a['address']}" + (f"\nNIP: {a['nip']}" if a["nip"] else "")),
            ("title", f"DOWÓD WEWNĘTRZNY NR {d['_number']}"),
            ("table", {"header": ["", ""], "rows": rows, "widths": [0.3, 0.7], "no_header": True}),
            ("note", "Lp. zapisu w zestawieniu przychodów i kosztów: ............ (tym numerem oznacza się dowód)."),
            ("sign", [f"Sporządził(a): {d['sporzadzil']}", f"Zatwierdził(a): {d.get('zatwierdzil') or ''}"]),
        ]


DOCS = {cls.key: cls() for cls in (ElectionResolution, ElectionNotice, ResignationNotice, Principles, Flows, IncomeResolution, CitSheet,
                                    InternalProof)}


def flows_data(year, col4=None, col5=None, col6=None):
    from . import services

    f = services.flows(year)
    if col4 is not None or col5 is not None or col6 is not None:
        f["col4"] = col4 if col4 is not None else f["col4"]
        f["col5"] = col5 if col5 is not None else f["col5"]
        f["col6"] = col6 if col6 is not None else f["col6"]
        f["col7"] = (f["col4"] or 0) + (f["col5"] or 0) - (f["col6"] or 0)
    return f


# --- kontekst, formularz, zapis ---------------------------------------------------------


def context(user=None):
    from apps.core.models import AssociationSettings

    from .models import Record

    a = AssociationSettings.load()
    s = AccountingSettings.load()
    revenue_by_year = {}
    for year in Record.objects.values_list("year", flat=True).distinct():
        from . import services

        from .supplementary import revenue_for_limit

        revenue_by_year[year] = revenue_for_limit(year)  # bez darowizn rzeczowych na działalność statutową
    return {
        "association": {"name": a.name, "address": a.address, "nip": a.nip, "regon": a.regon},
        "settings": {"tax_office": s.tax_office, "approving_body": s.approving_body, "signatories": s.signatories},
        "place": (a.range_place or "").split(",")[0].strip(), "auto_from": s.auto_from,
        "user_name": user.full_name if user is not None and getattr(user, "full_name", "") else "",
        "revenue_by_year": revenue_by_year,
    }


def form_class(doc, ctx):
    return type(f"{doc.key}Form", (forms.Form,), dict(doc.fields(ctx)))


def _jsonable(data):
    out = {}
    for k, v in data.items():
        out[k] = v.isoformat() if isinstance(v, date) else v
    return out


def _restore(data):
    out = dict(data)
    for k, v in data.items():
        if isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-":
            try:
                out[k] = date.fromisoformat(v)
            except ValueError:
                pass
    return out


def create(doc, cleaned, *, by, ctx):
    year = cleaned["data"].year if isinstance(cleaned.get("data"), date) else timezone.localdate().year
    cleaned = doc.prepare(dict(cleaned), ctx)
    data = _jsonable(cleaned)
    data["_context"] = {"association": ctx["association"], "settings": ctx["settings"]}
    for _ in range(3):
        try:
            with transaction.atomic():
                last = GeneratedDocument.objects.filter(kind=doc.key, year=year).aggregate(m=Max("number"))["m"] or 0
                number = last + 1
                if doc.prefix:
                    data["_number"] = f"{doc.prefix}/{number}/{year}"
                title = doc.doc_title(_restore(data))
                obj = GeneratedDocument.objects.create(kind=doc.key, year=year, number=number, title=title[:255], data=data, created_by=by)
                doc.after_create(cleaned, ctx)
            break
        except IntegrityError:
            continue
    else:
        raise RuntimeError("Nie udało się nadać numeru dokumentu.")
    from apps.audit.services import log_event

    log_event("ksiegowosc.dokument", None, f"Wygenerowano: {obj.title}", actor=by)
    return obj


def blocks_for(obj):
    doc = DOCS[obj.kind]
    data = _restore(obj.data)
    ctx = dict(context())
    ctx.update(obj.data.get("_context", {}))
    return doc, doc.blocks(data, ctx)


# --- wydruk PDF i DOCX ------------------------------------------------------------------


def to_pdf(obj):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    fonts = Path(__file__).resolve().parent.parent / "reports" / "fonts"
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", str(fonts / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(fonts / "DejaVuSans-Bold.ttf")))
    doc, blocks = blocks_for(obj)
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=10, leading=14, alignment=TA_JUSTIFY, spaceAfter=6)
    small = ParagraphStyle("s", parent=base, fontSize=8, leading=10, alignment=0)
    cell = ParagraphStyle("c", parent=base, fontSize=8, leading=10, alignment=0, spaceAfter=0)
    cellb = ParagraphStyle("cb", parent=cell, fontName="DejaVu-Bold")
    styles = {
        "title": ParagraphStyle("t", parent=base, fontName="DejaVu-Bold", fontSize=13, leading=17, alignment=TA_CENTER, spaceBefore=10),
        "subtitle": ParagraphStyle("st", parent=base, fontName="DejaVu-Bold", fontSize=10.5, alignment=TA_CENTER, spaceAfter=10),
        "section": ParagraphStyle("se", parent=base, fontName="DejaVu-Bold", alignment=TA_CENTER, spaceBefore=6, spaceAfter=2),
        "heading": ParagraphStyle("h", parent=base, fontName="DejaVu-Bold", spaceBefore=8),
        "right": ParagraphStyle("r", parent=base, alignment=TA_RIGHT),
        "left": ParagraphStyle("l", parent=base, alignment=0),
        "addressee": ParagraphStyle("a", parent=base, fontName="DejaVu-Bold", leftIndent=95 * mm, alignment=0, spaceBefore=8, spaceAfter=12),
    }
    pagesize = landscape(A4) if doc.landscape else A4
    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=pagesize, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
                            title=obj.title, author=obj.data.get("_context", {}).get("association", {}).get("name", ""))

    def para(text, style):
        return Paragraph(escape(text).replace("\n", "<br/>"), style)

    story = []
    for kind, value in blocks:
        if kind in styles:
            story.append(para(value, styles[kind]))
        elif kind == "p":
            story.append(para(value, base))
        elif kind == "note":
            story.append(para(value, small))
        elif kind == "list":
            story.append(ListFlowable([ListItem(para(x, base), leftIndent=12) for x in value], bulletType="1", bulletFontName="DejaVu",
                                      bulletFontSize=10, leftIndent=14))
        elif kind == "table":
            rows = [] if value.get("no_header") else [[para(h, cellb) for h in value["header"]]]
            if value.get("numbers"):
                rows.append([para(str(i), cellb) for i in range(1, len(value["header"]) + 1)])
            rows += [[para(str(c), cell) for c in r] for r in value["rows"]]
            n = len(value["header"])
            widths = [pdf.width * w for w in value.get("widths", [1 / n] * n)]
            t = Table(rows, colWidths=widths, repeatRows=0 if value.get("no_header") else 1)
            style = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#777777")), ("VALIGN", (0, 0), (-1, -1), "TOP")]
            if not value.get("no_header"):
                style.append(("BACKGROUND", (0, 0), (-1, 0 + (1 if value.get("numbers") else 0)), colors.HexColor("#eeeeee")))
            t.setStyle(TableStyle(style))
            story += [Spacer(1, 3 * mm), t, Spacer(1, 3 * mm)]
        elif kind == "sign":
            cells = [[para("....................................................\n" + name, ParagraphStyle("sg", parent=small, alignment=TA_CENTER))
                      for name in value[i:i + 2]] for i in range(0, len(value), 2)]
            for row in cells:
                while len(row) < 2:
                    row.append("")
            story.append(Spacer(1, 14 * mm))
            story.append(KeepTogether(Table(cells, colWidths=[pdf.width / 2] * 2, rowHeights=None,
                                            style=[("TOPPADDING", (0, 0), (-1, -1), 14)])))
        elif kind == "pagebreak":
            story.append(PageBreak())
    pdf.build(story)
    return buf.getvalue()


def to_docx(obj):
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.shared import Cm, Pt

    doc, blocks = blocks_for(obj)
    out = Document()
    normal = out.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    section = out.sections[0]
    if doc.landscape:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = section.page_height, section.page_width
    for margin in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, margin, Cm(2))

    def paragraph(text, align=None, bold=False, size=None, indent=None):
        p = out.add_paragraph()
        for i, line in enumerate(text.split("\n")):
            run = p.add_run(line)
            run.bold = bold
            if size:
                run.font.size = Pt(size)
            if i < len(text.split("\n")) - 1:
                run.add_break()
        if align is not None:
            p.alignment = align
        if indent:
            p.paragraph_format.left_indent = Cm(indent)
        return p

    for kind, value in blocks:
        if kind == "title":
            paragraph(value, WD_ALIGN_PARAGRAPH.CENTER, bold=True, size=14)
        elif kind == "subtitle":
            paragraph(value, WD_ALIGN_PARAGRAPH.CENTER, bold=True)
        elif kind == "section":
            paragraph(value, WD_ALIGN_PARAGRAPH.CENTER, bold=True)
        elif kind == "heading":
            paragraph(value, bold=True)
        elif kind == "right":
            paragraph(value, WD_ALIGN_PARAGRAPH.RIGHT)
        elif kind == "left":
            paragraph(value)
        elif kind == "addressee":
            paragraph(value, bold=True, indent=9)
        elif kind == "p":
            paragraph(value, WD_ALIGN_PARAGRAPH.JUSTIFY)
        elif kind == "note":
            paragraph(value, size=8)
        elif kind == "list":
            for i, item in enumerate(value, start=1):
                paragraph(f"{i}) {item}", WD_ALIGN_PARAGRAPH.JUSTIFY, indent=0.6)
        elif kind == "table":
            rows = [] if value.get("no_header") else [value["header"]]
            if value.get("numbers"):
                rows.append([str(i) for i in range(1, len(value["header"]) + 1)])
            rows += value["rows"]
            table = out.add_table(rows=len(rows), cols=len(value["header"]))
            table.style = "Table Grid"
            for r, row in enumerate(rows):
                for c, text in enumerate(row):
                    table.cell(r, c).text = str(text)
                    for run in table.cell(r, c).paragraphs[0].runs:
                        run.font.size = Pt(9)
                        run.bold = r == 0 and not value.get("no_header")
            out.add_paragraph()
        elif kind == "sign":
            out.add_paragraph()
            for name in value:
                paragraph("\n....................................................\n" + name, WD_ALIGN_PARAGRAPH.RIGHT, size=9)
        elif kind == "pagebreak":
            out.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()


def filename(obj, ext):
    from django.utils.text import slugify

    return f"{slugify(obj.title)[:80] or 'dokument'}.{ext}"
