"""Początkowe „Częste pytania” — do edycji w panelu (Strzelnica → Częste pytania)."""

from django.db import migrations

ITEMS = [
    ("Kto może zarezerwować oś?",
     "Oś rezerwuje osoba z uprawnieniami prowadzącego strzelanie (PS), zweryfikowanymi przez obsługę w systemie. "
     "Dodaj numer uprawnień i zdjęcie legitymacji w zakładce „Moje dokumenty” — zdjęcie służy tylko do weryfikacji i jest usuwane po decyzji.\n\n"
     "Nie masz uprawnień PS? Możesz strzelać pod nadzorem prowadzącego, który ma rezerwację — wpiszesz się do książki pobytu kodem QR jego rezerwacji."),
    ("Jak zarezerwować oś?",
     "1. Załóż konto i potwierdź adres e-mail.\n2. Zaakceptuj regulaminy.\n3. Dodaj uprawnienia PS i poczekaj na weryfikację (dostaniesz e-mail).\n"
     "4. Wejdź w „Zajętość osi”, wybierz oś, dzień i godziny, a potem potwierdź rezerwację.\n\n"
     "Na pulpicie widać listę kontrolną „co jeszcze, żeby zarezerwować”."),
    ("Ile kosztuje korzystanie z osi?",
     "Aktualne opłaty i godziny otwarcia są w zakładce „Opłaty”. Opłata za godzinę to mniejsza z kwot: liczba osób × opłata za osobę albo ryczałt za oś. "
     "Kwotę do zapłaty widzisz przed potwierdzeniem rezerwacji.\n\n"
     "Opłaty są jednakowe dla członków Stowarzyszenia i osób spoza Stowarzyszenia."),
    ("Jak mogę zapłacić?",
     "Online (BLIK, szybki przelew, karta — przez Tpay), przelewem tradycyjnym z numerem rezerwacji w tytule albo gotówką na miejscu. "
     "Dostępne sposoby zależą od Twojej grupy i są pokazane przy rezerwacji. Jeśli do płatności online doliczana jest opłata za jej obsługę, zobaczysz ją przed zapłatą.\n\n"
     "Po zaksięgowaniu wpłaty pobierzesz potwierdzenie w „Moich płatnościach”."),
    ("Jak anulować rezerwację i kiedy dostanę zwrot?",
     "Otwórz rezerwację i wybierz „Anuluj rezerwację”. Bez opłat anulujesz samodzielnie do terminu podanego przy rezerwacji — później tylko przez obsługę.\n\n"
     "Wpłata online wraca automatycznie na kartę lub rachunek, z którego płaciłeś (zwykle kilka dni roboczych). "
     "Opłata za obsługę płatności online nie podlega zwrotowi. Wpłaty gotówką i przelewem zwraca obsługa."),
    ("Termin jest zajęty — co mogę zrobić?",
     "Zapisz się na listę oczekujących. Gdy termin się zwolni, pierwsza osoba z listy dostanie wiadomość, a termin będzie przez chwilę zatrzymany tylko dla niej."),
    ("Co zabrać na strzelnicę?",
     "Dokument tożsamości, pozwolenie na broń (jeśli je masz), legitymację prowadzącego strzelanie (prowadzący), ochronniki słuchu i okulary ochronne. "
     "Zasady pobytu i strzelania opisuje Regulamin strzelnicy."),
    ("Czym jest książka pobytu i jak się do niej wpisać?",
     "Każda osoba przebywająca na strzelnicy wpisuje się do książki pobytu — to obowiązek wynikający z przepisów. "
     "Wpis zrobisz telefonem przez kod QR rezerwacji, w kiosku przy wejściu albo u obsługi. Prowadzący po swoim wpisie może udostępnić towarzyszom kod, którym dopiszą się sami."),
    ("Czy mogę przyjść z osobą, która nie ma pozwolenia na broń?",
     "Tak — strzela pod nadzorem prowadzącego, który ma rezerwację, i wpisuje się do książki pobytu. Liczbę osób podajesz przy rezerwacji; "
     "jeśli przyjdzie więcej osób, należność zostanie wyrównana według opłat z chwili rezerwacji."),
    ("Jak włączyć powiadomienia na telefonie?",
     "Wejdź w „Powiadomienia” (dzwonek) i stuknij „Włącz powiadomienia na tym urządzeniu”. "
     "Na Androidzie możesz też zainstalować stronę jako aplikację (Chrome → ⋮ → „Zainstaluj aplikację”) i włączyć powiadomienia w ustawieniach telefonu. "
     "Na iPhonie dodaj stronę do ekranu początkowego (Safari → Udostępnij → „Do ekranu początkowego”) i włącz powiadomienia w aplikacji."),
    ("Jak zostać członkiem Stowarzyszenia?",
     "Wypełnij deklarację członkowską w zakładce „Stowarzyszenie” na swoim pulpicie. O przyjęciu decyduje Zarząd uchwałą — dostaniesz e-mail z decyzją."),
    ("Jak pobrać moje dane albo usunąć konto?",
     "W zakładce „Moje dane” pobierzesz wszystkie swoje dane i możesz poprosić o zamknięcie konta. "
     "Dane, które Stowarzyszenie musi przechowywać na podstawie przepisów (np. książka pobytu, rozliczenia), zostają przez wymagany okres — szczegóły w Informacji RODO."),
]


def seed(apps, schema_editor):
    FaqItem = apps.get_model("core", "FaqItem")
    if FaqItem.objects.exists():
        return
    FaqItem.objects.bulk_create(FaqItem(question=q, answer=a, order=(i + 1) * 10) for i, (q, a) in enumerate(ITEMS))


class Migration(migrations.Migration):
    dependencies = [("core", "0008_faq")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
