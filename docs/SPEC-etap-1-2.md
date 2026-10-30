# Strzelnica Sątok — specyfikacja etapu 1 i 2

**Projekt:** `strzelnica-satok`
**Właściciel:** Stowarzyszenie zwykłe STRZELNICA SĄTOK (Sątok k. Bierutowa)
**Wersja dokumentu:** 0.2, 2026-10-30
**Status:** zaakceptowana przez właściciela; etap 1 i etap 2 zbudowane (bez kodów dostępu dla klubów — pkt 5.5)

---

## 1. Cel systemu

Jeden system dla stowarzyszenia, dostępny w przeglądarce i instalowany na telefonie jako aplikacja (PWA). Obsługuje:

- rezerwacje osi strzeleckich,
- elektroniczną książkę pobytu zgodną z przepisami o broni i amunicji (wpis kodem QR, z konta lub bez konta),
- płatności, w tym saldo strzelca zasilane wpłatami (przelew, gotówka, BLIK, Tpay),
- prowadzenie stowarzyszenia: członkowie, składki, uchwały, księgowość.

System działa **bez stałej obsługi na strzelnicy**. Wszystko, co dziś robiłby dyżurny, musi dać się wykonać samoobsługowo przez strzelca albo zdalnie przez zarząd. Rola „obsługa/dyżurny” jest przewidziana w modelu, ale włączymy ją dopiero wtedy, gdy pojawi się personel.

## 2. Słownik

| Pojęcie | Znaczenie |
|---|---|
| **Obiekt** | Część strzelnicy: strzelnica kulowa, w przyszłości strzelnica śrutowa |
| **Oś** | Rezerwowalna jednostka obiektu. Ma typ, długość i cennik |
| **Rezerwacja** | Blokada całej osi na przedział czasu, z zadeklarowaną liczbą osób i wskazanym prowadzącym strzelanie |
| **PS** | Prowadzący strzelanie, czyli osoba z uprawnieniami prowadzącego strzelanie |
| **Blokada administracyjna** | Wyłączenie osi przez zarząd, np. na zawody, prace lub awarię |
| **Kod dostępu (voucher)** | Kod wydany np. klubowi na okres umowy, z ustaloną ceną i limitami |
| **Saldo** | Konto przedpłat strzelca. Wpłaty je zwiększają, opłaty zmniejszają |
| **Książka pobytu** | Elektroniczny rejestr pobytu osób na strzelnicy |

## 3. Role

| Rola | Opis | Etap |
|---|---|---|
| **Gość** | Bez konta. Widzi publiczny kalendarz zajętości osi bez danych osobowych. Może dokonać wpisu do książki pobytu bez konta | 1 (kalendarz), 3 (wpis) |
| **Strzelec** | Ma konto, profil i dokumenty, akceptuje regulamin, widzi swoje rezerwacje, saldo i historię | 1–2 |
| **Prowadzący strzelanie** | Strzelec ze zweryfikowanymi uprawnieniami PS. Może rezerwować osie | 1–2 |
| **Opiekun klubu** | Realizuje rezerwacje na kod dostępu klubu | 2 (wstępnie) |
| **Zarząd / administrator** | Pełny panel: weryfikacja dokumentów, osie, cennik, blokady, regulamin, rezerwacje, raporty | 1–2 |
| **Obsługa / dyżurny** | Przewidziana w modelu, nieaktywna | później |
| **Urządzenie** | Konto techniczne czytnika QR (Raspberry Pi) z własnym kluczem | 3 / 6 |

Jedna osoba może mieć kilka ról, np. strzelec, PS i członek zarządu.

---

## 4. Etap 1: fundament

### 4.1 Konta i logowanie

