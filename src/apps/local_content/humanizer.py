"""Strict loopback adapter and protected-prose verification for the local humanizer."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from urllib.parse import urlsplit

import httpx

from apps.local_content.rendering import ProseBlock

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_INPUT_BYTES = 5 * 1024 * 1024
MAX_RESULT_BYTES = 5 * 1024 * 1024
HUMANIZATION_DEADLINE_SECONDS = 10 * 60.0
MAX_LINGERING_HUMANIZATIONS = 2
_TRANSFORM_CONTENT_TYPE = "application/x-ndjson"
_HEALTH_CONTENT_TYPE = "application/json"
_TOKEN_PATTERN = re.compile(r"\[\[P\d{4,}\]\]")
_BLOCK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_BLOCK_PATTERN = re.compile(
    r"<!-- WSW:block:(?P<block_id>[A-Za-z0-9][A-Za-z0-9._-]{0,63}) -->\n"
    r"(?P<body>.*?)\n"
    r"<!-- WSW:endblock:(?P=block_id) -->",
    re.DOTALL,
)
_LINK_PATTERN = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(?P<target><[^>\n]+>|[^\s)]+)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)"
)
_MARKDOWN_PATTERN = re.compile(
    r"<!--\s*WSW:(?:end)?block:[^>\r\n]+-->"
    r"|!?\[[^\]\n]*\]\(\s*(?:<[^>\n]+>|[^\s)]+)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)"
    r"|\[\^[^\]\n]+\]|\[[^\]\n]+\]"
    r"|`{1,3}[^`\n]*`{1,3}"
    r"|\\[*_~`\[\]()]|\*{1,3}|_{1,3}|~~|\|"
    r"|^(?: {0,3}(?:#{1,6}(?=\s)|>|[-+*](?=\s)|\d+[.)](?=\s)|`{3,}|~{3,}))",
    re.MULTILINE,
)
_URL_PATTERN = re.compile(r"https?://[^\s<>\])]+", re.IGNORECASE)
_DIGIT_TOKEN_PATTERN = re.compile(r"(?<!\S)\S*\d\S*(?!\S)")
_REFERENCE_START_PATTERN = re.compile(
    r"^ {0,3}\[[^\]\r\n]+\]:[ \t]*(?P<rest>[^\r\n]*)$"
)
_REFERENCE_TITLE_PATTERNS = (
    re.compile(r'^"(?:\\.|[^"\\])*"$'),
    re.compile(r"^'(?:\\.|[^'\\])*'$"),
    re.compile(r"^\((?:\\.|[^()\\])*\)$"),
)
_SENSITIVE_PATTERNS = (_DIGIT_TOKEN_PATTERN,)
_EVENT_PHASES = frozenset({"sanitize", "chunk", "transform", "verify", "assemble"})
_ERROR_CODES = frozenset(
    {
        "EMPTY_INPUT",
        "QUEUE_FULL",
        "ENGINE_UNAVAILABLE",
        "ENGINE_TIMEOUT",
        "ENGINE_OUTPUT_INVALID",
        "VALIDATION_FAILED",
        "INSUFFICIENT_DISK",
        "CLIENT_ABORTED",
        "INTERNAL_ERROR",
    }
)
_WATCHDOG_SLOTS = threading.BoundedSemaphore(MAX_LINGERING_HUMANIZATIONS)


class HumanizationError(Exception):
    """A persistence-safe failure while calling the local humanizer."""


class HumanizationVerificationError(HumanizationError):
    """A candidate changed protected or structural article material."""


@dataclass(frozen=True)
class ProtectedArticleProse:
    """The only document permitted to cross the humanizer boundary."""

    document: str
    original_blocks: tuple[ProseBlock, ...]
    token_values: tuple[tuple[str, str], ...]
    token_occurrences: tuple[str, ...]
    link_targets: tuple[str, ...]
    anchors: tuple[str, ...]
    anchor_occurrences: tuple[str, ...]


@dataclass(frozen=True)
class HumanizationAuditResult:
    """Body-free proof identifiers for one reverified humanization job."""

    job_hash: str
    verification_hash: str


class HumanizerClient:
    """A bounded client for the sibling service's public loopback API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3210",
        *,
        transport: httpx.BaseTransport | None = None,
        deadline_seconds: float = HUMANIZATION_DEADLINE_SECONDS,
    ) -> None:
        if (
            isinstance(deadline_seconds, bool)
            or not isinstance(deadline_seconds, int | float)
            or not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0
            or deadline_seconds > HUMANIZATION_DEADLINE_SECONDS
        ):
            raise ValueError("humanizer deadline must be positive and at most 10 minutes")
        self._base_url = _validated_loopback_base_url(base_url)
        self._transport = transport
        self._deadline_seconds = float(deadline_seconds)

    def health(self) -> bool:
        """Return whether the sibling service reports its public ready state."""

        try:
            with self._client(timeout=10.0) as client:
                with client.stream("GET", f"{self._base_url}/api/health") as response:
                    if response.status_code != 200:
                        return False
                    if _media_type(response.headers.get("content-type")) != _HEALTH_CONTENT_TYPE:
                        return False
                    content = _read_limited_response(response, deadline=monotonic() + 10.0)
            payload = _strict_json_loads(content.decode("utf-8", errors="strict"))
        except (httpx.HTTPError, UnicodeDecodeError, ValueError, HumanizationError):
            return False
        return _valid_health_payload(payload)

    def transform(self, document: str) -> str:
        """Stream one transform and return output only after a single valid terminal done."""

        started = monotonic()
        request_body = _utf8_bytes(document, boundary=HumanizationError)
        if len(request_body) > MAX_INPUT_BYTES:
            raise HumanizationError("Humanizer input exceeded the 5 MiB size limit")
        deadline = started + self._deadline_seconds
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise HumanizationError("Humanizer request exceeded the total deadline")
        return _run_with_watchdog(
            lambda cancelled: self._transform_request(
                request_body,
                deadline=deadline,
                cancelled=cancelled,
            ),
            deadline=deadline,
        )

    def _transform_request(
        self,
        request_body: bytes,
        *,
        deadline: float,
        cancelled: threading.Event,
    ) -> str:
        remaining = max(0.001, deadline - monotonic())
        try:
            with self._client(timeout=remaining) as client:
                if cancelled.is_set():
                    raise HumanizationError("Humanizer request exceeded the total deadline")
                with client.stream(
                    "POST",
                    f"{self._base_url}/api/transform",
                    headers={
                        "Content-Type": "text/plain; charset=utf-8",
                        "Accept": _TRANSFORM_CONTENT_TYPE,
                    },
                    content=request_body,
                ) as response:
                    if response.status_code != 200:
                        raise HumanizationError(
                            f"Humanizer returned HTTP status {response.status_code}"
                        )
                    if (
                        _media_type(response.headers.get("content-type"))
                        != _TRANSFORM_CONTENT_TYPE
                    ):
                        raise HumanizationError("Humanizer response content type was not NDJSON")
                    return _consume_transform_stream(
                        response,
                        deadline=deadline,
                        cancelled=cancelled,
                    )
        except HumanizationError:
            raise
        except httpx.TimeoutException:
            raise HumanizationError("Humanizer request exceeded the total deadline") from None
        except httpx.HTTPError as exc:
            raise HumanizationError(
                f"Humanizer request failed ({exc.__class__.__name__})"
            ) from None

    def _client(self, *, timeout: float) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
        )


