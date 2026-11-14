import secrets

from django.db import migrations, models

import apps.clients.models


def fill_secrets(apps, schema_editor):
    Person = apps.get_model("clients", "Person")
    for p in Person.objects.all().only("pk"):
        Person.objects.filter(pk=p.pk).update(qr_secret=secrets.token_hex(16))  # osobny sekret dla każdej osoby


class Migration(migrations.Migration):
    dependencies = [("clients", "0006_osoba_email")]

    operations = [
        migrations.AddField(model_name="person", name="qr_secret", field=models.CharField(default="", editable=False, max_length=32)),
        migrations.RunPython(fill_secrets, migrations.RunPython.noop),
        migrations.AlterField(model_name="person", name="qr_secret",
                              field=models.CharField(default=apps.clients.models._new_card_secret, editable=False, max_length=32)),
        migrations.AddField(model_name="person", name="card_printed_at",
                            field=models.DateTimeField(blank=True, null=True, verbose_name="karta wydrukowana")),
    ]
