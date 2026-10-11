"""Local semiconductor metadata collection boundaries and archive integrity."""

import hashlib
import importlib
import importlib.util
import json
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

NOW = datetime(2026, 10, 8, 2, tzinfo=UTC)
RSS = b"""<rss><channel><item><title>HBM chip release</title>
<link>https://news.samsung.com/global/hbm-chip-release</link>
<pubDate>Thu, 01 Oct 2026 00:00:00 +0900</pubDate>
<description>Full body must never be stored</description></item>
<item><title>Unknown date</title><link>https://news.samsung.com/global/unknown</link></item>
<item><title>Old chip</title><link>https://news.samsung.com/global/old</link>
<pubDate>Wed, 30 Sep 2026 00:00:00 +0900</pubDate></item></channel></rss>"""
SIA = b"""<div class="resource-item"><a href="/chip-update/"><h3>Industry chips</h3></a>
<div class="resource-item-meta">October 2, 2026</div></div>"""
MOTIR = """<span class="board-count">2</span><table id="mytable"><thead>
<tr><th>제목</th><th>등록일</th></tr></thead><tbody>
<tr><td><a onclick="article.view('101')">반도체 투자 발표</a></td><td>2026-10-03</td></tr>
<tr><td><a onclick="article.view('102')">섬유 산업 발표</a></td><td>2026-10-03</td></tr>
</tbody></table>""".encode()


def _module():
    assert importlib.util.find_spec("wisdome_writer.portal.news_archive") is not None
    return importlib.import_module("wisdome_writer.portal.news_archive")


class FixtureFetcher:
    def __init__(self, source, **kwargs):
        self.source = source
        self.request_count = 0

    def get(self, url, *, kind):
        self.request_count += 1
        content = {
            "motie": MOTIR,
            "samsung_global_newsroom": RSS,
            "semiconductor_industry_association": SIA,
        }[self.source["key"]]
        return SimpleNamespace(content=content, url=url, fetched_at=NOW)

    def close(self):
        pass


def _collect(root, **kwargs):
    return _module().collect_news_archive(
        root, 2026, 10, now=NOW, fetcher_factory=FixtureFetcher, **kwargs
    )


def _period(basis="published", month=10):
    from wisdome_writer.portal.periods import PeriodSelection

    return PeriodSelection(2026, month, basis=basis)


