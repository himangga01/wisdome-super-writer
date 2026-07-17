from django.urls import path

from . import api


app_name = "publishing"

urlpatterns = [
    path("targets", api.targets, name="targets"),
    path("targets/<uuid:target_id>", api.target_detail, name="target-detail"),
    path("targets/<uuid:target_id>/preflight", api.target_preflight, name="target-preflight"),
    path("targets/<uuid:target_id>/canary", api.target_canary, name="target-canary"),
    path(
        "targets/<uuid:target_id>/auto-publish-validations",
        api.auto_publish_validations,
        name="auto-validations",
    ),
    path(
        "targets/<uuid:target_id>/auto-publish-validations/<uuid:validation_id>/decisions",
        api.auto_publish_validation_decisions,
        name="auto-validation-decisions",
    ),
    path(
        "targets/<uuid:target_id>/auto-publish-validations/<uuid:validation_id>/report",
        api.auto_publish_validation_report,
        name="auto-validation-report",
    ),
    path("targets/<uuid:target_id>/auto-publish", api.target_auto_publish, name="auto-publish"),
    path("targets/<uuid:target_id>/connection", api.target_connection, name="target-connection"),
    path("targets/<uuid:target_id>/oauth/start", api.target_oauth_start, name="target-oauth-start"),
    path(
        "publishing/oauth/google/callback",
        api.blogger_oauth_callback,
        name="blogger-oauth-callback",
    ),
    path(
        "articles/<uuid:article_id>/publication-intents",
        api.publication_intents,
        name="publication-intents",
    ),
    path("articles/<uuid:article_id>/preview", api.article_preview, name="article-preview"),
    path("articles/<uuid:article_id>/approvals", api.approvals, name="approvals"),
    path("articles/<uuid:article_id>/publish", api.publish, name="publish"),
    path(
        "articles/<uuid:article_id>/publications",
        api.article_publications,
        name="article-publications",
    ),
    path(
        "publications/<uuid:publication_id>/attempts",
        api.publication_attempts,
        name="publication-attempts",
    ),
    path(
        "publication-attempts/<uuid:attempt_id>/retry",
        api.retry_publication_attempt,
        name="retry-publication-attempt",
    ),
    path(
        "corrections/<uuid:correction_id>/prepare-publication",
        api.prepare_correction,
        name="prepare-correction",
    ),
]
