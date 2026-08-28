from __future__ import annotations

import gzip
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

import apps.local_content.humanizer as humanizer_module
from apps.local_content.humanizer import (
    MAX_RESPONSE_BYTES,
    HumanizationError,
    HumanizationVerificationError,
    HumanizerClient,
    protect_article_prose,
    verify_humanized_candidate,
)
from apps.local_content.rendering import ArticleSource, ProseBlock, RenderedArticle


def _closed_audit_fixture() -> dict[str, object]:
    build_audit = humanizer_module.build_humanization_audit
    verify_audit = humanizer_module.verify_humanization_audit
    article = RenderedArticle(
        title="공식 주거 공고",
        slug="official-housing-notice",
        frontmatter=(
            'title: "공식 주거 공고"\n'
            "source_checksum: " + "a" * 64 + "\n"
            "external_id: official-1"
        ),
        prose_blocks=(
            ProseBlock("intro", "기존 안내 문장입니다."),
            ProseBlock("context", "공식 링크에서 확인하세요."),
        ),
        factual_markdown="## 공식 사실\n\n- 공급 상태: 접수 중",
        sources=(),
        protected_anchors=("공식 링크",),
    )
    protected = protect_article_prose(
        article.prose_blocks,
        article.protected_anchors,
    )
    candidate = protected.document.replace("기존 안내", "다듬은 안내")
    verified_blocks = verify_humanized_candidate(protected, candidate)
    final_article = replace(article, prose_blocks=verified_blocks)
    sources = b'[{"source_key":"applyhome"}]\n'
    images = {
        "assets/hero.png": b"hero-image-bytes",
        "assets/summary-card.webp": b"summary-image-bytes",
        "assets/timeline.webp": b"timeline-image-bytes",
    }
    audit = build_audit(
        draft_markdown=article.to_markdown(),
        final_markdown=final_article.to_markdown(),
        protected=protected,
        candidate=candidate,
        sources=sources,
        images=images,
    )
    return {
        "verify": verify_audit,
        "audit": audit,
        "draft": article.to_markdown(),
        "final": final_article.to_markdown(),
        "input": protected.document,
        "output": candidate,
        "sources": sources,
        "images": images,
    }


def _event_bytes(events: list[dict[str, object]], *, trailing_newline: bool = True) -> bytes:
    payload = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    if trailing_newline:
        payload += "\n"
    return payload.encode("utf-8")


def _transport(
    body: bytes,
    *,
    status_code: int = 200,
    content_type: str = "application/x-ndjson; charset=utf-8",
    inspect_request: Callable[[httpx.Request], None] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if inspect_request is not None:
            inspect_request(request)
        return httpx.Response(
            status_code,
            headers={"content-type": content_type},
            content=body,
        )

    return httpx.MockTransport(handler)


@contextmanager
def _acquire_semaphore_slots_for_evidence(
    semaphore: threading.BoundedSemaphore,
    *,
    count: int,
    timeout: float,
) -> Iterator[list[bool]]:
    acquired_slots: list[bool] = []
    try:
        for _ in range(count):
            acquired_slots.append(semaphore.acquire(timeout=timeout))
        yield acquired_slots
    finally:
        for acquired in acquired_slots:
            if acquired:
                semaphore.release()


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield from self._chunks


class _BlockingStream(httpx.SyncByteStream):
    def __init__(
        self,
        blocked: threading.Event,
        release: threading.Event,
        exited: threading.Event,
    ) -> None:
        self._blocked = blocked
        self._release = release
        self._exited = exited

    def __iter__(self):  # type: ignore[no-untyped-def]
        try:
            self._blocked.set()
            self._release.wait()
            yield _event_bytes(COMPLETE_EVENTS)
        finally:
            self._exited.set()


class _MidStreamBlock(httpx.SyncByteStream):
    def __init__(
        self,
        blocked: threading.Event,
        release: threading.Event,
        exited: threading.Event,
    ) -> None:
        self._blocked = blocked
        self._release = release
        self._exited = exited

    def __iter__(self):  # type: ignore[no-untyped-def]
        try:
            yield _event_bytes([COMPLETE_EVENTS[0], {"type": "result-start"}])
            self._blocked.set()
            self._release.wait()
            yield _event_bytes(
                [
                    {"type": "result-delta", "text": "늦은 결과"},
                    COMPLETE_EVENTS[-1],
                ]
            )
        finally:
            self._exited.set()


COMPLETE_EVENTS: list[dict[str, object]] = [
    {"type": "accepted", "jobId": "j1", "position": 0},
    {"type": "queued", "position": 1},
    {
        "type": "progress",
        "phase": "transform",
        "current": 1,
        "total": 1,
        "completedUnits": 1,
        "totalUnits": 1,
        "message": "1/1 구간을 다듬는 중입니다.",
    },
    {"type": "warning", "code": "STYLE_NOTICE", "message": "표현을 보존했습니다."},
    {"type": "result-start"},
    {"type": "result-delta", "text": "자연스러운 "},
    {"type": "result-delta", "text": "문장"},
    {"type": "done", "sourceChars": 2, "outputChars": 8, "chunks": 1, "elapsedMs": 10},
]


def test_client_accepts_captured_sibling_contract_and_sends_plain_text() -> None:
    def inspect(request: httpx.Request) -> None:
        assert request.method == "POST"
        assert str(request.url) == "http://127.0.0.1:3210/api/transform"
        assert request.headers["content-type"] == "text/plain; charset=utf-8"
        assert request.headers["accept"] == "application/x-ndjson"
        assert request.content.decode("utf-8") == "원문"

    client = HumanizerClient(
        "http://127.0.0.1:3210",
        transport=_transport(_event_bytes(COMPLETE_EVENTS), inspect_request=inspect),
    )

    assert client.transform("원문") == "자연스러운 문장"


def test_client_decodes_utf8_and_ndjson_split_across_stream_chunks() -> None:
    payload = _event_bytes(COMPLETE_EVENTS)
    korean_boundary = payload.index("자연스러운".encode()) + 1
    line_boundary = payload.index(b"\n") + 1

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson; charset=utf-8"},
            stream=_ChunkStream(
                [
                    payload[:line_boundary],
                    payload[line_boundary:korean_boundary],
                    payload[korean_boundary : korean_boundary + 1],
                    payload[korean_boundary + 1 :],
                ]
            ),
        )

    client = HumanizerClient(
        "http://127.0.0.1:3210", transport=httpx.MockTransport(handler)
    )

    assert client.transform("원문") == "자연스러운 문장"