- Rejestracja adresem e-mail i hasłem, z potwierdzeniem adresu e-mail. Hasło ma co najmniej 12 znaków i jest sprawdzane ze słownikiem haseł popularnych.
- Reset hasła linkiem jednorazowym, ważnym 1 godzinę.
- **Dwuskładnikowe logowanie (aplikacja TOTP)** jest obowiązkowe dla zarządu i opcjonalne dla strzelca.
- Ograniczenie prób logowania (ochrona przed zgadywaniem haseł) i rejestr logowań.
- Konto można dezaktywować. Usunięcie danych odbywa się zgodnie z zasadami RODO (pkt 7.2), z zachowaniem wpisów, które przepisy każą przechowywać.

### 4.2 Profil strzelca

- Dane podstawowe: imię, nazwisko, telefon, e-mail, adres. Ostateczna lista pól wynika z wymogów książki pobytu (pytanie P5).
- **Dokumenty i uprawnienia** (każdy wpis ma numer, organ wydający, datę wydania i opcjonalnie datę ważności):
  - pozwolenie na broń (rodzaj lub cel),
  - uprawnienia prowadzącego strzelanie,
  - opcjonalnie licencja zawodnicza, patent, legitymacja klubowa.
- **Weryfikacja przez zarząd.** Statusy: `oczekuje` → `zweryfikowany` / `odrzucony` (z podaniem powodu) → `wygasły` (automatycznie po dacie ważności).
- Skan dokumentu można dołączyć do weryfikacji. **Po weryfikacji skan jest automatycznie usuwany**, a zostaje tylko informacja, kto, kiedy i na jakiej podstawie zweryfikował dokument. To minimalizuje ryzyko przy danych o broni.
- Zmiana numeru zweryfikowanego dokumentu przywraca status `oczekuje`.

### 4.3 Regulamin

- Zarząd wprowadza kolejne wersje regulaminu (treść i data obowiązywania) oraz osobne dokumenty: regulamin strzelnicy, regulamin rezerwacji, informację RODO.
- Opublikowanej wersji nie można edytować. Zmiana treści oznacza nową wersję.
- Po publikacji nowej wersji użytkownik musi ją zaakceptować przed kolejną rezerwacją lub wpisem do książki.
- Rejestr akceptacji przechowuje: kto, kiedy, którą wersję, adres IP.

### 4.4 Obiekty i osie

Model jest ogólny (obiekt → oś → typ), więc strzelnica śrutowa wejdzie później bez przebudowy.

Stan początkowy:

| Oś | Typ | Długość | Uwagi |
|---|---|---|---|
| Oś 1 | kulowa krótka | 25 m | |
| Oś 2 | kulowa krótka | 25 m | |
| Oś 3 | kulowa krótka | 35 m | |
| Oś 4 | kulowa długa | 100 m | |
| Oś 5 | dynamiczno-myśliwska | — | zawsze na wyłączność, stawka ryczałtowa |

Każda oś ma: nazwę, typ, długość, opis, status (aktywna lub nieaktywna) i przypisany cennik. Nazwy osi zmienisz w panelu.

### 4.5 Cennik

Cennik jest wersjonowany, a każda rezerwacja zapamiętuje cenę z chwili utworzenia.

| Oś | Stawka za osobę | Ryczałt | Próg ryczałtu |
|---|---|---|---|
| 25 m, 35 m | 35 zł/os./h | 100 zł/h | od 4 osób |
| 100 m | 60 zł/os./h | 200 zł/h | od 4 osób |
| dynamiczno-myśliwska | — | 150 zł/h | zawsze |

Reguła wyliczenia jest do potwierdzenia (pytanie **P1**).

### 4.6 Ustawienia stowarzyszenia

Pola: nazwa, adres, NIP i REGON, numer rachunku bankowego, dane kontaktowe, godziny dostępności strzelnicy, adres e-mail powiadomień zarządu.

### 4.7 Dziennik zdarzeń (audyt)

- Każda zmiana danych (kto, co, kiedy, stara i nowa wartość) trafia do dziennika, którego nie da się zmienić z panelu.
- Osobno logowany jest każdy **wgląd** zarządu w dane o pozwoleniach i dokumentach.

