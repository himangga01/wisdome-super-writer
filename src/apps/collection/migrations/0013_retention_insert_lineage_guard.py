from django.db import migrations


def retain_insert_lineage(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    table = schema_editor.quote_name("collection_sourceitem")
    trigger = schema_editor.quote_name("collection_source_item_append_only")
    schema_editor.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    schema_editor.execute(
        f"CREATE TRIGGER {trigger} BEFORE INSERT ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION collection_guard_source_item()"
    )


def restore_original_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    table = schema_editor.quote_name("collection_sourceitem")
    trigger = schema_editor.quote_name("collection_source_item_append_only")
    schema_editor.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    schema_editor.execute(
        f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION collection_guard_source_item()"
    )


class Migration(migrations.Migration):
    dependencies = [("collection", "0012_sourceitem_optional_attachments")]
    operations = [migrations.RunPython(retain_insert_lineage, restore_original_guard)]
