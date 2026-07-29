from importlib import import_module

from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from wisdome_writer.api.health import live, ready
from wisdome_writer import console

admin.site.site_header = "Wisdome Super Writer"
admin.site.site_title = "Wisdome Super Writer Admin"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="console-home", permanent=False)),
    path("admin/", admin.site.urls),
    path("console/", console.home, name="console-home"),
    path("console/runs/", console.runs, name="console-runs"),
    path("console/runs/<uuid:run_id>/", console.run_detail, name="console-run-detail"),
    path("console/articles/", console.articles, name="console-articles"),
    path("console/sources/", console.sources, name="console-sources"),
    path("console/publishing/", console.publishing, name="console-publishing"),
    path("console/publishing/", include("apps.publishing.console_urls")),
    path("console/operations/", console.operations, name="console-operations"),
    path("health/live", live, name="health-live"),
    path("health/ready", ready, name="health-ready"),
    path("api/v1/", include("wisdome_writer.api.urls")),
    path("api/v1/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.audit.urls")),
]

for app_name in ("topics", "collection", "evidence", "editorial", "publishing", "scheduling"):
    module_name = f"apps.{app_name}.urls"
    try:
        import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
    else:
        urlpatterns.append(path("api/v1/", include(module_name)))

handler400 = "wisdome_writer.api.problems.handler400"
handler403 = "wisdome_writer.api.problems.handler403"
handler404 = "wisdome_writer.api.problems.handler404"
handler500 = "wisdome_writer.api.problems.handler500"
