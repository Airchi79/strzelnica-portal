"""Nazwa stowarzyszenia zgodnie z rejestrem GUS: „STRZELNICA SĄTOK” (forma prawna: stowarzyszenie zwykłe).
Poprawia tylko wartości równe dawnym brzmieniom — nazw wpisanych inaczej przez zarząd nie zmienia."""

from django.db import migrations

NEW_NAME = "Stowarzyszenie „STRZELNICA SĄTOK”"
OLD_NAMES = ["Stowarzyszenie STRZELNICA SĄTOK", "Stowarzyszenie „Strzelnica Sątok”"]
OLD_TITLE = "Regulamin Stowarzyszenia „Strzelnica Sątok”"
NEW_TITLE = "Regulamin Stowarzyszenia „STRZELNICA SĄTOK”"


def forward(apps, schema_editor):
    AssociationSettings = apps.get_model("core", "AssociationSettings")
    AssociationSettings.objects.filter(name__in=OLD_NAMES).update(name=NEW_NAME)
    RegulationDocument = apps.get_model("regulations", "RegulationDocument")
    RegulationDocument.objects.filter(title=OLD_TITLE).update(title=NEW_TITLE)


class Migration(migrations.Migration):
    dependencies = [("core", "0010_association_name_default"), ("regulations", "0003_association_rules_document")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
