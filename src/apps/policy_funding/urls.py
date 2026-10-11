from django.urls import path

from apps.policy_funding import views

urlpatterns = [
    path("", views.index, name="policy-funding-index"),
    path("guide/", views.guide, name="policy-funding-guide"),
    path("glossary/", views.glossary, name="policy-funding-glossary"),
    path("history/", views.history, name="policy-funding-history"),
    path("sources/", views.sources, name="policy-funding-sources"),
    path("notices/<str:notice_id>/", views.detail, name="policy-funding-detail"),
    path("runs/<str:run_name>/", views.run_detail, name="policy-funding-run"),
]
