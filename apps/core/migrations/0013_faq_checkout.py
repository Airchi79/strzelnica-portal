"""FAQ: opis trzymania terminu na czas płatności i przelewu tradycyjnego (rezerwacja potwierdzana po płatności).
Zmienia odpowiedzi tylko wtedy, gdy nie były edytowane w panelu (treść identyczna z początkową)."""

from django.db import migrations

OLD_PAY = (
    "Online (BLIK, szybki przelew, karta — przez Tpay), przelewem tradycyjnym z numerem rezerwacji w tytule albo gotówką na miejscu. "
    "Dostępne sposoby zależą od Twojej grupy i są pokazane przy rezerwacji. Jeśli do płatności online doliczana jest opłata za jej obsługę, zobaczysz ją przed zapłatą.\n\n"
    "Po zaksięgowaniu wpłaty pobierzesz potwierdzenie w „Moich płatnościach”."
)
NEW_PAY = (
    "Po wybraniu terminu masz 5 minut na wybór sposobu płatności — w tym czasie termin jest zatrzymany dla Ciebie.\n\n"
    "• Online (BLIK, szybki przelew, karta — przez Tpay): rezerwacja potwierdza się po zapłacie; na zapłatę masz 15 minut.\n"
    "• Gotówką na miejscu: rezerwacja potwierdza się od razu, płacisz u obsługi przed strzelaniem.\n"
    "• Przelewem tradycyjnym (numer rezerwacji w tytule) — gdy do strzelania zostały co najmniej 4 dni: termin czeka na zaksięgowanie "
    "wpłaty do 3 dni, dzień przed terminem przypomnimy e-mailem, a potwierdzenie przyjdzie po zaksięgowaniu.\n\n"
    "Dostępne sposoby zależą od Twojej grupy i są pokazane przy rezerwacji. Środki na saldzie są wykorzystywane najpierw. "
    "Jeśli do płatności online doliczana jest opłata za jej obsługę, zobaczysz ją przed zapłatą.\n\n"
    "Po zaksięgowaniu wpłaty pobierzesz potwierdzenie w „Moich płatnościach”."
)
OLD_STEP = "4. Wejdź w „Zajętość osi”, wybierz oś, dzień i godziny, a potem potwierdź rezerwację.\n\n"
NEW_STEP = "4. Wejdź w „Zajętość osi”, wybierz oś, dzień i godziny, kliknij „Rezerwuję” i wybierz sposób płatności.\n\n"


def forward(apps, schema_editor):
    FaqItem = apps.get_model("core", "FaqItem")
    FaqItem.objects.filter(question="Jak mogę zapłacić?", answer=OLD_PAY).update(answer=NEW_PAY)
    for item in FaqItem.objects.filter(question="Jak zarezerwować oś?", answer__contains=OLD_STEP):
        item.answer = item.answer.replace(OLD_STEP, NEW_STEP)
        item.save(update_fields=["answer"])


def backward(apps, schema_editor):
    FaqItem = apps.get_model("core", "FaqItem")
    FaqItem.objects.filter(question="Jak mogę zapłacić?", answer=NEW_PAY).update(answer=OLD_PAY)
    for item in FaqItem.objects.filter(question="Jak zarezerwować oś?", answer__contains=NEW_STEP):
        item.answer = item.answer.replace(NEW_STEP, OLD_STEP)
        item.save(update_fields=["answer"])


class Migration(migrations.Migration):
    dependencies = [("core", "0012_range_location")]
    operations = [migrations.RunPython(forward, backward)]