def protect_article_prose(
    blocks: Mapping[str, str] | Iterable[ProseBlock],
    anchors: Iterable[str],
) -> ProtectedArticleProse:
    """Create a tokenized document containing prose blocks and no factual article slots."""

    material = _coerce_blocks(blocks)
    _validate_blocks(material)
    anchor_values = _validated_anchors(anchors)
    document = "\n\n".join(
        f"<!-- WSW:block:{block.block_id} -->\n{block.markdown}\n"
        f"<!-- WSW:endblock:{block.block_id} -->"
        for block in material
    )
    _utf8_bytes(document, boundary=ValueError)
    if _TOKEN_PATTERN.search(document):
        raise ValueError("prose contains a reserved protected token")
    protected_values = _protected_values(document, anchor_values)
    value_tokens = {
        value: f"[[P{index:04d}]]" for index, value in enumerate(protected_values, start=1)
    }
    if protected_values:
        alternation = re.compile("|".join(re.escape(value) for value in protected_values))
        tokenized = alternation.sub(lambda match: value_tokens[match.group(0)], document)
    else:
        tokenized = document
    token_values = tuple((value_tokens[value], value) for value in protected_values)
    return ProtectedArticleProse(
        document=tokenized,
        original_blocks=material,
        token_values=token_values,
        token_occurrences=tuple(match.group(0) for match in _TOKEN_PATTERN.finditer(tokenized)),
        link_targets=_link_targets(document),
        anchors=anchor_values,
        anchor_occurrences=_anchor_occurrences(document, anchor_values),
    )