### 4.8 Kryteria odbioru etapu 1

1. Strzelec zakłada konto, potwierdza e-mail, uzupełnia profil, dodaje uprawnienia PS i akceptuje regulamin.
2. Zarząd widzi wniosek, weryfikuje go, a strzelec otrzymuje e-mail z wynikiem.
3. Uprawnienia, którym minęła data ważności, same zmieniają status na `wygasły`.
4. Publikacja nowej wersji regulaminu wymusza ponowną akceptację.
5. Każdy wgląd w dokumenty jest widoczny w dzienniku.
6. Testy automatyczne przechodzą (zakres opisany w pkt 7.4).

---

## 5. Etap 2: rezerwacje osi

### 5.1 Zasady

- **Rezerwacja blokuje całą oś.** Nie rezerwuje się pojedynczych stanowisk.
- Rezerwację może utworzyć osoba, która ma PS. Kto dokładnie może być wskazany jako PS, ustalamy w pytaniu **P3**.
- Przy rezerwacji podaje się: oś, datę, godzinę od–do, **liczbę osób**, PS, opcjonalnie kod dostępu i uwagi.
- Liczba osób wyznacza należność (pkt 4.5). Ostateczne rozliczenie może zostać skorygowane według faktycznej liczby wpisów w książce pobytu (łączymy to w etapie 3 lub 4).
- Siatka czasu, godziny dostępności, minimalna i maksymalna długość rezerwacji, wyprzedzenie oraz termin bezpłatnego anulowania to ustawienia zarządu (pytanie **P4**).
- **Brak podwójnych rezerwacji** jest gwarantowany przez bazę danych (ograniczenie wykluczające nakładanie się przedziałów czasu), a nie tylko przez kod aplikacji. Zabezpiecza to również przed jednoczesnymi kliknięciami.

### 5.2 Statusy rezerwacji

`potwierdzona` → `zakończona` lub `anulowana` (przez strzelca albo zarząd, z powodem) lub `nieodbyta`.

Należność zapisuje się przy rezerwacji ze statusem płatności `do zapłaty`. Rozliczenie z saldem, przelewem i Tpay dochodzi w etapie 4. Do tego czasu zarząd może ręcznie oznaczyć rezerwację jako opłaconą.

### 5.3 Widoki

- **Publiczny kalendarz zajętości:** dzień i tydzień dla każdej osi. Pokazuje tylko „zajęte” albo „wolne”, bez nazwisk.
- **Strzelec:** nowa rezerwacja w 3 krokach (oś i termin → osoby i PS → podsumowanie z ceną), lista „moje rezerwacje”, anulowanie.
- **Zarząd:** kalendarz wszystkich osi, lista z filtrami, tworzenie rezerwacji w czyimś imieniu, edycja, anulowanie, blokady, eksport CSV.

### 5.4 Blokady administracyjne

- Zarząd blokuje jedną lub wiele osi na przedział czasu (zawody, prace, awaria), także cyklicznie.
- Jeśli blokada koliduje z istniejącymi rezerwacjami, system pokazuje listę kolizji. Zarząd decyduje, czy je anulować, a strzelcy otrzymują e-mail.

### 5.5 Kody dostępu dla klubów (propozycja, do decyzji — NIE wdrożone, czeka na decyzję właściciela)

- Zarząd tworzy kod z: nazwą (np. klub X), okresem ważności, dozwolonymi osiami, limitem godzin lub rezerwacji, **stawką umowną** (np. stała kwota za godzinę albo pakiet opłacony z góry).
- Rezerwujący wpisuje kod, a cena liczy się według kodu. Wykorzystanie kodu widać w panelu zarządu.
- Rezerwacje cykliczne klubów (np. każdy wtorek) mogą zostać dodane później jako rozszerzenie kodu.

### 5.6 Powiadomienia

