from __future__ import annotations

from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_django_fixture_teardown = TransactionTestCase._fixture_teardown


def _restore_migration_leaves(test_case: TransactionTestCase) -> None:
    for alias in test_case._databases_names(include_mirrors=False):
        connection = connections[alias]
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        if executor.migration_plan(leaves):
            executor.migrate(leaves)


def _drop_sqlite_triggers(
    test_case: TransactionTestCase,
    dropped: list[tuple[object, str]],
) -> None:
    for alias in test_case._databases_names(include_mirrors=False):
        connection = connections[alias]
        if connection.vendor != "sqlite":
            continue
        with connection.cursor() as cursor:
            rows = cursor.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND sql IS NOT NULL ORDER BY name"
            ).fetchall()
            for name, sql in rows:
                cursor.execute(f"DROP TRIGGER {connection.ops.quote_name(name)}")
                dropped.append((connection, sql))


def _restore_sqlite_triggers(
    dropped: list[tuple[object, str]],
) -> list[BaseException]:
    failures: list[BaseException] = []
    for connection, statement in dropped:
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement)
        except BaseException as exc:  # noqa: BLE001 - cleanup must preserve every primary failure
            failures.append(exc)
    return failures


def _raise_with_restore_failures(
    primary: BaseException | None,
    restore_failures: list[BaseException],
) -> None:
    if primary is not None:
        for failure in restore_failures:
            primary.add_note(
                "SQLite trigger restoration also failed: "
                f"{failure.__class__.__name__}: {failure}"
            )
        raise primary.with_traceback(primary.__traceback__)
    if restore_failures:
        first, *remaining = restore_failures
        for failure in remaining:
            first.add_note(
                "Additional SQLite trigger restoration failure: "
                f"{failure.__class__.__name__}: {failure}"
            )
        raise first.with_traceback(first.__traceback__)


def _fixture_teardown_with_sqlite_trigger_suspension(
    test_case: TransactionTestCase,
) -> None:
    _restore_migration_leaves(test_case)
    dropped: list[tuple[object, str]] = []
    primary: BaseException | None = None
    try:
        _drop_sqlite_triggers(test_case, dropped)
        _django_fixture_teardown(test_case)
    except BaseException as exc:  # noqa: BLE001 - preserve drop/flush over cleanup failures
        primary = exc
    restore_failures = _restore_sqlite_triggers(dropped)
    _raise_with_restore_failures(primary, restore_failures)


TransactionTestCase._fixture_teardown = _fixture_teardown_with_sqlite_trigger_suspension