def test_fixture_collection_saves_metadata_and_truthful_source_outcomes(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    run = _collect(tmp_path)
    saved = _module().read_news_archive(tmp_path, run.name)
    assert saved["mode"] == "fixture"
    assert saved["coverage_limited"] is True
    assert len(saved["records"]) == 4
    assert {row["title"] for row in saved["records"]} == {
        "반도체 투자 발표",
        "HBM chip release",
        "Unknown date",
        "Industry chips",
    }
    assert all(row["provenance"] == "fixture" for row in saved["records"])
    assert all(
        set(row)
        == {
            "source_id",
            "title",
            "url",
            "publisher",
            "published_at",
            "collected_at",
            "rights_status",
            "provenance",
        }
        for row in saved["records"]
    )
    outcomes = {row["source_id"]: row for row in saved["source_outcomes"]}
    assert outcomes["skhynix_newsroom"]["status"] == "disabled"
    assert outcomes["skhynix_newsroom"]["request_count"] == 0
    assert outcomes["krx_kind"]["status"] == "unsupported"
    assert outcomes["krx_kind"]["request_count"] == 0
    assert "Full body" not in run.joinpath("notices.json").read_text(encoding="utf-8")
    assert saved["window"]["end"] == NOW.isoformat()


def test_captured_motir_return_false_identity_is_normalized_without_following_cosmetic_query(
    tmp_path, settings
):
    settings.IS_LOCAL_RUNTIME = True

    class CurrentMotir(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            if self.source["key"] == "motie":
                board = url.split("?", 1)[0]
                page.content = MOTIR.replace(
                    b'''onclick="article.view('101')"''',
                    (
                        f'''href="{board}/101/view?mno=&amp;pageIndex=1" '''
                        '''onclick="article.view('101'); return false;"'''
                    ).encode(),
                )
            return page

    run = _module().collect_news_archive(tmp_path, 2026, 10, now=NOW, fetcher_factory=CurrentMotir)
    records = _module().read_news_archive(tmp_path, run.name)["records"]
    motir = [row for row in records if row["source_id"] == "motie"]
    assert len(motir) == 1
    assert motir[0]["url"].endswith("/101/view")
    assert "?" not in motir[0]["url"]


def test_motir_click_wrapper_inside_title_markup_is_still_checked(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True

    class NestedMotir(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            if self.source["key"] == "motie":
                board = url.split("?", 1)[0]
                page.content = MOTIR.replace(
                    b"""<a onclick="article.view('101')">""",
                    (
                        f'''<span><a href="{board}/101/view?mno=&amp;pageIndex=1" '''
                        """onclick="article.view('101'); return false;">"""
                    ).encode(),
                ).replace("반도체 투자 발표</a>".encode(), "반도체 투자 발표</a></span>".encode())
            return page

    run = _module().collect_news_archive(tmp_path, 2026, 10, now=NOW, fetcher_factory=NestedMotir)
    records = _module().read_news_archive(tmp_path, run.name)["records"]
    assert sum(row["source_id"] == "motie" for row in records) == 1


@pytest.mark.parametrize(
    "target,handler",
    [
        (
            "/kor/article/ATCL3f49a5a8c/999/view?mno=&pageIndex=1",
            "article.view('101'); return false;",
        ),
        (
            "/kor/article/ATCL3f49a5a8c/101/view?mno=x&pageIndex=1",
            "article.view('101'); return false;",
        ),
        (
            "/kor/article/ATCL3f49a5a8c/101/view?mno=&pageIndex=2",
            "article.view('101'); return false;",
        ),
        ("/kor/article/ATCL3f49a5a8c/101/view?unexpected=1", "article.view('101'); return false;"),
        ("/kor/article/ATCL3f49a5a8c/101/view", "article.view('101'); otherFunction();"),
    ],
)
def test_motir_wrapper_cannot_accept_mismatched_identity_or_arbitrary_query(
    tmp_path, settings, target, handler
):
    settings.IS_LOCAL_RUNTIME = True

    class UnsafeMotir(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            if self.source["key"] == "motie":
                page.content = MOTIR.replace(
                    b'''onclick="article.view('101')"''',
                    (f'''href="{target.replace("&", "&amp;")}" onclick="{handler}"''').encode(),
                )
            return page

    run = _module().collect_news_archive(tmp_path, 2026, 10, now=NOW, fetcher_factory=UnsafeMotir)
    saved = _module().read_news_archive(tmp_path, run.name)
    assert not any(row["source_id"] == "motie" for row in saved["records"])
    assert (
        next(row for row in saved["source_outcomes"] if row["source_id"] == "motie")["status"]
        == "failed"
    )


def test_publication_month_discloses_unknown_dates_without_assigning_capture_month(
    tmp_path, settings
):
    settings.IS_LOCAL_RUNTIME = True
    _collect(tmp_path)
    data = _module().load_news_archive(tmp_path, period=_period())
    assert data["unknown_date_count"] == 1
    assert data["total"] == 3
    assert "발표일 미상" in data["message"]
    collected = _module().load_news_archive(tmp_path, period=_period("collected"))
    assert collected["total"] == 4
    assert collected["unknown_date_count"] == 1


def test_metadata_only_samsung_feed_ignores_mime_free_media_without_weakening_shared_parser(
    tmp_path, settings
):
    from adapters.sources.http import SourceSchemaError
    from adapters.sources.semiconductor.common import feed_entries

    settings.IS_LOCAL_RUNTIME = True
    payload = b"""<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item>
<title>October HBM announcement</title><link>https://news.samsung.com/global/october-hbm</link>
<pubDate>Thu, 01 Oct 2026 00:00:00 +0900</pubDate>
<media:content url="https://img.global.news.samsung.com/public-photo.jpg" medium="image"/>
<enclosure url="https://img.global.news.samsung.com/public-photo.jpg"/>
<description>Full article must not be archived</description></item></channel></rss>"""
    with pytest.raises(SourceSchemaError, match="enclosure"):
        feed_entries(payload)

    class MediaFeed(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            if self.source["key"] == "samsung_global_newsroom":
                page.content = payload
            return page

    run = _module().collect_news_archive(tmp_path, 2026, 10, now=NOW, fetcher_factory=MediaFeed)
    saved = _module().read_news_archive(tmp_path, run.name)
    samsung = [row for row in saved["records"] if row["source_id"] == "samsung_global_newsroom"]
    assert len(samsung) == 1
    assert samsung[0]["title"] == "October HBM announcement"
    assert samsung[0]["published_at"] == "2026-09-30T15:00:00+00:00"
    assert "public-photo" not in (run / "notices.json").read_text(encoding="utf-8")
    assert "Full article" not in (run / "notices.json").read_text(encoding="utf-8")


def test_metadata_only_atom_feed_ignores_enclosure_link_but_requires_original_link(
    tmp_path, settings
):
    settings.IS_LOCAL_RUNTIME = True
    payload = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom chip</title>
<link href="https://news.samsung.com/global/atom-chip" rel="alternate"/>
<link href="https://img.global.news.samsung.com/media.jpg" rel="enclosure"/>
<published>2026-10-02T00:00:00Z</published><content>Hidden body</content></entry></feed>"""

    class AtomFeed(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            if self.source["key"] == "samsung_global_newsroom":
                page.content = payload
            return page

    run = _module().collect_news_archive(tmp_path, 2026, 10, now=NOW, fetcher_factory=AtomFeed)
    saved = _module().read_news_archive(tmp_path, run.name)
    assert [
        row["url"] for row in saved["records"] if row["source_id"] == "samsung_global_newsroom"
    ] == ["https://news.samsung.com/global/atom-chip"]


@pytest.mark.parametrize("label", ["Press Release", "Blog"])
def test_exact_sia_content_label_and_date_is_metadata_normalized(tmp_path, settings, label):
    from lxml import html

    from adapters.sources.http import SourceSchemaError
    from adapters.sources.semiconductor.common import parse_sia_list

    settings.IS_LOCAL_RUNTIME = True
    payload = (
        f"""<div class="resource-item"><a href="/october-chip/"><h3>October chip</h3></a>
<div class="resource-item-meta">{label}: 10/05/26</div></div>"""
    ).encode()
    with pytest.raises(SourceSchemaError, match="date"):
        parse_sia_list(html.fromstring(payload))

    class LabelledSia(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            if self.source["key"] == "semiconductor_industry_association":
                page.content = payload
            return page

    run = _module().collect_news_archive(tmp_path, 2026, 10, now=NOW, fetcher_factory=LabelledSia)
    saved = _module().read_news_archive(tmp_path, run.name)
    sia = [
        row for row in saved["records"] if row["source_id"] == "semiconductor_industry_association"
    ]
    assert len(sia) == 1
    assert sia[0]["published_at"] == "2026-10-05T00:00:00+00:00"
    assert sia[0]["rights_status"] == "internal_analysis_only"


@pytest.mark.parametrize(
    "date_text",
    [
        "Other: 10/05/26",
        "Press Release: 10/05/26 and 10/06/26",
        "Blog: 13/40/26",
        "Blog: 10/05/26 extra",
    ],
)
def test_sia_metadata_normalization_rejects_unrecognized_or_ambiguous_date(
    tmp_path, settings, date_text
):
    settings.IS_LOCAL_RUNTIME = True

    class InvalidSia(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            if self.source["key"] == "semiconductor_industry_association":
                page.content = (
                    """<div class="resource-item"><a href="/chip/"><h3>Chip</h3></a>"""
                    f"""<div class="resource-item-meta">{date_text}</div></div>"""
                ).encode()
            return page

    run = _module().collect_news_archive(tmp_path, 2026, 10, now=NOW, fetcher_factory=InvalidSia)
    saved = _module().read_news_archive(tmp_path, run.name)
    assert not any(
        row["source_id"] == "semiconductor_industry_association" for row in saved["records"]
    )
    assert (
        next(
            row
            for row in saved["source_outcomes"]
            if row["source_id"] == "semiconductor_industry_association"
        )["status"]
        == "failed"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"""<rss><channel><item><title>No original link</title>
<link rel="enclosure" href="https://img.global.news.samsung.com/photo.jpg"/></item></channel></rss>""",
        b"""<rss><channel><item><title>Foreign original</title>
<link>https://unapproved.example/chip</link></item></channel></rss>""",
        b"""<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///untrusted">]>
<rss><channel><item><title>&xxe;</title><link>https://news.samsung.com/global/chip</link></item></channel></rss>""",
    ],
)
def test_metadata_only_feed_still_rejects_missing_link_foreign_origin_and_entities(
    tmp_path, settings, payload
):
    settings.IS_LOCAL_RUNTIME = True

    class UnsafeFeed(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            if self.source["key"] == "samsung_global_newsroom":
                page.content = payload
            return page

    run = _module().collect_news_archive(tmp_path, 2026, 10, now=NOW, fetcher_factory=UnsafeFeed)
    saved = _module().read_news_archive(tmp_path, run.name)
    assert not any(row["source_id"] == "samsung_global_newsroom" for row in saved["records"])
    assert (
        next(
            row for row in saved["source_outcomes"] if row["source_id"] == "samsung_global_newsroom"
        )["status"]
        == "failed"
    )


def test_sia_labelled_original_year_is_not_replaced_by_requested_archive_year(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True

    class OlderYearSia(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            if self.source["key"] == "semiconductor_industry_association":
                page.content = b"""<div class="resource-item">
<a href="/chip/"><h3>Older chip</h3></a>
<div class="resource-item-meta">Press Release: 10/05/25</div></div>"""
            return page

    run = _module().collect_news_archive(tmp_path, 2026, 10, now=NOW, fetcher_factory=OlderYearSia)
    saved = _module().read_news_archive(tmp_path, run.name)
    assert not any(
        row["source_id"] == "semiconductor_industry_association" for row in saved["records"]
    )
    outcome = next(
        row
        for row in saved["source_outcomes"]
        if row["source_id"] == "semiconductor_industry_association"
    )
    assert outcome["status"] == "completed"
    assert outcome["count"] == 0


def test_archives_are_immutable_generations_and_tampering_is_rejected(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    first = _collect(tmp_path)
    original = first.joinpath("notices.json").read_bytes()
    second = _collect(tmp_path)
    assert first != second
    assert first.joinpath("notices.json").read_bytes() == original
    first.joinpath("notices.json").write_bytes(original + b" ")
    with pytest.raises(ValueError):
        _module().read_news_archive(tmp_path, first.name)


def test_read_does_not_create_missing_storage(tmp_path):
    root = tmp_path / "absent"
    assert _module().load_news_archive(root) is None
    assert not root.exists()


def test_month_and_query_filter_before_limit_preserve_unknown_dates(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    _collect(tmp_path)
    data = _module().load_news_archive(tmp_path, period=_period(), query="")
    assert data["total"] == 3
    assert all(row["published_at"] is not None for row in data["news"])
    assert data["inventory_label"] == "로컬 원문 메타데이터"
    collected = _module().load_news_archive(tmp_path, period=_period("collected"))
    assert collected["total"] == 4
    assert any(row["published_at"] is None for row in collected["news"])
    empty = _module().load_news_archive(tmp_path, period=_period(month=9))
    assert empty["available"] is True
    assert empty["total"] == 0
    searched = _module().load_news_archive(tmp_path, period=_period(), query="hbm")
    assert searched["total"] == 1


def test_collection_rejects_nonlocal_runtime_without_creating_root(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = False
    with pytest.raises(ValueError, match="local"):
        _collect(tmp_path / "absent")
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    "year,month,items,seconds",
    [(2026, 13, 60, 180), (2026, 10, 0, 180), (2026, 10, 60, 0), (2027, 1, 60, 180)],
)
def test_invalid_or_future_collection_is_rejected(tmp_path, settings, year, month, items, seconds):
    settings.IS_LOCAL_RUNTIME = True
    with pytest.raises(ValueError):
        _module().collect_news_archive(
            tmp_path,
            year,
            month,
            max_items=items,
            max_seconds=seconds,
            now=NOW,
            fetcher_factory=FixtureFetcher,
        )


class Clock:
    def __init__(self):
        self.value = 1_800_000_000.0
        self.sleeps = []

    def time(self):
        return self.value

    monotonic = time

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def _network(monkeypatch, handlers):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    return httpx.MockTransport(handlers)


def test_robots_denial_prevents_source_request(tmp_path, monkeypatch):
    module = _module()
    urls = []

    def response(request):
        urls.append(str(request.url))
        return httpx.Response(
            200, headers={"Content-Type": "text/plain"}, text="User-agent: *\nDisallow: /global/\n"
        )

    clock = Clock()
    budget = module.RequestBudget(10, 180, clock=clock)
    source = next(
        row for row in module.load_news_sources() if row["key"] == "samsung_global_newsroom"
    )
    fetcher = module.MetadataFetcher(
        source, state_root=tmp_path, budget=budget, transport=_network(monkeypatch, response)
    )
    with pytest.raises(ValueError, match="robots"):
        fetcher.get(
            source["material"]["externalConfig"]["entrypoints"][0] + "?paged=1", kind="feed"
        )
    fetcher.close()
    assert len(urls) == 1
    assert "robots.txt" in urls[0]


def test_redirect_target_checks_robots_before_second_request(tmp_path, monkeypatch):
    module = _module()
    urls = []

    def response(request):
        urls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain"},
                text=(
                    "User-agent: *\nDisallow: /global/category/products/semiconductors/feed/atom\n"
                ),
            )
        return httpx.Response(
            302,
            headers={
                "Location": "https://news.samsung.com/global/category/products/semiconductors/feed/atom?paged=1"
            },
        )

    source = next(
        row for row in module.load_news_sources() if row["key"] == "samsung_global_newsroom"
    )
    fetcher = module.MetadataFetcher(
        source,
        state_root=tmp_path,
        budget=module.RequestBudget(10, 180, clock=Clock()),
        transport=_network(monkeypatch, response),
    )
    with pytest.raises(ValueError, match="robots"):
        fetcher.get(
            source["material"]["externalConfig"]["entrypoints"][0] + "?paged=1", kind="feed"
        )
    fetcher.close()
    assert len(urls) == 2


def test_first_page_feed_accepts_same_approved_bare_canonical_redirect(tmp_path, monkeypatch):
    module = _module()
    diagnostics = module.CollectionDiagnostics(tmp_path / "diagnostics", mode="fixture")
    urls = []
    target = "https://news.samsung.com/global/category/products/semiconductors/feed/"

    def response(request):
        urls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, headers={"Content-Type": "text/plain"}, text="User-agent: *\nAllow: /\n"
            )
        if request.url.query:
            return httpx.Response(301, headers={"Location": target})
        return httpx.Response(200, headers={"Content-Type": "application/rss+xml"}, content=RSS)

    source = next(
        row for row in module.load_news_sources() if row["key"] == "samsung_global_newsroom"
    )
    fetcher = module.MetadataFetcher(
        source,
        state_root=tmp_path / "limiter",
        budget=module.RequestBudget(10, 180, clock=Clock()),
        diagnostics=diagnostics,
        transport=_network(monkeypatch, response),
    )
    page = fetcher.get(
        source["material"]["externalConfig"]["entrypoints"][0] + "?paged=1", kind="feed"
    )
    fetcher.close()
    assert page.url == target
    assert len(urls) == 3
    saved = json.loads((diagnostics.root / "diagnostics.json").read_text(encoding="utf-8"))
    assert saved["redirects"][0]["allowed"] is True
    assert saved["redirects"][0]["target_query_empty"] is True


@pytest.mark.parametrize(
    "page,target",
    [
        ("2", "https://news.samsung.com/global/category/products/semiconductors/feed/"),
        (
            "1",
            "https://news.samsung.com/global/category/products/semiconductors/feed/?unexpected=1",
        ),
        ("1", "https://news.samsung.com/global/category/products/semiconductors/feed/atom"),
    ],
)
def test_first_page_canonical_redirect_exception_does_not_accept_other_scopes(
    tmp_path, monkeypatch, page, target
):
    module = _module()
    urls = []

    def response(request):
        urls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, headers={"Content-Type": "text/plain"}, text="User-agent: *\nAllow: /\n"
            )
        return httpx.Response(301, headers={"Location": target})

    source = next(
        row for row in module.load_news_sources() if row["key"] == "samsung_global_newsroom"
    )
    fetcher = module.MetadataFetcher(
        source,
        state_root=tmp_path,
        budget=module.RequestBudget(10, 180, clock=Clock()),
        transport=_network(monkeypatch, response),
    )
    with pytest.raises(ValueError):
        fetcher.get(
            source["material"]["externalConfig"]["entrypoints"][0] + "?paged=" + page, kind="feed"
        )
    fetcher.close()
    assert len(urls) == 2


def test_rate_spacing_poll_and_concurrency_survive_fetcher_recreation(tmp_path, monkeypatch):
    module = _module()
    clock = Clock()

    def response(request):
        content = b"User-agent: *\nAllow: /\n" if request.url.path == "/robots.txt" else MOTIR
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=content)

    source = next(row for row in module.load_news_sources() if row["key"] == "motie")
    url = source["material"]["externalConfig"]["entrypoints"][0]
    url += "?pageIndex=1&rowPageC=50&startDtD=2026-10-01&endDtD=2026-10-08"

    def make():
        return module.MetadataFetcher(
            source,
            state_root=tmp_path,
            budget=module.RequestBudget(10, 180, clock=clock),
            transport=_network(monkeypatch, response),
        )

    first = make()
    first.get(url, kind="list")
    assert clock.sleeps == [12.0]
    second = make()
    with pytest.raises(ValueError, match="concurrency"):
        second.get(url, kind="list")
    first.close()
    third = make()
    with pytest.raises(ValueError, match="poll"):
        third.get(url, kind="list")
    third.close()


def test_disabled_source_cannot_instantiate_transport(tmp_path):
    module = _module()
    source = next(row for row in module.load_news_sources() if row["key"] == "skhynix_newsroom")
    with pytest.raises(ValueError, match="disabled|prohibited"):
        module.MetadataFetcher(source, state_root=tmp_path, budget=module.RequestBudget(10, 180))


def test_private_diagnostics_preserve_response_facts_without_headers(tmp_path, monkeypatch):
    module = _module()
    assert hasattr(module, "CollectionDiagnostics")
    clock = Clock()
    diagnostics = module.CollectionDiagnostics(tmp_path / "diagnostics", mode="fixture")

    def response(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, headers={"Content-Type": "text/plain"}, text="User-agent: *\nAllow: /\n"
            )
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/rss+xml; charset=utf-8",
                "Set-Cookie": "private-header-must-not-escape",
            },
            content=RSS,
        )

    source = next(
        row for row in module.load_news_sources() if row["key"] == "samsung_global_newsroom"
    )
    fetcher = module.MetadataFetcher(
        source,
        state_root=tmp_path / "limiter",
        budget=module.RequestBudget(10, 180, clock=clock),
        diagnostics=diagnostics,
        transport=_network(monkeypatch, response),
    )
    fetcher.get(source["material"]["externalConfig"]["entrypoints"][0] + "?paged=1", kind="feed")
    fetcher.close()
    saved = json.loads((diagnostics.root / "diagnostics.json").read_text(encoding="utf-8"))
    content = saved["responses"][1]
    assert content["status"] == 200
    assert content["mime_type"] == "application/rss+xml"
    assert content["encoding"] == "utf-8"
    assert content["sha256"] == hashlib.sha256(RSS).hexdigest()
    assert (diagnostics.root / content["body_file"]).read_bytes() == RSS
    assert "?" not in content["request_url"]
    assert "private-header" not in str(saved)
    assert "headers" not in str(saved)


def test_schema_failure_keeps_exact_redacted_detail_in_private_diagnostics(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True

    class InvalidFeed(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            if self.source["key"] == "samsung_global_newsroom":
                page.content = b"<not-xml"
            return page

    run = _module().collect_news_archive(
        tmp_path / "archive",
        2026,
        10,
        now=NOW,
        fetcher_factory=InvalidFeed,
        diagnostics_root=tmp_path / "diagnostics",
    )
    index = next((tmp_path / "diagnostics").glob("*/diagnostics.json"))
    saved = json.loads(index.read_text(encoding="utf-8"))
    error = next(row for row in saved["errors"] if row["source_id"] == "samsung_global_newsroom")
    assert error["code"] == "source_schema_invalid"
    assert error["detail"] == "Source feed XML is malformed."
    assert error["stage"] == "parse_feed"
    assert saved["archive_run_name"] == run.name
    archive = _module().read_news_archive(tmp_path / "archive", run.name)
    assert (
        next(
            row
            for row in archive["source_outcomes"]
            if row["source_id"] == "samsung_global_newsroom"
        )["status"]
        == "failed"
    )


def test_private_exception_detail_removes_url_credentials_and_query_values(tmp_path):
    from adapters.sources.http import SourceSchemaError

    diagnostics = _module().CollectionDiagnostics(tmp_path, mode="fixture")
    diagnostics.error(
        "motie",
        SourceSchemaError(
            "Invalid https://user:password@www.motir.go.kr/path?token=must-not-escape"
        ),
    )
    saved = (tmp_path / "diagnostics.json").read_text(encoding="utf-8")
    assert "must-not-escape" not in saved
    assert "password" not in saved
    assert "www.motir.go.kr/path" in saved


def test_cli_is_explicit_local_collection(tmp_path, settings):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    settings.IS_LOCAL_RUNTIME = False
    with pytest.raises(CommandError, match="local"):
        call_command("collect_semiconductor_news", year=2026, month=10)


@pytest.mark.parametrize(
    "change",
    [
        "body",
        "rights",
        "future_capture",
        "future_publication",
        "duplicate",
        "outcome_count",
        "disabled_request",
        "schema",
        "mode_type",
        "source_type",
        "global_requests",
    ],
)
def test_hash_bound_but_invalid_metadata_schema_is_rejected(tmp_path, settings, change):
    from apps.local_content.bundles import ArticleBundleWriter

    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    run = _collect(tmp_path)
    notices = json.loads((run / "notices.json").read_text(encoding="utf-8"))
    obs = json.loads((run / "raw-observations.json").read_text(encoding="utf-8"))
    if change == "body":
        notices["records"][0]["body"] = "hidden full article"
    elif change == "rights":
        notices["records"][0]["rights_status"] = "allowed"
    elif change == "future_capture":
        notices["records"][0]["collected_at"] = "2027-01-01T00:00:00+00:00"
    elif change == "future_publication":
        notices["records"][0]["published_at"] = "2027-01-01T00:00:00+00:00"
    elif change == "duplicate":
        notices["records"].append(notices["records"][0].copy())
    elif change == "outcome_count":
        obs["source_outcomes"][0]["count"] += 1
    elif change == "disabled_request":
        next(row for row in obs["source_outcomes"] if row["source_id"] == "skhynix_newsroom")[
            "request_count"
        ] = 1
    elif change == "mode_type":
        notices["mode"] = []
    elif change == "source_type":
        notices["records"][0]["source_id"] = []
    elif change == "global_requests":
        for row in obs["source_outcomes"]:
            if row["status"] == "completed":
                row["request_count"] = 30
    else:
        obs["unrecognized"] = True
    writer = ArticleBundleWriter(tmp_path)
    lease = writer.reserve_run_directory(NOW.date(), workflow_id="adversarial-contract-test")
    notices["run_name"] = obs["run_name"] = lease.name

    def encode(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    obs["records_hash"] = hashlib.sha256(encode(notices)).hexdigest()
    writer.write_run(
        NOW.date(),
        "# Metadata\n",
        run_directory=lease,
        run_files={"notices.json": encode(notices), "raw-observations.json": encode(obs)},
    )
    with pytest.raises(ValueError):
        module.read_news_archive(tmp_path, lease.name)


def test_shared_collection_lock_blocks_second_writer_without_reusing_run(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    with module.ExclusiveLock(tmp_path / ".collection.lock"):
        with pytest.raises(ValueError, match="concurrency"):
            _collect(tmp_path)
    assert not list(tmp_path.glob("2026-*"))


def test_historical_month_does_not_store_next_month_midnight(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    run = _module().collect_news_archive(tmp_path, 2026, 9, now=NOW, fetcher_factory=FixtureFetcher)
    saved = _module().read_news_archive(tmp_path, run.name)
    assert [row["title"] for row in saved["records"]] == ["Unknown date", "Old chip"]


def test_year_selection_uses_latest_snapshot_per_month_without_losing_history(tmp_path, settings):
    from wisdome_writer.portal.periods import PeriodSelection

    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    module.collect_news_archive(tmp_path, 2026, 9, now=NOW, fetcher_factory=FixtureFetcher)
    _collect(tmp_path)
    _collect(tmp_path)
    data = module.load_news_archive(tmp_path, period=PeriodSelection(2026))
    assert data["total"] == 4
    assert {row["title"] for row in data["news"]} == {
        "Old chip",
        "HBM chip release",
        "Industry chips",
        "반도체 투자 발표",
    }
    assert data["archive_snapshot_count"] == 2


class FailedFetcher(FixtureFetcher):
    def get(self, url, *, kind):
        from adapters.sources.http import SourceSchemaError

        self.request_count += 1
        raise SourceSchemaError("Source contract marker changed.")


def test_all_failed_archive_is_unavailable_without_earlier_good_records(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    module.collect_news_archive(tmp_path, 2026, 10, now=NOW, fetcher_factory=FailedFetcher)
    data = module.load_news_archive(tmp_path, period=_period())
    assert data["available"] is False
    assert data["total"] is None
    assert data["news"] == []
    assert "실패" in data["message"]
    assert data["last_attempt_at"] == NOW
    assert data["last_seen"] is None
    assert sum(row["status"] == "failed" for row in data["source_outcomes"]) == 3


def test_failed_refresh_preserves_older_observations_and_original_save_time(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    _collect(tmp_path)
    latest = NOW + timedelta(minutes=5)
    module.collect_news_archive(tmp_path, 2026, 10, now=latest, fetcher_factory=FailedFetcher)
    data = module.load_news_archive(tmp_path, period=_period())
    assert data["available"] is True
    assert data["total"] == 3
    assert {row["title"] for row in data["news"]} == {
        "HBM chip release",
        "반도체 투자 발표",
        "Industry chips",
    }
    assert data["last_seen"] == NOW
    assert data["last_attempt_at"] == latest
    assert "실패" in data["message"]


def test_collected_basis_uses_capture_time_across_publication_archive_month(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    module.collect_news_archive(tmp_path, 2026, 9, now=NOW, fetcher_factory=FixtureFetcher)
    data = module.load_news_archive(tmp_path, period=_period("collected"))
    assert data["available"] is True
    assert data["total"] == 2
    assert {row["title"] for row in data["news"]} == {"Unknown date", "Old chip"}


def test_existing_healthy_dataset_has_explicit_empty_other_selected_month(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    _collect(tmp_path)
    data = module.load_news_archive(tmp_path, period=_period(month=8))
    assert data is not None
    assert data["available"] is True
    assert data["total"] == 0
    assert data["news"] == []
    assert "선택" in data["message"]


def test_projection_prefers_live_observations_over_fixture_when_available(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    first = _collect(tmp_path)
    fixture = module.read_news_archive(tmp_path, first.name)
    _collect(tmp_path)
    names = [entry.name for entry in tmp_path.iterdir() if module.RUN_NAME.fullmatch(entry.name)]
    live = {
        **fixture,
        "mode": "live",
        "run_name": names[0],
        "captured_at": (NOW + timedelta(minutes=1)).isoformat(),
        "records": [
            {**row, "provenance": "live", "title": "Live official " + row["title"]}
            for row in fixture["records"]
        ],
    }
    from unittest.mock import patch

    # The reader's closed-schema tests are separate; simulate two authenticated
    # producer documents here to test projection selection independently of HTTP.
    with patch.object(
        module,
        "read_news_archive",
        side_effect=lambda root, name: live if name == names[0] else fixture,
    ):
        data = module.load_news_archive(tmp_path, period=_period())
    assert data["mode"] == "live"
    assert all(row["provenance"] == "live" for row in data["news"])
    assert all(row["title"].startswith("Live official") for row in data["news"])


def test_item_limited_refresh_cannot_delete_older_first_page_observations(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    _collect(tmp_path)
    latest = NOW + timedelta(minutes=5)
    module.collect_news_archive(
        tmp_path, 2026, 10, max_items=1, now=latest, fetcher_factory=FixtureFetcher
    )
    data = module.load_news_archive(tmp_path, period=_period())
    assert data["total"] == 3
    assert data["last_attempt_at"] == latest
    assert "누락" in data["message"]
    assert any(row["status"] == "partial" for row in data["source_outcomes"])


def test_collection_period_filters_before_deduplicating_a_later_observation(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    earlier = datetime(2026, 9, 30, 2, tzinfo=UTC)

    class EarlierFetcher(FixtureFetcher):
        def get(self, url, *, kind):
            page = super().get(url, kind=kind)
            page.fetched_at = earlier
            return page

    module.collect_news_archive(tmp_path, 2026, 9, now=earlier, fetcher_factory=EarlierFetcher)
    module.collect_news_archive(tmp_path, 2026, 9, now=NOW, fetcher_factory=FixtureFetcher)
    data = module.load_news_archive(tmp_path, period=_period("collected", month=9))
    assert data["total"] == 2
    assert {row["first_collected_at"] for row in data["news"]} == {earlier}


def test_failed_live_attempt_cannot_be_filled_with_healthy_fixture_records(tmp_path, settings):
    from unittest.mock import patch

    settings.IS_LOCAL_RUNTIME = True
    module = _module()
    healthy = _collect(tmp_path)
    fixture = module.read_news_archive(tmp_path, healthy.name)
    failed = module.collect_news_archive(
        tmp_path, 2026, 10, now=NOW + timedelta(minutes=5), fetcher_factory=FailedFetcher
    )
    failed_live = {**module.read_news_archive(tmp_path, failed.name), "mode": "live"}
    with patch.object(
        module,
        "read_news_archive",
        side_effect=lambda root, name: failed_live if name == failed.name else fixture,
    ):
        data = module.load_news_archive(tmp_path, period=_period())
    assert data["mode"] == "live"
    assert data["available"] is False
    assert data["news"] == []
    assert "테스트" in data["message"]


def test_transport_itself_requires_local_runtime(tmp_path, settings):
    settings.IS_LOCAL_RUNTIME = False
    module = _module()
    source = module.load_news_sources()[0]
    with pytest.raises(ValueError, match="local"):
        module.MetadataFetcher(source, state_root=tmp_path, budget=module.RequestBudget(10, 180))
