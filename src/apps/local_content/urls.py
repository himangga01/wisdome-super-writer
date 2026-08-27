from django.urls import path

from apps.local_content import views

urlpatterns = [
    path("local-articles/", views.local_article_index, name="local-article-index"),
    path(
        "local-articles/assets/<str:run_name>/<str:article_name>/<str:checksum>/<path:asset_path>",
        views.local_article_asset,
        name="local-article-asset",
    ),
    path(
        "local-articles/<str:run_name>/",
        views.local_article_run,
        name="local-article-run",
    ),
    path(
        "local-articles/<str:run_name>/<str:article_name>/",
        views.local_article_detail,
        name="local-article-detail",
    ),
    path(
        "api/v1/local-articles/status",
        views.local_article_status,
        name="local-article-status",
    ),
]
