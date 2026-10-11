"""Local, bounded official-source metadata archives; no editorial approval or publishing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from django.conf import settings
from lxml import html

from adapters.sources.errors import SourceAccessError
from adapters.sources.http import SourceSchemaError, parse_source_datetime
from adapters.sources.semiconductor.adapters import (
    MotirAdapter,
    SamsungNewsroomAdapter,
    SiaLatestAdapter,
)
from adapters.sources.semiconductor.common import (
    canonical_source_url,
    feed_entries,
    parse_motir_list,
    parse_motir_total,
    parse_sia_list,
)
from apps.local_content.bundles import (
    ArticleBundleWriter,
    BundleValidationError,
    _parse_strict_json_object,
)
from apps.topics.services import normalize_registry_import
from wisdome_writer.infrastructure.http_safety import (
    HttpSafetyError,
    redact_url,
    redact_urls_in_text,
    safe_get,
    validated_logical_url,
)

SEOUL = ZoneInfo("Asia/Seoul")
TOPIC = "semiconductor_news"
RUN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})(?:--run-[a-f0-9]{12})?")
PROFILES = {
    "motie": MotirAdapter,
    "samsung_global_newsroom": SamsungNewsroomAdapter,
    "semiconductor_industry_association": SiaLatestAdapter,
}
RELEVANCE = re.compile(
    r"반도체|파운드리|메모리|semiconductor|foundry|\b(?:chip|hbm|dram|nand)\b", re.I
)
MOTIR_RETURN_FALSE = re.compile(
    r"^article\.view\(['\"](?P<id>\d+)['\"]\)\s*;\s*return\s+false\s*;\s*$"
)
SIA_LABELLED_DATE = re.compile(r"^(?:Press Release|Blog):\s*(\d{2}/\d{2}/\d{2})$")
RECORD_KEYS = {
    "source_id",
    "title",
    "url",
    "publisher",
    "published_at",
    "collected_at",
    "rights_status",
    "provenance",
}


class NewsAccessError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _hash(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _safe_root(root, *, create=False):
    root = Path(os.path.abspath(root))
    for entry in (root, *root.parents):
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise NewsAccessError("archive_root_unsafe")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _read_file(path, limit=4096):
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & 0x400
        or metadata.st_size > limit
    ):
        raise NewsAccessError("local_state_unsafe")
    return path.read_bytes()


class CollectionDiagnostics:
    """Bounded private evidence for one local operation, separate from portal data."""

    def __init__(self, root, *, mode):
        if not settings.IS_LOCAL_RUNTIME or mode not in {"live", "fixture"}:
            raise ValueError("diagnostics require explicit local provenance")
        self.root = _safe_root(root, create=True)
        self.document = {
            "schema_version": 1,
            "mode": mode,
            "archive_run_name": None,
            "responses": [],
            "errors": [],
            "redirects": [],
        }
        self.body_bytes = 0
        self._save()

    def _write(self, path, payload):
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def _save(self):
        target = self.root / "diagnostics.json"
        temporary = self.root / (uuid.uuid4().hex + ".tmp")
        self._write(temporary, _json(self.document))
        if target.exists():
            _read_file(target, limit=128 * 1024)
        os.replace(temporary, target)

    def response(self, source, url, kind, response, *, robots=False):
        if len(self.document["responses"]) >= 30:
            raise NewsAccessError("diagnostic_response_bound_exceeded")
        body = response.content
        body_file = None
        if self.body_bytes + len(body) <= 8 * 1024 * 1024:
            body_file = f"{source['key']}-{len(self.document['responses']) + 1:02d}.bin"
            self._write(self.root / body_file, body)
            self.body_bytes += len(body)
        self.document["responses"].append(
            {
                "source_id": source["key"],
                "purpose": "robots" if robots else kind,
                "request_url": redact_url(url),
                "response_url": redact_url(validated_logical_url(response)),
                "configured_entrypoints": [
                    redact_url(value)
                    for value in source["material"]["externalConfig"]["entrypoints"]
                ],
                "status": response.status_code,
                "mime_type": response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()[:80],
                "encoding": (response.encoding or "utf-8")[:40],
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "body_file": body_file,
            }
        )
        self._save()

    def error(self, source_id, exc, *, stage=None):
        if len(self.document["errors"]) >= 5:
            raise NewsAccessError("diagnostic_error_bound_exceeded")
        detail = redact_urls_in_text(exc.detail) if isinstance(exc, SourceAccessError) else None
        self.document["errors"].append(
            {
                "source_id": source_id,
                "exception_type": type(exc).__name__,
                "stage": stage,
                "code": getattr(exc, "code", "source_metadata_invalid"),
                "detail": detail[:512] if detail else None,
            }
        )
        self._save()

    def finish(self, name):
        self.document["archive_run_name"] = name
        self._save()

    def redirect(self, source_id, requested, target, *, allowed):
        if len(self.document["redirects"]) >= 30:
            raise NewsAccessError("diagnostic_redirect_bound_exceeded")
        self.document["redirects"].append(
            {
                "source_id": source_id,
                "request_url": redact_url(requested),
                "target_url": redact_url(target),
                "target_query_empty": not urlsplit(target).query,
                "allowed": allowed,
            }
        )
        self._save()


class ExclusiveLock:
    """Fail-closed local lease; never delete another process's replacement lock."""

    def __init__(self, path):
        self.path, self.identity = Path(path), None

    def acquire(self):
        _safe_root(self.path.parent, create=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise NewsAccessError("source_concurrency_limited") from exc
        try:
            os.write(descriptor, uuid.uuid4().hex.encode())
            os.fsync(descriptor)
            meta = os.fstat(descriptor)
            self.identity = (meta.st_dev, meta.st_ino)
        finally:
            os.close(descriptor)

    def close(self):
        if self.identity is not None:
            meta = self.path.lstat()
            if (meta.st_dev, meta.st_ino) != self.identity:
                raise NewsAccessError("local_lock_replaced")
            self.path.unlink()
            self.identity = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.close()


class RequestBudget:
    def __init__(self, max_requests, max_seconds, *, clock=time):
        if (
            type(max_requests) is not int
            or not 1 <= max_requests <= 30
            or type(max_seconds) not in {int, float}
            or not 1 <= max_seconds <= 900
        ):
            raise ValueError("invalid collection budget")
        self.clock, self.max_requests, self.request_count = clock, max_requests, 0
        self.deadline = clock.monotonic() + max_seconds

    def remaining(self):
        remaining = self.deadline - self.clock.monotonic()
        if remaining <= 0:
            raise NewsAccessError("source_time_budget_exhausted")
        return remaining

    def claim(self):
        self.remaining()
        if self.request_count >= self.max_requests:
            raise NewsAccessError("source_request_budget_exhausted")
        self.request_count += 1


@dataclass(frozen=True)
class MetadataPage:
    content: bytes
    url: str
    fetched_at: datetime
    encoding: str = "utf-8"


def load_news_sources():
    path = settings.REPOSITORY_ROOT / "config/source-registry/semiconductor_news.json"
    manifest = _parse_strict_json_object(path.read_bytes(), "semiconductor catalog")
    return normalize_registry_import(manifest)["sources"]


def _canonical(source, value, kind):
    profile = PROFILES.get(source["key"])
    if profile is None:
        raise NewsAccessError("source_metadata_unsupported")
    hosts, paths, query_keys = profile.URL_PROFILES[kind]
    return canonical_source_url(value, hosts=hosts, paths=paths, allowed_query_keys=query_keys)


class MetadataFetcher:
    """Pinned HTTP with runtime robots and persistent local source-wide limits.

    This is an explicit local metadata workflow. It neither instantiates the
    distributed adapters nor substitutes a no-op for their Redis limiter.
    """

    def __init__(self, source, *, state_root, budget, transport=None, diagnostics=None):
        if not settings.IS_LOCAL_RUNTIME:
            raise ValueError("metadata transport requires local runtime")
        self.source, self.material = source, source["material"]
        if not self.material["enabled"] or self.material["defaultRightsStatus"] == "prohibited":
            raise NewsAccessError("source_disabled_or_prohibited")
        if source["key"] not in PROFILES:
            raise NewsAccessError("source_metadata_unsupported")
        self.policy = self.material["externalConfig"]["accessPolicy"]
        if self.policy["decision"] != "approved" or self.policy["trafficScope"] not in {
            "collection",
            "collection_and_source_check",
        }:
            raise NewsAccessError("source_access_policy_denied")
        self.budget, self.transport, self.request_count = budget, transport, 0
        self.diagnostics = diagnostics
        root = _safe_root(state_root, create=True)
        self.lock = ExclusiveLock(root / (source["key"] + ".lock"))
        self.ledger = root / (source["key"] + ".json")
        self.state = None
        self.robots = None
        self.first_page_request = None

    def _save_state(self):
        temporary = self.ledger.with_name(self.ledger.name + "." + uuid.uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, _json(self.state))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if self.ledger.exists():
            _read_file(self.ledger)
        os.replace(temporary, self.ledger)

    def _reserve(self):
        if self.state is not None:
            return
        self.lock.acquire()
        try:
            state = (
                _parse_strict_json_object(_read_file(self.ledger), "local source limiter")
                if self.ledger.exists()
                else {"last_request": None, "poll_until": 0}
            )
            if (
                set(state) != {"last_request", "poll_until"}
                or type(state["poll_until"]) not in {int, float}
                or state["last_request"] is not None
                and type(state["last_request"]) not in {int, float}
            ):
                raise NewsAccessError("source_limiter_state_invalid")
            now = self.budget.clock.time()
            if state["poll_until"] > now:
                raise NewsAccessError("source_poll_interval_reserved")
            self.state = state
            state["poll_until"] = now + self.material["pollIntervalSeconds"]
            self._save_state()
        except BaseException:
            self.state = None
            self.lock.close()
            raise

    def _claim(self):
        self._reserve()
        rpm = self.material["rateLimitPolicy"]["requestsPerMinute"]
        last = self.state["last_request"]
        delay = 0 if last is None else max(0, last + 60 / rpm - self.budget.clock.time())
        if delay >= self.budget.remaining():
            raise NewsAccessError("source_time_budget_exhausted")
        if delay:
            self.budget.clock.sleep(delay)
        self.budget.claim()
        if self.request_count >= self.policy["maxRequests"]:
            raise NewsAccessError("source_request_budget_exhausted")
        self.request_count += 1
        self.state["last_request"] = self.budget.clock.time()
        self._save_state()

    def _validate(self, url, kind, *, robots=False):
        parsed = urlsplit(url)
        if robots:
            expected = self.policy["originPolicies"][0]["robotsUrl"]
            if url != expected:
                raise NewsAccessError("source_robots_redirect_denied")
            return
        canonical = _canonical(self.source, url, kind)
        normalized = urlsplit(canonical)
        if (
            parsed.fragment
            or parsed.path != normalized.path
            or sorted(parse_qsl(parsed.query, keep_blank_values=True))
            != sorted(parse_qsl(normalized.query, keep_blank_values=True))
        ):
            raise NewsAccessError("source_url_profile_denied")
        origins = [
            row
            for row in self.policy["originPolicies"]
            if row["host"] == parsed.hostname
            and "GET" in row["methods"]
            and "collection" in row["purposes"]
            and any(parsed.path.startswith(prefix) for prefix in row["pathPrefixes"])
        ]
        if len(origins) != 1 or origins[0]["robotsMode"] != "runtime_fetch":
            raise NewsAccessError("source_origin_policy_denied")
        initial = urlsplit(self.first_page_request) if self.first_page_request else None
        canonical_first_page = (
            self.source["key"] == "samsung_global_newsroom"
            and kind == "feed"
            and initial is not None
            and not parsed.query
            and parse_qsl(initial.query, keep_blank_values=True) == [("paged", "1")]
            and parsed.hostname == initial.hostname
            and parsed.path.rstrip("/") == initial.path.rstrip("/")
        )
        if not canonical_first_page:
            PROFILES[self.source["key"]]._validate_query_values(
                self, kind, dict(parse_qsl(parsed.query, keep_blank_values=True))
            )
        if self.robots is not None and not self.robots.can_fetch(self.policy["userAgent"], url):
            raise NewsAccessError("source_robots_denied")

    def _request(self, url, kind, *, robots=False):
        self._reserve()
        remaining = min(20.0, self.budget.remaining())
        mimes = (
            {"text/plain", "text/html"}
            if robots
            else set(
                self.material["externalConfig"][
                    "feedContentTypes" if kind == "feed" else "listContentTypes"
                ]
            )
        )

        def validate_target(target):
            try:
                self._validate(target, kind, robots=robots)
            except (SourceAccessError, ValueError):
                if target != url and self.diagnostics is not None:
                    self.diagnostics.redirect(self.source["key"], url, target, allowed=False)
                raise
            if target != url and self.diagnostics is not None:
                self.diagnostics.redirect(self.source["key"], url, target, allowed=True)

        response = safe_get(
            url,
            max_bytes=128 * 1024 if robots else 2 * 1024 * 1024,
            timeout=remaining,
            max_elapsed_seconds=remaining,
            allowed_hosts={row["host"] for row in self.policy["originPolicies"]},
            https_only=True,
            max_redirects=0 if robots else self.policy["maxRedirects"],
            url_validator=validate_target,
            before_request=self._claim,
            allowed_content_types=mimes,
            headers={"User-Agent": self.policy["userAgent"], "Accept-Encoding": "identity"},
            transport=self.transport,
        )
        if self.diagnostics is not None:
            self.diagnostics.response(self.source, url, kind, response, robots=robots)
        if robots and response.status_code == 404:
            return MetadataPage(b"", validated_logical_url(response), datetime.now(UTC))
        if response.status_code != 200:
            raise NewsAccessError("source_http_error")
        return MetadataPage(
            response.content,
            validated_logical_url(response),
            datetime.now(UTC),
            response.encoding or "utf-8",
        )

    def get(self, url, *, kind):
        self.first_page_request = url
        self._validate(url, kind)
        if self.robots is None:
            robots = self._request(self.policy["originPolicies"][0]["robotsUrl"], kind, robots=True)
            parser = RobotFileParser()
            parser.parse(robots.content.decode("utf-8", errors="strict").splitlines())
            self.robots = parser
        self._validate(url, kind)
        return self._request(url, kind)

    def close(self):
        self.lock.close()


def _metadata_feed_entries(payload):
    """Keep only original record metadata; media/body validation is unrelated here.

    The shared/distributed feed parser still validates all attachments. This
    local-only projection supplies no media or body to that unchanged parser.
    """
    try:
        root = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, DefusedXmlException) as exc:
        raise SourceSchemaError("Source feed XML is malformed.") from exc
    names = {"title", "link", "pubdate", "published", "updated", "id", "guid"}
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1].lower() not in {"item", "entry"}:
            continue
        for child in list(entry):
            name = child.tag.rsplit("}", 1)[-1].lower()
            if name not in names or name == "link" and child.get("rel") == "enclosure":
                entry.remove(child)
    return feed_entries(ElementTree.tostring(root))


