# Płatności online przez Axepta BNP Paribas

System obsługuje dwóch operatorów płatności online: **Tpay** i **Axepta BNP Paribas**. Operatora nowych płatności
wybiera jedno ustawienie w `.env`: `ONLINE_PAYMENTS_PROVIDER=tpay` albo `axepta`. Wszystko inne działa tak samo
dla obu operatorów: rezerwacje, kiosk (płatność kodem QR na telefonie), składki, opłaty startowe, doładowanie salda,
zwroty, raporty wpływów, potwierdzenia wpłat i opłata za płatność online.

## Jak to działa

1. Strzelec klika „Zapłać online”. System tworzy w Axepcie **link płatności** (kwota w groszach, numer zamówienia =
   identyfikator wpłaty w systemie, link ważny 2 godziny) i przekierowuje na stronę płatności Axepty.
2. Na stronie Axepty strzelec wybiera **BLIK, szybki przelew, kartę albo Google Pay / Apple Pay**. Dane kart nigdy nie
   trafiają do systemu strzelnicy.
3. Axepta wysyła **powiadomienie** na adres `https://rezerwacje.strzelnicasatok.pl/platnosci/axepta/powiadomienie/`.
   System sprawdza podpis (SHA-256 z treści i klucza sklepu), zgodność sklepu, identyfikatora, numeru zamówienia, kwoty
   i waluty — dopiero wtedy księguje wpłatę. Rozbieżność → wpłata „nieprzypisana” do decyzji zarządu.
4. Po powrocie strzelca z bramki system dodatkowo sprawdza stan płatności w API (gdyby powiadomienie się spóźniło).
5. **Zwroty** (anulowanie, odrzucenie, odwołanie wydarzenia, „Zwróć przez operatora” w panelu) idą przez API Axepty na
   kartę / rachunek płacącego, bez opłaty za płatność online. Axepta wymaga ≥ 5 s między zwrotami — przy wielu zwrotach
   naraz kolejne wysyła harmonogram (w ciągu kilku minut). Zwrot odrzucony przez Axeptę → kwota wraca na saldo strzelca,
   zarząd dostaje powiadomienie.

## Konto testowe (sandbox) — można założyć samemu, bez umowy z bankiem

1. Wejdź na **https://axepta.sandbox.bnpparibas.pl/** → „Utwórz konto Akceptanta” → podaj e-mail → aktywuj konto
   linkiem z wiadomości → zaloguj się.
2. Do konta przypisany jest sklep testowy z kluczami. W panelu znajdź (Ustawienia → Klucze API / dane sklepu):
   - **identyfikator klienta** (merchantId) → `AXEPTA_MERCHANT_ID`,
   - **identyfikator sklepu** (serviceId, format UUID) → `AXEPTA_SERVICE_ID`,
   - **token API** (Bearer) → `AXEPTA_API_TOKEN`,
   - **klucz sklepu do podpisu powiadomień** → `AXEPTA_SERVICE_KEY`.
3. W panelu Axepty ustaw **adres powiadomień** sklepu:
   `https://test.strzelnicasatok.pl/platnosci/axepta/powiadomienie/` (dla kopii testowej).
4. W kopii testowej (`/share/CACHEDEV3_DATA/strzelnica-test`) zapisz dane — wartości nie pojawią się na ekranie:

```
cd /share/CACHEDEV3_DATA/strzelnica-test
sh deploy/ustaw-env.sh AXEPTA_MERCHANT_ID
sh deploy/ustaw-env.sh AXEPTA_SERVICE_ID
sh deploy/ustaw-env.sh AXEPTA_API_TOKEN
sh deploy/ustaw-env.sh AXEPTA_SERVICE_KEY
sed -i '/^ONLINE_PAYMENTS_PROVIDER=/d' .env && echo 'ONLINE_PAYMENTS_PROVIDER=axepta' >> .env
docker compose up -d app cron
```

Kopia testowa zawsze wymusza `AXEPTA_SANDBOX=true` (inaczej nie wystartuje).

### Dane testowe w sandboxie (z dokumentacji Axepty)

| Metoda | Dane | Wynik |
|---|---|---|
| BLIK | kod `777xxx` (xxx dowolne) | zrealizowana |
| BLIK | kod `960111` | odrzucona (brak środków) |
| Karta | Visa `4111 1111 1111 1111`, 12/29, CVV 123 | zrealizowana |
| Karta | Visa `4012 0010 0700 2005`, 12/29, CVV 123 | błąd dostawcy |
| Szybki przelew | tytuł `TEST-100010` | błąd dostawcy |

