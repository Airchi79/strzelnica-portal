"""Nowe, węższe uprawnienia: osoby, które miały „Rezerwacje”, dostają też „Ustawienia rezerwacji”,
a osoby z „Płatnościami” — „Wpłaty na miejscu”, żeby nikt nie stracił dotychczasowego dostępu."""

from django.conf import settings
from django.db import migrations

GRANTS = [
    (("bookings", "manage_reservations"), ("bookings", "manage_booking_settings")),
    (("payments", "manage_payments"), ("payments", "accept_onsite")),
]


def forwards(apps, schema_editor):
    from django.contrib.auth.management import create_permissions

    for label in ("bookings", "payments"):
        config = apps.get_app_config(label)
        original = getattr(config, "models_module", None)
        config.models_module = original or True  # historyczne aplikacje migracji nie mają modułu modeli
        try:
            create_permissions(config, apps=apps, verbosity=0)
        finally:
            config.models_module = original
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    for (src_app, src), (dst_app, dst) in GRANTS:
        old = Permission.objects.filter(content_type__app_label=src_app, codename=src).first()
        new = Permission.objects.filter(content_type__app_label=dst_app, codename=dst).first()
        if not old or not new:
            continue
        for user in User.objects.filter(user_permissions=old):
            user.user_permissions.add(new)
        for group in Group.objects.filter(permissions=old):
            group.permissions.add(new)


class Migration(migrations.Migration):
    dependencies = [
        ("bookings", "0009_alter_reservation_options"),
        ("payments", "0004_alter_payment_options"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
