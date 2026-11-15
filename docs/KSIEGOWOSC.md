# Księgowość — uproszczona ewidencja przychodów i kosztów (UEPiK)

Podstawa: art. 10a ustawy o działalności pożytku publicznego i o wolontariacie (Dz. U. 2025 poz. 1338, brzmienie od 1.01.2026)
i rozporządzenie MFiG z 22.12.2025 (Dz. U. poz. 1880). Panel → Płatności → **Księgowość** (uprawnienie „Księgowość”).

## Zestawienie przychodów i kosztów (zał. 1)
- Kolumny 1–8 wzoru, sumy „Razem w okresie sprawozdawczym” i „Razem od początku roku”, wydruk PDF/XLSX (miesiąc, rok).
- **Zmiana treści zapisu** (§ 7 ust. 2 pkt 1 — w ewidencji elektronicznej „wpisanie nowej treści zapisu”): wymaga powodu;
  numer Lp. zostaje, poprzednia treść, osoba i czas trafiają do historii zapisu (chronionej w bazie przed zmianą).
- **Usunięcie zapisu**: z przyczyną; zapis zachowuje Lp., na wydruku jest oznaczony „ZAPIS USUNIĘTY”, nie wchodzi do sum.
  Można go przywrócić.
- **Zamknięcie roku** (zakładka „Lata i zamknięcia”): blokuje zmiany po złożeniu CIT-8; otwarcie wymaga powodu.
- Przychody z systemu: dzienne raporty wpływów → jeden zapis na kategorię dnia (od dnia ustawionego w ustawieniach).

## Zestawienie uzupełniające (§ 4)
- Forma działalności każdego zapisu (art. 10 ust. 1 udpp): nieodpłatna / odpłatna pożytku publicznego, pozostałe
  (np. dochody z majątku), koszty wspólne — przychody, koszty i wynik każdej formy. Przypisanie w formularzu zapisu
  albo zbiorczo (nowa wersja zapisu z powodem). Przychody z systemu dostają formę z ustawień.
- Umowy o realizację zadania publicznego (art. 16 ust. 5): dotacja, przychody i koszty przypisane do umowy, pozostała kwota.
- Darowizny rzeczowe (bez danych osobowych) i limit przychodów 1 000 000 zł bez darowizn rzeczowych na cele statutowe
  (art. 10a ust. 1 pkt 5); kwota trafia też do podpowiedzi w uchwale o wyborze ewidencji.
- Wydruk PDF/XLSX za rok.

## Wykaz środków trwałych i WNiP (§ 3)
- Kolumny wg § 3 ust. 1 pkt 1–11; wydruk PDF/XLSX za rok.
- Metody: liniowa (stawka z Wykazu stawek amortyzacyjnych — zał. nr 1 do ustawy o CIT), bez odpisów do 10 000 zł
  (art. 16d ust. 1), bez amortyzacji (art. 16c, np. grunt).
- Odpisy od miesiąca po wprowadzeniu do wykazu, do zrównania z wartością albo miesiąca likwidacji/zbycia (art. 16h ust. 1 pkt 1).
- Wprowadzenie po miesiącu przyjęcia do używania oznaczane jako ujawnienie (§ 3 ust. 3).
- Składniki sprzed systemu: dotychczasowe odpisy + rok, do którego ich dokonano.
- Likwidacja / zbycie / niedobór = wykreślenie z datą, przyczyną i dowodem (KSeF faktury zbycia).
- „Ujmij odpisy w zestawieniu” — jeden zapis kosztu (kol. 6 albo 7) za miesiąc lub cały rok.

## Faktury KSeF
- Import plików XML (FA(2), FA(3)) lub ZIP pobranych z KSeF; numer KSeF odczytywany z nazwy pliku.
- Faktura: **przyjmij do ewidencji** (koszt kol. 6/7 albo przychód, kwota należności ogółem), **nie dotyczy ewidencji**
  (z przyczyną), **usuń z ewidencji** (zapis usunięty, faktura wraca do decyzji).
- Kierunek (zakup/sprzedaż) — po NIP stowarzyszenia z „Dane stowarzyszenia”.
- Faktury w walucie obcej — wpis ręczny po przeliczeniu (§ 7 ust. 5).
- **Automatyczne pobieranie z KSeF** (API KSeF 2.0) — co godzinę faktury zakupu (stowarzyszenie jako nabywca),
  opcjonalnie sprzedaży; przyrostowo wg daty trwałego zapisu w KSeF; limity KSeF (20 zapytań / 64 pobrania na godzinę)
  — jeden przebieg pobiera do 40 faktur, resztę kolejne. Przycisk „Zapisz i pobierz teraz” w zakładce Faktury KSeF.