def _metadata_sia_rows(doc):
    """Normalize only the captured flat type-label/date string, retaining strict rows."""
    for node in doc.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' resource-item-meta ')]"
    ):
        text = " ".join(" ".join(node.itertext()).split())
        match = SIA_LABELLED_DATE.fullmatch(text)
        if match is not None and len(node) == 0:
            node.text = match[1]
    return parse_sia_list(doc)


def _metadata(source, page, start, end):
    key = source["key"]
    if key == "samsung_global_newsroom":
        rows = _metadata_feed_entries(page.content)
    elif key == "motie":
        doc = html.fromstring(
            page.content.decode(getattr(page, "encoding", "utf-8"), errors="strict")
        )
        total = parse_motir_total(doc)
        # Normalize only the captured return-false wrapper, with an independent
        # matching href on this exact board. No JS execution or query following.
        for link in doc.xpath("//table[@id='mytable']/tbody/tr/*//a[@onclick]"):
            match = MOTIR_RETURN_FALSE.fullmatch(link.get("onclick", ""))
            if match is None:
                continue
            target = urlsplit(urljoin(page.url, link.get("href", "")))
            pairs = parse_qsl(target.query, keep_blank_values=True)
            values = dict(pairs)
            if (
                target.fragment
                or len(values) != len(pairs)
                or set(values) - {"mno", "pageIndex"}
                or values.get("mno", "") != ""
                or values.get("pageIndex", "1") != "1"
            ):
                raise NewsAccessError("motir_identity_query_invalid")
            canonical = _canonical(source, target._replace(query="").geturl(), "detail")
            expected = urlsplit(page.url)._replace(query="").geturl().rstrip("/")
            expected += "/" + match["id"] + "/view"
            if canonical != expected:
                raise NewsAccessError("motir_identity_href_mismatch")
            link.set("onclick", "article.view('" + match["id"] + "')")
        rows = [
            {
                "title": row["title"],
                "published": parse_source_datetime(row["published"]),
                "url": urlsplit(page.url)._replace(query="").geturl().rstrip("/")
                + "/"
                + row["sequence"]
                + "/view",
            }
            for row in parse_motir_list(doc, expected_total=total)
            if RELEVANCE.search(row["title"])
        ]
    else:
        rows = _metadata_sia_rows(
            html.fromstring(
                page.content.decode(getattr(page, "encoding", "utf-8"), errors="strict")
            )
        )
    for row in rows:
        published = parse_source_datetime(row["published"])
        if published is not None and (
            not start <= published <= end
            or (published.astimezone(SEOUL).year, published.astimezone(SEOUL).month)
            != (start.year, start.month)
        ):
            continue
        title = row["title"].strip()
        if not title or len(title) > 1000 or any(ord(char) < 32 for char in title):
            raise NewsAccessError("source_title_invalid")
        yield {
            "source_id": key,
            "title": title,
            "url": _canonical(source, urljoin(page.url, row["url"]), "detail"),
            "publisher": source["material"]["publisher"],
            "published_at": published.isoformat() if published else None,
            "collected_at": page.fetched_at.isoformat(),
            "rights_status": source["material"]["defaultRightsStatus"],
        }


