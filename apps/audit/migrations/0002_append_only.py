from django.db import migrations

SQL = """
CREATE OR REPLACE FUNCTION satok_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Tabela % przyjmuje tylko nowe wpisy (dziennik niezmienialny)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER auditlog_append_only BEFORE UPDATE OR DELETE ON audit_auditlog
    FOR EACH ROW EXECUTE FUNCTION satok_append_only();
CREATE TRIGGER sensitiveaccesslog_append_only BEFORE UPDATE OR DELETE ON audit_sensitiveaccesslog
    FOR EACH ROW EXECUTE FUNCTION satok_append_only();
"""

REVERSE = """
DROP TRIGGER IF EXISTS auditlog_append_only ON audit_auditlog;
DROP TRIGGER IF EXISTS sensitiveaccesslog_append_only ON audit_sensitiveaccesslog;
DROP FUNCTION IF EXISTS satok_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