| Zdarzenie | Strzelec (e-mail) | Zarząd (e-mail + panel) |
|---|---|---|
| Nowa rezerwacja | tak | tak |
| Zmiana lub anulowanie | tak | tak |
| Przypomnienie dzień wcześniej | tak | — |
| Blokada koliduje z rezerwacją | tak | tak |
| Nowy dokument do weryfikacji | — | tak |

W panelu zarządu jest lista powiadomień z oznaczaniem jako przeczytane.

### 5.7 Kryteria odbioru etapu 2

1. PS rezerwuje oś, widzi wyliczoną cenę i dostaje e-mail. Zarząd widzi rezerwację i powiadomienie.
2. Dwie jednoczesne próby rezerwacji tej samej osi w tym samym czasie: dokładnie jedna się udaje (test współbieżności).
3. Osoba bez zweryfikowanego PS nie może zarezerwować osi.
4. Blokada zarządu ukrywa terminy, a przy kolizji pokazuje listę rezerwacji do decyzji.
5. Cena jest zgodna z cennikiem dla każdej osi i liczby osób od 1 do 10 (testy jednostkowe).
6. Publiczny kalendarz nie ujawnia danych osobowych.

---

## 6. Kolejne etapy (zarys, poza zakresem tej specyfikacji)

| Etap | Zakres |
|---|---|
| 3. Książka pobytu | Wpis i wypis kodem QR (z rezerwacji lub stały kod strzelca), z konta, **bez konta** (formularz z oświadczeniami), wydruk i eksport na potrzeby kontroli, zgodność z przepisami o broni i amunicji |
| 4. Płatności | Saldo strzelca, przelew z unikalnym tytułem i import wyciągu CSV, gotówka i BLIK na telefon rejestrowane w panelu, Tpay (najpierw sandbox), karnety |
| 5. Stowarzyszenie | Członkowie, składki, kluby, uchwały i postanowienia zarządu, raporty obłożenia i wpływów |
| 6. Czytnik QR | Raspberry Pi z kamerą i ekranem, klucz urządzenia, **praca bez internetu z buforem wpisów** i późniejszą synchronizacją |
| 7. Strzelnica śrutowa | Skeet, trap i inne konkurencje jako nowy obiekt i typy stanowisk |
| 8. Księgowość NGO | Ewidencja na bazie obecnie prowadzonej, KSeF, sprawozdawczość (zakres do ustalenia, pytanie P6) |
| 9. Notatnik treningów | Treningi statyczne i dynamiczne, liczenie punktów z tarcz ISSF (pistolet, karabin) |

## 7. Wymagania niefunkcjonalne

### 7.1 Technologia

- **Backend:** Python 3.12, Django 5, PostgreSQL 16.
- **Interfejs:** strony renderowane po stronie serwera z HTMX. Jest szybki na telefonie, ma mało kodu do utrzymania i działa jako PWA (instalacja na ekranie głównym).
- **API REST** (Django REST Framework) dla czytnika QR i przyszłych integracji.
- **Uruchomienie:** Docker Compose (aplikacja, baza, serwer WWW z HTTPS), automatyczne kopie zapasowe bazy.
- Strefa czasowa Europe/Warsaw, język polski, kwoty w groszach (bez błędów zaokrągleń).

### 7.2 Bezpieczeństwo i RODO

- Dostęp według ról, sprawdzany po stronie serwera dla każdej operacji.
- Ochrona CSRF i XSS, nagłówki bezpieczeństwa, sesje tylko po HTTPS.
- Sekrety (klucze Tpay, hasła) wyłącznie w zmiennych środowiskowych, nigdy w repozytorium.
- Minimalizacja danych, logowanie wglądu w dane wrażliwe, zdefiniowane okresy przechowywania, eksport danych osoby na jej żądanie.

### 7.3 Dostępność i użyteczność

Interfejs projektowany najpierw na telefon, z dużymi przyciskami i czytelny w słońcu. Najczęstsze czynności wymagają najwyżej 3 kroków.

### 7.4 Jakość

