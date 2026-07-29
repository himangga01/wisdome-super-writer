from django.contrib import admin

from .models import (
    DocumentExtraction,
    EvidenceAsset,
    EvidenceAuditSnapshot,
    EvidenceReviewDecision,
    ExtractionProfileDecision,
    ExtractionProfileSnapshot,
    ExtractionRun,
    GenericExtractionAttempt,
)


class ReadOnlyEvidenceAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ExtractionProfileSnapshot)
class ExtractionProfileSnapshotAdmin(ReadOnlyEvidenceAdmin):
    list_display = ("profile_key", "profile_version", "engine", "approval_state", "created_at")
    list_filter = ("engine", "approval_state")
    search_fields = ("profile_key", "profile_material_hash")


@admin.register(ExtractionProfileDecision, EvidenceAuditSnapshot, EvidenceReviewDecision)
class AppendOnlyEvidenceAdmin(ReadOnlyEvidenceAdmin):
    pass


@admin.register(DocumentExtraction)
class DocumentExtractionAdmin(ReadOnlyEvidenceAdmin):
    list_display = ("id", "source_item", "input_kind", "state", "document_complete", "created_at")
    list_filter = ("input_kind", "state", "document_complete")
    readonly_fields = ("coverage_manifest_hash", "selected_evidence_manifest_hash")


@admin.register(ExtractionRun)
class ExtractionRunAdmin(ReadOnlyEvidenceAdmin):
    list_display = ("id", "document_extraction", "engine", "state", "profile_key", "created_at")
    list_filter = ("engine", "state")


@admin.register(GenericExtractionAttempt)
class GenericExtractionAttemptAdmin(ReadOnlyEvidenceAdmin):
    list_display = ("id", "source_item", "engine", "validation_mode", "state", "created_at")
    list_filter = ("engine", "validation_mode", "state")


@admin.register(EvidenceAsset)
class EvidenceAssetAdmin(ReadOnlyEvidenceAdmin):
    list_display = ("id", "source_item", "derivation_type", "kind", "review_state", "publishable")
    list_filter = ("derivation_type", "kind", "rights_status", "review_state", "publishable")
    search_fields = ("source_item__title", "evidence_content_hash", "review_subject_hash")

