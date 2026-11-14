"""Częste pytania: operator płatności online Axepta BNP Paribas zamiast Tpay (decyzja właściciela, listopad 2026).
Zmienia tylko fragment „przez Tpay” w odpowiedziach — pozostała treść (także poprawiona w panelu) bez zmian."""

from django.db import migrations

REPLACEMENTS = [
    ("BLIK, szybki przelew, karta — przez Tpay", "BLIK, szybki przelew, karta, Google Pay, Apple Pay — przez Axepta BNP Paribas"),
    ("przez Tpay", "przez Axepta BNP Paribas"),
    ("Tpay", "Axepta BNP Paribas"),
]


def forward(apps, schema_editor):
    FaqItem = apps.get_model("core", "FaqItem")
    for item in FaqItem.objects.filter(answer__contains="Tpay") | FaqItem.objects.filter(question__contains="Tpay"):
        answer, question = item.answer, item.question
        for old, new in REPLACEMENTS:
            answer, question = answer.replace(old, new), question.replace(old, new)
        FaqItem.objects.filter(pk=item.pk).update(answer=answer, question=question[:200])


class Migration(migrations.Migration):
    dependencies = [("core", "0014_outgoing_email")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