def verify_humanized_candidate(
    protected: ProtectedArticleProse,
    candidate: str,
) -> tuple[ProseBlock, ...]:
    """Verify, restore, and return prose blocks without constructing a final article."""

    candidate_bytes = _utf8_bytes(candidate, boundary=HumanizationVerificationError)
    if len(candidate_bytes) > MAX_RESPONSE_BYTES:
        raise HumanizationVerificationError("candidate exceeded the 5 MiB size limit")
    observed_tokens = tuple(match.group(0) for match in _TOKEN_PATTERN.finditer(candidate))
    if observed_tokens != protected.token_occurrences:
        raise HumanizationVerificationError("protected token multiset or order changed")
    replacements = dict(protected.token_values)
    if any(token not in replacements for token in observed_tokens):
        raise HumanizationVerificationError("protected token was not recognized")
    restored = _TOKEN_PATTERN.sub(lambda match: replacements[match.group(0)], candidate)
    _utf8_bytes(restored, boundary=HumanizationVerificationError)
    _validate_markdown_text(restored)
    blocks = _extract_blocks(restored)
    expected_ids = tuple(block.block_id for block in protected.original_blocks)
    observed_ids = tuple(block.block_id for block in blocks)
    if observed_ids != expected_ids:
        raise HumanizationVerificationError("block IDs or order changed")
    if _link_targets(restored) != protected.link_targets:
        raise HumanizationVerificationError("Markdown link targets changed")
    if _anchor_occurrences(restored, protected.anchors) != protected.anchor_occurrences:
        raise HumanizationVerificationError("protected anchor multiset or order changed")
    original_document = _restore_document(protected)
    if Counter(_markdown_occurrences(restored)) != Counter(
        _markdown_occurrences(original_document)
    ):
        raise HumanizationVerificationError("Markdown markers changed")
    original_sensitive = Counter(_sensitive_occurrences(original_document))
    candidate_sensitive = Counter(_sensitive_occurrences(restored))
    if candidate_sensitive - original_sensitive:
        raise HumanizationVerificationError(
            "candidate introduced a numeric, date, or currency token"
        )
    return blocks


def build_humanization_audit(
    *,
    draft_markdown: str,
    final_markdown: str,
    protected: ProtectedArticleProse,
    candidate: str,
    sources: bytes,
    images: Mapping[str, bytes],
) -> dict[str, object]:
    """Build closed hash material that lets a later audit repeat verification."""

    draft_blocks, draft_skeleton, draft_frontmatter = _article_audit_material(
        draft_markdown
    )
    final_blocks, final_skeleton, final_frontmatter = _article_audit_material(
        final_markdown
    )
    if draft_blocks != protected.original_blocks:
        raise HumanizationVerificationError(
            "draft prose does not match protected humanizer input"
        )
    if draft_skeleton != final_skeleton or draft_frontmatter != final_frontmatter:
        raise HumanizationVerificationError(
            "final article changed frontmatter or factual material"
        )
    if protected.document != _require_text(input_value=protected.document):
        raise HumanizationVerificationError("protected input is invalid")
    rebuilt = protect_article_prose(draft_blocks, protected.anchors)
    if rebuilt != protected:
        raise HumanizationVerificationError("protected humanizer material is inconsistent")
    verified_blocks = verify_humanized_candidate(protected, candidate)
    if final_blocks != verified_blocks:
        raise HumanizationVerificationError(
            "final article prose does not match verified humanizer output"
        )
    hashes = _humanization_audit_hashes(
        draft_markdown=draft_markdown,
        final_markdown=final_markdown,
        frontmatter=draft_frontmatter,
        factual_skeleton=draft_skeleton,
        protected=protected,
        candidate=candidate,
        verified_blocks=verified_blocks,
        sources=sources,
        images=images,
    )
    job_material = {
        "schema_version": 1,
        "anchors": list(protected.anchors),
        "hashes": hashes,
    }
    return {
        **job_material,
        "status": "verified",
        "job_hash": _canonical_sha256(job_material),
    }