def test_client_total_deadline_returns_while_a_late_read_is_blocked() -> None:
    blocked = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            stream=_MidStreamBlock(blocked, release, exited),
        )

    client = HumanizerClient(
        "http://127.0.0.1:3210",
        transport=httpx.MockTransport(handler),
        deadline_seconds=0.05,
    )
    started = time.monotonic()
    try:
        with pytest.raises(HumanizationError, match="total deadline"):
            client.transform("원문")
        elapsed = time.monotonic() - started
        assert blocked.is_set()
        assert elapsed < 0.5
    finally:
        release.set()
        assert exited.wait(1.0)


def test_client_caps_lingering_blocked_watchdog_workers() -> None:
    blocked_reads: list[threading.Event] = []
    releases: list[threading.Event] = []
    exits: list[threading.Event] = []

    def blocking_client() -> HumanizerClient:
        blocked = threading.Event()
        release = threading.Event()
        exited = threading.Event()
        blocked_reads.append(blocked)
        releases.append(release)
        exits.append(exited)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/x-ndjson"},
                stream=_BlockingStream(blocked, release, exited),
            )

        return HumanizerClient(
            "http://127.0.0.1:3210",
            transport=httpx.MockTransport(handler),
            deadline_seconds=0.02,
        )

    try:
        for _ in range(humanizer_module.MAX_LINGERING_HUMANIZATIONS):
            with pytest.raises(HumanizationError, match="total deadline"):
                blocking_client().transform("원문")
            assert blocked_reads[-1].is_set()

        started = time.monotonic()
        with pytest.raises(HumanizationError, match="capacity"):
            blocking_client().transform("원문")
        assert time.monotonic() - started < 0.2
    finally:
        for release in releases:
            release.set()
        for exited in exits[:-1]:
            assert exited.wait(1.0)
        with _acquire_semaphore_slots_for_evidence(
            humanizer_module._WATCHDOG_SLOTS,
            count=humanizer_module.MAX_LINGERING_HUMANIZATIONS,
            timeout=1.0,
        ) as reacquired_slots:
            assert reacquired_slots == [True, True]

    success_client = HumanizerClient(
        "http://127.0.0.1:3210", transport=_transport(_event_bytes(COMPLETE_EVENTS))
    )
    assert success_client.transform("원문") == "자연스러운 문장"


def test_semaphore_evidence_cleanup_survives_a_failed_assertion() -> None:
    slots = threading.BoundedSemaphore(2)

    with pytest.raises(AssertionError):
        with _acquire_semaphore_slots_for_evidence(
            slots,
            count=2,
            timeout=0.0,
        ) as acquired_slots:
            assert acquired_slots == [True, False]

    reacquired_slots: list[bool] = []
    try:
        reacquired_slots = [slots.acquire(blocking=False) for _ in range(2)]
        assert reacquired_slots == [True, True]
    finally:
        for reacquired in reacquired_slots:
            if reacquired:
                slots.release()


def test_watchdog_recomputes_remaining_budget_after_thread_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    started = threading.Event()
    thread_start_called = threading.Event()
    observed_waits: list[float] = []
    real_thread = threading.Thread

    class TrackingThread(real_thread):
        def start(self) -> None:
            super().start()
            thread_start_called.set()

    monkeypatch.setattr(humanizer_module.threading, "Thread", TrackingThread)

    def operation(_cancelled: threading.Event) -> str:
        started.set()
        release.wait()
        return "late"

    def wait_for_completion(event: threading.Event, timeout: float) -> bool:
        assert started.wait(1.0)
        observed_waits.append(timeout)
        return event.is_set()

    def post_setup_clock() -> float:
        assert thread_start_called.is_set()
        return 9.75

    try:
        with pytest.raises(HumanizationError, match="total deadline"):
            humanizer_module._run_with_watchdog(
                operation,
                deadline=10.0,
                clock=post_setup_clock,
                wait_for_completion=wait_for_completion,
            )
        assert observed_waits == [0.25]
    finally:
        release.set()
        reacquired = humanizer_module._WATCHDOG_SLOTS.acquire(timeout=1.0)
        assert reacquired
        if reacquired:
            humanizer_module._WATCHDOG_SLOTS.release()


