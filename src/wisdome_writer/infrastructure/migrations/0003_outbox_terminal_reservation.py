from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
from django.db.models import Q


SQLITE_TRIGGER_NAMES = (
    "outbox_receipt_terminal_reservation_insert_t021",
    "outbox_receipt_terminal_reservation_update_t021",
    "outbox_receipt_terminal_reservation_delete_t021",
)

SQLITE_GUARD_SQL = (
    """
    CREATE TRIGGER outbox_receipt_terminal_reservation_insert_t021
    BEFORE INSERT ON infrastructure_outboxconsumerreceipt
    WHEN (
      NEW.terminal_reserved_at IS NULL AND (
        NEW.terminal_lease_generation <> 0
        OR NEW.terminal_lease_token IS NOT NULL
        OR NEW.terminal_lease_token_hash <> ''
        OR NEW.terminal_error_code <> ''
      )
    ) OR (
      NEW.terminal_reserved_at IS NOT NULL AND (
        NEW.state <> 'processing'
        OR NEW.terminal_lease_generation < 1
        OR NEW.terminal_lease_generation <> NEW.lease_generation
        OR NEW.lease_token IS NULL
        OR NEW.terminal_lease_token IS NULL
        OR NEW.terminal_lease_token IS NOT NEW.lease_token
        OR length(NEW.terminal_lease_token_hash) <> 64
        OR NEW.terminal_lease_token_hash GLOB '*[^0-9a-f]*'
        OR length(NEW.terminal_error_code) < 1
      )
    ) OR (
      NEW.state = 'succeeded' AND NEW.terminal_reserved_at IS NOT NULL
    )
    BEGIN
      SELECT RAISE(ABORT, 'outbox terminal reservation is invalid');
    END
    """,
    """
    CREATE TRIGGER outbox_receipt_terminal_reservation_update_t021
    BEFORE UPDATE ON infrastructure_outboxconsumerreceipt
    BEGIN
      SELECT CASE WHEN (
        NEW.terminal_reserved_at IS NULL AND (
          NEW.terminal_lease_generation <> 0
          OR NEW.terminal_lease_token IS NOT NULL
          OR NEW.terminal_lease_token_hash <> ''
          OR NEW.terminal_error_code <> ''
        )
      ) OR (
        NEW.terminal_reserved_at IS NOT NULL AND (
          NEW.terminal_lease_generation < 1
          OR NEW.terminal_lease_token IS NULL
          OR length(NEW.terminal_lease_token_hash) <> 64
          OR NEW.terminal_lease_token_hash GLOB '*[^0-9a-f]*'
          OR length(NEW.terminal_error_code) < 1
        )
      ) THEN RAISE(ABORT, 'outbox terminal reservation is incomplete') END;
      SELECT CASE WHEN NEW.state = 'succeeded'
        AND NEW.terminal_reserved_at IS NOT NULL
        THEN RAISE(ABORT, 'succeeded receipt cannot be terminal reserved') END;
      SELECT CASE WHEN OLD.terminal_reserved_at IS NOT NULL AND (
        NEW.terminal_reserved_at IS NOT OLD.terminal_reserved_at
        OR NEW.terminal_lease_generation IS NOT OLD.terminal_lease_generation
        OR NEW.terminal_lease_token IS NOT OLD.terminal_lease_token
        OR NEW.terminal_lease_token_hash IS NOT OLD.terminal_lease_token_hash
        OR NEW.terminal_error_code IS NOT OLD.terminal_error_code
      ) THEN RAISE(ABORT, 'outbox terminal reservation is immutable') END;
      SELECT CASE WHEN OLD.terminal_reserved_at IS NOT NULL AND (
        NOT (
          (
            OLD.state = 'processing' AND NEW.state = 'processing'
            AND NEW.lease_generation IS OLD.lease_generation
            AND NEW.lease_token IS OLD.lease_token
            AND NEW.claimed_at IS OLD.claimed_at
            AND NEW.claimed_until IS OLD.claimed_until
          )
          OR (
            OLD.state = 'processing' AND NEW.state = 'dead_letter'
            AND NEW.lease_generation IS OLD.lease_generation
            AND NEW.lease_token IS NULL
            AND NEW.claimed_at IS NULL
            AND NEW.claimed_until IS NULL
          )
          OR (
            OLD.state = 'dead_letter' AND NEW.state = 'dead_letter'
            AND NEW.lease_generation IS OLD.lease_generation
            AND NEW.lease_token IS OLD.lease_token
            AND NEW.claimed_at IS OLD.claimed_at
            AND NEW.claimed_until IS OLD.claimed_until
          )
        )
      ) THEN RAISE(ABORT, 'outbox terminal reservation lease is immutable') END;
      SELECT CASE WHEN OLD.terminal_reserved_at IS NULL
        AND NEW.terminal_reserved_at IS NOT NULL AND (
          OLD.state <> 'processing'
          OR NEW.state <> 'processing'
          OR OLD.lease_token IS NULL
          OR NEW.lease_token IS NULL
          OR NEW.lease_token IS NOT OLD.lease_token
          OR NEW.lease_generation <> OLD.lease_generation
          OR NEW.terminal_lease_generation <> OLD.lease_generation
          OR NEW.terminal_lease_token IS NOT OLD.lease_token
        ) THEN RAISE(ABORT, 'outbox terminal reservation lost its lease') END;
    END
    """,
    """
    CREATE TRIGGER outbox_receipt_terminal_reservation_delete_t021
    BEFORE DELETE ON infrastructure_outboxconsumerreceipt
    BEGIN
      SELECT RAISE(ABORT, 'outbox consumer receipt is append-only');
    END
    """,
)