def verify_humanization_audit(
    audit: Mapping[str, object],
    *,
    draft_markdown: str,
    final_markdown: str,
    input_document: str,
    candidate: str,
    sources: bytes,
    images: Mapping[str, bytes],
) -> HumanizationAuditResult:
    """Rebuild protection and verify every byte/hash bound by a closed audit."""

    if not isinstance(audit, Mapping) or set(audit) != {
        "schema_version",
        "status",
        "anchors",
        "hashes",
        "job_hash",
    }:
        raise HumanizationVerificationError("humanization audit schema is not closed")
    if audit.get("schema_version") != 1 or audit.get("status") != "verified":
        raise HumanizationVerificationError("humanization audit status is invalid")
    raw_anchors = audit.get("anchors")
    if not isinstance(raw_anchors, list):
        raise HumanizationVerificationError("humanization audit anchors are invalid")
    try:
        anchors = _validated_anchors(raw_anchors)
    except ValueError as exc:
        raise HumanizationVerificationError(
            "humanization audit anchors are invalid"
        ) from exc
    if list(anchors) != raw_anchors:
        raise HumanizationVerificationError("humanization audit anchors are not canonical")
    draft_blocks, draft_skeleton, draft_frontmatter = _article_audit_material(
        draft_markdown
    )
    final_blocks, final_skeleton, final_frontmatter = _article_audit_material(
        final_markdown
    )
    if draft_skeleton != final_skeleton or draft_frontmatter != final_frontmatter:
        raise HumanizationVerificationError(
            "final article changed frontmatter or factual material"
        )
    protected = protect_article_prose(draft_blocks, anchors)
    if protected.document != input_document:
        raise HumanizationVerificationError("humanizer input does not match draft protection")
    verified_blocks = verify_humanized_candidate(protected, candidate)
    if final_blocks != verified_blocks:
        raise HumanizationVerificationError(
            "final article prose does not match verified humanizer output"
        )
    expected_hashes = _humanization_audit_hashes(
        draft_markdown=draft_markdown,
        final_markdown=final_markdown,
        frontmatter=draft_frontmatter,
        factual_skeleton=draft_skeleton,
        protected=protected,
        candidate=candidate,
        verified_blocks=verified_blocks,
        sources=sources,
        images=images,
    )
    if audit.get("hashes") != expected_hashes:
        raise HumanizationVerificationError("humanization audit byte hashes changed")
    job_material = {
        "schema_version": 1,
        "anchors": list(anchors),
        "hashes": expected_hashes,
    }
    job_hash = _canonical_sha256(job_material)
    if audit.get("job_hash") != job_hash:
        raise HumanizationVerificationError("humanization audit job hash changed")
    return HumanizationAuditResult(
        job_hash=job_hash,
        verification_hash=_canonical_sha256(dict(audit)),
    )


def _humanization_audit_hashes(
    *,
    draft_markdown: str,
    final_markdown: str,
    frontmatter: str,
    factual_skeleton: str,
    protected: ProtectedArticleProse,
    candidate: str,
    verified_blocks: tuple[ProseBlock, ...],
    sources: bytes,
    images: Mapping[str, bytes],
) -> dict[str, object]:
    if not isinstance(sources, bytes):
        raise HumanizationVerificationError("humanization audit sources must be bytes")
    image_hashes: dict[str, str] = {}
    for path, payload in sorted(images.items()):
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(payload, bytes)
            or path in image_hashes
        ):
            raise HumanizationVerificationError("humanization audit images are invalid")
        image_hashes[path] = _sha256_bytes(payload)
    if not image_hashes:
        raise HumanizationVerificationError("humanization audit images are missing")
    protection_material = {
        "document_sha256": _sha256_text(protected.document),
        "blocks": [
            [block.block_id, _sha256_text(block.markdown)]
            for block in protected.original_blocks
        ],
        "token_values": [
            [token, _sha256_text(value)] for token, value in protected.token_values
        ],
        "token_occurrences": list(protected.token_occurrences),
        "link_targets": list(protected.link_targets),
        "anchors": list(protected.anchors),
        "anchor_occurrences": list(protected.anchor_occurrences),
    }
    verified_material = [
        [block.block_id, _sha256_text(block.markdown)] for block in verified_blocks
    ]
    return {
        "draft_sha256": _sha256_text(draft_markdown),
        "final_sha256": _sha256_text(final_markdown),
        "frontmatter_sha256": _sha256_text(frontmatter),
        "factual_skeleton_sha256": _sha256_text(factual_skeleton),
        "input_sha256": _sha256_text(protected.document),
        "output_sha256": _sha256_text(candidate),
        "protection_sha256": _canonical_sha256(protection_material),
        "verified_prose_sha256": _canonical_sha256(verified_material),
        "sources_sha256": _sha256_bytes(sources),
        "images": image_hashes,
    }


def _article_audit_material(
    markdown: str,
) -> tuple[tuple[ProseBlock, ...], str, str]:
    document = _require_text(input_value=markdown)
    matches = tuple(_BLOCK_PATTERN.finditer(document))
    if not matches or document.count("<!-- WSW:block:") != len(matches) or document.count(
        "<!-- WSW:endblock:"
    ) != len(matches):
        raise HumanizationVerificationError("article prose block markers are invalid")
    blocks = tuple(
        ProseBlock(match.group("block_id"), match.group("body")) for match in matches
    )
    try:
        _validate_blocks(blocks)
    except ValueError as exc:
        raise HumanizationVerificationError("article prose blocks are invalid") from exc
    pieces: list[str] = []
    cursor = 0
    for match in matches:
        pieces.append(document[cursor : match.start("body")])
        pieces.append(f"[[WSW:AUDIT-PROSE:{match.group('block_id')}]]")
        cursor = match.end("body")
    pieces.append(document[cursor:])
    frontmatter_end = document.find("\n---\n", 4) if document.startswith("---\n") else -1
    if frontmatter_end < 0:
        raise HumanizationVerificationError("article frontmatter is invalid")
    frontmatter = document[: frontmatter_end + len("\n---\n")]
    return blocks, "".join(pieces), frontmatter