@pytest.mark.parametrize(
    "events",
    [
        COMPLETE_EVENTS[:-1],
        [*COMPLETE_EVENTS, COMPLETE_EVENTS[-1]],
        [
            COMPLETE_EVENTS[0],
            {"type": "result-delta", "text": "시작 전 결과"},
            COMPLETE_EVENTS[4],
            COMPLETE_EVENTS[-1],
        ],
        [
            COMPLETE_EVENTS[0],
            {"type": "error", "code": "ENGINE_TIMEOUT", "message": "지연", "retryable": True},
        ],
        [
            COMPLETE_EVENTS[0],
            COMPLETE_EVENTS[4],
            COMPLETE_EVENTS[4],
            COMPLETE_EVENTS[-1],
        ],
        [
            *COMPLETE_EVENTS,
            {
                "type": "progress",
                "phase": "verify",
                "current": 1,
                "total": 1,
                "message": "late",
            },
        ],
    ],
    ids=[
        "truncated",
        "double-done",
        "delta-before-start",
        "error-event",
        "double-result-start",
        "event-after-done",
    ],
)
def test_client_rejects_invalid_terminal_streams(events: list[dict[str, object]]) -> None:
    client = HumanizerClient(
        "http://127.0.0.1:3210", transport=_transport(_event_bytes(events))
    )

    with pytest.raises(HumanizationError):
        client.transform("원문")


@pytest.mark.parametrize(
    "late_event",
    [
        {"type": "queued", "position": 1},
        {
            "type": "progress",
            "phase": "verify",
            "current": 1,
            "total": 1,
            "message": "late progress",
        },
        {"type": "warning", "code": "LATE", "message": "late warning"},
        {"type": "accepted", "jobId": "j2", "position": 0},
        {
            "type": "error",
            "code": "ENGINE_TIMEOUT",
            "message": "late error",
            "retryable": True,
        },
    ],
    ids=["queued", "progress", "warning", "accepted", "error"],
)
def test_client_rejects_non_result_event_after_result_start(
    late_event: dict[str, object],
) -> None:
    events = [
        COMPLETE_EVENTS[0],
        {"type": "result-start"},
        late_event,
        COMPLETE_EVENTS[-1],
    ]
    client = HumanizerClient(
        "http://127.0.0.1:3210", transport=_transport(_event_bytes(events))
    )

    with pytest.raises(HumanizationError, match="after result-start"):
        client.transform("원문")


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b'{"type":"accepted"}\nnot-json\n', "application/x-ndjson"),
        (b'{"type":"accepted","jobId":"j1","position":0}\n\xff\n', "application/x-ndjson"),
        (
            _event_bytes(
                [
                    {"type": "accepted", "jobId": "j1", "position": 0},
                    {
                        "type": "progress",
                        "phase": "unknown",
                        "current": 0,
                        "total": 1,
                        "message": "x",
                    },
                ]
            ),
            "application/x-ndjson",
        ),
        (_event_bytes(COMPLETE_EVENTS), "application/json"),
    ],
    ids=["invalid-json", "invalid-utf8", "invalid-event-schema", "wrong-content-type"],
)
def test_client_rejects_malformed_protocol(body: bytes, content_type: str) -> None:
    client = HumanizerClient(
        "http://127.0.0.1:3210",
        transport=_transport(body, content_type=content_type),
    )

    with pytest.raises(HumanizationError):
        client.transform("원문")


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"accepted","jobId":"j1","jobId":"j2","position":0}',
        '{"type":"accepted","jobId":"j1","position":NaN}',
    ],
    ids=["duplicate-key", "non-standard-number"],
)
def test_client_rejects_json_not_accepted_by_sibling_json_parse(line: str) -> None:
    client = HumanizerClient(
        "http://127.0.0.1:3210",
        transport=_transport((line + "\n").encode("utf-8")),
    )

    with pytest.raises(HumanizationError, match="invalid NDJSON"):
        client.transform("원문")


