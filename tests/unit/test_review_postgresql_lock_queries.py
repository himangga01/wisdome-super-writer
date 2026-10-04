"""Compile the production locking expressions without opening a database connection."""

import ast
import inspect
import uuid

import pytest
from django.db.backends.postgresql.base import DatabaseWrapper

from apps.collection.models import CollectionRun
from apps.editorial import corrections
from apps.editorial import services as editorial_services
from apps.editorial import tasks as editorial_tasks
from apps.editorial.models import ArticleRevision, DraftArticle
from apps.evidence import services as evidence_services
from apps.evidence import tasks as evidence_tasks
from apps.evidence.models import DocumentExtraction, GenericExtractionAttempt

CASES = [
    (evidence_services, "aggregate_document_extraction", "document"),
    (evidence_services, "aggregate_document_extraction", "evidence"),
    (evidence_tasks, "consume_other_ready", "attempt"),
    (evidence_tasks, "consume_other_ready", "evidence_assets"),
    (evidence_tasks, "finalize_run_evidence", "documents"),
    (evidence_tasks, "finalize_run_evidence", "attempts"),
    (evidence_tasks, "finalize_run_evidence", "locked_evidence"),
    (editorial_tasks, "revalidate_manual_revision", "revision"),
    (editorial_tasks, "finalize_manual_revalidation_delivery_failure", "revision"),
    (editorial_services, "build_source_grounded_draft", "event_revision"),
    (editorial_services, "_validate_revision_publishable_by_id", "revision"),
    (editorial_services, "_validate_revision_publishable_by_id", "placement"),
    (corrections, "decide_correction_case", "case"),
]


def _production_query(module, function_name, binding):
    tree = ast.parse(inspect.getsource(module))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    expressions = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == binding for target in node.targets)
    ]
    if not expressions:
        expressions = [
            node.iter
            for node in ast.walk(function)
            if isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == binding
        ]
    expression = expressions[0]
    if isinstance(expression, ast.IfExp):
        expression = expression.body
    if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name):
        assert expression.func.id == "list"
        expression = expression.args[0]
    if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Attribute):
        if expression.func.attr in {"get", "first"}:
            # Keep the same production joins, filters and lock set; never fetch rows.
            expression.func.attr = "filter"
    identity = uuid.uuid4()
    scope = {
        **vars(module),
        "alias": "default",
        "using": "default",
        "run": CollectionRun(pk=identity),
        "document": DocumentExtraction(pk=identity),
        "attempt": GenericExtractionAttempt(pk=identity),
        "article": DraftArticle(pk=identity),
        "revision": ArticleRevision(pk=identity),
        "lock": True,
        "selected_run_ids": [identity],
        "document_id": identity,
        "generic_extraction_attempt_id": identity,
        "article_revision_id": identity,
        "revision_id": identity,
        "normalized_case_id": identity,
    }
    return eval(compile(ast.Expression(expression), "<production-query>", "eval"), scope)


@pytest.mark.parametrize("module,function_name,binding", CASES)
def test_postgresql_optional_joins_lock_only_owned_rows(module, function_name, binding):
    query = _production_query(module, function_name, binding)
    pg = DatabaseWrapper(
        {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "compile-only",
            "OPTIONS": {},
            "TIME_ZONE": None,
        },
        alias="compile-only",
    )

    def forbid_connection():
        raise AssertionError("Compiler regression must not connect to PostgreSQL")

    pg.ensure_connection = forbid_connection
    pg.get_autocommit = lambda: False
    pg.in_atomic_block = True
    sql, _ = query.query.get_compiler(connection=pg).as_sql()
    assert "LEFT OUTER JOIN" in sql
    lock_tables = [query.model._meta.db_table]
    if module is corrections:
        lock_tables.append(DraftArticle._meta.db_table)
    suffix = "FOR UPDATE OF " + ", ".join(f'"{table}"' for table in lock_tables)
    assert sql.endswith(suffix)
