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
- Pobieranie bezpośrednio z API KSeF — kolejny etap (wymaga tokenu / certyfikatu KSeF).

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
- Zestawienie uzupełniające (§ 4).
- Pobieranie faktur z API KSeF.
