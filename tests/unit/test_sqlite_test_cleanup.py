from __future__ import annotations

from dataclasses import dataclass, field

import conftest as cleanup
import pytest


class DropFailure(RuntimeError):
    pass


class FlushFailure(RuntimeError):
    pass


class RestoreFailure(RuntimeError):
    pass


@dataclass
class FakeCursor:
    rows: tuple[tuple[str, str], ...]
    fail_drop: str | None = None
    fail_restore: bool = False
    dropped: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str) -> FakeCursor:
        if statement.startswith("SELECT name, sql"):
            return self
        if statement.startswith("DROP TRIGGER"):
            name = statement.removeprefix("DROP TRIGGER ").strip('"')
            if name == self.fail_drop:
                raise DropFailure(f"drop failed: {name}")
            self.dropped.append(name)
            return self
        if self.fail_restore:
            raise RestoreFailure("restore failed")
        self.restored.append(statement)
        return self

    def fetchall(self) -> tuple[tuple[str, str], ...]:
        return self.rows


@dataclass
class FakeConnection:
    cursor_value: FakeCursor
    vendor: str = "sqlite"

    class Ops:
        @staticmethod
        def quote_name(value: str) -> str:
            return f'"{value}"'

    ops = Ops()

    def cursor(self) -> FakeCursor:
        return self.cursor_value


class FailingConnections(dict[str, FakeConnection]):
    def __init__(self, *args, fail_alias: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_alias = fail_alias

    def __getitem__(self, alias: str) -> FakeConnection:
        if alias == self.fail_alias:
            raise RuntimeError(f"alias failed: {alias}")
        return super().__getitem__(alias)


@dataclass
class FakeTestCase:
    aliases: tuple[str, ...]

    def _databases_names(self, *, include_mirrors: bool) -> tuple[str, ...]:
        assert include_mirrors is False
        return self.aliases


def _rows() -> tuple[tuple[str, str], ...]:
    return (
        ("guard_one", "CREATE TRIGGER guard_one AFTER INSERT ON one BEGIN SELECT 1; END"),
        ("guard_two", "CREATE TRIGGER guard_two AFTER INSERT ON two BEGIN SELECT 1; END"),
    )


def test_partial_drop_restores_each_successfully_dropped_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeCursor(_rows(), fail_drop="guard_two")
    monkeypatch.setattr(cleanup, "connections", FailingConnections(default=FakeConnection(cursor)))
    monkeypatch.setattr(cleanup, "_restore_migration_leaves", lambda _case: None)

    with pytest.raises(DropFailure, match="guard_two"):
        cleanup._fixture_teardown_with_sqlite_trigger_suspension(
            FakeTestCase(("default",))
        )

    assert cursor.dropped == ["guard_one"]
    assert cursor.restored == [_rows()[0][1]]


def test_later_alias_failure_restores_prior_alias_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeCursor((_rows()[0],))
    connections = FailingConnections(
        {"default": FakeConnection(cursor)},
        fail_alias="later",
    )
    monkeypatch.setattr(cleanup, "connections", connections)
    monkeypatch.setattr(cleanup, "_restore_migration_leaves", lambda _case: None)

    with pytest.raises(RuntimeError, match="alias failed: later"):
        cleanup._fixture_teardown_with_sqlite_trigger_suspension(
            FakeTestCase(("default", "later"))
        )

    assert cursor.dropped == ["guard_one"]
    assert cursor.restored == [_rows()[0][1]]


def test_flush_failure_is_preserved_when_trigger_restore_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeCursor((_rows()[0],), fail_restore=True)
    monkeypatch.setattr(cleanup, "connections", FailingConnections(default=FakeConnection(cursor)))
    monkeypatch.setattr(cleanup, "_restore_migration_leaves", lambda _case: None)

    def fail_flush(_case: object) -> None:
        raise FlushFailure("flush failed")

    monkeypatch.setattr(cleanup, "_django_fixture_teardown", fail_flush)

    with pytest.raises(FlushFailure, match="flush failed") as raised:
        cleanup._fixture_teardown_with_sqlite_trigger_suspension(
            FakeTestCase(("default",))
        )

    assert any("restore failed" in note for note in raised.value.__notes__)


def test_restore_failure_is_raised_when_drop_and_flush_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeCursor((_rows()[0],), fail_restore=True)
    monkeypatch.setattr(cleanup, "connections", FailingConnections(default=FakeConnection(cursor)))
    monkeypatch.setattr(cleanup, "_restore_migration_leaves", lambda _case: None)
    monkeypatch.setattr(cleanup, "_django_fixture_teardown", lambda _case: None)

    with pytest.raises(RestoreFailure, match="restore failed"):
        cleanup._fixture_teardown_with_sqlite_trigger_suspension(
            FakeTestCase(("default",))
        )