POSTGRES_GUARD_SQL = (
    """
    CREATE OR REPLACE FUNCTION outbox_receipt_terminal_reservation_t021_fn()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'outbox consumer receipt is append-only';
      END IF;
      IF (
        NEW.terminal_reserved_at IS NULL AND ROW(
          NEW.terminal_lease_generation,
          NEW.terminal_lease_token,
          NEW.terminal_lease_token_hash,
          NEW.terminal_error_code
        ) IS DISTINCT FROM ROW(0, NULL::uuid, '', '')
      ) OR (
        NEW.terminal_reserved_at IS NOT NULL AND (
          NEW.terminal_lease_generation < 1
          OR NEW.terminal_lease_token IS NULL
          OR NEW.terminal_lease_token_hash !~ '^[0-9a-f]{64}$'
          OR length(NEW.terminal_error_code) < 1
        )
      ) THEN
        RAISE EXCEPTION 'outbox terminal reservation is incomplete';
      END IF;
      IF NEW.state = 'succeeded' AND NEW.terminal_reserved_at IS NOT NULL THEN
        RAISE EXCEPTION 'succeeded receipt cannot be terminal reserved';
      END IF;
      IF TG_OP = 'INSERT' THEN
        IF NEW.terminal_reserved_at IS NOT NULL AND (
          NEW.state <> 'processing'
          OR NEW.terminal_lease_generation <> NEW.lease_generation
          OR NEW.lease_token IS NULL
          OR NEW.terminal_lease_token IS DISTINCT FROM NEW.lease_token
        ) THEN
          RAISE EXCEPTION 'outbox terminal reservation is invalid';
        END IF;
        RETURN NEW;
      END IF;
      IF OLD.terminal_reserved_at IS NOT NULL AND ROW(
        NEW.terminal_reserved_at,
        NEW.terminal_lease_generation,
        NEW.terminal_lease_token,
        NEW.terminal_lease_token_hash,
        NEW.terminal_error_code
      ) IS DISTINCT FROM ROW(
        OLD.terminal_reserved_at,
        OLD.terminal_lease_generation,
        OLD.terminal_lease_token,
        OLD.terminal_lease_token_hash,
        OLD.terminal_error_code
      ) THEN
        RAISE EXCEPTION 'outbox terminal reservation is immutable';
      END IF;
      IF OLD.terminal_reserved_at IS NOT NULL AND (
        NOT (
          (
            OLD.state = 'processing' AND NEW.state = 'processing'
            AND NEW.lease_generation IS NOT DISTINCT FROM OLD.lease_generation
            AND NEW.lease_token IS NOT DISTINCT FROM OLD.lease_token
            AND NEW.claimed_at IS NOT DISTINCT FROM OLD.claimed_at
            AND NEW.claimed_until IS NOT DISTINCT FROM OLD.claimed_until
          )
          OR (
            OLD.state = 'processing' AND NEW.state = 'dead_letter'
            AND NEW.lease_generation IS NOT DISTINCT FROM OLD.lease_generation
            AND NEW.lease_token IS NULL
            AND NEW.claimed_at IS NULL
            AND NEW.claimed_until IS NULL
          )
          OR (
            OLD.state = 'dead_letter' AND NEW.state = 'dead_letter'
            AND NEW.lease_generation IS NOT DISTINCT FROM OLD.lease_generation
            AND NEW.lease_token IS NOT DISTINCT FROM OLD.lease_token
            AND NEW.claimed_at IS NOT DISTINCT FROM OLD.claimed_at
            AND NEW.claimed_until IS NOT DISTINCT FROM OLD.claimed_until
          )
        )
      ) THEN
        RAISE EXCEPTION 'outbox terminal reservation lease is immutable';
      END IF;
      IF OLD.terminal_reserved_at IS NULL
         AND NEW.terminal_reserved_at IS NOT NULL AND (
           OLD.state <> 'processing'
           OR NEW.state <> 'processing'
           OR OLD.lease_token IS NULL
           OR NEW.lease_token IS NULL
           OR NEW.lease_token IS DISTINCT FROM OLD.lease_token
           OR NEW.lease_generation <> OLD.lease_generation
           OR NEW.terminal_lease_generation <> OLD.lease_generation
           OR NEW.terminal_lease_token IS DISTINCT FROM OLD.lease_token
         ) THEN
        RAISE EXCEPTION 'outbox terminal reservation lost its lease';
      END IF;
      RETURN NEW;
    END $$
    """,
    """CREATE TRIGGER outbox_receipt_terminal_reservation_insert_t021 BEFORE INSERT ON infrastructure_outboxconsumerreceipt FOR EACH ROW EXECUTE FUNCTION outbox_receipt_terminal_reservation_t021_fn()""",
    """CREATE TRIGGER outbox_receipt_terminal_reservation_update_t021 BEFORE UPDATE ON infrastructure_outboxconsumerreceipt FOR EACH ROW EXECUTE FUNCTION outbox_receipt_terminal_reservation_t021_fn()""",
    """CREATE TRIGGER outbox_receipt_terminal_reservation_delete_t021 BEFORE DELETE ON infrastructure_outboxconsumerreceipt FOR EACH ROW EXECUTE FUNCTION outbox_receipt_terminal_reservation_t021_fn()""",
)


