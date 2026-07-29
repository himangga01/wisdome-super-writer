from django.db import migrations


LEGACY_AMBIGUOUS_CODES = {
    "legacy_reconcile_event_missing",
    "legacy_reconcile_generation_ambiguous",
}
SUCCESS_PUBLICATION_STATE_BY_ACTION = {
    "create": "published",
    "update": "published",
    "mark_withdrawn": "marked_withdrawn",
    "unpublish": "withdrawn",
}


def _has_durable_success_proof(
    Intent,
    Generation,
    attempt,
    publication,
    *,
    db_alias,
):
    generation_success = (
        Generation.objects.using(db_alias)
        .filter(
            publication_attempt_id=attempt.id,
            state="completed",
            result_state="succeeded",
        )
        .exclude(result_identity="")
        .exclude(result_identity__isnull=True)
        .exists()
    )
    if generation_success:
        return True
    if (
        attempt.finished_at is None
        or publication.last_success_at is None
        or attempt.finished_at != publication.last_success_at
    ):
        return False
    revision_no = (
        Intent.objects.using(db_alias)
        .filter(pk=attempt.publication_intent_id)
        .values_list("revision_no", flat=True)
        .first()
    )
    if (
        revision_no is None
        or revision_no != publication.published_revision_no
    ):
        return False
    if attempt.resolved_action == "unpublish":
        return bool(publication.remote_post_id) or (
            publication.remote_state in {"withdrawn", "deleted"}
        )
    return bool(publication.remote_post_id)


def restore_legacy_terminal_projection(apps, schema_editor):
    Attempt = apps.get_model("publishing", "PublicationAttempt")
    Publication = apps.get_model("publishing", "Publication")
    Intent = apps.get_model("publishing", "PublicationIntent")
    Generation = apps.get_model(
        "publishing",
        "PublicationReconcileGeneration",
    )
    db_alias = schema_editor.connection.alias

    # This successor corrects terminal states left by already-applied 0005
    # migrations. It does not repeat the broader generation repair.
    legacy_attempts = (
        Attempt.objects.using(db_alias)
        .select_for_update()
        .filter(
            state="manual_required",
            error_code__in=LEGACY_AMBIGUOUS_CODES,
        )
        .order_by("created_at", "id")
    )
    for attempt in legacy_attempts.iterator():
        publication = (
            Publication.objects.using(db_alias)
            .select_for_update()
            .get(pk=attempt.publication_id)
        )
        if not _has_durable_success_proof(
            Intent,
            Generation,
            attempt,
            publication,
            db_alias=db_alias,
        ):
            continue
        Attempt.objects.using(db_alias).filter(
            pk=attempt.id,
            state="manual_required",
            error_code__in=LEGACY_AMBIGUOUS_CODES,
        ).update(
            state="succeeded",
            error_code="",
            error_detail_redacted="",
        )

    # Older restored successes are deliberately ignored here. Only the latest
    # attempt may replace a legacy manual-required Publication projection.
    contaminated_publications = (
        Publication.objects.using(db_alias)
        .select_for_update()
        .filter(
            state="manual_required",
            last_error_code__in=LEGACY_AMBIGUOUS_CODES,
        )
        .order_by("id")
    )
    for publication in contaminated_publications.iterator():
        latest = (
            Attempt.objects.using(db_alias)
            .select_for_update()
            .filter(publication_id=publication.id)
            .order_by("-created_at", "-id")
            .first()
        )
        if latest is None:
            continue
        if latest.state == "succeeded":
            publication_state = SUCCESS_PUBLICATION_STATE_BY_ACTION.get(
                latest.resolved_action
            )
            if publication_state is None:
                continue
            Publication.objects.using(db_alias).filter(
                pk=publication.id,
                state="manual_required",
                last_error_code__in=LEGACY_AMBIGUOUS_CODES,
            ).update(
                state=publication_state,
                last_error_code="",
            )
        elif latest.state == "permanent_failed":
            Publication.objects.using(db_alias).filter(
                pk=publication.id,
                state="manual_required",
                last_error_code__in=LEGACY_AMBIGUOUS_CODES,
            ).update(
                state="permanent_failed",
                last_error_code=latest.error_code,
            )
        elif (
            latest.state == "manual_required"
            and bool(latest.error_code)
            and latest.error_code not in LEGACY_AMBIGUOUS_CODES
        ):
            Publication.objects.using(db_alias).filter(
                pk=publication.id,
                state="manual_required",
                last_error_code__in=LEGACY_AMBIGUOUS_CODES,
            ).update(
                state="manual_required",
                last_error_code=latest.error_code,
            )
        # Stale, legacy-manual without proof, and nonterminal latest attempts
        # intentionally retain the fail-closed legacy Publication projection.


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0005_repair_ambiguous_reconcile_generations"),
    ]

    operations = [
        migrations.RunPython(
            restore_legacy_terminal_projection,
            migrations.RunPython.noop,
        ),
    ]
