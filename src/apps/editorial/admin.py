from django.contrib import admin

from .models import ArticleRevision, Claim, ClaimEvidence, CorrectionCase, DraftArticle, EventCluster, GenerationAttempt, QualityCheck, VisualizationRender

admin.site.register([DraftArticle, ArticleRevision, GenerationAttempt, Claim, ClaimEvidence, QualityCheck, VisualizationRender, EventCluster, CorrectionCase])