def _require_text(*, input_value: str) -> str:
    try:
        payload = _utf8_bytes(input_value, boundary=HumanizationVerificationError)
    except TypeError:
        raise HumanizationVerificationError("humanization audit text is invalid") from None
    if len(payload) > MAX_RESPONSE_BYTES:
        raise HumanizationVerificationError("humanization audit text exceeded size limit")
    return input_value


def _sha256_text(value: str) -> str:
    return _sha256_bytes(_utf8_bytes(value, boundary=HumanizationVerificationError))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise HumanizationVerificationError(
            "humanization audit material is not canonical"
        ) from exc
    return _sha256_bytes(payload)


def _validated_loopback_base_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or value.endswith("/")
        or "?" in value
        or "#" in value
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    ):
        raise ValueError("humanizer URL must be an unambiguous loopback HTTP URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except (TypeError, ValueError):
        raise ValueError("humanizer URL must be an unambiguous loopback HTTP URL") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("humanizer URL must be an unambiguous loopback HTTP URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("humanizer URL must use a literal loopback IP address") from None
    if not address.is_loopback:
        raise ValueError("humanizer URL must use a loopback IP address")
    display_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    display_port = f":{port}" if port is not None else ""
    origin = f"{parsed.scheme.lower()}://{display_host}{display_port}"
    if value != origin:
        raise ValueError("humanizer URL must be an exact loopback HTTP origin")
    return origin


def _run_with_watchdog(
    operation: Callable[[threading.Event], str],
    *,
    deadline: float,
    clock: Callable[[], float] = monotonic,
    wait_for_completion: Callable[[threading.Event, float], bool] | None = None,
) -> str:
    if not _WATCHDOG_SLOTS.acquire(blocking=False):
        raise HumanizationError("Humanizer watchdog capacity is exhausted")
    cancelled = threading.Event()
    completed = threading.Event()
    outcome: dict[str, str | Exception] = {}

    def worker() -> None:
        try:
            outcome["result"] = operation(cancelled)
        except Exception as exc:
            outcome["error"] = exc
        finally:
            _WATCHDOG_SLOTS.release()
            completed.set()

    thread = threading.Thread(
        target=worker,
        name="local-humanizer-watchdog",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        _WATCHDOG_SLOTS.release()
        raise HumanizationError("Humanizer watchdog could not start") from None
    remaining = deadline - clock()
    wait = wait_for_completion or (lambda event, timeout: event.wait(timeout))
    if remaining <= 0 or not wait(completed, remaining):
        cancelled.set()
        raise HumanizationError("Humanizer request exceeded the total deadline")
    error = outcome.get("error")
    if isinstance(error, Exception):
        raise error
    result = outcome.get("result")
    if not isinstance(result, str):
        raise HumanizationError("Humanizer watchdog returned no result")
    return result


def _consume_transform_stream(
    response: httpx.Response,
    *,
    deadline: float,
    cancelled: threading.Event,
) -> str:
    parser = _NdjsonBuffer()
    received = 0
    state = _StreamState()
    for chunk in response.iter_bytes():
        if cancelled.is_set() or monotonic() > deadline:
            raise HumanizationError("Humanizer request exceeded the total deadline")
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise HumanizationError("Humanizer response exceeded the 5 MiB size limit")
        parser.feed(chunk, state)
    if cancelled.is_set() or monotonic() > deadline:
        raise HumanizationError("Humanizer request exceeded the total deadline")
    parser.finish(state)
    if not state.done:
        raise HumanizationError("Humanizer NDJSON stream was truncated before done")
    return "".join(state.deltas)


@dataclass
class _StreamState:
    accepted: bool = False
    result_started: bool = False
    done: bool = False
    event_count: int = 0
    result_bytes: int = 0
    deltas: list[str] | None = None

    def __post_init__(self) -> None:
        if self.deltas is None:
            self.deltas = []


@dataclass
class _NdjsonBuffer:
    pending: bytearray = field(default_factory=bytearray)
    scan_offset: int = 0
    scan_work: int = 0
    compactions: int = 0
    compaction_work: int = 0

    def feed(self, chunk: bytes, state: _StreamState) -> None:
        self.pending.extend(chunk)
        consumed = 0
        while True:
            line_end = self.pending.find(b"\n", self.scan_offset)
            if line_end < 0:
                self.scan_work += len(self.pending) - self.scan_offset
                self.scan_offset = len(self.pending)
                break
            self.scan_work += line_end + 1 - self.scan_offset
            line = bytes(self.pending[consumed:line_end])
            consumed = line_end + 1
            self.scan_offset = consumed
            if line.endswith(b"\r"):
                line = line[:-1]
            if line:
                _consume_event_bytes(line, state)
        if consumed:
            remaining = len(self.pending) - consumed
            self.compaction_work += remaining
            self.compactions += 1
            del self.pending[:consumed]
            self.scan_offset -= consumed

    def finish(self, state: _StreamState) -> None:
        if self.pending:
            _consume_event_bytes(bytes(self.pending), state)
        self.pending.clear()
        self.scan_offset = 0


def _consume_event_bytes(line: bytes, state: _StreamState) -> None:
    try:
        decoded = line.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise HumanizationError("Humanizer response was not valid UTF-8") from None
    _consume_event_line(decoded, state)


def _consume_event_line(line: str, state: _StreamState) -> None:
    try:
        event = _strict_json_loads(line)
    except (ValueError, RecursionError):
        raise HumanizationError("Humanizer returned invalid NDJSON") from None
    if not isinstance(event, dict) or not all(isinstance(key, str) for key in event):
        raise HumanizationError("Humanizer returned an invalid event schema")
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise HumanizationError("Humanizer returned an invalid event schema")
    if state.done:
        raise HumanizationError("Humanizer returned an event after terminal done")
    if state.event_count == 0 and event_type != "accepted":
        raise HumanizationError("Humanizer stream did not begin with accepted")
    state.event_count += 1
    _validate_event(event_type, event)
    if state.result_started and event_type not in {"result-delta", "done"}:
        raise HumanizationError("Humanizer returned an event after result-start")
    if event_type == "accepted":
        if state.accepted:
            raise HumanizationError("Humanizer returned duplicate accepted events")
        state.accepted = True
    elif event_type == "result-start":
        if state.result_started:
            raise HumanizationError("Humanizer returned duplicate result-start events")
        state.result_started = True
    elif event_type == "result-delta":
        if not state.result_started:
            raise HumanizationError("Humanizer returned a result delta before result-start")
        assert state.deltas is not None
        delta_bytes = _utf8_bytes(event["text"], boundary=HumanizationError)
        state.deltas.append(event["text"])
        state.result_bytes += len(delta_bytes)
        if state.result_bytes > MAX_RESULT_BYTES:
            raise HumanizationError("Humanizer result exceeded the 5 MiB size limit")
    elif event_type == "done":
        if not state.result_started:
            raise HumanizationError("Humanizer returned done before result-start")
        state.done = True
    elif event_type == "error":
        raise HumanizationError(f"Humanizer returned terminal error {event['code']}")


def _validate_event(event_type: str, event: dict[str, object]) -> None:
    if event_type == "accepted":
        _exact_keys(event, {"type", "jobId", "position"})
        _string(event, "jobId", nonempty=True)
        _integer(event, "position")
    elif event_type == "queued":
        _exact_keys(event, {"type", "position"})
        _integer(event, "position")
    elif event_type == "progress":
        required = {"type", "phase", "current", "total", "message"}
        optional = {"completedUnits", "totalUnits"}
        if not required.issubset(event) or not set(event).issubset(required | optional):
            raise HumanizationError("Humanizer returned an invalid event schema")
        if event["phase"] not in _EVENT_PHASES:
            raise HumanizationError("Humanizer returned an invalid event schema")
        _integer(event, "current")
        _integer(event, "total")
        _string(event, "message")
        for key in optional & set(event):
            _integer(event, key)
    elif event_type == "warning":
        _exact_keys(event, {"type", "code", "message"})
        _string(event, "code", nonempty=True)
        _string(event, "message")
    elif event_type == "result-start":
        _exact_keys(event, {"type"})
    elif event_type == "result-delta":
        _exact_keys(event, {"type", "text"})
        _string(event, "text")
    elif event_type == "done":
        _exact_keys(event, {"type", "sourceChars", "outputChars", "chunks", "elapsedMs"})
        for key in ("sourceChars", "outputChars", "chunks", "elapsedMs"):
            _integer(event, key)
    elif event_type == "error":
        _exact_keys(event, {"type", "code", "message", "retryable"})
        if event["code"] not in _ERROR_CODES or type(event["retryable"]) is not bool:
            raise HumanizationError("Humanizer returned an invalid event schema")
        _string(event, "message")
    else:
        raise HumanizationError("Humanizer returned an unknown event type")


def _exact_keys(event: Mapping[str, object], expected: set[str]) -> None:
    if set(event) != expected:
        raise HumanizationError("Humanizer returned an invalid event schema")


def _string(event: Mapping[str, object], key: str, *, nonempty: bool = False) -> None:
    value = event.get(key)
    if not isinstance(value, str) or (nonempty and not value):
        raise HumanizationError("Humanizer returned an invalid event schema")


def _integer(event: Mapping[str, object], key: str) -> None:
    value = event.get(key)
    if type(value) is not int or value < 0:
        raise HumanizationError("Humanizer returned an invalid event schema")


def _read_limited_response(response: httpx.Response, *, deadline: float) -> bytes:
    chunks: list[bytes] = []
    received = 0
    for chunk in response.iter_bytes():
        if monotonic() > deadline:
            raise HumanizationError("Humanizer health request timed out")
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise HumanizationError("Humanizer health response exceeded the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _valid_health_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    required = {
        "status",
        "engine",
        "engineVersion",
        "humanizeVersion",
        "activeJobs",
        "queuedJobs",
    }
    if set(payload) != required or payload.get("status") != "ready":
        return False
    return (
        payload.get("engine") == "codex-cli"
        and isinstance(payload.get("engineVersion"), str)
        and isinstance(payload.get("humanizeVersion"), str)
        and type(payload.get("activeJobs")) is int
        and payload["activeJobs"] >= 0
        and type(payload.get("queuedJobs")) is int
        and payload["queuedJobs"] >= 0
    )


def _coerce_blocks(
    blocks: Mapping[str, str] | Iterable[ProseBlock],
) -> tuple[ProseBlock, ...]:
    if isinstance(blocks, Mapping):
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in blocks.items()
        ):
            raise ValueError("prose blocks must map string block IDs to string Markdown")
        return tuple(ProseBlock(key, value) for key, value in blocks.items())
    try:
        material = tuple(blocks)
    except TypeError:
        raise ValueError("prose blocks must be a mapping or iterable of ProseBlock") from None
    if not all(isinstance(block, ProseBlock) for block in material):
        raise ValueError("prose blocks must contain ProseBlock values")
    return material


def _validate_blocks(blocks: tuple[ProseBlock, ...]) -> None:
    if not blocks:
        raise ValueError("at least one prose block is required")
    ids = tuple(block.block_id for block in blocks)
    invalid_id = any(_BLOCK_ID_PATTERN.fullmatch(value) is None for value in ids)
    if len(set(ids)) != len(ids) or invalid_id:
        raise ValueError("prose block id must be unique and safe")
    for block in blocks:
        if not isinstance(block.markdown, str):
            raise ValueError("prose block Markdown must be text")
        if "<!-- WSW:" in block.markdown:
            raise ValueError("prose contains a reserved WSW marker")


def _validated_anchors(anchors: Iterable[str]) -> tuple[str, ...]:
    try:
        material = tuple(anchors)
    except TypeError:
        raise ValueError("protected anchors must be an iterable of strings") from None
    result: list[str] = []
    seen: set[str] = set()
    for anchor in material:
        if not isinstance(anchor, str):
            raise ValueError("protected anchors must contain strings")
        _utf8_bytes(anchor, boundary=ValueError)
        if anchor and anchor not in seen:
            seen.add(anchor)
            result.append(anchor)
    return tuple(result)


def _protected_values(document: str, anchors: tuple[str, ...]) -> tuple[str, ...]:
    values: set[str] = set()
    digit_spans = tuple(match.span() for match in _DIGIT_TOKEN_PATTERN.finditer(document))
    for anchor in anchors:
        if anchor in document and not _anchor_overlaps_spans(document, anchor, digit_spans):
            values.add(anchor)
    values.update(_markdown_protected_lines(document))
    for pattern in (_MARKDOWN_PATTERN, _URL_PATTERN, *_SENSITIVE_PATTERNS):
        values.update(match.group(0) for match in pattern.finditer(document) if match.group(0))
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def _anchor_overlaps_spans(
    document: str,
    anchor: str,
    spans: tuple[tuple[int, int], ...],
) -> bool:
    cursor = 0
    while (start := document.find(anchor, cursor)) >= 0:
        end = start + len(anchor)
        if any(start < span_end and end > span_start for span_start, span_end in spans):
            return True
        cursor = start + 1
    return False


def _anchor_occurrences(document: str, anchors: tuple[str, ...]) -> tuple[str, ...]:
    occurrences: list[tuple[int, int, int, str]] = []
    for anchor_index, anchor in enumerate(anchors):
        cursor = 0
        while (start := document.find(anchor, cursor)) >= 0:
            occurrences.append((start, -len(anchor), anchor_index, anchor))
            cursor = start + 1
    occurrences.sort()
    return tuple(anchor for _start, _length, _index, anchor in occurrences)


def _restore_document(protected: ProtectedArticleProse) -> str:
    replacements = dict(protected.token_values)
    return _TOKEN_PATTERN.sub(lambda match: replacements[match.group(0)], protected.document)


def _extract_blocks(document: str) -> tuple[ProseBlock, ...]:
    blocks: list[ProseBlock] = []
    cursor = 0
    for match in _BLOCK_PATTERN.finditer(document):
        if document[cursor : match.start()].strip():
            raise HumanizationVerificationError("content appeared outside protected prose blocks")
        blocks.append(ProseBlock(match.group("block_id"), match.group("body")))
        cursor = match.end()
    if document[cursor:].strip():
        raise HumanizationVerificationError("content appeared outside protected prose blocks")
    if not blocks:
        raise HumanizationVerificationError("protected prose block markers were missing")
    return tuple(blocks)


def _link_targets(document: str) -> tuple[str, ...]:
    return tuple(match.group("target") for match in _LINK_PATTERN.finditer(document))


def _sensitive_occurrences(document: str) -> tuple[str, ...]:
    matches: list[tuple[int, int, str]] = []
    for pattern in _SENSITIVE_PATTERNS:
        matches.extend(
            (match.start(), match.end(), match.group(0))
            for match in pattern.finditer(document)
        )
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    result: list[str] = []
    end = -1
    for start, stop, value in matches:
        if start >= end:
            result.append(value)
            end = stop
    return tuple(result)


def _markdown_occurrences(document: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _MARKDOWN_PATTERN.finditer(document))


def _markdown_protected_lines(document: str) -> tuple[str, ...]:
    protected: list[str] = []
    lines = document.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index]
        first_line = line.rstrip("\r\n")
        reference = _REFERENCE_START_PATTERN.fullmatch(first_line)
        if reference is not None:
            block = [line]
            index += 1
            rest = reference.group("rest")
            destination_valid = False
            title_present = False
            if rest:
                destination_valid, title_present = _parse_reference_destination(rest)
            elif index < len(lines):
                destination = _indented_reference_content(lines[index])
                if destination is not None:
                    destination_valid, title_present = _parse_reference_destination(
                        destination
                    )
                    if destination_valid:
                        block.append(lines[index])
                        index += 1
            if destination_valid and not title_present and index < len(lines):
                title = _indented_reference_content(lines[index])
                if title is not None and _is_reference_title(title):
                    block.append(lines[index])
                    index += 1
            protected.append("".join(block))
            continue
        if "](" in line:
            protected.append(line)
        index += 1
    return tuple(protected)


def _indented_reference_content(line: str) -> str | None:
    content = line.rstrip("\r\n")
    if not content.strip() or not content.startswith((" ", "\t")):
        return None
    return content.lstrip(" \t")


def _parse_reference_destination(value: str) -> tuple[bool, bool]:
    material = value.strip(" \t")
    if not material or material.startswith(("\"", "'")):
        return False, False
    destination_end = (
        _angle_destination_end(material)
        if material.startswith("<")
        else _bare_destination_end(material)
    )
    if destination_end is None:
        return False, False
    remainder = material[destination_end:].strip(" \t")
    if remainder and not _is_reference_title(remainder):
        return False, False
    return True, bool(remainder)


def _angle_destination_end(value: str) -> int | None:
    if not value.startswith("<"):
        return None
    escaped = False
    for index, character in enumerate(value[1:], start=1):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "<":
            return None
        elif character == ">":
            return index + 1
    return None


def _bare_destination_end(value: str) -> int | None:
    depth = 0
    escaped = False
    for index, character in enumerate(value):
        codepoint = ord(character)
        if codepoint <= 0x20 or codepoint == 0x7F:
            if character in " \t" and depth == 0:
                return index
            return None
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                return None
            depth -= 1
    return len(value) if depth == 0 and not escaped else None


def _is_reference_title(value: str) -> bool:
    return any(pattern.fullmatch(value) is not None for pattern in _REFERENCE_TITLE_PATTERNS)


def _validate_markdown_text(document: str) -> None:
    for character in document:
        category = unicodedata.category(character)
        if category == "Cs" or (category == "Cc" and character not in "\n\r\t"):
            raise HumanizationVerificationError("candidate is not valid UTF-8 Markdown text")
    if "\x00" in document:
        raise HumanizationVerificationError("candidate is not valid UTF-8 Markdown text")


def _utf8_bytes(value: object, *, boundary: type[Exception]) -> bytes:
    if not isinstance(value, str):
        raise boundary("value must be UTF-8 text")
    if any(unicodedata.category(character) in {"Cf", "Cs"} for character in value):
        raise boundary("value is not valid UTF-8 text")
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise boundary("value is not valid UTF-8 text") from None


def _media_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _strict_json_loads(value: str) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError("non-standard JSON number")

    return json.loads(
        value,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
    )
