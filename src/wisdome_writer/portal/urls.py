from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="portal-home"),
    path("housing/", views.housing, name="housing-overview"),
    path("housing/notices/", views.housing, {"section": "notices"}, name="housing-notices"),
    path("housing/sources/", views.housing, {"section": "sources"}, name="housing-sources"),
    path("semiconductor/", views.semiconductor, name="semiconductor-overview"),
    path(
        "semiconductor/news/", views.semiconductor, {"section": "news"}, name="semiconductor-news"
    ),
    path(
        "semiconductor/articles/",
        views.semiconductor,
        {"section": "articles"},
        name="semiconductor-articles",
    ),
    path(
        "semiconductor/sources/",
        views.semiconductor,
        {"section": "sources"},
        name="semiconductor-sources",
    ),
]
