from django.contrib import admin

from .models import ArticleRevision, Claim, ClaimEvidence, CorrectionCase, CorrectionDecision, DraftArticle, EventCluster, GenerationAttempt, QualityCheck, VisualizationRender


class ReadOnlyEditorialAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(
    [
        DraftArticle,
        ArticleRevision,
        GenerationAttempt,
        Claim,
        ClaimEvidence,
        QualityCheck,
        VisualizationRender,
        EventCluster,
        CorrectionCase,
        CorrectionDecision,
    ],
    ReadOnlyEditorialAdmin,
)
