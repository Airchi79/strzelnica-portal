"""Rezerwacja z blokadą terminu na czas płatności: status „oczekuje”, wybrany sposób płatności, przelew i gotówka
osobno w zasadach grup (gotówka na miejscu: członkowie i powracający), czasy blokady i przelewu w ustawieniach."""

from django.db import migrations, models
from django.db.models import Q


def split_offline(apps, schema_editor):
    GroupRule = apps.get_model("bookings", "GroupRule")
    for rule in GroupRule.objects.all():
        # dawne „offline” = przelew + gotówka; gotówka na miejscu tylko dla członków i powracających (decyzja 8.11.2026)
        rule.pay_cash = rule.pay_transfer and rule.group in ("czlonek", "powracajacy")
        rule.save(update_fields=["pay_cash"])


class Migration(migrations.Migration):
    dependencies = [("bookings", "0018_lead_hours_help")]

    operations = [
        migrations.RemoveConstraint(model_name="grouprule", name="group_some_payment"),
        migrations.RenameField(model_name="grouprule", old_name="pay_offline", new_name="pay_transfer"),
        migrations.AlterField(model_name="grouprule", name="pay_transfer",
                              field=models.BooleanField(default=True, verbose_name="przelew tradycyjny")),
        migrations.AddField(model_name="grouprule", name="pay_cash",
                            field=models.BooleanField(default=True, verbose_name="gotówka na miejscu")),
        migrations.RunPython(split_offline, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="grouprule",
            constraint=models.CheckConstraint(condition=Q(pay_online=True) | Q(pay_transfer=True) | Q(pay_cash=True) | Q(pay_pass=True),
                                              name="group_some_payment"),
        ),
        migrations.AddField(model_name="reservation", name="pay_method",
                            field=models.CharField(blank=True, choices=[("online", "Online (Tpay)"), ("przelew", "Przelew tradycyjny"),
                                                                        ("gotowka", "Gotówka na miejscu")],
                                                   max_length=10, verbose_name="wybrany sposób płatności")),
        migrations.AddField(model_name="reservation", name="needs_approval",
                            field=models.BooleanField(default=False, verbose_name="po opłaceniu czeka na zatwierdzenie")),
    ]
