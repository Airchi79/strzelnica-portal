from django.db import migrations


def seed(apps, schema_editor):
    RegulationDocument = apps.get_model("regulations", "RegulationDocument")
    RegulationDocument.objects.get_or_create(
        slug="regulamin-stowarzyszenia",
        defaults={"title": "Regulamin Stowarzyszenia „Strzelnica Sątok”", "order": 0, "requires_acceptance": False},
    )


class Migration(migrations.Migration):
    """Regulamin stowarzyszenia akceptują osoby składające deklarację członkowską, a nie wszyscy użytkownicy."""

    dependencies = [("regulations", "0002_initial_documents")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
