from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class PublisherCapabilities:
    create: bool = True
    update: bool = True
    unpublish: bool = True
    mark_withdrawn: bool = True
    draft: bool = True
    schedule: bool = False
    media_upload: bool = False
    max_title_chars: int = 0
    max_body_bytes: int = 0
    max_media_count: int = 0
    supported_media_types: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "create": self.create,
            "update": self.update,
            "unpublish": self.unpublish,
            "mark_withdrawn": self.mark_withdrawn,
            "draft": self.draft,
            "schedule": self.schedule,
            "media_upload": self.media_upload,
            "max_title_chars": self.max_title_chars,
            "max_body_bytes": self.max_body_bytes,
            "max_media_count": self.max_media_count,
            "supported_media_types": list(self.supported_media_types),
        }


@dataclass(frozen=True)
class RenderedMedia:
    asset_id: str
    delivery_kind: str
    delivery_id: str
    delivery_url: str
    mime_type: str
    checksum: str
    alt_text: str
    caption: str = ""
    attribution: str = ""
    rights_status: str = "allowed"


@dataclass(frozen=True)
class RenderedArticle:
    article_id: str
    revision_no: int
    channel_role: str
    render_stage: str
    title: str
    body_html: str
    source_links: tuple[str, ...]
    included_claim_ids: tuple[str, ...]
    canonical_link_state: str
    template_hash: str
    content_hash: str
    source_manifest_hash: str
    canonical_source_url: str | None = None
    labels: tuple[str, ...] = ()
    media: tuple[RenderedMedia, ...] = ()
    correction_history: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PublishCommand:
    publication_attempt_id: str
    action: str
    target_command_hash: str
    idempotency_key: str
    remote_lookup_key: str
    target_id: str
    publication_intent_id: str
    approval_id: str
    approval_subject_hash: str
    target_snapshot_id: str
    target_config_hash: str
    publisher_contract_version: str
    publisher_adapter_manifest_hash: str
    remote_post_id: str | None = None
    rendered_article: RenderedArticle | None = None
    auto_publish_activation_id: str | None = None
    auto_publish_activation_hash: str | None = None
    publish_at: datetime | None = None
    requested_at: datetime | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class PublishResult:
    status: str
    remote_post_id: str | None = None
    remote_url: str | None = None
    remote_state: str = "unknown"
    remote_revision: str | None = None
    scheduled_for: datetime | None = None
    published_at: datetime | None = None
    request_id: str | None = None
    reconcile_required: bool = False
    http_status: int | None = None
    error_code: str | None = None
    error_detail_redacted: str | None = None


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    remote_identity: str | None
    remote_url: str | None
    capabilities: PublisherCapabilities
    checks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    error_code: str | None = None


class PublisherError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        category: str,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
        detail_redacted: str = "",
    ):
        super().__init__(code)
        self.code = code
        self.category = category
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.detail_redacted = detail_redacted[:500]


class PublisherAdapter(Protocol):
    capabilities: PublisherCapabilities

    def preflight_connection(self) -> PreflightResult: ...

    def execute(self, command: PublishCommand) -> PublishResult: ...

    def reconcile(self, command: PublishCommand) -> PublishResult: ...

    def fetch_remote_state(self, remote_post_id: str) -> PublishResult: ...

