from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

import httpx
import pytest

from apps.local_content.humanizer import (
    MAX_RESPONSE_BYTES,
    HumanizationError,
    HumanizationVerificationError,
    HumanizerClient,
    protect_article_prose,
    verify_humanized_candidate,
)
from apps.local_content.rendering import ArticleSource, ProseBlock, RenderedArticle


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


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield from self._chunks


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


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://localhost:3210",
        "ftp://127.0.0.1:3210",
        "http://user:secret@127.0.0.1:3210",
        "http://127.0.0.1:3210/unexpected",
        "http://127.0.0.1:3210?target=elsewhere",
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
    candidate = protected.document.replace("확인하십시오.", "꼭 확인해 보세요.")
    candidate = candidate.replace("비교하십시오.", "함께 살펴보세요.")

    result = verify_humanized_candidate(protected, candidate)

    assert result == (
        ProseBlock("intro", f"정확한 내용은 {original_link}에서 꼭 확인해 보세요."),
        ProseBlock("context", "신청 전 원문을 함께 살펴보세요."),
    )


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


def test_block_ids_must_be_unique_and_safe() -> None:
    with pytest.raises(ValueError, match="block id"):
        protect_article_prose(
            (ProseBlock("intro", "하나"), ProseBlock("intro", "둘")), anchors=()
        )
    with pytest.raises(ValueError, match="block id"):
        protect_article_prose((ProseBlock("../facts", "하나"),), anchors=())
