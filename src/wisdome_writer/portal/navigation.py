"""Display-only navigation; never reads topic storage or makes network requests."""

from django.conf import settings

TOPICS = (
    ("housing", "주택 청약", "/housing/", "집을 찾는 정보", "01"),
    ("semiconductor", "반도체 뉴스", "/semiconductor/", "산업의 흐름을 읽는 정보", "02"),
    ("funding", "정책자금", "/policy-funding/", "사업을 준비하는 정보", "03"),
)
MENUS = {
    "housing": (
        ("대시보드", "/housing/", "overview"),
        ("청약 공고", "/housing/notices/", "notices"),
        ("작성된 콘텐츠", "/local-articles/", "history"),
        ("공식 출처", "/housing/sources/", "sources"),
    ),
    "semiconductor": (
        ("대시보드", "/semiconductor/", "overview"),
        ("수집한 뉴스", "/semiconductor/news/", "news"),
        ("콘텐츠 초안", "/semiconductor/articles/", "articles"),
        ("출처 디렉터리", "/semiconductor/sources/", "sources"),
    ),
    "funding": (
        ("지원 공고", "/policy-funding/", "notices"),
        ("처음 배우기", "/policy-funding/guide/", "guide"),
        ("쉬운 용어 사전", "/policy-funding/glossary/", "glossary"),
        ("공식 기관·출처", "/policy-funding/sources/", "sources"),
        ("수집 기록", "/policy-funding/history/", "history"),
    ),
    "home": (("포털 홈", "/", "overview"),),
}


def portal_navigation(request):
    path = request.path
    topic_key = "home"
    for key, _name, prefix, _caption, _number in TOPICS:
        if path.startswith(prefix):
            topic_key = key
    if path.startswith("/local-articles/"):
        topic_key = "housing"
    menus = MENUS[topic_key]
    active = menus[0][2]
    for _label, href, key in menus[1:]:
        if path.startswith(href):
            active = key
    if path.startswith("/policy-funding/runs/"):
        active = "history"
    title = next((row[1] for row in TOPICS if row[0] == topic_key), "콘텐츠 포털")
    return {
        "portal": {
            "topic_key": topic_key,
            "topic_title": title,
            "local_runtime": settings.IS_LOCAL_RUNTIME,
            "topics": [
                dict(
                    key=k,
                    name=n,
                    href=h,
                    caption=c,
                    number=i,
                    active=k == topic_key,
                    available=k != "housing" or settings.IS_LOCAL_RUNTIME,
                )
                for k, n, h, c, i in TOPICS
            ],
            "menu": [
                dict(label=label, href=href, key=key, active=key == active)
                for label, href, key in menus
            ],
            "section_title": next(label for label, _href, key in menus if key == active),
        },
    }