def _identity(row):
    # MOTIR's two configured boards can announce the same provider sequence.
    value = urlsplit(row["url"]).path.split("/")[-2] if row["source_id"] == "motie" else row["url"]
    return row["source_id"], value


def collect_news_archive(
    root,
    year,
    month,
    max_items=60,
    max_seconds=180,
    *,
    now=None,
    fetcher_factory=None,
    diagnostics_root=None,
):
    """Save a bounded first-page metadata observation, never an exhaustive month crawl.

    A supplied test fetcher always produces fixture-labelled storage. Live CLI
    execution uses only the default transport, real clock and persisted limiter.
    """
    if not settings.IS_LOCAL_RUNTIME:
        raise ValueError("semiconductor archive requires local runtime")
    if type(max_items) is not int or not 1 <= max_items <= 300:
        raise ValueError("invalid max_items")
    if type(year) is not int or type(month) is not int:
        raise ValueError("invalid year/month")
    start = datetime(year, month, 1, tzinfo=SEOUL)
    next_month = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=SEOUL)
    captured = now or datetime.now(UTC)
    if captured.tzinfo is None or start > captured:
        raise ValueError("invalid or future collection month")
    end = min(captured, next_month)
    budget = RequestBudget(30, max_seconds)
    sources = load_news_sources()
    mode = "fixture" if fetcher_factory is not None else "live"
    factory = fetcher_factory or MetadataFetcher
    diagnostics = None
    if mode == "live" or diagnostics_root is not None:
        diagnostic_base = (
            diagnostics_root or settings.LOCAL_STATE_ROOT / "semiconductor-diagnostics"
        )
        diagnostics = CollectionDiagnostics(Path(diagnostic_base) / uuid.uuid4().hex, mode=mode)
    root = _safe_root(root, create=True)
    with ExclusiveLock(root / ".collection.lock"):
        records, outcomes, identities = [], [], set()
        for source in sources:
            key, material = source["key"], source["material"]
            outcome = {
                "source_id": key,
                "status": "completed",
                "request_count": 0,
                "count": 0,
                "error_code": None,
            }
            fetcher = None
            stage = "transport_init"
            if not material["enabled"] or material["defaultRightsStatus"] == "prohibited":
                outcome["status"] = "disabled"
            elif key not in PROFILES:
                outcome["status"] = "unsupported"
                outcome["error_code"] = "source_metadata_unsupported"
            else:
                try:
                    fetcher = factory(
                        source,
                        state_root=settings.LOCAL_STATE_ROOT / "semiconductor-http",
                        budget=budget,
                        diagnostics=diagnostics,
                    )
                    for entrypoint in material["externalConfig"]["entrypoints"]:
                        kind = "feed" if key == "samsung_global_newsroom" else "list"
                        params = (
                            {"paged": 1}
                            if kind == "feed"
                            else {
                                "pageIndex": 1,
                                "rowPageC": 50,
                                "startDtD": start.strftime("%Y-%m-%d"),
                                "endDtD": end.astimezone(SEOUL).strftime("%Y-%m-%d"),
                            }
                            if key == "motie"
                            else {}
                        )
                        url = entrypoint + ("?" + urlencode(params) if params else "")
                        stage = "fetch_" + kind
                        page = fetcher.get(url, kind=kind)
                        stage = "parse_" + kind
                        for row in _metadata(source, page, start, end):
                            identity = _identity(row)
                            if identity in identities:
                                continue
                            if len(records) >= max_items:
                                outcome["status"] = "partial"
                                outcome["error_code"] = "record_limit_reached"
                                continue
                            identities.add(identity)
                            records.append({**row, "provenance": mode})
                            outcome["count"] += 1
                except (ValueError, HttpSafetyError, OSError) as exc:
                    outcome["status"] = "partial" if outcome["count"] else "failed"
                    outcome["error_code"] = getattr(exc, "code", "source_metadata_invalid")
                    if diagnostics is not None:
                        diagnostics.error(key, exc, stage=stage)
                finally:
                    if fetcher is not None:
                        outcome["request_count"] = fetcher.request_count
                        fetcher.close()
            outcomes.append(outcome)
        writer = ArticleBundleWriter(root)
        finished = captured if mode == "fixture" else datetime.now(UTC)
        run_date = finished.astimezone(SEOUL).date()
        lease = writer.reserve_run_directory(run_date, workflow_id=uuid.uuid4().hex)
        notices = {
            "schema_version": 1,
            "topic": TOPIC,
            "run_name": lease.name,
            "mode": mode,
            "records": records,
        }
        observations = {
            "schema_version": 1,
            "topic": TOPIC,
            "run_name": lease.name,
            "mode": mode,
            "captured_at": finished.isoformat(),
            "window": {
                "year": year,
                "month": month,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "catalog_hash": _hash(sources),
            "records_hash": _hash(notices),
            "coverage_limited": True,
            "source_outcomes": outcomes,
            "limits": {"max_items": max_items, "max_requests": 30, "max_seconds": max_seconds},
        }
        path = writer.write_run(
            run_date,
            "# Semiconductor source metadata\n\nBounded local observation.\n",
            run_directory=lease,
            run_files={
                "notices.json": _json(notices),
                "raw-observations.json": _json(observations),
            },
        )
        if diagnostics is not None:
            diagnostics.finish(path.name)
        return path


class _ReadOnlyWriter(ArticleBundleWriter):
    def _safe_root(self):
        root = _safe_root(self.root)
        if not root.is_dir():
            raise ValueError("archive unavailable")
        return root


def _aware(value):
    if not isinstance(value, str):
        raise ValueError("invalid archive timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("invalid archive timestamp")
    return parsed


def read_news_archive(root, name):
    """Read hash-bound metadata with closed schemas and known source profiles."""
    if not isinstance(name, str) or not (match := RUN_NAME.fullmatch(name)):
        raise ValueError("invalid archive name")
    run_date = date.fromisoformat(match[1])
    writer = _ReadOnlyWriter(root)
    notices = _parse_strict_json_object(
        writer.read_validated_run_file(run_date, "notices.json", run_directory=name), "news notices"
    )
    obs = _parse_strict_json_object(
        writer.read_validated_run_file(run_date, "raw-observations.json", run_directory=name),
        "news outcomes",
    )
    if (
        set(notices) != {"schema_version", "topic", "run_name", "mode", "records"}
        or type(notices["schema_version"]) is not int
        or notices["schema_version"] != 1
        or notices["topic"] != TOPIC
        or notices["run_name"] != name
        or not isinstance(notices["mode"], str)
        or notices["mode"] not in {"fixture", "live"}
        or not isinstance(notices["records"], list)
        or len(notices["records"]) > 300
    ):
        raise ValueError("invalid metadata schema")
    if (
        set(obs)
        != {
            "schema_version",
            "topic",
            "run_name",
            "mode",
            "captured_at",
            "window",
            "catalog_hash",
            "records_hash",
            "coverage_limited",
            "source_outcomes",
            "limits",
        }
        or type(obs["schema_version"]) is not int
        or obs["schema_version"] != 1
        or obs["topic"] != TOPIC
        or obs["run_name"] != name
        or obs["mode"] != notices["mode"]
        or obs["records_hash"] != _hash(notices)
        or obs["coverage_limited"] is not True
    ):
        raise ValueError("invalid observation schema")
    sources = load_news_sources()
    catalog = {source["key"]: source for source in sources}
    if obs["catalog_hash"] != _hash(sources):
        raise ValueError("archive source profiles changed")
    captured = _aware(obs["captured_at"])
    window = obs["window"]
    if (
        not isinstance(window, dict)
        or set(window) != {"year", "month", "start", "end"}
        or type(window["year"]) is not int
        or type(window["month"]) is not int
    ):
        raise ValueError("invalid archive window")
    start, end = _aware(window["start"]), _aware(window["end"])
    expected = datetime(window["year"], window["month"], 1, tzinfo=SEOUL)
    following = datetime(
        window["year"] + (window["month"] == 12), window["month"] % 12 + 1, 1, tzinfo=SEOUL
    )
    if (
        start != expected
        or not start <= end <= min(captured, following)
        or captured.astimezone(SEOUL).date() != run_date
    ):
        raise ValueError("invalid archive capture/window")
    limits = obs["limits"]
    if (
        not isinstance(limits, dict)
        or set(limits) != {"max_items", "max_requests", "max_seconds"}
        or type(limits["max_items"]) is not int
        or not 1 <= limits["max_items"] <= 300
        or limits["max_requests"] != 30
        or type(limits["max_seconds"]) not in {int, float}
        or not 1 <= limits["max_seconds"] <= 900
        or len(notices["records"]) > limits["max_items"]
    ):
        raise ValueError("invalid archive limits")
    identities, counts = set(), {key: 0 for key in catalog}
    for row in notices["records"]:
        if (
            not isinstance(row, dict)
            or set(row) != RECORD_KEYS
            or not isinstance(row["source_id"], str)
            or row["source_id"] not in PROFILES
        ):
            raise ValueError("invalid archived metadata")
        source = catalog[row["source_id"]]
        material = source["material"]
        if (
            not material["enabled"]
            or row["rights_status"] != material["defaultRightsStatus"]
            or row["publisher"] != material["publisher"]
            or row["provenance"] != notices["mode"]
            or not isinstance(row["title"], str)
            or not 1 <= len(row["title"]) <= 1000
            or any(ord(char) < 32 for char in row["title"])
            or not isinstance(row["url"], str)
            or _canonical(source, row["url"], "detail") != row["url"]
        ):
            raise ValueError("archived source metadata violates profile")
        published = _aware(row["published_at"]) if row["published_at"] is not None else None
        collected = _aware(row["collected_at"])
        if published is not None and (not start <= published <= end or published >= following):
            raise ValueError("archive publication outside captured window")
        if not start <= collected <= captured:
            raise ValueError("archive collection timestamp invalid")
        identity = _identity(row)
        if identity in identities:
            raise ValueError("duplicate archived identity")
        identities.add(identity)
        counts[row["source_id"]] += 1
    outcomes = obs["source_outcomes"]
    if not isinstance(outcomes, list) or len(outcomes) != len(catalog):
        raise ValueError("invalid source outcomes")
    seen = set()
    for row in outcomes:
        if (
            not isinstance(row, dict)
            or set(row) != {"source_id", "status", "request_count", "count", "error_code"}
            or not isinstance(row["source_id"], str)
            or row["source_id"] not in catalog
            or row["source_id"] in seen
            or not isinstance(row["status"], str)
            or row["status"] not in {"completed", "partial", "failed", "unsupported", "disabled"}
            or type(row["request_count"]) is not int
            or not 0 <= row["request_count"] <= 30
            or type(row["count"]) is not int
            or row["count"] != counts[row["source_id"]]
            or row["error_code"] is not None
            and (
                not isinstance(row["error_code"], str)
                or not re.fullmatch(r"[a-z0-9_]{1,100}", row["error_code"])
            )
        ):
            raise ValueError("invalid source outcome")
        key = row["source_id"]
        material = catalog[key]["material"]
        disabled = not material["enabled"] or material["defaultRightsStatus"] == "prohibited"
        if (disabled and (row["status"] != "disabled" or row["count"] or row["request_count"])) or (
            not disabled and key not in PROFILES and row["status"] != "unsupported"
        ):
            raise ValueError("invalid disabled/unsupported source outcome")
        seen.add(key)
    if sum(row["request_count"] for row in outcomes) > limits["max_requests"]:
        raise ValueError("archive request budget exceeded")
    return {**obs, "records": notices["records"]}


def load_news_archive(root, query="", period=None):
    """Project the latest validated snapshot for the selected archival month."""
    if not settings.IS_LOCAL_RUNTIME:
        return None
    root = _safe_root(root)
    if not root.exists():
        return None
    candidates, invalid = [], False
    with os.scandir(root) as entries:
        for count, entry in enumerate(entries, 1):
            if count > 512:
                raise ValueError("archive history bound exceeded")
            if RUN_NAME.fullmatch(entry.name):
                candidates.append(entry.name)
                if len(candidates) > 128:
                    raise ValueError("archive history bound exceeded")
    saved = []
    for name in candidates:
        try:
            document = read_news_archive(root, name)
            saved.append(document)
        except (ValueError, OSError, BundleValidationError):
            invalid = True
    if not saved:
        return (
            {
                "available": False,
                "total": None,
                "news": [],
                "drafts": [],
                "runs": [],
                "last_seen": None,
                "inventory_label": "로컬 원문 메타데이터",
                "message": "저장된 반도체 메타데이터 검증에 실패했습니다.",
            }
            if invalid
            else None
        )
    # Fixture-only storage remains inspectable and labelled. Any real live
    # archive takes precedence, including a failed attempt; sample data must
    # never make a failed real collection look successful.
    live = [snapshot for snapshot in saved if snapshot["mode"] == "live"]
    excluded_fixtures = len(saved) - len(live) if live else 0
    saved = live or saved
    saved.sort(key=lambda row: (_aware(row["captured_at"]), row["run_name"]), reverse=True)
    document = saved[0]
    latest_months = {}
    for snapshot in saved:
        key = snapshot["window"]["year"], snapshot["window"]["month"]
        latest_months.setdefault(key, snapshot)
    modes = {snapshot["mode"] for snapshot in saved}
    mode = next(iter(modes)) if len(modes) == 1 else "mixed"
    query = query.strip().casefold()
    basis = getattr(period, "basis", "published")
    observed = {}
    latest_identities = {}
    for snapshot in saved:
        for row in snapshot["records"]:
            latest_identities.setdefault(_identity(row), row)
    unknown_date_count = sum(row["published_at"] is None for row in latest_identities.values())
    # A bounded first page is a positive observation, not an exhaustive deletion
    # inventory. Retain older identities when later attempts omit them or fail.
    # Filter record dates first so a newer out-of-period observation cannot hide
    # the eligible older observation of the same provider identity.
    for snapshot in saved:
        for row in snapshot["records"]:
            published = _aware(row["published_at"]) if row["published_at"] else None
            collected = _aware(row["collected_at"])
            chosen_date = collected if basis == "collected" else published
            if period is not None and period.active and not period.matches(chosen_date):
                continue
            identity = _identity(row)
            observed.setdefault(
                identity,
                {
                    **row,
                    "published_at": published,
                    "first_collected_at": collected,
                    "status": "metadata",
                    "status_label": "저장된 메타데이터",
                    "rights_label": "출처 표시 필요"
                    if row["rights_status"] == "attribution_required"
                    else "내부 분석 전용",
                },
            )
    matching = [
        row
        for row in observed.values()
        if not query or any(query in row[key].casefold() for key in ("title", "publisher"))
    ]
    matching.sort(
        key=lambda row: (
            row["published_at"] or datetime.min.replace(tzinfo=UTC),
            row["first_collected_at"],
            row["url"],
        ),
        reverse=True,
    )
    message = (
        "로컬 수집의 원문 제목·출처 메타데이터입니다. "
        "첫 페이지 범위이며 월 전체 수집을 뜻하지 않습니다."
    )
    supported = [row for row in document["source_outcomes"] if row["source_id"] in PROFILES]
    refresh_failed = bool(supported) and all(row["status"] == "failed" for row in supported)
    refresh_limited = any(row["status"] in {"partial", "failed"} for row in supported)
    available = bool(observed) or not refresh_failed
    if refresh_failed:
        message += (
            " 최근 수집은 모든 지원 출처에서 실패했습니다. 이전 검증 기록을 유지합니다."
            if observed
            else " 모든 지원 출처의 수집이 실패해 확인할 메타데이터가 없습니다."
        )
    elif refresh_limited:
        message += " 최근 수집에 일부 실패·누락이 있어 이전 검증 기록도 유지합니다."
    if available and not observed and period is not None and period.active:
        message += " 선택한 기간에 해당하는 저장된 원문 메타데이터가 없습니다."
    if mode == "fixture":
        message += " 테스트 자료입니다."
    elif mode == "mixed":
        message += " 실제 수집과 테스트 자료가 함께 포함되며 각 행의 원래 구분을 유지합니다."
    if invalid:
        message += " 검증할 수 없는 다른 실행은 제외했습니다."
    if excluded_fixtures:
        message += " 테스트 자료는 실제 수집 기록과 분리해 제외했습니다."
    if period is not None and period.active and period.basis == "published" and unknown_date_count:
        message += (
            f" 발표일 미상 {unknown_date_count}건은 선택한 발표일 기간에 배정하지 않았습니다."
        )
    labels = {
        "completed": "완료",
        "partial": "일부",
        "failed": "실패",
        "unsupported": "미지원",
        "disabled": "중지",
    }
    outcome_summary = " · ".join(
        f"{label} {sum(row['status'] == status for row in document['source_outcomes'])}곳"
        for status, label in labels.items()
    )
    return {
        "available": available,
        "total": len(matching) if available else None,
        "news": matching[:60],
        "drafts": [],
        "runs": [],
        "last_seen": max((row["first_collected_at"] for row in observed.values()), default=None),
        "last_attempt_at": _aware(document["captured_at"]),
        "unknown_date_count": unknown_date_count,
        "outcome_summary": outcome_summary,
        "message": message,
        "inventory_label": "로컬 원문 메타데이터",
        "mode": mode,
        "archive_snapshot_count": len(latest_months),
        "windows": [snapshot["window"] for snapshot in latest_months.values()],
        "source_outcomes": document["source_outcomes"],
        "snapshot_name": document["run_name"],
        "coverage_limited": True,
        "window": document["window"],
    }
