from django.urls import path

from . import api

urlpatterns = [
    path("extraction-profiles", api.extraction_profiles, name="extraction-profiles"),
    path("extraction-profiles/<uuid:profile_id>", api.extraction_profile_detail, name="extraction-profile-detail"),
    path(
        "extraction-profiles/<uuid:profile_id>/decisions",
        api.extraction_profile_decisions,
        name="extraction-profile-decisions",
    ),
    path(
        "extraction-profiles/<uuid:profile_id>/report",
        api.extraction_profile_report,
        name="extraction-profile-report",
    ),
    path("runs/<uuid:run_id>/evidence", api.run_evidence, name="run-evidence"),
    path("evidence/<uuid:evidence_id>", api.evidence_detail, name="evidence-detail"),
    path(
        "evidence/<uuid:evidence_id>/review-decisions",
        api.evidence_review_decisions,
        name="evidence-review-decisions",
    ),
    path(
        "document-extractions/<uuid:document_extraction_id>",
        api.document_extraction_detail,
        name="document-extraction-detail",
    ),
]

