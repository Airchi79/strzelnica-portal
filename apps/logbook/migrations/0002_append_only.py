from django.db import migrations

SQL = """
CREATE OR REPLACE FUNCTION satok_logbook_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Jedyna dozwolona zmiana: jednorazowe ustawienie godziny wyjścia.
        IF OLD.exited_at IS NULL AND NEW.exited_at IS NOT NULL
           AND (to_jsonb(NEW) - 'exited_at') = (to_jsonb(OLD) - 'exited_at') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'Wpisu w książce pobytu nie można zmieniać';
    END IF;
    -- Usunięcie wyłącznie przez procedurę czyszczenia po okresie przechowywania.
    IF current_setting('satok.logbook_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'Wpisu w książce pobytu nie można usunąć';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER logbook_entry_guard BEFORE UPDATE OR DELETE ON logbook_entry
    FOR EACH ROW EXECUTE FUNCTION satok_logbook_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS logbook_entry_guard ON logbook_entry;
DROP FUNCTION IF EXISTS satok_logbook_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("logbook", "0001_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