Testy jednostkowe (cennik, reguły rezerwacji, uprawnienia), integracyjne (baza i współbieżność) oraz E2E dla głównych ścieżek. Testy uruchamiane są automatycznie przy każdej zmianie.

---

## 8. Repozytorium

- Proponowana nazwa: **`strzelnica-satok`**. Repozytorium prywatne.
- Nie mam uprawnień do zakładania repozytoriów na Twoim koncie GitHub. Załóż puste repozytorium o tej nazwie i podaj mi `właściciel/strzelnica-satok`, a ja je podłączę i wypchnę kod. Do tego czasu pracuję lokalnie.

## 9. Decyzje właściciela (30.10.2026)

| Nr | Decyzja |
|---|---|
| P1 | Cena za godzinę = **mniejsza z kwot** (osoby × stawka, ryczałt). Na osiach 25/35 m 3 osoby płacą 100 zł |
| P2 | Rezerwacje wyłącznie w **pełnych godzinach** |
| P3 | Rezerwuje **wyłącznie osoba, która sama ma zweryfikowane uprawnienia PS** (wariant a) |
| P4 | Godziny otwarcia ustawiane w panelu **osobno dla każdej osi i dnia tygodnia**; obecnie wszystkie osie 9–18. **Brak limitu długości** rezerwacji (dowolnie wiele wolnych godzin w dniu). Rezerwować można **do chwili rozpoczęcia** (także na miejscu). **Bezpłatne anulowanie do 48 h** przed terminem |
| P5 | Książka pobytu ma być zgodna z ustawą o broni i amunicji — lista pól zostanie ustalona na podstawie przepisów w etapie 3 |
| P6 | „2 do 2” to obecnie używany zewnętrzny program do prowadzenia uproszczonej ewidencji przychodów i kosztów (UEPiK) dla NGO — etap 8 |
| P7 | Na start jeden administrator (właściciel) z możliwością nadawania uprawnień innym osobom w panelu |

Zmiany względem wersji 0.1 wynikające z decyzji:
- pkt 5.1: rezerwujący musi sam mieć zweryfikowane uprawnienia PS; pola „PS” w rezerwacji nie ma;
- pkt 5.1: minimalna długość 1 h, brak maksymalnej, brak minimalnego wyprzedzenia (początek nie może być w przeszłości);
- pkt 5.2: anulowanie przez strzelca na 48 h lub więcej przed terminem jest bezpłatne; później — zgodnie z regulaminem rezerwacji (do doprecyzowania w etapie 4).

## 10. Stan wdrożenia etapu 2 (30.10.2026)

- Rezerwacja blokuje całą oś; nakładanie się rezerwacji na jednej osi blokuje baza danych (ograniczenie wykluczające).
- Rezerwuje wyłącznie zweryfikowany PS z potwierdzonym e-mailem i zaakceptowanymi regulaminami.
- Pełne godziny w godzinach otwarcia osi; bez limitu długości; można zarezerwować bieżącą, rozpoczętą godzinę.
- Domyślne ustawienia (zmienialne w panelu): bezpłatne anulowanie 48 h, rezerwacja do 180 dni naprzód, maks. 50 osób.
- Anulowanie przez strzelca po terminie: rezerwacja anulowana, należność pozostaje „do zapłaty”; zarząd może zwolnić z opłaty.
- Anulowanie przez zarząd: zawsze bez opłaty, z podaniem powodu.
- Numer rezerwacji `SATOK-R-RRRR-NNNN` służy jako tytuł przelewu; rozliczenie ręczne w panelu do czasu etapu 4.
- Blokady jednorazowe i cotygodniowe; przy kolizji zarząd wybiera: anulować rezerwacje albo je pozostawić (strzelcy dostają e-mail).
- Powiadomienia: e-mail strzelca (utworzenie, zmiana, anulowanie, przypomnienie ~24 h przed), e-mail i lista powiadomień w panelu zarządu.
- Eksport listy rezerwacji do CSV (Excel, separator „;”).
