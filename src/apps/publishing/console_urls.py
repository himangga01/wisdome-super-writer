from django.urls import path

from . import api


app_name = "publishing-console"

urlpatterns = [
    path("targets/<uuid:target_id>/", api.console_target, name="target"),
    path("articles/<uuid:article_id>/", api.console_article, name="article"),
]
