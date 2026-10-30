from django.db import migrations

DOCS = [
    ("regulamin-strzelnicy", "Regulamin strzelnicy", 1),
    ("regulamin-rezerwacji", "Regulamin rezerwacji i opłat", 2),
    ("rodo", "Informacja o przetwarzaniu danych osobowych (RODO)", 3),
]


def seed(apps, schema_editor):
    RegulationDocument = apps.get_model("regulations", "RegulationDocument")
    for slug, title, order in DOCS:
        RegulationDocument.objects.get_or_create(slug=slug, defaults={"title": title, "order": order})


class Migration(migrations.Migration):
    dependencies = [("regulations", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
