from django.db import migrations

SQL = """
CREATE TRIGGER ledgerentry_append_only BEFORE UPDATE OR DELETE ON payments_ledgerentry
    FOR EACH ROW EXECUTE FUNCTION satok_append_only();
"""


class Migration(migrations.Migration):
    """Operacji na saldzie nie można zmienić ani usunąć — korekta to nowa operacja."""

    dependencies = [("payments", "0001_initial"), ("audit", "0002_append_only")]
    operations = [migrations.RunSQL(SQL, "DROP TRIGGER IF EXISTS ledgerentry_append_only ON payments_ledgerentry;")]
