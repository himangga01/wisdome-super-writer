from django.urls import path

from . import api

urlpatterns = [
    path("articles", api.articles, name="articles"),
    path("articles/<uuid:article_id>", api.article_detail, name="article-detail"),
    path("articles/<uuid:article_id>/revisions", api.revise_article, name="article-revise"),
    path(
        "articles/<uuid:article_id>/corrections",
        api.article_corrections,
        name="article-corrections",
    ),
    path(
        "corrections/<uuid:correction_id>/decisions",
        api.correction_decisions,
        name="correction-decisions",
    ),
]