@pytest.mark.parametrize("status_code", [400, 429, 503])
def test_client_rejects_http_errors_without_exposing_body(
    status_code: int, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "본문과 서버 진단은 로그에 남으면 안 됩니다"
    client = HumanizerClient(
        "http://127.0.0.1:3210",
        transport=_transport(secret.encode("utf-8"), status_code=status_code),
    )

    with pytest.raises(HumanizationError, match=str(status_code)) as caught:
        client.transform(secret)

    assert secret not in str(caught.value)
    assert secret not in caplog.text


def test_client_rejects_response_larger_than_five_mib() -> None:
    client = HumanizerClient(
        "http://127.0.0.1:3210",
        transport=_transport(b" " * (MAX_RESPONSE_BYTES + 1)),
    )

    with pytest.raises(HumanizationError, match="5 MiB"):
        client.transform("원문")


def test_client_rejects_decompressed_response_larger_than_five_mib() -> None:
    compressed = gzip.compress(b" " * (MAX_RESPONSE_BYTES + 1))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/x-ndjson",
                "content-encoding": "gzip",
            },
            stream=_ChunkStream([compressed]),
        )

    client = HumanizerClient(
        "http://127.0.0.1:3210", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(HumanizationError, match="5 MiB"):
        client.transform("원문")


def test_client_rejects_input_larger_than_five_mib() -> None:
    client = HumanizerClient(
        "http://127.0.0.1:3210", transport=_transport(_event_bytes(COMPLETE_EVENTS))
    )

    with pytest.raises(HumanizationError, match="input.*5 MiB"):
        client.transform("x" * (MAX_RESPONSE_BYTES + 1))


def test_client_input_cap_is_independent_from_response_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=_event_bytes(COMPLETE_EVENTS))

    monkeypatch.setattr(humanizer_module, "MAX_INPUT_BYTES", 3)
    monkeypatch.setattr(humanizer_module, "MAX_RESPONSE_BYTES", 1000)
    client = HumanizerClient(
        "http://127.0.0.1:3210", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(HumanizationError, match="input.*5 MiB"):
        client.transform("four")
    assert called is False


def test_client_enforces_result_limit_independently_of_response_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_limit = humanizer_module.MAX_RESULT_BYTES
    monkeypatch.setattr(humanizer_module, "MAX_RESPONSE_BYTES", result_limit + 4096)
    events = [
        COMPLETE_EVENTS[0],
        {"type": "result-start"},
        {"type": "result-delta", "text": "x" * (result_limit + 1)},
        COMPLETE_EVENTS[-1],
    ]
    client = HumanizerClient(
        "http://127.0.0.1:3210", transport=_transport(_event_bytes(events))
    )

    with pytest.raises(HumanizationError, match="result.*5 MiB"):
        client.transform("원문")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:3210",
        "ftp://127.0.0.1:3210",
        "http://user:secret@127.0.0.1:3210",
        "http://127.0.0.1:3210/unexpected",
        "http://127.0.0.1:3210?target=elsewhere",
        "http://127.0.0.1:3210/",
        "http://127.0.0.1:3210?",
        "http://127.0.0.1:3210#",
        "\nhttp://127.0.0.1:3210",
        "http://127.0.0.1:3210\n",
        " http://127.0.0.1:3210",
    ],
)
def test_client_refuses_non_loopback_or_ambiguous_urls(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        HumanizerClient(url)


def test_health_requires_ready_sibling_contract() -> None:
    ready = {
        "status": "ready",
        "engine": "codex-cli",
        "engineVersion": "0.149.0",
        "humanizeVersion": "2.3.2",
        "activeJobs": 0,
        "queuedJobs": 0,
    }
    client = HumanizerClient(
        "http://127.0.0.1:3210",
        transport=_transport(
            json.dumps(ready).encode("utf-8"), content_type="application/json"
        ),
    )

    assert client.health() is True


@pytest.mark.parametrize(
    "events",
    [
        [{"type": "accepted", "jobId": "j1", "position": True}],
        [
            COMPLETE_EVENTS[0],
            {"type": "result-start"},
            {
                "type": "done",
                "sourceChars": 2,
                "outputChars": 2,
                "chunks": False,
                "elapsedMs": 1,
            },
        ],
    ],
    ids=["accepted-position", "done-chunks"],
)
def test_client_rejects_boolean_integer_metrics(events: list[dict[str, object]]) -> None:
    client = HumanizerClient(
        "http://127.0.0.1:3210", transport=_transport(_event_bytes(events))
    )

    with pytest.raises(HumanizationError, match="event schema"):
        client.transform("원문")


def test_client_disables_redirects_and_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client
    options: dict[str, object] = {}
    requests: list[httpx.Request] = []

    def client_factory(**kwargs: object) -> httpx.Client:
        options.update(kwargs)

        def redirect(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9999/elsewhere"})

        kwargs["transport"] = httpx.MockTransport(redirect)
        return real_client(**kwargs)

    monkeypatch.setattr(humanizer_module.httpx, "Client", client_factory)
    client = HumanizerClient("http://127.0.0.1:3210")

    with pytest.raises(HumanizationError, match="302"):
        client.transform("원문")

    assert options["follow_redirects"] is False
    assert options["trust_env"] is False
    assert len(requests) == 1


def test_protection_hides_anchors_numbers_links_and_markdown_structure() -> None:
    blocks = (
        ProseBlock(
            "intro",
            "## 안내\n\n양주회천 A-26BL은 2026년 8월 28일 10:30에 "
            "1,200세대를 모집합니다. [공식 공고](https://example.test/a?id=26)를 확인하세요.",
        ),
        ProseBlock("strategy", "문의는 02-1234-5678이며 보증금은 ₩3,000만원입니다."),
    )

    protected = protect_article_prose(
        blocks,
        anchors=("양주회천 A-26BL", "2026년 8월 28일"),
    )

    assert [block.block_id for block in protected.original_blocks] == ["intro", "strategy"]
    for value in (
        "양주회천 A-26BL",
        "2026년 8월 28일",
        "10:30",
        "1,200세대",
        "https://example.test/a?id=26",
        "02-1234-5678",
        "₩3,000만원",
        "<!-- WSW:block:intro -->",
        "## ",
    ):
        assert value not in protected.document
    assert "[[P0001]]" in protected.document


def test_protection_covers_entire_contiguous_token_containing_a_digit() -> None:
    source = (
        "변동률은 -12.5%이고 면적은 .5평, 공급은 10평이며 일정은 "
        "2026년 8월 28일, 10층 A-26BL입니다."
    )
    protected = protect_article_prose({"intro": source}, anchors=())
    protected_values = {value for _token, value in protected.token_values}

    assert {
        "-12.5%이고",
        ".5평,",
        "10평이며",
        "2026년",
        "8월",
        "28일,",
        "10층",
        "A-26BL입니다.",
    }.issubset(protected_values)
    without_tokens = protected.document
    for token in protected.token_occurrences:
        without_tokens = without_tokens.replace(token, "")
    assert not any(character.isdigit() for character in without_tokens)


def test_digit_tokens_take_priority_over_overlapping_anchor_spans() -> None:
    source = "one housing A-26x two housing A-26y"
    protected = protect_article_prose(
        {"intro": source},
        anchors=("housing A-26",),
    )
    values = {value: token for token, value in protected.token_values}

    assert "A-26x" in values
    assert "A-26y" in values
    assert "x" not in protected.document
    assert "y" not in protected.document
    assert protected.anchor_occurrences == (
        "housing A-26",
        "housing A-26",
    )

    first = values["A-26x"]
    second = values["A-26y"]
    suffix_swap = protected.document.replace(first, "[[SWAP]]", 1)
    suffix_swap = suffix_swap.replace(second, first, 1).replace("[[SWAP]]", second, 1)
    with pytest.raises(HumanizationVerificationError, match="protected token"):
        verify_humanized_candidate(protected, suffix_swap)

    anchor_change = protected.document.replace("one housing", "one lodging", 1)
    with pytest.raises(HumanizationVerificationError, match="anchor"):
        verify_humanized_candidate(protected, anchor_change)


def test_overlapping_anchor_occurrences_verify_without_false_positive() -> None:
    protected = protect_article_prose(
        {"intro": "housing A-26x and housing A-26x"},
        anchors=("housing A-26", "A-26", "housing"),
    )

    assert verify_humanized_candidate(protected, protected.document) == (
        ProseBlock("intro", "housing A-26x and housing A-26x"),
    )


@pytest.mark.parametrize(
    ("source_value", "mutated_value"),
    [
        ("-12.5%입니다.", "+12.5%입니다."),
        ("10평입니다.", "10㎡입니다."),
        ("2026년입니다.", "2027년입니다."),
        ("8월입니다.", "9월입니다."),
        ("10층입니다.", "11호입니다."),
    ],
)
def test_candidate_cannot_change_sign_unit_date_or_floor_token(
    source_value: str, mutated_value: str
) -> None:
    protected = protect_article_prose({"intro": f"값은 {source_value}"}, anchors=())
    token = next(token for token, value in protected.token_values if value == source_value)
    candidate = protected.document.replace(token, mutated_value)

    with pytest.raises(HumanizationVerificationError, match="protected token"):
        verify_humanized_candidate(protected, candidate)


def test_protected_values_must_survive_exactly() -> None:
    protected = protect_article_prose(
        {"intro": "양주회천 A-26BL은 2026년 8월 28일 공고됐습니다."},
        anchors=("양주회천 A-26BL", "2026년 8월 28일"),
    )
    token = protected.token_occurrences[0]
    tampered = protected.document.replace(token, "", 1)

    with pytest.raises(HumanizationVerificationError, match="protected token"):
        verify_humanized_candidate(protected, tampered)


def test_protected_token_order_cannot_change() -> None:
    protected = protect_article_prose(
        {"intro": "첫 일정은 2026-08-28이고 다음 일정은 2026-08-29입니다."},
        anchors=(),
    )
    first, second = protected.token_occurrences[1:3]
    tampered = protected.document.replace(first, "[[SWAP]]", 1).replace(second, first, 1)
    tampered = tampered.replace("[[SWAP]]", second, 1)

    with pytest.raises(HumanizationVerificationError, match="protected token"):
        verify_humanized_candidate(protected, tampered)


@pytest.mark.parametrize(
    "insertion",
    ["새 공급은 77세대입니다.", "새 일정은 2027-01-03입니다.", "추가 금액은 ₩9,000입니다."],
)
def test_candidate_cannot_introduce_new_numeric_date_or_currency_tokens(insertion: str) -> None:
    protected = protect_article_prose({"intro": "기존 안내 문장입니다."}, anchors=())
    candidate = protected.document.replace("기존 안내 문장입니다.", insertion)

    with pytest.raises(HumanizationVerificationError, match="numeric, date, or currency"):
        verify_humanized_candidate(protected, candidate)


@pytest.mark.parametrize("insertion", ["값은 -12.5%입니다.", "면적은 .5평입니다."])
def test_candidate_cannot_introduce_signed_or_leading_decimal_tokens(insertion: str) -> None:
    protected = protect_article_prose({"intro": "기존 안내입니다."}, anchors=())
    candidate = protected.document.replace("기존 안내입니다.", insertion)

    with pytest.raises(HumanizationVerificationError, match="numeric, date, or currency"):
        verify_humanized_candidate(protected, candidate)


def test_candidate_must_preserve_block_ids_and_order() -> None:
    protected = protect_article_prose(
        (ProseBlock("intro", "첫 문장"), ProseBlock("context", "둘째 문장")),
        anchors=(),
    )
    first_marker, second_marker = protected.token_occurrences[0], protected.token_occurrences[2]
    tampered = protected.document.replace(first_marker, "[[SWAP]]", 1)
    tampered = tampered.replace(second_marker, first_marker, 1).replace(
        "[[SWAP]]", second_marker, 1
    )

    with pytest.raises(HumanizationVerificationError, match="protected token|block"):
        verify_humanized_candidate(protected, tampered)


def test_candidate_cannot_add_material_outside_prose_blocks() -> None:
    protected = protect_article_prose({"intro": "원래 문장"}, anchors=())

    with pytest.raises(HumanizationVerificationError, match="outside protected prose"):
        verify_humanized_candidate(protected, "source_checksum: forged\n" + protected.document)


def test_task8_factual_and_manifest_slots_never_cross_or_change_at_boundary() -> None:
    article = RenderedArticle(
        title="전송 금지 제목 2026",
        slug="secret-slug",
        frontmatter="source_checksum: deadbeef",
        prose_blocks=(ProseBlock("intro", "딱딱한 안내 문장입니다."),),
        factual_markdown="| 공급 | 1,200세대 |\n![이미지](assets/hero.png)",
        sources=(
            ArticleSource(
                source_key="lh",
                title="전송 금지 출처",
                publisher="LH",
                url="https://apply.lh.or.kr/secret",
                checksum="a" * 64,
            ),
        ),
        protected_anchors=("전송 금지 출처",),
    )
    protected = protect_article_prose(article.prose_blocks, article.protected_anchors)

    assert "deadbeef" not in protected.document
    assert "1,200세대" not in protected.document
    assert "assets/hero.png" not in protected.document
    assert "https://apply.lh.or.kr/secret" not in protected.document
    verified_blocks = verify_humanized_candidate(
        protected,
        protected.document.replace("딱딱한", "자연스러운"),
    )
    candidate_article = replace(article, prose_blocks=verified_blocks)

    assert candidate_article.frontmatter == article.frontmatter
    assert candidate_article.factual_markdown == article.factual_markdown
    assert candidate_article.sources == article.sources
    assert candidate_article.title == article.title


def test_verified_candidate_restores_links_and_returns_only_prose_blocks() -> None:
    original_link = "[공식 공고](<https://example.test/path?q=26>)"
    protected = protect_article_prose(
        (
            ProseBlock("intro", f"정확한 내용은 {original_link}에서 확인하십시오."),
            ProseBlock("context", "신청 전 원문을 비교하십시오."),
        ),
        anchors=(),
    )
    candidate = protected.document.replace("비교하십시오.", "함께 살펴보세요.")

    result = verify_humanized_candidate(protected, candidate)

    assert result == (
        ProseBlock("intro", f"정확한 내용은 {original_link}에서 확인하십시오."),
        ProseBlock("context", "신청 전 원문을 함께 살펴보세요."),
    )


def test_whole_lines_protect_nested_links_and_reference_definitions() -> None:
    linked_line = (
        "중첩 주소는 [공식 링크](https://example.test/a_(b\\)c)?q=(d))에서 확인합니다."
    )
    reference_line = '[공식]: https://example.test/a_(b\\)c) "공식 제목"'
    protected = protect_article_prose(
        {"intro": f"{linked_line}\n{reference_line}\n다른 문장입니다."}, anchors=()
    )

    assert linked_line not in protected.document
    assert reference_line not in protected.document
    result = verify_humanized_candidate(
        protected,
        protected.document.replace("다른 문장입니다.", "이 문장만 다듬었습니다."),
    )
    assert result == (
        ProseBlock("intro", f"{linked_line}\n{reference_line}\n이 문장만 다듬었습니다."),
    )

    link_token = next(
        token
        for token, value in protected.token_values
        if value.rstrip("\r\n") == linked_line
    )
    tampered = protected.document.replace(
        link_token, linked_line.replace("example.test", "evil.test") + "\n"
    )
    with pytest.raises(HumanizationVerificationError, match="protected token"):
        verify_humanized_candidate(protected, tampered)


def test_reference_definition_protection_includes_its_line_ending() -> None:
    reference_line = "[공식]: https://example.test/a_(b)c"
    protected = protect_article_prose(
        {"intro": f"{reference_line}\n다음 문장입니다."}, anchors=()
    )
    reference_token, reference_value = next(
        (token, value)
        for token, value in protected.token_values
        if value.rstrip("\r\n") == reference_line
    )
    assert reference_value.endswith("\n")
    candidate = protected.document.replace(reference_token, reference_line, 1)

    with pytest.raises(HumanizationVerificationError, match="protected token|Markdown"):
        verify_humanized_candidate(protected, candidate)


def test_multiline_reference_definition_is_one_protected_logical_block() -> None:
    reference_block = (
        "[공식]:\n"
        "  <https://example.test/a_(b)c>\n"
        '  "공식 제목 (원문)"\n'
    )
    protected = protect_article_prose(
        {"intro": f"{reference_block}다음 문장입니다."}, anchors=()
    )
    token, value = next(
        (token, value)
        for token, value in protected.token_values
        if value.startswith("[공식]:")
    )

    assert value == reference_block
    assert "example.test" not in protected.document
    assert verify_humanized_candidate(
        protected,
        protected.document.replace("다음 문장입니다.", "다듬은 문장입니다."),
    ) == (ProseBlock("intro", f"{reference_block}다듬은 문장입니다."),)

    split_target_mutation = protected.document.replace(
        token,
        reference_block.replace("example.test", "evil.test"),
        1,
    )
    with pytest.raises(HumanizationVerificationError, match="protected token"):
        verify_humanized_candidate(protected, split_target_mutation)


@pytest.mark.parametrize(
    ("source", "protected_value", "mutable_values"),
    [
        (
            "[공식]:\n"
            "  <https://example.test/a_(b)c>\n"
            '  "유효 제목"\n'
            "  추가 들여쓰기 문장\n"
            "본문입니다.",
            '[공식]:\n  <https://example.test/a_(b)c>\n  "유효 제목"\n',
            ("추가 들여쓰기 문장", "본문입니다."),
        ),
        (
            "[공식]:\n"
            "  목적지가 아닌 여러 단어 문장\n"
            '  "제목처럼 보이는 다음 줄"\n'
            "본문입니다.",
            "[공식]:\n",
            ("목적지가 아닌 여러 단어 문장", "제목처럼 보이는 다음 줄"),
        ),
        (
            "[공식]: https://example.test/a\n"
            "  제목 문법이 아닌 일반 문장\n"
            '  "두 번째 줄도 흡수 금지"\n'
            "본문입니다.",
            "[공식]: https://example.test/a\n",
            ("제목 문법이 아닌 일반 문장", "두 번째 줄도 흡수 금지"),
        ),
        (
            "[공식]:\n"
            "  <https://example.test/a>\n"
            "  (유효 괄호 제목)\n"
            "본문입니다.",
            "[공식]:\n  <https://example.test/a>\n  (유효 괄호 제목)\n",
            ("본문입니다.",),
        ),
    ],
    ids=[
        "excess-indented-prose",
        "invalid-destination",
        "invalid-title",
        "valid-parenthesized-title",
    ],
)
def test_reference_definition_uses_bounded_valid_continuation_grammar(
    source: str,
    protected_value: str,
    mutable_values: tuple[str, ...],
) -> None:
    protected = protect_article_prose({"intro": source}, anchors=())

    assert protected_value in {value for _token, value in protected.token_values}
    for mutable in mutable_values:
        assert mutable in protected.document


@pytest.mark.parametrize(
    ("source", "protected_value", "invalid_continuation"),
    [
        (
            "[공식]:\n  <https://example.test/a<b>\n본문입니다.",
            "[공식]:\n",
            "<https://example.test/a<b>",
        ),
        (
            "[공식]:\n  <https://example.test/a\n본문입니다.",
            "[공식]:\n",
            "<https://example.test/a",
        ),
        (
            "[공식]:\n  https://example.test/a(b c)\n본문입니다.",
            "[공식]:\n",
            "https://example.test/a(b c)",
        ),
        (
            "[공식]:\n"
            "  https://example.test/a\n"
            "  (bad (title)\n"
            "본문입니다.",
            "[공식]:\n  https://example.test/a\n",
            "(bad (title)",
        ),
    ],
    ids=[
        "nested-angle-destination",
        "unterminated-angle-destination",
        "bare-destination-whitespace-in-parentheses",
        "parenthesized-title-with-inner-parenthesis",
    ],
)
def test_invalid_reference_continuation_is_not_whole_line_protected(
    source: str,
    protected_value: str,
    invalid_continuation: str,
) -> None:
    protected = protect_article_prose({"intro": source}, anchors=())
    protected_values = {value for _token, value in protected.token_values}

    assert protected_value in protected_values
    assert all(invalid_continuation not in value for value in protected_values)


@pytest.mark.parametrize(
    ("source", "protected_value", "mutable_text"),
    [
        (
            "[공식]: https://example.test/a\n  \"제목\"\n본문입니다.",
            '[공식]: https://example.test/a\n  "제목"\n',
            "본문입니다.",
        ),
        (
            "[공식]:\n\n  들여쓴 일반 문장\n본문입니다.",
            "[공식]:\n",
            "들여쓴 일반 문장",
        ),
        (
            "[공식]:\n목적지는 들여쓰지 않음\n본문입니다.",
            "[공식]:\n",
            "목적지는 들여쓰지 않음",
        ),
    ],
    ids=["same-block-title", "blank-terminates", "unindented-terminates"],
)
def test_reference_definition_logical_block_boundaries(
    source: str,
    protected_value: str,
    mutable_text: str,
) -> None:
    protected = protect_article_prose({"intro": source}, anchors=())

    assert protected_value in {value for _token, value in protected.token_values}
    assert mutable_text in protected.document


def test_incremental_ndjson_scanner_does_linear_work_for_one_byte_chunks() -> None:
    parser = humanizer_module._NdjsonBuffer()
    state = humanizer_module._StreamState()
    size = 20_000

    for _ in range(size):
        parser.feed(b"x", state)

    assert parser.scan_work == size
    assert len(parser.pending) == size


def test_ndjson_parser_compacts_large_many_line_chunk_only_once() -> None:
    parser = humanizer_module._NdjsonBuffer()
    state = humanizer_module._StreamState()
    blank_lines = 10_000
    payload = (
        _event_bytes([COMPLETE_EVENTS[0]])
        + (b"\n" * blank_lines)
        + _event_bytes(
            [
                {"type": "result-start"},
                {"type": "result-delta", "text": "결과"},
                COMPLETE_EVENTS[-1],
            ]
        )
    )

    parser.feed(payload, state)

    assert state.done is True
    assert parser.scan_work == len(payload)
    assert parser.compactions == 1
    assert parser.compaction_work == 0
    assert parser.pending == b""


def test_client_accepts_final_unterminated_valid_done_record() -> None:
    client = HumanizerClient(
        "http://127.0.0.1:3210",
        transport=_transport(_event_bytes(COMPLETE_EVENTS, trailing_newline=False)),
    )

    assert client.transform("원문") == "자연스러운 문장"


def test_candidate_preserves_inline_markdown_and_citation_markers() -> None:
    source = "**중요** 안내와 [공식 출처] 표기를 _그대로_ 확인하십시오."
    protected = protect_article_prose({"intro": source}, anchors=())
    assert "**" not in protected.document
    assert "[공식 출처]" not in protected.document
    assert "_" not in protected.document

    result = verify_humanized_candidate(
        protected,
        protected.document.replace("확인하십시오.", "살펴보세요."),
    )

    assert result == (
        ProseBlock("intro", "**중요** 안내와 [공식 출처] 표기를 _그대로_ 살펴보세요."),
    )


def test_candidate_rejects_invalid_utf8_scalar() -> None:
    protected = protect_article_prose({"intro": "원래 문장"}, anchors=())
    candidate = protected.document.replace("원래 문장", "잘못된 \ud800 문장")

    with pytest.raises(HumanizationVerificationError, match="UTF-8"):
        verify_humanized_candidate(protected, candidate)


def test_candidate_rejects_unicode_format_control() -> None:
    protected = protect_article_prose({"intro": "원래 문장"}, anchors=())
    candidate = protected.document.replace("원래 문장", "보이지 않는\u200b 문장")

    with pytest.raises(HumanizationVerificationError, match="UTF-8"):
        verify_humanized_candidate(protected, candidate)


@pytest.mark.parametrize("value", ["원문\u200b", "원문\ud800"])
def test_client_rejects_format_controls_and_surrogates_as_humanization_error(
    value: str,
) -> None:
    client = HumanizerClient(
        "http://127.0.0.1:3210", transport=_transport(_event_bytes(COMPLETE_EVENTS))
    )

    with pytest.raises(HumanizationError, match="UTF-8"):
        client.transform(value)


def test_client_rejects_escaped_surrogate_result_without_raw_encode_error() -> None:
    body = (
        b'{"type":"accepted","jobId":"j1","position":0}\n'
        b'{"type":"result-start"}\n'
        b'{"type":"result-delta","text":"\\ud800"}\n'
        b'{"type":"done","sourceChars":1,"outputChars":1,"chunks":1,"elapsedMs":1}\n'
    )
    client = HumanizerClient("http://127.0.0.1:3210", transport=_transport(body))

    with pytest.raises(HumanizationError, match="UTF-8"):
        client.transform("원문")


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- WSW:block:forged -->",
        "<!-- WSW:endblock:forged -->",
        "<!-- WSW:slot:facts -->",
    ],
)
def test_source_prose_rejects_reserved_wsw_markers(marker: str) -> None:
    with pytest.raises(ValueError, match="reserved WSW marker"):
        protect_article_prose({"intro": f"원문 {marker}"}, anchors=())


