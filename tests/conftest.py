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


def _suspend_sqlite_triggers(test_case: TransactionTestCase) -> dict[str, tuple[str, ...]]:
    suspended: dict[str, tuple[str, ...]] = {}
    for alias in test_case._databases_names(include_mirrors=False):
        connection = connections[alias]
        if connection.vendor != "sqlite":
            continue
        with connection.cursor() as cursor:
            rows = cursor.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND sql IS NOT NULL ORDER BY name"
            ).fetchall()
            for name, _sql in rows:
                cursor.execute(f"DROP TRIGGER {connection.ops.quote_name(name)}")
        suspended[alias] = tuple(sql for _name, sql in rows)
    return suspended


def _restore_sqlite_triggers(suspended: dict[str, tuple[str, ...]]) -> None:
    for alias, statements in suspended.items():
        with connections[alias].cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)


def _fixture_teardown_with_sqlite_trigger_suspension(
    test_case: TransactionTestCase,
) -> None:
    _restore_migration_leaves(test_case)
    suspended = _suspend_sqlite_triggers(test_case)
    try:
        _django_fixture_teardown(test_case)
    finally:
        _restore_sqlite_triggers(suspended)


TransactionTestCase._fixture_teardown = _fixture_teardown_with_sqlite_trigger_suspension