def install_terminal_reservation_guards(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in {"sqlite", "postgresql"}:
        raise RuntimeError(f"terminal reservation guards do not support {vendor}")
    statements = SQLITE_GUARD_SQL if vendor == "sqlite" else POSTGRES_GUARD_SQL
    with schema_editor.connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def remove_terminal_reservation_guards(apps, schema_editor):
    Receipt = apps.get_model("infrastructure", "OutboxConsumerReceipt")
    alias = schema_editor.connection.alias
    if Receipt.objects.using(alias).filter(
        terminal_reserved_at__isnull=False
    ).exists():
        raise IrreversibleError(
            "outbox terminal reservations cannot be reversed while populated"
        )
    drop_terminal_reservation_guards(schema_editor)


def drop_terminal_reservation_guards(schema_editor):
    vendor = schema_editor.connection.vendor
    with schema_editor.connection.cursor() as cursor:
        if vendor == "sqlite":
            for name in SQLITE_TRIGGER_NAMES:
                cursor.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        elif vendor == "postgresql":
            for name in SQLITE_TRIGGER_NAMES:
                cursor.execute(
                    f'DROP TRIGGER IF EXISTS "{name}" '
                    'ON "infrastructure_outboxconsumerreceipt"'
                )
            cursor.execute(
                "DROP FUNCTION IF EXISTS outbox_receipt_terminal_reservation_t021_fn()"
            )
        else:
            raise RuntimeError(f"terminal reservation guards do not support {vendor}")


class Migration(migrations.Migration):
    dependencies = [("infrastructure", "0002_outboxconsumerreceipt_and_more")]

    operations = [
        migrations.AddField(
            model_name="outboxconsumerreceipt",
            name="terminal_reserved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="outboxconsumerreceipt",
            name="terminal_lease_generation",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="outboxconsumerreceipt",
            name="terminal_lease_token",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="outboxconsumerreceipt",
            name="terminal_lease_token_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="outboxconsumerreceipt",
            name="terminal_error_code",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddConstraint(
            model_name="outboxconsumerreceipt",
            constraint=models.CheckConstraint(
                condition=(
                    Q(
                        terminal_reserved_at__isnull=True,
                        terminal_lease_generation=0,
                        terminal_lease_token__isnull=True,
                        terminal_lease_token_hash="",
                        terminal_error_code="",
                    )
                    | Q(
                        terminal_reserved_at__isnull=False,
                        terminal_lease_generation__gte=1,
                        terminal_lease_token__isnull=False,
                    )
                    & ~Q(terminal_lease_token_hash="")
                    & ~Q(terminal_error_code="")
                ),
                name="ck_outbox_receipt_terminal_reservation_complete",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboxconsumerreceipt",
            constraint=models.CheckConstraint(
                condition=(
                    ~Q(state="succeeded")
                    | Q(terminal_reserved_at__isnull=True)
                ),
                name="ck_outbox_succeeded_receipt_not_terminal_reserved",
            ),
        ),
        migrations.RunPython(
            install_terminal_reservation_guards,
            remove_terminal_reservation_guards,
        ),
    ]