def test_closed_humanization_audit_reruns_verifier_and_binds_all_material() -> None:
    material = _closed_audit_fixture()

    result = material["verify"](
        material["audit"],
        draft_markdown=material["draft"],
        final_markdown=material["final"],
        input_document=material["input"],
        candidate=material["output"],
        sources=material["sources"],
        images=material["images"],
    )

    assert result.job_hash == material["audit"]["job_hash"]
    assert len(result.verification_hash) == 64


@pytest.mark.parametrize(
    "tamper",
    ["final_fact", "sources", "image", "output", "protection"],
)
def test_closed_humanization_audit_rejects_counterexamples(tamper: str) -> None:
    material = _closed_audit_fixture()
    audit = deepcopy(material["audit"])
    final = material["final"]
    output = material["output"]
    sources = material["sources"]
    images = dict(material["images"])
    if tamper == "final_fact":
        final = final.replace("공식 사실", "위조 사실")
    elif tamper == "sources":
        sources += b"tampered"
    elif tamper == "image":
        images["assets/hero.png"] += b"tampered"
    elif tamper == "output":
        output = output.replace("다듬은 안내", "변조된 안내")
    else:
        audit["anchors"] = ["변조된 앵커"]

    with pytest.raises(HumanizationVerificationError):
        material["verify"](
            audit,
            draft_markdown=material["draft"],
            final_markdown=final,
            input_document=material["input"],
            candidate=output,
            sources=sources,
            images=images,
        )


def test_block_ids_must_be_unique_and_safe() -> None:
    with pytest.raises(ValueError, match="block id"):
        protect_article_prose(
            (ProseBlock("intro", "하나"), ProseBlock("intro", "둘")), anchors=()
        )
    with pytest.raises(ValueError, match="block id"):
        protect_article_prose((ProseBlock("../facts", "하나"),), anchors=())