### Co sprawdzić w sandboxie (lista kontrolna)

1. Płatność za rezerwację BLIK-iem `777123` → rezerwacja opłacona, e-mail, potwierdzenie wpłaty (PDF).
2. Płatność odrzucona (BLIK `960111`) → wpłata „nieudana”, rezerwacja nadal do zapłaty.
3. Płatność z kiosku (kod QR na telefonie) → kiosk pokazuje opłacenie.
4. Anulowanie opłaconej rezerwacji → zwrot widoczny w panelu Axepty i w systemie (Płatności → wpłata → Zwroty online).
5. Odwołanie wydarzenia z kilkoma opłaconymi zapisami → wszystkie zwroty wysłane (kolejne co ≥ 5 s, w ciągu kilku minut).
6. Panel → Płatności: brak wpłat „nieprzypisanych”; wpłaty testowe oznaczone „TEST”.

Rzeczy do potwierdzenia w sandboxie (dokumentacja ich nie rozstrzyga jednoznacznie): dokładna nazwa klucza do podpisu
w panelu, postać powiadomienia o zwrocie (`notifyTransactionData.type = "refund"`) i czy Axepta respektuje ważność linku
(`activeTo`). Jeśli coś zadziała inaczej — zgłoś, poprawię przed uruchomieniem produkcyjnym.

## Uruchomienie na produkcji (gdy będzie umowa z BNP Paribas)

1. W produkcyjnym panelu Axepty (https://axepta.bnpparibas.pl/) ustaw adres powiadomień
   `https://rezerwacje.strzelnicasatok.pl/platnosci/axepta/powiadomienie/`.
2. Na produkcji zapisz dane jak wyżej (w `/share/strzelnica`), ustaw `AXEPTA_SANDBOX=false`
   i `ONLINE_PAYMENTS_PROVIDER=axepta`, potem `docker compose up -d app cron`.
3. Dane Tpay zostaw w `.env`, dopóki wszystkie wcześniejsze płatności i zwroty Tpay nie zostaną rozliczone.
4. **Dokumenty** (opublikuj nowe wersje w dniu przełączenia):
   - Informacja RODO § 3 ust. 1:
     > 1. BNP Paribas Bank Polska S.A. z siedzibą w Warszawie (operator płatności Axepta BNP Paribas) — w zakresie płatności
     > online (BLIK, szybki przelew, karta, Google Pay, Apple Pay) i zwrotów. Operator przetwarza dane płatności jako odrębny
     > administrator, na zasadach swojej polityki prywatności. Przy płatności Google Pay lub Apple Pay dane płatności
     > przetwarzają także Google lub Apple — na zasadach regulaminu wybranej usługi.

     Jeżeli przed przełączeniem przez Tpay przechodziły prawdziwe płatności, dopisz jako ust. 2 (do czasu rozliczenia
     ich zwrotów): „Krajowy Integrator Płatności S.A. (operator płatności Tpay) — w zakresie płatności online dokonanych
     przed zmianą operatora płatności i ich zwrotów. Operator przetwarza dane płatności jako odrębny administrator, na
     zasadach swojej polityki prywatności.” (kolejne ustępy przesuń o jeden).
   - Regulamin rezerwacji i opłat § 6 ust. 1 lit. a:
     > a) online przez operatora płatności Axepta BNP Paribas (BLIK, szybki przelew, karta, Google Pay, Apple Pay),
   - Regulamin rezerwacji i opłat § 7 ust. 3:
     > 3. Przy anulowaniu bez opłat wpłata online wraca automatycznie na rachunek lub kartę, z których dokonano płatności,
     > za pośrednictwem tego samego operatora płatności — zwykle w ciągu kilku dni roboczych. Wpłaty gotówką i przelewem
     > tradycyjnym zwraca obsługa w uzgodniony sposób.
5. Zapytaj bank o: prowizje (do ustawienia „opłaty za płatność online” w panelu), termin wypłat na rachunek,
   możliwość zwrotów przez API na koncie produkcyjnym.

Powiadomienia Axepta przychodzą z adresów 141.95.10.64/28 i 51.83.82.16/28 — o autentyczności decyduje podpis,
dodatkowa reguła w Cloudflare nie jest potrzebna.

## Źródła

- Dokumentacja API Axepta (Polska), wersja 1.0.3: https://bump.sh/pgw/doc/axepta-api
- Dokumentacja techniczna integracji z systemem transakcyjnym Axepta (PDF): https://www.bnpparibas.pl/axepta
- Sandbox: https://axepta.sandbox.bnpparibas.pl/ (API: https://api.sandbox.axepta.pl/v1)