### Uruchomienie pobierania z KSeF
1. W Aplikacji Podatnika KSeF (ksef.podatki.gov.pl) zaloguj się w kontekście NIP stowarzyszenia (osoba uprawniona
   do działania w imieniu stowarzyszenia) i wygeneruj **token** z uprawnieniem do **przeglądania faktur**
   (wystarczy samo przeglądanie). Token pokazuje się tylko raz.
2. Na serwerze (w folderze projektu): `sh deploy/ustaw-env.sh KSEF_TOKEN` → wklej token → `docker compose up -d`.
   Token jest tylko w `.env` — nie trafia do bazy, panelu, logów ani repozytorium.
3. Panel → Księgowość → Faktury KSeF → „Pobieranie z KSeF”: zaznacz „pobieranie włączone”, ustaw datę „od”
   (np. 1.02.2026) i kliknij „Zapisz i pobierz teraz”.
4. NIP kontekstu pochodzi z „Dane stowarzyszenia”. Kopia testowa / próby: `KSEF_ENVIRONMENT=test` (api-test.ksef.mf.gov.pl).

## Wyciąg bankowy
- Import „Zestawienia operacji” z bankowości BNP Paribas (eksport XLSX) — wpływy i wydatki do skrzynki „Do decyzji”;
  ponowne wgranie nie tworzy duplikatów; operacje odrzucone są pomijane; z kontrahenta zapisywany jest tylko rachunek i nazwa.
- Wpłaty strzelców rozpoznane po tytule (rezerwacja, doładowanie) rozlicza moduł Płatności — przychód ujmuje raport
  wpływów (bez podwójnego zapisu). Nierozpoznane wpływy nie trafiają do „nieprzypisanych” w Płatnościach.
- Wydatek równy fakturze KSeF (najpierw po numerze faktury w tytule) — „Zapłata faktury KSeF”, koszt ujmuje faktura.
- Pozostałe: „Zaksięguj” (przychód kol. 5 / koszt kol. 6–7, forma działalności, umowa), także zbiorczo dla zaznaczonych;
  „Nie dotyczy ewidencji” z przyczyną. Usunięcie zapisu w ewidencji przywraca operację do decyzji.

## Płatności online — Stripe
- Import eksportów z panelu Stripe (konto Express stowarzyszenia): „Historia salda” i „Wypłaty” (Saldo → Eksportuj,
  zakres „Wszystko”, kolumny „Wszystko”, CSV). Bez danych klientów; ponowne wgranie bez duplikatów.
- Przychód dzienny = kwoty zapłacone przez klientów − zwroty (czas polski), zapis z dowodem „Stripe DD.MM.RRRR”;
  zestawienie wpłat online za miesiąc (PDF) z listą identyfikatorów transakcji Stripe = dowód.
- Prowizje dokumentują faktury platformy w KSeF — z eksportu Stripe są tylko do porównania (opcjonalnie koszt, gdy brak faktury).
- Wypłaty Stripe na BNP łączą się z wyciągiem (kwota, data ±4 dni; bank księguje zwykle dzień przed „Arrival Date”).

## Generator dokumentów (PDF do podpisu, DOCX do edycji)
- Uchwała organu zatwierdzającego o prowadzeniu UEPiK (art. 10a ust. 1).
- Zawiadomienie US o wyborze UEPiK (art. 10a ust. 3 — do końca 3. miesiąca roku podatkowego).
- Zawiadomienie US o rezygnacji / niespełnianiu warunków (art. 10a ust. 5 — 90 dni).
- Uchwała zarządu z opisem zasad prowadzenia ewidencji (§ 6 ust. 2).
- Zestawienie przepływów finansowych (zał. 2) — kol. 1–3 z ewidencji, kol. 4–6 wpisuje zarząd, kol. 5 przenoszona z kol. 7 roku poprzedniego.
- Arkusz danych do CIT-8 / CIT-8/O (art. 7, 17 ust. 1 pkt 4, 27 ust. 1 i 1c ustawy o CIT) — pomocniczy, nie zastępuje formularza.
- Dowód wewnętrzny (§ 9) z numerem DW/n/rok i przyciskiem „Wpisz koszt do ewidencji”.

Dane do dokumentów: „Dane stowarzyszenia” (nazwa, adres, NIP, REGON) oraz Księgowość → Ustawienia (urząd skarbowy,
organ zatwierdzający, osoby podpisujące). Każdy wygenerowany dokument jest zapamiętany i można go pobrać ponownie.

## Do zrobienia (kolejne etapy)
- Pobieranie faktur z API KSeF.
