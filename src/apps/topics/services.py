from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.text import slugify

from adapters.sources.manifests import (
    adapter_execution_manifest,
    adapter_execution_manifest_hash,
)
from apps.accounts.services import consume_reauthentication_proof
from apps.audit.models import AuditEvent
from apps.audit.redaction import AuditRedactionError, sanitize_audit_key
from apps.audit.services import (
    AuditContext,
    audit_event_id,
    record_audit_event,
    require_audit_replay,
)
from wisdome_writer.domain.concurrency import require_idempotent_match
from wisdome_writer.domain.errors import (
    Conflict,
    InvalidInput,
    RequestKeyConflict,
    StaleVersion,
    StateConflict,
)
from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)
from wisdome_writer.infrastructure.secrets import SecretRef

from .models import (
    SourceDefinition,
    SourceDefinitionSnapshot,
    SourceRegistryDecision,
    SourceRegistryMembership,
    SourceRegistryMutation,
    SourceRegistrySnapshot,
    TopicCode,
    TopicPolicy,
    TopicRegistryHead,
)


SOURCE_SNAPSHOT_SCHEMA_V2 = "source-definition-snapshot-v2"
SOURCE_SNAPSHOT_SCHEMA_V3 = "source-definition-snapshot-v3"
SOURCE_REGISTRY_MANIFEST_SCHEMA_V2 = "source-registry-manifest-v2"
SOURCE_AUDIT_SCHEMA_V2 = "source-definition-audit-v2"
REGISTRY_AUDIT_SCHEMA_V2 = "source-registry-audit-v2"

SUPPORTED_ADAPTER_KEYS = frozenset(
    {
        "housing_applyhome",
        "housing_lh",
        "open_data_json",
        "public_html",
        "rss",
        "semiconductor_motir",
        "semiconductor_krx_kind",
        "semiconductor_samsung_newsroom",
        "semiconductor_skhynix_newsroom",
        "semiconductor_sia_latest",
    }
)
_ADAPTER_ACCESS_METHODS = {
    "semiconductor_motir": frozenset({"public_html"}),
    "semiconductor_krx_kind": frozenset({"public_html"}),
    "semiconductor_samsung_newsroom": frozenset({"rss_atom"}),
    "semiconductor_skhynix_newsroom": frozenset({"rss_atom"}),
    "semiconductor_sia_latest": frozenset({"public_html"}),
    "housing_applyhome": frozenset(
        {"open_data_api", "public_api", "public_html"}
    ),
    "housing_lh": frozenset(
        {"open_data_api", "public_api", "public_html"}
    ),
    "open_data_json": frozenset({"open_data_api", "public_api"}),
    "public_html": frozenset({"public_file", "public_html"}),
    "rss": frozenset({"rss_atom"}),
}
_ADAPTER_CONFIG_KEYS = {
    **{
        key: frozenset(
            {
                "entrypoints",
                "identityNamespace",
                "maxPages",
                "pageSize",
                "recordHosts",
                "reconciliationDays",
                "maxRequests",
                "maxElapsedSeconds",
                "sourceCheckDays",
                "feedContentTypes",
                "listContentTypes",
                "detailContentTypes",
                "attachmentContentTypes",
                "mediaDownloadPolicy",
                "accessPolicy",
                "rightsPolicy",
                *(("issuerCodes",) if key == "semiconductor_krx_kind" else ()),
            }
        )
        for key in (
            "semiconductor_motir",
            "semiconductor_krx_kind",
            "semiconductor_samsung_newsroom",
            "semiconductor_skhynix_newsroom",
            "semiconductor_sia_latest",
        )
    },
    "housing_applyhome": frozenset(
        {
            "entrypoints",
            "maxPages",
            "pageSize",
            "recordHosts",
            "reconciliationDays",
            "maxRequests",
            "maxElapsedSeconds",
            "sourceCheckDays",
            "apiContentTypes",
            "detailContentTypes",
            "attachmentContentTypes",
            "accessPolicy",
            "rightsPolicy",
        }
    ),
    "housing_lh": frozenset(
        {
            "entrypoints",
            "maxPages",
            "pageSize",
            "detailEntrypoint",
            "supplyEntrypoint",
            "recordHosts",
            "reconciliationDays",
            "maxRequests",
            "maxElapsedSeconds",
            "sourceCheckDays",
            "apiContentTypes",
            "detailContentTypes",
            "attachmentContentTypes",
            "accessPolicy",
            "rightsPolicy",
        }
    ),
    "open_data_json": frozenset(
        {
            "entrypoints",
            "maxPages",
            "pageSize",
            "recordHosts",
            "maxRequests",
            "maxElapsedSeconds",
            "sourceCheckDays",
            "apiContentTypes",
            "attachmentContentTypes",
            "accessPolicy",
            "rightsPolicy",
        }
    ),
    "public_html": frozenset({"entrypoints", "accessPolicy", "rightsPolicy"}),
    "rss": frozenset({"entrypoints", "accessPolicy", "rightsPolicy"}),
}
_MOTIR_ATTACHMENT_MIMES = frozenset(
    {
        "application/pdf",
        "application/haansofthwp",
        "application/x-hwp",
        "application/vnd.hancom.hwp",
        "application/hwp+zip",
        "application/vnd.hancom.hwpx",
        "application/zip",
    }
)
_SEMICONDUCTOR_REQUIRED_CONFIG_KEYS = frozenset(
    {
        "entrypoints",
        "identityNamespace",
        "maxPages",
        "pageSize",
        "recordHosts",
        "reconciliationDays",
        "maxRequests",
        "maxElapsedSeconds",
        "sourceCheckDays",
        "mediaDownloadPolicy",
    }
)
_SEMICONDUCTOR_PROFILES = {
    "semiconductor_motir": {
        "name": "산업통상부",
        "ownerName": "대한민국 산업통상부",
        "editorialControlName": "대한민국 산업통상부",
        "baseUrl": "https://www.motir.go.kr/",
        "authorityTier": "primary_official",
        "independenceGroupId": "kr-motie",
        "defaultRightsStatus": "attribution_required",
        "termsUrl": "https://www.motir.go.kr/kor/contents/81",
        "enabled": True,
        "entrypoints": [
            "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c",
            "https://www.motir.go.kr/kor/article/ATCLe0854704d",
        ],
        "recordHosts": ["www.motir.go.kr"],
        "identityNamespace": "motie:81",
        "pageSize": 50,
        "mimes": {
            "listContentTypes": {"text/html"},
            "detailContentTypes": {"text/html"},
            "attachmentContentTypes": _MOTIR_ATTACHMENT_MIMES,
        },
    },
    "semiconductor_krx_kind": {
        "name": "KRX KIND",
        "ownerName": "한국거래소",
        "editorialControlName": "한국거래소",
        "baseUrl": "https://kind.krx.co.kr/",
        "authorityTier": "primary_regulatory",
        "independenceGroupId": "krx-kind",
        "defaultRightsStatus": "internal_analysis_only",
        "termsUrl": (
            "https://info.krx.co.kr/contents/KRX/06/06070200/"
            "KRX06070200.jsp"
        ),
        "enabled": True,
        "entrypoints": [
            "https://kind.krx.co.kr/disclosure/details.do"
            "?method=searchDetailsMain"
        ],
        "recordHosts": ["kind.krx.co.kr"],
        "identityNamespace": "krx-kind",
        "pageSize": 100,
        "issuerCodes": ["A005930", "A000660"],
        "mimes": {
            "listContentTypes": {"text/html"},
            "detailContentTypes": {"text/html"},
            "attachmentContentTypes": {"text/html", "application/pdf"},
        },
    },
    "semiconductor_samsung_newsroom": {
        "name": "Samsung Global Newsroom Semiconductor",
        "ownerName": "Samsung Electronics",
        "editorialControlName": "Samsung Electronics",
        "baseUrl": "https://news.samsung.com/",
        "authorityTier": "primary_corporate",
        "independenceGroupId": "samsung",
        "defaultRightsStatus": "internal_analysis_only",
        "termsUrl": "https://news.samsung.com/global/terms",
        "enabled": True,
        "entrypoints": [
            "https://news.samsung.com/global/category/products/"
            "semiconductors/feed",
            "https://news.samsung.com/global/category/products/"
            "semiconductors/feed/atom",
        ],
        "recordHosts": [
            "img.global.news.samsung.com",
            "news.samsung.com",
        ],
        "identityNamespace": "samsung-global:wp-post",
        "pageSize": 50,
        "mimes": {
            "feedContentTypes": {
                "application/rss+xml",
                "application/atom+xml",
                "application/xml",
                "text/xml",
            },
            "detailContentTypes": {"text/html"},
            "attachmentContentTypes": {
                "application/pdf",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "audio/mpeg",
                "video/mp4",
            },
        },
    },
    "semiconductor_skhynix_newsroom": {
        "name": "SK hynix Newsroom",
        "ownerName": "SK hynix",
        "editorialControlName": "SK hynix",
        "baseUrl": "https://news.skhynix.com/",
        "authorityTier": "primary_corporate",
        "independenceGroupId": "skhynix",
        "defaultRightsStatus": "prohibited",
        "termsUrl": "https://news.skhynix.com/en/terms-of-use/",
        "enabled": False,
        "entrypoints": [
            "https://news.skhynix.com/en/feed/",
            "https://news.skhynix.com/en/feed/atom/",
        ],
        "recordHosts": [
            "d18r0a86za96sg.cloudfront.net",
            "news.skhynix.com",
        ],
        "identityNamespace": "skhynix:wp-post",
        "pageSize": 10,
        "mimes": {
            "feedContentTypes": {
                "application/rss+xml",
                "application/atom+xml",
                "application/xml",
                "text/xml",
            },
            "detailContentTypes": {"text/html"},
            "attachmentContentTypes": {
                "application/pdf",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "image/jpeg",
                "image/png",
                "image/webp",
            },
        },
    },
    "semiconductor_sia_latest": {
        "name": "Semiconductor Industry Association",
        "ownerName": "Semiconductor Industry Association",
        "editorialControlName": "Semiconductor Industry Association",
        "baseUrl": "https://www.semiconductors.org/",
        "authorityTier": "trusted_industry",
        "independenceGroupId": "sia",
        "defaultRightsStatus": "internal_analysis_only",
        "termsUrl": "https://www.semiconductors.org/terms-of-use/",
        "enabled": True,
        "entrypoints": [
            "https://www.semiconductors.org/news-events/latest-news/"
        ],
        "recordHosts": ["www.semiconductors.org"],
        "identityNamespace": "sia:wp-post",
        "pageSize": 12,
        "mimes": {
            "listContentTypes": {"text/html"},
            "detailContentTypes": {"text/html"},
            "attachmentContentTypes": {
                "application/pdf",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "image/jpeg",
                "image/png",
            },
        },
    },
}
_AUTHENTICATED_ADAPTER_PROFILES = {
    "housing_applyhome": {
        "secretRef": "env://DATA_GO_KR_SERVICE_KEY",
        "host": "api.odcloud.kr",
        "paths": frozenset(
            {
                "/api/ApplyhomeInfoDetailSvc/v1/getAPTLttotPblancDetail",
                "/api/ApplyhomeInfoDetailSvc/v1/getUrbtyOfctlLttotPblancDetail",
                "/api/ApplyhomeInfoDetailSvc/v1/getRemndrLttotPblancDetail",
                "/api/ApplyhomeInfoDetailSvc/v1/getPblPvtRentLttotPblancDetail",
                "/api/ApplyhomeInfoDetailSvc/v1/getOPTLttotPblancDetail",
            }
        ),
        "recordHosts": frozenset(
            {
                "api.odcloud.kr",
                "applyhome.co.kr",
                "www.applyhome.co.kr",
            }
        ),
    },
    "housing_lh": {
        "secretRef": "env://DATA_GO_KR_SERVICE_KEY",
        "host": "apis.data.go.kr",
        "paths": frozenset(
            {
                "/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1",
                "/B552555/lhLeaseNoticeDtlInfo1/getLeaseNoticeDtlInfo1",
                "/B552555/lhLeaseNoticeSplInfo1/getLeaseNoticeSplInfo1",
            }
        ),
        "recordHosts": frozenset(
            {
                "apis.data.go.kr",
                "apply.lh.or.kr",
            }
        ),
    },
    "open_data_json": {
        "secretRef": "env://DATA_GO_KR_SERVICE_KEY",
        "host": "api.odcloud.kr",
        "paths": frozenset(
            {
                "/api/ApplyhomeInfoDetailSvc/v1/getAPTLttotPblancDetail",
            }
        ),
        "recordHosts": frozenset(
            {
                "api.odcloud.kr",
                "applyhome.co.kr",
                "www.applyhome.co.kr",
            }
        ),
    },
}
_ADAPTER_KEY_PATTERN = re.compile(r"^[a-z0-9_.-]+$")
_SECRET_KEY_PATTERN = re.compile(
    r"(?:authorization|cookie|token|password|passwd|secret|api.?key|"
    r"credential|access.?key|private.?key|client.?secret|session)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(
        r"(?:authorization|cookie|token|password|passwd|api.?key|"
        r"credential|client.?secret)\s*(?:=|:|\s)\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+",
        re.IGNORECASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\btop[-_]?secret\b", re.IGNORECASE),
)
_SECRET_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "key",
        "password",
        "secret",
        "service_key",
        "servicekey",
        "session",
        "sig",
        "signature",
        "subscription_key",
        "subscriptionkey",
        "token",
        "x_auth",
        "x_amz_signature",
        "x_goog_signature",
    }
)
_SECRET_COMPACT_KEYS = frozenset(
    re.sub(r"[^a-z0-9]+", "", key)
    for key in _SECRET_QUERY_KEYS
)
_SOURCE_CONTRACT_FIELDS = (
    "topic",
    "name",
    "publisher",
    "authorityTier",
    "independenceGroupId",
    "ownerName",
    "editorialControlName",
    "baseUrl",
    "accessMethod",
    "adapterKey",
    "externalConfig",
    "secretRef",
    "allowedMimeTypes",
    "defaultRightsStatus",
    "termsUrl",
    "robotsUrl",
    "licenseUrl",
    "pollIntervalSeconds",
    "rateLimitPolicy",
    "enabled",
)

_ACCESS_POLICY_SCHEMA = "source-access-policy-v1"
_RIGHTS_POLICY_SCHEMA = "source-rights-policy-v1"
_ACCESS_POLICY_CONFIG_KEY = "accessPolicy"
_RIGHTS_POLICY_CONFIG_KEY = "rightsPolicy"
_ACCESS_POLICY_DECISIONS = frozenset({"approved"})
_POLICY_DECISIONS = frozenset(
    {"approved", "not_applicable", "not_applicable_official_api"}
)
_ACCESS_PURPOSES = frozenset(
    {
        "collection",
        "source_check",
        "record_fetch",
        "attachment",
        "attachment_metadata",
    }
)
_ACCESS_METHODS = frozenset({"GET", "HEAD", "POST"})
_RIGHTS_SCOPES = (
    "record",
    "documentAttachment",
    "mediaAttachment",
)
_RIGHTS_BASIS = frozenset({"license", "terms", "none"})
_RIGHTS_STATUS_ORDER = {
    "prohibited": 0,
    "unknown": 1,
    "internal_analysis_only": 2,
    "attribution_required": 3,
    "allowed": 4,
}


@dataclass(frozen=True)
class RegistryImportResult:
    topic_code: str
    registry_id: str
    source_count: int
    created: bool


def _hash(material: Any) -> str:
    try:
        return canonical_hash(
            material,
            schema_version=CANONICAL_HASH_SCHEMA_V1,
        )
    except (OverflowError, TypeError, UnicodeError, ValueError) as exc:
        raise InvalidInput("Canonical source material is invalid.") from exc


def source_registry_audit_request_key(request_key: str) -> str:
    """Preserve legacy-safe keys and hash only contract-valid unsafe keys."""

    try:
        return sanitize_audit_key(
            request_key,
            field_name="source registry request key",
        )
    except AuditRedactionError:
        return _hash(
            {
                "schemaVersion": "source-registry-audit-request-key-v1",
                "requestKey": request_key,
            }
        )


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


def _require_admin_context(
    *,
    audit_context: AuditContext,
    admin: Any,
    request_key: str,
    reason: str,
) -> None:
    if (
        audit_context.actor_type != AuditEvent.ActorType.ADMIN
        or _id(audit_context.actor_id) != _id(admin.pk)
        or audit_context.request_key
        != source_registry_audit_request_key(request_key)
        or audit_context.reason_code != reason
    ):
        raise ValueError(
            "Audit provenance does not match the source registry administrator."
        )


def _require_admin_audit_replay(
    *,
    context: AuditContext,
    action: str,
    entity: Any,
    identity_key: str,
    request_hash: str,
    admin: Any,
) -> None:
    normalized_identity_key = source_registry_audit_request_key(
        identity_key
    )
    expected_event_id = audit_event_id(
        action=action,
        entity=entity,
        identity_key=normalized_identity_key,
    )
    stored = (
        AuditEvent.objects.using(context.database_alias)
        .only("actor_type", "actor_id")
        .filter(pk=expected_event_id)
        .first()
    )
    if stored is not None and (
        stored.actor_type != AuditEvent.ActorType.ADMIN
        or stored.actor_id != admin.pk
    ):
        raise RequestKeyConflict(
            "The request key belongs to another administrator."
        )
    require_audit_replay(
        context=context,
        action=action,
        entity=entity,
        identity_key=normalized_identity_key,
        request_hash=request_hash,
    )


def _require_request_hash(request_hash: str) -> str:
    if not isinstance(request_hash, str) or not re.fullmatch(
        r"[a-f0-9]{64}",
        request_hash,
    ):
        raise ValueError("A canonical OpenAPI request hash is required.")
    return request_hash


def _validate_public_url(
    value: Any,
    *,
    field_name: str,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > 500:
        raise InvalidInput(f"{field_name} must be a public HTTP URL.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise InvalidInput(f"{field_name} is invalid.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise InvalidInput(
            f"{field_name} must not contain credentials or an invalid target."
        )
    decoded_value = unquote(value)
    if any(ord(character) < 32 for character in decoded_value) or any(
        pattern.search(decoded_value)
        for pattern in _SECRET_VALUE_PATTERNS
    ):
        raise InvalidInput(
            f"{field_name} must not contain credential material."
        )
    del port
    for key, query_value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        if normalized_key in _SECRET_QUERY_KEYS or _SECRET_KEY_PATTERN.search(
            normalized_key
        ):
            raise InvalidInput(
                f"{field_name} must not contain credential query parameters."
            )
        if any(
            pattern.search(query_value)
            for pattern in _SECRET_VALUE_PATTERNS
        ):
            raise InvalidInput(
                f"{field_name} must not contain credential query values."
            )
    return value


def _validate_external_config(
    value: Any,
    *,
    base_url: str,
    depth: int = 0,
    path: str = "externalConfig",
) -> Any:
    if depth > 8:
        raise InvalidInput("externalConfig is nested too deeply.")
    if isinstance(value, Mapping):
        if len(value) > 200:
            raise InvalidInput("externalConfig contains too many fields.")
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 200:
                raise InvalidInput("externalConfig contains an invalid field name.")
            normalized_key = re.sub(
                r"[^a-z0-9]+",
                "",
                key.lower(),
            )
            if (
                normalized_key in _SECRET_COMPACT_KEYS
                or _SECRET_KEY_PATTERN.search(normalized_key)
            ):
                raise InvalidInput(
                    "externalConfig must not contain credential fields."
                )
            if key in {_ACCESS_POLICY_CONFIG_KEY, _RIGHTS_POLICY_CONFIG_KEY}:
                normalized[key] = item
            else:
                normalized[key] = _validate_external_config(
                    item,
                    base_url=base_url,
                    depth=depth + 1,
                    path=f"{path}.{key}",
                )
        return normalized
    if isinstance(value, list):
        if len(value) > 500:
            raise InvalidInput("externalConfig contains too many list items.")
        return [
            _validate_external_config(
                item,
                base_url=base_url,
                depth=depth + 1,
                path=f"{path}[]",
            )
            for item in value
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str) or len(value) > 4000:
        raise InvalidInput("externalConfig contains an invalid value.")
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        raise InvalidInput("externalConfig must not contain credential values.")
    if "://" in value:
        target = _validate_public_url(value, field_name=path)
        base_host = (urlsplit(base_url).hostname or "").rstrip(".").lower()
        target_host = (urlsplit(target).hostname or "").rstrip(".").lower()
        if target_host != base_host:
            raise InvalidInput(
                "externalConfig URL hosts must match the approved baseUrl host."
            )
    return value


def _validate_secret_ref(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 300
        or any(character.isspace() or ord(character) < 32 for character in value)
        or any(character in value for character in ("?", "#", "@"))
    ):
        raise InvalidInput("secretRef is invalid.")
    try:
        reference = SecretRef.parse(value)
    except ValueError as exc:
        raise InvalidInput("secretRef is invalid.") from exc
    if (
        not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", reference.provider)
        or not reference.locator
        or len(reference.locator) > 240
    ):
        raise InvalidInput("secretRef is invalid.")
    return value


def _normalize_hostname_list(
    value: Any,
    *,
    field_name: str,
) -> list[str]:
    if not isinstance(value, list) or not value:
        raise InvalidInput(f"{field_name} must be a non-empty hostname array.")
    normalized: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 253
            or item != item.strip()
            or item.endswith(".")
        ):
            raise InvalidInput(f"{field_name} contains an invalid hostname.")
        hostname = item.encode("idna").decode("ascii").lower()
        labels = hostname.split(".")
        if any(
            not label
            or len(label) > 63
            or re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?",
                label,
            )
            is None
            for label in labels
        ):
            raise InvalidInput(f"{field_name} contains an invalid hostname.")
        normalized.append(hostname)
    if len(set(normalized)) != len(normalized):
        raise InvalidInput(f"{field_name} must not contain duplicates.")
    return sorted(normalized)


def _normalize_mime_type_list(
    value: Any,
    *,
    field_name: str,
) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise InvalidInput(f"{field_name} must be a non-empty MIME array.")
    normalized: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 160
            or re.fullmatch(
                r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+",
                item.lower(),
            )
            is None
        ):
            raise InvalidInput(f"{field_name} contains an invalid MIME type.")
        normalized.append(item.lower())
    return sorted(set(normalized))


def _validate_authenticated_adapter_profile(
    *,
    adapter_key: str,
    access_method: str,
    base_url: str,
    external_config: Mapping[str, Any],
    secret_ref: str | None,
) -> None:
    profile = _AUTHENTICATED_ADAPTER_PROFILES.get(adapter_key)
    if (
        profile is None
        or access_method not in {"open_data_api", "public_api"}
    ):
        return
    parsed_base = urlsplit(base_url)
    expected_host = str(profile["host"])
    if (
        parsed_base.scheme.lower() != "https"
        or (parsed_base.hostname or "").rstrip(".").lower()
        != expected_host
        or parsed_base.port not in {None, 443}
        or parsed_base.path not in {"", "/"}
        or parsed_base.query
    ):
        raise InvalidInput(
            "Authenticated source baseUrl is outside its approved HTTPS profile."
        )
    if secret_ref is not None and secret_ref != profile["secretRef"]:
        raise InvalidInput(
            "secretRef is not bound to the selected source adapter."
        )

    endpoint_values = list(external_config.get("entrypoints", []))
    endpoint_values.extend(
        external_config[field_name]
        for field_name in ("detailEntrypoint", "supplyEntrypoint")
        if external_config.get(field_name)
    )
    approved_paths = profile["paths"]
    for endpoint in endpoint_values:
        parsed = urlsplit(str(endpoint))
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").rstrip(".").lower()
            != expected_host
            or parsed.port not in {None, 443}
            or parsed.path not in approved_paths
            or parsed.query
            or parsed.fragment
        ):
            raise InvalidInput(
                "Authenticated source endpoint is outside its approved HTTPS profile."
            )

    record_hosts = set(external_config.get("recordHosts", []))
    if record_hosts != set(profile["recordHosts"]):
        raise InvalidInput(
            "recordHosts must exactly match the adapter security profile."
        )


def _validate_semiconductor_profile(material: Mapping[str, Any]) -> None:
    adapter_key = str(material["adapterKey"])
    profile = _SEMICONDUCTOR_PROFILES.get(adapter_key)
    if profile is None:
        return
    external_config = material["externalConfig"]
    required_config_keys = {
        *_SEMICONDUCTOR_REQUIRED_CONFIG_KEYS,
        *profile["mimes"],
    }
    missing_config_keys = required_config_keys - set(external_config)
    if missing_config_keys:
        raise InvalidInput(
            f"{adapter_key} externalConfig is missing required fields."
        )
    for field_name in (
        "name",
        "ownerName",
        "editorialControlName",
        "baseUrl",
        "authorityTier",
        "independenceGroupId",
        "defaultRightsStatus",
        "termsUrl",
        "enabled",
    ):
        if field_name in profile and material[field_name] != profile[field_name]:
            raise InvalidInput(
                f"{adapter_key} {field_name} is outside its approved profile."
            )
    for field_name in (
        "entrypoints",
        "recordHosts",
        "identityNamespace",
        "pageSize",
        "issuerCodes",
    ):
        if field_name in profile and external_config.get(field_name) != profile[field_name]:
            raise InvalidInput(
                f"{adapter_key} externalConfig.{field_name} does not match "
                "its approved profile."
            )
    if external_config.get("mediaDownloadPolicy") != "metadata_only":
        raise InvalidInput(
            "Semiconductor mediaDownloadPolicy must be metadata_only."
        )
    approved_mimes = set(material["allowedMimeTypes"])
    required_union: set[str] = set()
    for field_name, expected_values in profile["mimes"].items():
        actual_values = set(external_config.get(field_name, []))
        if actual_values != set(expected_values):
            raise InvalidInput(
                f"{adapter_key} externalConfig.{field_name} does not match "
                "its approved MIME profile."
            )
        required_union.update(actual_values)
    if required_union != approved_mimes:
        raise InvalidInput(
            "Semiconductor allowedMimeTypes must exactly match the union "
            "of its purpose MIME contracts."
        )


def _source_projection_material(source: SourceDefinition) -> dict[str, Any]:
    return {
        "topic": source.topic_code,
        "name": source.display_name,
        "publisher": source.publisher,
        "authorityTier": source.authority_tier,
        "independenceGroupId": source.independence_group,
        "ownerName": source.owner_name,
        "editorialControlName": source.editorial_control_name,
        "baseUrl": source.base_url,
        "accessMethod": source.access_method,
        "adapterKey": source.adapter_key,
        "externalConfig": source.external_config,
        "secretRef": source.secret_ref,
        "allowedMimeTypes": source.allowed_mime_types,
        "defaultRightsStatus": source.default_rights_status,
        "termsUrl": source.terms_url,
        "robotsUrl": source.robots_url,
        "licenseUrl": source.license_url,
        "pollIntervalSeconds": source.poll_interval_seconds,
        "rateLimitPolicy": source.rate_limit_policy,
        "enabled": source.enabled,
    }


def _normalize_access_policy(
    value: Any,
    *,
    access_method: str,
    base_url: str,
    record_hosts: Iterable[str],
) -> dict[str, Any]:
    required = {
        "schemaVersion", "decision", "reviewedAt", "userAgent",
        "trafficScope", "maxHttpAttempts", "maxRetryDelaySeconds",
        "maxRedirects", "maxRequests", "maxElapsedSeconds",
        "originPolicies", "termsDecision", "licenseDecision",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise InvalidInput("externalConfig.accessPolicy is incomplete.")
    if value["schemaVersion"] != _ACCESS_POLICY_SCHEMA:
        raise InvalidInput("accessPolicy schemaVersion is invalid.")
    if value["decision"] not in _ACCESS_POLICY_DECISIONS:
        raise InvalidInput("accessPolicy decision must be approved.")
    reviewed_at = value["reviewedAt"]
    if not isinstance(reviewed_at, str):
        raise InvalidInput("accessPolicy reviewedAt must be ISO-8601.")
    try:
        if datetime.fromisoformat(reviewed_at.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise InvalidInput("accessPolicy reviewedAt must be ISO-8601.") from exc
    user_agent = value["userAgent"]
    if not isinstance(user_agent, str) or not user_agent.strip() or len(user_agent) > 300:
        raise InvalidInput("accessPolicy userAgent is invalid.")
    traffic_scope = value["trafficScope"]
    if traffic_scope not in {"collection", "source_check", "collection_and_source_check"}:
        raise InvalidInput("accessPolicy trafficScope is invalid.")
    numeric_limits = {
        "maxHttpAttempts": (1, 5),
        "maxRetryDelaySeconds": (0, 3600),
        "maxRedirects": (0, 20),
        "maxRequests": (1, 20000),
        "maxElapsedSeconds": (1, 3600),
    }
    normalized: dict[str, Any] = {
        "schemaVersion": _ACCESS_POLICY_SCHEMA,
        "decision": "approved",
        "reviewedAt": reviewed_at,
        "userAgent": user_agent.strip(),
        "trafficScope": traffic_scope,
    }
    for field_name, (minimum, maximum) in numeric_limits.items():
        item = value[field_name]
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise InvalidInput(f"accessPolicy {field_name} is invalid.")
        normalized[field_name] = item
    if value["termsDecision"] not in _POLICY_DECISIONS or value["licenseDecision"] not in _POLICY_DECISIONS:
        raise InvalidInput("accessPolicy legal decisions are invalid.")
    for decision_name in ("termsDecision", "licenseDecision"):
        decision = value[decision_name]
        if decision == "not_applicable_official_api" and access_method not in {"open_data_api", "public_api"}:
            raise InvalidInput("Official API not-applicable decisions require an API access method.")
        normalized[decision_name] = decision
    allowed_hosts = {
        (urlsplit(base_url).hostname or "").rstrip(".").lower(),
        *(str(host).rstrip(".").lower() for host in record_hosts),
    }
    origin_policies = value["originPolicies"]
    if not isinstance(origin_policies, list) or not origin_policies:
        raise InvalidInput("accessPolicy originPolicies are required.")
    normalized_origins: list[dict[str, Any]] = []
    for index, origin in enumerate(origin_policies):
        if not isinstance(origin, Mapping) or set(origin) != {"host", "purposes", "methods", "pathPrefixes", "robotsMode", "robotsUrl"}:
            raise InvalidInput("accessPolicy origin policy is invalid.")
        host = origin["host"]
        if not isinstance(host, str) or host.rstrip(".").lower() not in allowed_hosts:
            raise InvalidInput("accessPolicy origin host is outside source scope.")
        purposes = origin["purposes"]
        methods = origin["methods"]
        prefixes = origin["pathPrefixes"]
        robots_mode = origin["robotsMode"]
        if (not isinstance(purposes, list) or not purposes
                or any(not isinstance(purpose, str) or purpose not in _ACCESS_PURPOSES for purpose in purposes)
                or not isinstance(methods, list) or not methods
                or any(not isinstance(method, str) or method not in _ACCESS_METHODS for method in methods)
                or not isinstance(prefixes, list) or not prefixes
                or any(not isinstance(prefix, str) or not prefix.startswith("/") for prefix in prefixes)
                or robots_mode not in {"runtime_fetch", "not_applicable_official_api"}):
            raise InvalidInput("accessPolicy origin policy is invalid.")
        robots_url = origin["robotsUrl"]
        if robots_mode == "runtime_fetch":
            robots_url = _validate_public_url(robots_url, field_name=f"accessPolicy.originPolicies[{index}].robotsUrl")
            if (
                urlsplit(robots_url).scheme.lower() != "https"
                or (urlsplit(robots_url).hostname or "").rstrip(".").lower()
                != host.rstrip(".").lower()
            ):
                raise InvalidInput("runtime_fetch robotsUrl must use HTTPS.")
        elif robots_url is not None:
            raise InvalidInput("Official API origin policy must not include robotsUrl.")
        normalized_origins.append({
            "host": host.rstrip(".").lower(), "purposes": sorted(set(purposes)),
            "methods": sorted(set(methods)), "pathPrefixes": sorted(set(prefixes)),
            "robotsMode": robots_mode, "robotsUrl": robots_url,
        })
    primary_purposes = {
        purpose
        for origin in normalized_origins
        for purpose in origin["purposes"]
        if purpose in {"collection", "source_check"}
    }
    required_primary_purposes = {
        "collection": {"collection"},
        "source_check": {"source_check"},
        "collection_and_source_check": {"collection", "source_check"},
    }[traffic_scope]
    if primary_purposes != required_primary_purposes:
        raise InvalidInput(
            "accessPolicy trafficScope does not match origin purposes."
        )
    if len({(item["host"], tuple(item["pathPrefixes"])) for item in normalized_origins}) != len(normalized_origins):
        raise InvalidInput("accessPolicy originPolicies must not duplicate scopes.")
    normalized["originPolicies"] = normalized_origins
    return normalized


def _normalize_rights_policy(
    value: Any,
    *,
    default_rights_status: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"schemaVersion", *_RIGHTS_SCOPES}:
        raise InvalidInput("externalConfig.rightsPolicy is incomplete.")
    if value["schemaVersion"] != _RIGHTS_POLICY_SCHEMA:
        raise InvalidInput("rightsPolicy schemaVersion is invalid.")
    normalized: dict[str, Any] = {"schemaVersion": _RIGHTS_POLICY_SCHEMA}
    for scope in _RIGHTS_SCOPES:
        item = value[scope]
        if not isinstance(item, Mapping) or set(item) != {"status", "basis", "basisUrl", "attributionText", "publishable"}:
            raise InvalidInput(f"rightsPolicy {scope} is invalid.")
        status, basis = item["status"], item["basis"]
        if status not in _RIGHTS_STATUS_ORDER or basis not in _RIGHTS_BASIS or not isinstance(item["publishable"], bool):
            raise InvalidInput(f"rightsPolicy {scope} is invalid.")
        basis_url = _validate_public_url(item["basisUrl"], field_name=f"rightsPolicy.{scope}.basisUrl", nullable=True)
        attribution = item["attributionText"]
        if attribution is not None and (not isinstance(attribution, str) or not attribution.strip() or len(attribution) > 1000):
            raise InvalidInput(f"rightsPolicy {scope} attributionText is invalid.")
        if basis == "none":
            if basis_url is not None or attribution is not None or status not in {"unknown", "internal_analysis_only"} or item["publishable"]:
                raise InvalidInput("Rights without a basis must remain non-publishable.")
        elif basis_url is None:
            raise InvalidInput(f"rightsPolicy {scope} requires a basisUrl.")
        if status == "attribution_required" and (basis_url is None or attribution is None):
            raise InvalidInput("Attribution-required rights need basisUrl and attributionText.")
        normalized[scope] = {
            "status": status, "basis": basis, "basisUrl": basis_url,
            "attributionText": attribution.strip() if isinstance(attribution, str) else None,
            "publishable": item["publishable"],
        }
    if normalized["record"]["status"] != default_rights_status:
        raise InvalidInput("rightsPolicy.record status must match defaultRightsStatus.")
    record_level = _RIGHTS_STATUS_ORDER[normalized["record"]["status"]]
    for scope in ("documentAttachment", "mediaAttachment"):
        item = normalized[scope]
        if _RIGHTS_STATUS_ORDER[item["status"]] > record_level or (item["publishable"] and not normalized["record"]["publishable"]):
            raise InvalidInput("Attachment rights cannot be broader than record rights.")
    return normalized


def _normalize_source_material(
    data: Mapping[str, Any],
    *,
    current: SourceDefinition | None = None,
) -> dict[str, Any]:
    if current is None:
        missing = [
            field for field in _SOURCE_CONTRACT_FIELDS if field not in data
        ]
        if missing:
            raise InvalidInput("The complete source definition is required.")
        material = {field: data[field] for field in _SOURCE_CONTRACT_FIELDS}
    else:
        if "topic" in data:
            raise InvalidInput("Source topic is immutable.")
        material = _source_projection_material(current)
        for field in _SOURCE_CONTRACT_FIELDS:
            if field != "topic" and field in data:
                material[field] = data[field]

    if material["topic"] not in TopicCode.values:
        raise InvalidInput("Unsupported source topic.")
    for field in (
        "name",
        "publisher",
        "independenceGroupId",
        "ownerName",
        "editorialControlName",
    ):
        value = material[field]
        if not isinstance(value, str) or not value.strip():
            raise InvalidInput(f"{field} must not be empty.")
        material[field] = value.strip()

    base_url = _validate_public_url(
        material["baseUrl"],
        field_name="baseUrl",
    )
    material["baseUrl"] = base_url
    for field in ("termsUrl", "robotsUrl", "licenseUrl"):
        material[field] = _validate_public_url(
            material[field],
            field_name=field,
            nullable=True,
        )

    adapter_key = material["adapterKey"]
    if (
        not isinstance(adapter_key, str)
        or not _ADAPTER_KEY_PATTERN.fullmatch(adapter_key)
        or adapter_key not in SUPPORTED_ADAPTER_KEYS
    ):
        raise InvalidInput("adapterKey is not registered.")
    external_config = _validate_external_config(
        material["externalConfig"],
        base_url=base_url,
    )
    if not isinstance(external_config, dict):
        raise InvalidInput("externalConfig must be an object.")
    if material["accessMethod"] not in _ADAPTER_ACCESS_METHODS[adapter_key]:
        raise InvalidInput(
            "accessMethod is incompatible with adapterKey."
        )
    unknown_config_keys = (
        set(external_config) - _ADAPTER_CONFIG_KEYS[adapter_key]
    )
    if unknown_config_keys:
        raise InvalidInput(
            "externalConfig contains fields unsupported by adapterKey."
        )
    entrypoints = external_config.get("entrypoints", [])
    if not isinstance(entrypoints, list) or any(
        not isinstance(item, str) for item in entrypoints
    ):
        raise InvalidInput("externalConfig.entrypoints must be a URL array.")
    normalized_entrypoints: list[str] = []
    base_host = (urlsplit(base_url).hostname or "").rstrip(".").lower()
    for index, entrypoint in enumerate(entrypoints):
        normalized_entrypoint = _validate_public_url(
            entrypoint,
            field_name=f"externalConfig.entrypoints[{index}]",
        )
        entrypoint_host = (
            urlsplit(normalized_entrypoint).hostname or ""
        ).rstrip(".").lower()
        if entrypoint_host != base_host:
            raise InvalidInput(
                "externalConfig URL hosts must match the approved baseUrl host."
            )
        normalized_entrypoints.append(normalized_entrypoint)
    if len(set(normalized_entrypoints)) != len(normalized_entrypoints):
        raise InvalidInput(
            "externalConfig.entrypoints must not contain duplicates."
        )
    if material["enabled"] is True and not normalized_entrypoints:
        raise InvalidInput(
            "An enabled source requires a probe entrypoint."
        )
    if "entrypoints" in external_config:
        external_config["entrypoints"] = normalized_entrypoints
    if "recordHosts" in external_config:
        external_config["recordHosts"] = _normalize_hostname_list(
            external_config["recordHosts"],
            field_name="externalConfig.recordHosts",
        )
    for field_name, maximum in (
        ("maxPages", 100),
        ("pageSize", 1000),
        ("reconciliationDays", 3650),
        ("maxRequests", 20000),
        ("maxElapsedSeconds", 3600),
        ("sourceCheckDays", 365),
    ):
        value = external_config.get(field_name)
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > maximum
        ):
            raise InvalidInput(
                f"externalConfig.{field_name} must be an integer "
                f"between 1 and {maximum}."
            )
    if adapter_key == "semiconductor_krx_kind" and external_config.get(
        "issuerCodes"
    ) != ["A005930", "A000660"]:
        raise InvalidInput(
            "KRX issuerCodes must exactly match the approved issuer scope."
        )
    if adapter_key.startswith("semiconductor_") and external_config.get(
        "mediaDownloadPolicy"
    ) != "metadata_only":
        raise InvalidInput(
            "Semiconductor mediaDownloadPolicy must be metadata_only."
        )
    if adapter_key == "housing_lh":
        for field_name in ("detailEntrypoint", "supplyEntrypoint"):
            value = external_config.get(field_name)
            if value is None:
                continue
            normalized_endpoint = _validate_public_url(
                value,
                field_name=f"externalConfig.{field_name}",
            )
            endpoint_host = (
                urlsplit(normalized_endpoint).hostname or ""
            ).rstrip(".").lower()
            if endpoint_host != base_host:
                raise InvalidInput(
                    "externalConfig URL hosts must match the approved baseUrl host."
                )
            external_config[field_name] = normalized_endpoint
        if (
            material["enabled"] is True
            and material["accessMethod"]
            in {"open_data_api", "public_api"}
            and (
                not external_config.get("detailEntrypoint")
                or not external_config.get("supplyEntrypoint")
            )
        ):
            raise InvalidInput(
                "Enabled housing_lh API sources require detail and "
                "supply entrypoints."
            )
    for field_name in (
        "apiContentTypes",
        "feedContentTypes",
        "listContentTypes",
        "detailContentTypes",
        "attachmentContentTypes",
    ):
        if field_name in external_config:
            external_config[field_name] = _normalize_mime_type_list(
                external_config[field_name],
                field_name=f"externalConfig.{field_name}",
            )
    material["externalConfig"] = external_config
    material["secretRef"] = _validate_secret_ref(material["secretRef"])
    if (
        material["enabled"] is True
        and adapter_key in {"housing_applyhome", "housing_lh"}
        and material["accessMethod"] in {"open_data_api", "public_api"}
        and material["secretRef"] is None
    ):
        raise InvalidInput(
            "Enabled authenticated source API requires secretRef."
        )
    _validate_authenticated_adapter_profile(
        adapter_key=adapter_key,
        access_method=material["accessMethod"],
        base_url=base_url,
        external_config=external_config,
        secret_ref=material["secretRef"],
    )

    allowed_mime_types = material["allowedMimeTypes"]
    if (
        not isinstance(allowed_mime_types, list)
        or len(allowed_mime_types) > 100
        or any(
            not isinstance(value, str)
            or not value
            or len(value) > 160
            or "/" not in value
            for value in allowed_mime_types
        )
    ):
        raise InvalidInput("allowedMimeTypes is invalid.")
    material["allowedMimeTypes"] = sorted(set(allowed_mime_types))
    if (
        adapter_key
        in {"housing_applyhome", "housing_lh", "open_data_json"}
        and material["accessMethod"]
        in {"open_data_api", "public_api"}
    ):
        required_mime_fields = {
            "apiContentTypes",
            "attachmentContentTypes",
        }
        if adapter_key in {"housing_applyhome", "housing_lh"}:
            required_mime_fields.add("detailContentTypes")
        if not required_mime_fields.issubset(external_config):
            raise InvalidInput(
                "Authenticated source MIME contracts are incomplete."
            )
        approved_mime_types = set(material["allowedMimeTypes"])
        if any(
            not set(external_config[field_name]).issubset(
                approved_mime_types
            )
            for field_name in required_mime_fields
        ):
            raise InvalidInput(
                "External MIME contracts must be included in allowedMimeTypes."
            )

    if material["authorityTier"] not in {
        *SourceDefinition.AuthorityTier.values,
        "primary_regulatory",
        "trusted_industry",
    }:
        raise InvalidInput("authorityTier is invalid.")
    if material["accessMethod"] not in SourceDefinition.AccessMethod.values:
        raise InvalidInput("accessMethod is invalid.")
    if (
        material["defaultRightsStatus"]
        not in SourceDefinition.RightsStatus.values
    ):
        raise InvalidInput("defaultRightsStatus is invalid.")

    access_policy = _normalize_access_policy(
        external_config.get(_ACCESS_POLICY_CONFIG_KEY),
        access_method=material["accessMethod"],
        base_url=base_url,
        record_hosts=external_config.get("recordHosts", []),
    )
    for decision_name, source_url in (
        ("termsDecision", material["termsUrl"]),
        ("licenseDecision", material["licenseUrl"]),
    ):
        decision = access_policy[decision_name]
        if decision == "approved" and source_url is None:
            raise InvalidInput(f"accessPolicy {decision_name} requires source evidence.")
        if decision == "not_applicable_official_api" and (
            material["accessMethod"] not in {"open_data_api", "public_api"}
            or (material["termsUrl"] is None and material["licenseUrl"] is None)
        ):
            raise InvalidInput("Official API legal exceptions require source evidence.")
    rights_policy = _normalize_rights_policy(
        external_config.get(_RIGHTS_POLICY_CONFIG_KEY),
        default_rights_status=material["defaultRightsStatus"],
    )
    external_config[_ACCESS_POLICY_CONFIG_KEY] = access_policy
    external_config[_RIGHTS_POLICY_CONFIG_KEY] = rights_policy
    if material["enabled"] and not external_config.get("attachmentContentTypes"):
        raise InvalidInput("Enabled sources require explicit attachment MIME types.")

    poll_interval = material["pollIntervalSeconds"]
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, int)
        or poll_interval < 60
        or poll_interval > 9_007_199_254_740_991
    ):
        raise InvalidInput("pollIntervalSeconds is invalid.")

    rate_policy = material["rateLimitPolicy"]
    if not isinstance(rate_policy, Mapping) or set(rate_policy) != {
        "maxConcurrency",
        "requestsPerMinute",
        "burst",
    }:
        raise InvalidInput("rateLimitPolicy is invalid.")
    limits = {
        "maxConcurrency": (1, 50),
        "requestsPerMinute": (1, 10000),
        "burst": (1, 1000),
    }
    normalized_rate_policy: dict[str, int] = {}
    for field, (minimum, maximum) in limits.items():
        value = rate_policy[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise InvalidInput("rateLimitPolicy is invalid.")
        normalized_rate_policy[field] = value
    material["rateLimitPolicy"] = normalized_rate_policy
    if not isinstance(material["enabled"], bool):
        raise InvalidInput("enabled must be boolean.")

    _validate_semiconductor_profile(material)

    # Ensure the exact material is canonicalizable before any row is changed.
    _hash(source_snapshot_material(material))
    return material


def source_snapshot_material(
    source_material: Mapping[str, Any],
) -> dict[str, Any]:
    implementation_manifest = adapter_execution_manifest(
        str(source_material["adapterKey"]),
        access_method=str(source_material["accessMethod"]),
    )
    external_config = source_material["externalConfig"]
    access_policy = external_config[_ACCESS_POLICY_CONFIG_KEY]
    rights_policy = external_config[_RIGHTS_POLICY_CONFIG_KEY]
    return {
        "schemaVersion": SOURCE_SNAPSHOT_SCHEMA_V3,
        **{field: source_material[field] for field in _SOURCE_CONTRACT_FIELDS},
        "accessPolicy": access_policy,
        "accessPolicyHash": _hash(access_policy),
        "rightsPolicy": rights_policy,
        "rightsPolicyHash": _hash(rights_policy),
        "adapterVersion": implementation_manifest["adapterVersion"],
        "adapterImplementationManifestHash": (
            adapter_execution_manifest_hash(implementation_manifest)
        ),
    }


def source_snapshot_hash(source_material: Mapping[str, Any]) -> str:
    return _hash(source_snapshot_material(source_material))


def _is_verifiable_source_snapshot(
    snapshot: SourceDefinitionSnapshot,
    *,
    expected_config_hash: str | None = None,
) -> bool:
    return (
        isinstance(snapshot.frozen_config, dict)
        and snapshot.frozen_config.get("schemaVersion")
        == SOURCE_SNAPSHOT_SCHEMA_V3
        and isinstance(
            snapshot.frozen_config.get("adapterVersion"),
            str,
        )
        and isinstance(
            snapshot.frozen_config.get(
                "adapterImplementationManifestHash"
            ),
            str,
        )
        and isinstance(snapshot.frozen_config.get("accessPolicy"), dict)
        and isinstance(snapshot.frozen_config.get("rightsPolicy"), dict)
        and snapshot.frozen_config.get("accessPolicyHash")
        == _hash(snapshot.frozen_config["accessPolicy"])
        and snapshot.frozen_config.get("rightsPolicyHash")
        == _hash(snapshot.frozen_config["rightsPolicy"])
        and (
            expected_config_hash is None
            or snapshot.config_hash == expected_config_hash
        )
        and _hash(snapshot.frozen_config)
        == snapshot.frozen_config_hash
        and snapshot.frozen_config_hash == snapshot.config_hash
    )


def _apply_source_projection(
    source: SourceDefinition,
    material: Mapping[str, Any],
) -> None:
    source.topic_code = material["topic"]
    source.display_name = material["name"]
    source.publisher = material["publisher"]
    source.authority_tier = material["authorityTier"]
    source.independence_group = material["independenceGroupId"]
    source.owner_name = material["ownerName"]
    source.editorial_control_name = material["editorialControlName"]
    source.base_url = material["baseUrl"]
    source.access_method = material["accessMethod"]
    source.adapter_key = material["adapterKey"]
    source.external_config = material["externalConfig"]
    source.secret_ref = material["secretRef"]
    source.allowed_mime_types = material["allowedMimeTypes"]
    source.default_rights_status = material["defaultRightsStatus"]
    source.terms_url = material["termsUrl"]
    source.robots_url = material["robotsUrl"]
    source.license_url = material["licenseUrl"]
    source.poll_interval_seconds = material["pollIntervalSeconds"]
    source.rate_limit_policy = material["rateLimitPolicy"]
    source.enabled = material["enabled"]


def _source_audit_material(source: SourceDefinition) -> dict[str, Any]:
    projection_hash = _hash(source_snapshot_material(
        _source_projection_material(source)
    ))
    return {
        "schema_version": SOURCE_AUDIT_SCHEMA_V2,
        "source_id": str(source.id),
        "topic_code": source.topic_code,
        "projection_hash": projection_hash,
        "latest_approved_snapshot_version": (
            source.latest_approved_snapshot_version
        ),
        "latest_draft_snapshot_id": _id(source.latest_draft_snapshot_id),
        "latest_draft_snapshot_version": (
            source.latest_draft_snapshot_version
        ),
        "latest_draft_config_hash": source.latest_draft_config_hash,
    }


def _next_source_snapshot_version(
    source: SourceDefinition,
    *,
    using: str,
) -> int:
    latest = (
        SourceDefinitionSnapshot.objects.using(using)
        .filter(source=source)
        .aggregate(value=Max("version"))["value"]
    )
    return int(latest or 0) + 1


def _create_draft_snapshot(
    *,
    source: SourceDefinition,
    material: Mapping[str, Any],
    version: int,
    request_key: str | None,
    request_hash: str | None,
    using: str,
) -> SourceDefinitionSnapshot:
    frozen_config = source_snapshot_material(material)
    frozen_config_hash = source_snapshot_hash(material)
    snapshot = SourceDefinitionSnapshot(
        source=source,
        topic_code=source.topic_code,
        version=version,
        state=SourceDefinitionSnapshot.State.DRAFT,
        config=frozen_config,
        config_hash=frozen_config_hash,
        frozen_config=frozen_config,
        frozen_config_hash=frozen_config_hash,
        independence_group=material["independenceGroupId"],
        owner_name=material["ownerName"],
        editorial_control_name=material["editorialControlName"],
        request_key=request_key,
        request_hash=request_hash,
    )
    snapshot.full_clean()
    snapshot.save(using=using)
    return snapshot


def _retire_current_draft(
    source: SourceDefinition,
    *,
    using: str,
) -> None:
    if source.latest_draft_snapshot_id is None:
        return
    snapshot = (
        SourceDefinitionSnapshot.objects.using(using)
        .select_for_update()
        .get(pk=source.latest_draft_snapshot_id)
    )
    if snapshot.state == SourceDefinitionSnapshot.State.DRAFT:
        snapshot.state = SourceDefinitionSnapshot.State.RETIRED
        snapshot.retired_at = timezone.now()
        snapshot.save(
            update_fields=["state", "retired_at"],
            using=using,
        )


def _source_creation_replay(
    *,
    request_key: str,
    request_hash: str,
    admin: Any,
    audit_context: AuditContext,
) -> SourceDefinition | None:
    existing = SourceDefinition.objects.using(
        audit_context.database_alias
    ).filter(
        creation_request_key=request_key
    ).first()
    if existing is None:
        return None
    require_idempotent_match(
        stored_hash=existing.creation_request_hash or "",
        expected_hash=request_hash,
    )
    _require_admin_audit_replay(
        context=audit_context,
        action="source_definition.created",
        entity=existing,
        identity_key=request_key,
        request_hash=request_hash,
        admin=admin,
    )
    return existing


def create_source_definition(
    data: Mapping[str, Any],
    *,
    admin: Any,
    audit_context: AuditContext,
    request_hash: str,
) -> tuple[SourceDefinition, bool]:
    request_key = str(data["requestKey"])
    reason = "source definition create"
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    request_hash = _require_request_hash(request_hash)
    alias = audit_context.database_alias

    existing = _source_creation_replay(
        request_key=request_key,
        request_hash=request_hash,
        admin=admin,
        audit_context=audit_context,
    )
    if existing is not None:
        return existing, False

    material = _normalize_source_material(data)
    with transaction.atomic(using=alias):
        _lock_topic_head(material["topic"], using=alias)
        existing = _source_creation_replay(
            request_key=request_key,
            request_hash=request_hash,
            admin=admin,
            audit_context=audit_context,
        )
        if existing is not None:
            return existing, False

        source_id = uuid.uuid4()
        generated_key = (
            slugify(material["name"])[:70].strip("-") or "source"
        )
        source = SourceDefinition(
            id=source_id,
            key=f"{generated_key}-{str(source_id)[:8]}",
            creation_request_key=request_key,
            creation_request_hash=request_hash,
        )
        _apply_source_projection(source, material)
        try:
            with transaction.atomic(using=alias):
                source.save(using=alias)
        except IntegrityError:
            existing = _source_creation_replay(
                request_key=request_key,
                request_hash=request_hash,
                admin=admin,
                audit_context=audit_context,
            )
            if existing is None:
                raise
            return existing, False
        snapshot = _create_draft_snapshot(
            source=source,
            material=material,
            version=1,
            request_key=request_key,
            request_hash=request_hash,
            using=alias,
        )
        source.current_snapshot_version = snapshot.version
        source.latest_draft_snapshot = snapshot
        source.latest_draft_snapshot_version = snapshot.version
        source.latest_draft_config_hash = snapshot.config_hash
        source.save(
            update_fields=[
                "current_snapshot_version",
                "latest_draft_snapshot",
                "latest_draft_snapshot_version",
                "latest_draft_config_hash",
                "updated_at",
            ],
            using=alias,
        )
        record_audit_event(
            context=audit_context,
            action="source_definition.created",
            entity=source,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=SOURCE_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            after_material=_source_audit_material(source),
            metadata={
                "request_hash": request_hash,
                "result": "created",
                "source_id": str(source.id),
                "snapshot_id": str(snapshot.id),
                "snapshot_version": snapshot.version,
                "config_hash": snapshot.config_hash,
            },
        )
        return source, True


def update_source_definition(
    source_id: Any,
    data: Mapping[str, Any],
    *,
    admin: Any,
    audit_context: AuditContext,
    request_hash: str,
) -> SourceDefinition:
    request_key = str(data["requestKey"])
    reason = "source definition update"
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    request_hash = _require_request_hash(request_hash)
    alias = audit_context.database_alias

    with transaction.atomic(using=alias):
        source = (
            SourceDefinition.objects.using(alias)
            .select_for_update()
            .get(pk=source_id)
        )
        existing = (
            SourceDefinitionSnapshot.objects.using(alias)
            .filter(source=source, request_key=request_key)
            .first()
        )
        if existing is not None:
            require_idempotent_match(
                stored_hash=existing.request_hash or "",
                expected_hash=request_hash,
            )
            _require_admin_audit_replay(
                context=audit_context,
                action="source_definition.updated",
                entity=source,
                identity_key=request_key,
                request_hash=request_hash,
                admin=admin,
            )
            return source

        expected = _id(data.get("expectedLatestDraftSnapshotId"))
        actual = _id(source.latest_draft_snapshot_id)
        if expected != actual:
            raise StaleVersion(
                "The latest source draft snapshot changed."
            )
        material = _normalize_source_material(data, current=source)
        before_material = _source_audit_material(source)
        _retire_current_draft(source, using=alias)
        version = _next_source_snapshot_version(source, using=alias)
        snapshot = _create_draft_snapshot(
            source=source,
            material=material,
            version=version,
            request_key=request_key,
            request_hash=request_hash,
            using=alias,
        )
        _apply_source_projection(source, material)
        source.current_snapshot_version = version
        source.latest_draft_snapshot = snapshot
        source.latest_draft_snapshot_version = version
        source.latest_draft_config_hash = snapshot.config_hash
        source.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="source_definition.updated",
            entity=source,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=SOURCE_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            before_material=before_material,
            after_material=_source_audit_material(source),
            metadata={
                "request_hash": request_hash,
                "result": "updated",
                "source_id": str(source.id),
                "snapshot_id": str(snapshot.id),
                "snapshot_version": snapshot.version,
                "config_hash": snapshot.config_hash,
            },
        )
        return source


def _membership_material(
    memberships: Iterable[
        SourceRegistryMembership | Mapping[str, Any]
    ],
) -> list[dict[str, Any]]:
    material: list[dict[str, Any]] = []
    for membership in memberships:
        if isinstance(membership, Mapping):
            source_id = membership["source_definition_id"]
            snapshot_id = membership["source_snapshot_id"]
            config_hash = membership["config_hash"]
            enabled = membership["enabled"]
            display_order = membership["display_order"]
        else:
            source_id = membership.source_definition_id
            snapshot_id = membership.source_snapshot_id
            config_hash = membership.source_snapshot.config_hash
            enabled = membership.enabled
            display_order = membership.display_order
        material.append(
            {
                "sourceDefinitionId": str(source_id),
                "sourceDefinitionSnapshotId": str(snapshot_id),
                "sourceDefinitionConfigHash": str(config_hash),
                "enabled": bool(enabled),
                "displayOrder": int(display_order),
            }
        )
    return sorted(
        material,
        key=lambda item: item["sourceDefinitionId"],
    )


def registry_manifest_hash_for_memberships(
    memberships: Iterable[
        SourceRegistryMembership | Mapping[str, Any]
    ],
) -> str:
    return _hash(
        {
            "schemaVersion": SOURCE_REGISTRY_MANIFEST_SCHEMA_V2,
            "memberships": _membership_material(memberships),
        }
    )


def registry_manifest_hash(
    registry: SourceRegistrySnapshot,
    *,
    using: str | None = None,
) -> str:
    alias = using or registry._state.db or "default"
    memberships = list(
        SourceRegistryMembership.objects.using(alias)
        .select_related("source_snapshot")
        .filter(registry=registry)
        .order_by("source_definition_id")
    )
    return registry_manifest_hash_for_memberships(memberships)


def _registry_audit_material(
    registry: SourceRegistrySnapshot,
) -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_AUDIT_SCHEMA_V2,
        "registry_id": str(registry.id),
        "topic_code": registry.topic_code,
        "version": registry.version,
        "state": registry.state,
        "row_version": registry.row_version,
        "manifest_hash": registry.manifest_hash,
        "base_approved_registry_id": _id(
            registry.base_approved_registry_id
        ),
        "base_approved_version": registry.base_approved_version,
        "base_approved_manifest_hash": (
            registry.base_approved_manifest_hash
        ),
        "latest_decision_id": _id(registry.latest_decision_id),
    }


def _lock_topic_head(
    topic_code: str,
    *,
    using: str,
) -> TopicRegistryHead:
    TopicRegistryHead.objects.using(using).get_or_create(
        topic_code=topic_code
    )
    return (
        TopicRegistryHead.objects.using(using)
        .select_for_update()
        .get(topic_code=topic_code)
    )


def _head_tuple(
    head: TopicRegistryHead,
) -> tuple[str | None, int | None, str | None]:
    return (
        _id(head.current_approved_registry_id),
        head.current_approved_version,
        head.current_approved_manifest_hash,
    )


def _request_head_tuple(
    data: Mapping[str, Any],
) -> tuple[str | None, int | None, str | None]:
    return (
        _id(data.get("expectedCurrentHeadRegistryId")),
        data.get("expectedCurrentHeadVersion"),
        data.get("expectedCurrentHeadManifestHash"),
    )


def _next_registry_version(topic_code: str, *, using: str) -> int:
    latest = (
        SourceRegistrySnapshot.objects.using(using)
        .filter(topic_code=topic_code)
        .aggregate(value=Max("version"))["value"]
    )
    return int(latest or 0) + 1


def create_registry_draft(
    data: Mapping[str, Any],
    *,
    admin: Any,
    audit_context: AuditContext,
    request_hash: str,
) -> tuple[SourceRegistrySnapshot, bool]:
    request_key = str(data["requestKey"])
    reason = "source registry draft create"
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    request_hash = _require_request_hash(request_hash)
    topic_code = str(data["topic"])
    if topic_code not in TopicCode.values:
        raise InvalidInput("Unsupported registry topic.")
    alias = audit_context.database_alias

    existing = SourceRegistrySnapshot.objects.using(alias).filter(
        topic_code=topic_code,
        draft_request_key=request_key,
    ).first()
    if existing is not None:
        require_idempotent_match(
            stored_hash=existing.draft_request_hash or "",
            expected_hash=request_hash,
        )
        _require_admin_audit_replay(
            context=audit_context,
            action="source_registry.draft_created",
            entity=existing,
            identity_key=request_key,
            request_hash=request_hash,
            admin=admin,
        )
        return existing, False

    with transaction.atomic(using=alias):
        head = _lock_topic_head(topic_code, using=alias)
        existing = SourceRegistrySnapshot.objects.using(alias).filter(
            topic_code=topic_code,
            draft_request_key=request_key,
        ).first()
        if existing is not None:
            require_idempotent_match(
                stored_hash=existing.draft_request_hash or "",
                expected_hash=request_hash,
            )
            _require_admin_audit_replay(
                context=audit_context,
                action="source_registry.draft_created",
                entity=existing,
                identity_key=request_key,
                request_hash=request_hash,
                admin=admin,
            )
            return existing, False

        expected_latest = (
            _id(data.get("baseRegistryId")),
            data.get("expectedLatestRegistryVersion"),
            data.get("expectedLatestRegistryManifestHash"),
        )
        if expected_latest != _head_tuple(head):
            raise StaleVersion(
                "The approved source registry head changed."
            )

        base = None
        carried: list[SourceRegistryMembership] = []
        if head.current_approved_registry_id is not None:
            base = (
                SourceRegistrySnapshot.objects.using(alias)
                .select_for_update()
                .get(pk=head.current_approved_registry_id)
            )
            if (
                base.state != SourceRegistrySnapshot.State.APPROVED
                or base.version != head.current_approved_version
                or base.manifest_hash
                != head.current_approved_manifest_hash
            ):
                raise Conflict("The source registry head is inconsistent.")
            carried = list(
                SourceRegistryMembership.objects.using(alias)
                .select_related("source_snapshot")
                .filter(registry=base)
                .order_by("source_definition_id")
            )

        manifest_hash = registry_manifest_hash_for_memberships(carried)
        registry = SourceRegistrySnapshot.objects.using(alias).create(
            topic_code=topic_code,
            version=_next_registry_version(topic_code, using=alias),
            state=SourceRegistrySnapshot.State.DRAFT,
            manifest_hash=manifest_hash,
            row_version=1,
            base_approved_registry=base,
            base_approved_version=(base.version if base else None),
            base_approved_manifest_hash=(
                base.manifest_hash if base else None
            ),
            draft_request_key=request_key,
            draft_request_hash=request_hash,
        )
        if carried:
            SourceRegistryMembership.objects.using(alias).bulk_create(
                [
                    SourceRegistryMembership(
                        registry=registry,
                        source_definition_id=member.source_definition_id,
                        source_snapshot_id=member.source_snapshot_id,
                        enabled=member.enabled,
                        display_order=member.display_order,
                    )
                    for member in carried
                ]
            )
        record_audit_event(
            context=audit_context,
            action="source_registry.draft_created",
            entity=registry,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=REGISTRY_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            after_material=_registry_audit_material(registry),
            metadata={
                "request_hash": request_hash,
                "result": "created",
                "registry_id": str(registry.id),
                "manifest_hash": registry.manifest_hash,
                "version": registry.version,
                "membership_count": len(carried),
            },
        )
        return registry, True


def update_registry_membership(
    registry_id: Any,
    source_id: Any,
    data: Mapping[str, Any],
    *,
    admin: Any,
    audit_context: AuditContext,
    request_hash: str,
) -> tuple[SourceRegistrySnapshot, bool]:
    request_key = str(data["requestKey"])
    reason = "source registry membership update"
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    request_hash = _require_request_hash(request_hash)
    alias = audit_context.database_alias

    existing = SourceRegistryMutation.objects.using(alias).filter(
        registry_id=registry_id,
        request_key=request_key,
    ).first()
    if existing is not None:
        require_idempotent_match(
            stored_hash=existing.request_hash,
            expected_hash=request_hash,
        )
        registry = SourceRegistrySnapshot.objects.using(alias).get(
            pk=registry_id
        )
        _require_admin_audit_replay(
            context=audit_context,
            action="source_registry.membership_updated",
            entity=registry,
            identity_key=request_key,
            request_hash=request_hash,
            admin=admin,
        )
        return registry, False

    with transaction.atomic(using=alias):
        registry = (
            SourceRegistrySnapshot.objects.using(alias)
            .select_for_update()
            .get(pk=registry_id)
        )
        existing = SourceRegistryMutation.objects.using(alias).filter(
            registry=registry,
            request_key=request_key,
        ).first()
        if existing is not None:
            require_idempotent_match(
                stored_hash=existing.request_hash,
                expected_hash=request_hash,
            )
            _require_admin_audit_replay(
                context=audit_context,
                action="source_registry.membership_updated",
                entity=registry,
                identity_key=request_key,
                request_hash=request_hash,
                admin=admin,
            )
            return registry, False
        if registry.state != SourceRegistrySnapshot.State.DRAFT:
            raise StateConflict(
                "Only a draft source registry can be changed."
            )
        if (
            registry.row_version != data["expectedRowVersion"]
            or registry.manifest_hash != data["expectedManifestHash"]
        ):
            raise StaleVersion(
                "The source registry draft changed."
            )

        source = (
            SourceDefinition.objects.using(alias)
            .select_for_update()
            .get(pk=source_id)
        )
        snapshot = (
            SourceDefinitionSnapshot.objects.using(alias)
            .select_for_update()
            .get(pk=data["sourceDefinitionSnapshotId"])
        )
        if (
            source.topic_code != registry.topic_code
            or snapshot.source_id != source.id
            or snapshot.topic_code != registry.topic_code
        ):
            raise InvalidInput(
                "The membership source, snapshot and registry must share one topic."
            )
        if snapshot.state == SourceDefinitionSnapshot.State.RETIRED:
            raise StateConflict(
                "A retired source snapshot cannot be selected."
            )
        if (
            snapshot.state == SourceDefinitionSnapshot.State.DRAFT
            and source.latest_draft_snapshot_id != snapshot.id
        ):
            raise StaleVersion(
                "The selected source draft is no longer current."
            )
        if data["enabled"] and not bool(
            snapshot.frozen_config.get("enabled", True)
        ):
            raise InvalidInput(
                "A disabled source snapshot cannot be enabled in a registry."
            )

        before_material = _registry_audit_material(registry)
        before_hash = registry.manifest_hash
        member = (
            SourceRegistryMembership.objects.using(alias)
            .select_for_update()
            .filter(
                registry=registry,
                source_definition=source,
            )
            .first()
        )
        if member is None:
            member = SourceRegistryMembership(
                registry=registry,
                source_definition=source,
            )
        member.source_snapshot = snapshot
        member.enabled = data["enabled"]
        member.display_order = data["displayOrder"]
        member.full_clean()
        member.save(using=alias)

        after_hash = registry_manifest_hash(registry, using=alias)
        registry.manifest_hash = after_hash
        registry.row_version += 1
        registry.save(
            update_fields=["manifest_hash", "row_version"],
            using=alias,
        )
        mutation = SourceRegistryMutation(
            registry=registry,
            source_definition=source,
            request_key=request_key,
            request_hash=request_hash,
            before_manifest_hash=before_hash,
            after_manifest_hash=after_hash,
            resulting_row_version=registry.row_version,
        )
        mutation.full_clean()
        mutation.save(using=alias)
        record_audit_event(
            context=audit_context,
            action="source_registry.membership_updated",
            entity=registry,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=REGISTRY_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            before_material=before_material,
            after_material=_registry_audit_material(registry),
            metadata={
                "request_hash": request_hash,
                "result": "updated",
                "registry_id": str(registry.id),
                "source_id": str(source.id),
                "source_snapshot_id": str(snapshot.id),
                "before_manifest_hash": before_hash,
                "after_manifest_hash": after_hash,
                "row_version": registry.row_version,
                "mutation_id": str(mutation.id),
            },
        )
        return registry, True


def _verify_registry_manifest(
    registry: SourceRegistrySnapshot,
    *,
    using: str,
) -> list[SourceRegistryMembership]:
    memberships = list(
        SourceRegistryMembership.objects.using(using)
        .select_related("source_definition", "source_snapshot")
        .filter(registry=registry)
        .order_by("source_definition_id")
    )
    if registry_manifest_hash_for_memberships(
        memberships
    ) != registry.manifest_hash:
        raise Conflict("The source registry manifest is inconsistent.")
    for member in memberships:
        if (
            member.source_definition_id != member.source_snapshot.source_id
            or member.source_definition.topic_code != registry.topic_code
            or member.source_snapshot.topic_code != registry.topic_code
        ):
            raise Conflict(
                "The source registry membership identity is inconsistent."
            )
    return memberships


def _lock_and_validate_approval_sources(
    memberships: Iterable[SourceRegistryMembership],
    *,
    topic_code: str,
    using: str,
) -> tuple[
    dict[Any, SourceDefinition],
    dict[Any, SourceDefinitionSnapshot],
]:
    enabled_memberships = [
        membership for membership in memberships if membership.enabled
    ]
    source_ids = sorted(
        {
            membership.source_definition_id
            for membership in enabled_memberships
        },
        key=str,
    )
    snapshot_ids = sorted(
        {
            membership.source_snapshot_id
            for membership in enabled_memberships
        },
        key=str,
    )
    locked_sources = {
        source.id: source
        for source in SourceDefinition.objects.using(using)
        .select_for_update()
        .filter(id__in=source_ids)
        .order_by("id")
    }
    locked_snapshots = {
        snapshot.id: snapshot
        for snapshot in SourceDefinitionSnapshot.objects.using(using)
        .select_for_update()
        .filter(id__in=snapshot_ids)
        .order_by("id")
    }
    if (
        len(locked_sources) != len(source_ids)
        or len(locked_snapshots) != len(snapshot_ids)
    ):
        raise Conflict(
            "The source registry membership target no longer exists."
        )

    for member in enabled_memberships:
        source = locked_sources[member.source_definition_id]
        snapshot = locked_snapshots[member.source_snapshot_id]
        if (
            source.topic_code != topic_code
            or snapshot.topic_code != topic_code
            or snapshot.source_id != source.id
        ):
            raise Conflict(
                "The locked source registry membership identity is inconsistent."
            )
        if snapshot.state == SourceDefinitionSnapshot.State.RETIRED:
            raise StateConflict(
                "A retired source snapshot cannot be approved."
            )
        if (
            snapshot.state == SourceDefinitionSnapshot.State.DRAFT
            and source.latest_draft_snapshot_id != snapshot.id
        ):
            raise StaleVersion(
                "A selected source draft is no longer current."
            )
        if snapshot.state not in {
            SourceDefinitionSnapshot.State.DRAFT,
            SourceDefinitionSnapshot.State.APPROVED,
        }:
            raise StateConflict(
                "The selected source snapshot state is invalid."
            )
        if not bool(snapshot.frozen_config.get("enabled", True)):
            raise InvalidInput(
                "An enabled membership selected a disabled source snapshot."
            )
        if not _is_verifiable_source_snapshot(snapshot):
            raise Conflict(
                "A selected source snapshot is not a current verifiable snapshot."
            )
        _require_current_successful_source_check(source, snapshot)
    return locked_sources, locked_snapshots


def _require_current_successful_source_check(
    source: SourceDefinition,
    snapshot: SourceDefinitionSnapshot,
) -> None:
    """Fail closed unless health is a current, policy-bound successful check."""

    health = source.last_health
    frozen = snapshot.frozen_config
    if not isinstance(health, Mapping) or not isinstance(frozen, Mapping):
        raise StateConflict("A current successful source check is required before approval.")
    required = {
        "taxonomyVersion", "snapshotId", "configHash", "accessPolicyHash",
        "rightsPolicyHash", "status", "checkedAt", "recordCount",
        "etag", "lastModified", "contentHash", "rightsDecision", "errorCode",
    }
    if set(health) != required or health.get("taxonomyVersion") != "source-check-taxonomy-v1":
        raise StateConflict("The source check result is not a current taxonomy result.")
    if (
        health.get("status") != "passed"
        or health.get("snapshotId") != str(snapshot.id)
        or health.get("configHash") != snapshot.config_hash
        or health.get("accessPolicyHash") != frozen.get("accessPolicyHash")
        or health.get("rightsPolicyHash") != frozen.get("rightsPolicyHash")
        or health.get("rightsDecision") != frozen.get("defaultRightsStatus")
    ):
        raise StateConflict("The source check is not successful for this frozen source policy.")
    try:
        checked_at = datetime.fromisoformat(str(health["checkedAt"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateConflict("The source check timestamp is invalid.") from exc
    source_check_days = (frozen.get("externalConfig") or {}).get("sourceCheckDays")
    if (
        isinstance(source_check_days, bool)
        or not isinstance(source_check_days, int)
        or checked_at.tzinfo is None
        or checked_at < timezone.now() - timedelta(days=source_check_days)
        or checked_at > timezone.now()
    ):
        raise StateConflict("A recent successful source check is required before approval.")


def decide_source_registry(
    registry_id: Any,
    data: Mapping[str, Any],
    *,
    admin: Any,
    request: Any,
    audit_context: AuditContext,
    request_hash: str,
) -> tuple[SourceRegistryDecision, bool]:
    request_key = str(data["requestKey"])
    reason = str(data["reason"])
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    request_hash = _require_request_hash(request_hash)
    alias = audit_context.database_alias

    existing = SourceRegistryDecision.objects.using(alias).filter(
        registry_id=registry_id,
        request_key=request_key,
    ).first()
    if existing is not None:
        if existing.decided_by_id != admin.pk:
            raise RequestKeyConflict(
                "The request key belongs to another administrator."
            )
        require_idempotent_match(
            stored_hash=existing.request_hash,
            expected_hash=request_hash,
        )
        require_audit_replay(
            context=audit_context,
            action=f"source_registry.{existing.decision}",
            entity=existing.registry,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            request_hash=request_hash,
        )
        return existing, False

    registry_topic = (
        SourceRegistrySnapshot.objects.using(alias)
        .only("topic_code")
        .get(pk=registry_id)
        .topic_code
    )
    with transaction.atomic(using=alias):
        # Topic head is always locked before a registry row. This serializes
        # every approval/retirement for one topic and avoids cross-draft races.
        head = _lock_topic_head(registry_topic, using=alias)
        registry = (
            SourceRegistrySnapshot.objects.using(alias)
            .select_for_update()
            .get(pk=registry_id)
        )
        existing = SourceRegistryDecision.objects.using(alias).filter(
            registry=registry,
            request_key=request_key,
        ).first()
        if existing is not None:
            if existing.decided_by_id != admin.pk:
                raise RequestKeyConflict(
                    "The request key belongs to another administrator."
                )
            require_idempotent_match(
                stored_hash=existing.request_hash,
                expected_hash=request_hash,
            )
            require_audit_replay(
                context=audit_context,
                action=f"source_registry.{existing.decision}",
                entity=registry,
                identity_key=source_registry_audit_request_key(
                    request_key
                ),
                request_hash=request_hash,
            )
            return existing, False

        if (
            registry.row_version != data["expectedRowVersion"]
            or registry.manifest_hash != data["expectedManifestHash"]
        ):
            raise StaleVersion("The source registry changed.")
        if _head_tuple(head) != _request_head_tuple(data):
            raise StaleVersion(
                "The approved source registry head changed."
            )
        if _id(registry.latest_decision_id) != _id(
            data.get("expectedLatestDecisionId")
        ):
            raise StaleVersion(
                "The source registry decision projection changed."
            )
        latest_decision = (
            SourceRegistryDecision.objects.using(alias).get(
                pk=registry.latest_decision_id
            )
            if registry.latest_decision_id
            else None
        )

        decision_value = data["decision"]
        if decision_value not in SourceRegistryDecision.Decision.values:
            raise InvalidInput("Unsupported source registry decision.")
        memberships = _verify_registry_manifest(registry, using=alias)
        before_material = _registry_audit_material(registry)
        prior_head: SourceRegistrySnapshot | None = None
        locked_sources: dict[Any, SourceDefinition] = {}
        locked_snapshots: dict[Any, SourceDefinitionSnapshot] = {}

        if decision_value == SourceRegistryDecision.Decision.APPROVED:
            if registry.state != SourceRegistrySnapshot.State.DRAFT:
                raise StateConflict(
                    "Only a draft source registry can be approved."
                )
            base_tuple = (
                _id(registry.base_approved_registry_id),
                registry.base_approved_version,
                registry.base_approved_manifest_hash,
            )
            if base_tuple != _head_tuple(head):
                raise StaleVersion(
                    "The registry draft is based on a stale approved head."
                )
            enabled_memberships = [
                member for member in memberships if member.enabled
            ]
            if not enabled_memberships:
                raise InvalidInput(
                    "An approved registry requires an enabled source."
                )
            if head.current_approved_registry_id is not None:
                prior_head = (
                    SourceRegistrySnapshot.objects.using(alias)
                    .select_for_update()
                    .get(pk=head.current_approved_registry_id)
                )
                if (
                    prior_head.state
                    != SourceRegistrySnapshot.State.APPROVED
                ):
                    raise Conflict(
                        "The current source registry head is inconsistent."
                    )
            else:
                # Legacy approved rows are intentionally not promoted into a
                # trusted head by migration. An explicit re-authenticated v2
                # approval may retire the single legacy row while establishing
                # the first authoritative head.
                prior_head = (
                    SourceRegistrySnapshot.objects.using(alias)
                    .select_for_update()
                    .filter(
                        topic_code=registry.topic_code,
                        state=SourceRegistrySnapshot.State.APPROVED,
                    )
                    .exclude(pk=registry.pk)
                    .first()
                )
            (
                locked_sources,
                locked_snapshots,
            ) = _lock_and_validate_approval_sources(
                enabled_memberships,
                topic_code=registry.topic_code,
                using=alias,
            )
        else:
            if (
                registry.state != SourceRegistrySnapshot.State.APPROVED
                or head.current_approved_registry_id != registry.id
            ):
                raise StateConflict(
                    "Only the current approved registry can be retired."
                )

        consume_reauthentication_proof(
            request=request,
            proof_id=data["reauthProofId"],
            action_scope="registry_decision",
            entity_type="source_registry_snapshot",
            entity_id=registry.id,
        )
        now = timezone.now()
        version = (
            latest_decision.version + 1
            if latest_decision is not None
            else 1
        )
        decision_id = uuid.uuid4()
        decision_hash = _hash(
            {
                "schemaVersion": "source-registry-decision-v2",
                "id": str(decision_id),
                "registryId": str(registry.id),
                "version": version,
                "decision": decision_value,
                "expectedRowVersion": data["expectedRowVersion"],
                "expectedManifestHash": data["expectedManifestHash"],
                "expectedCurrentHeadRegistryId": _id(
                    data.get("expectedCurrentHeadRegistryId")
                ),
                "expectedCurrentHeadVersion": data.get(
                    "expectedCurrentHeadVersion"
                ),
                "expectedCurrentHeadManifestHash": data.get(
                    "expectedCurrentHeadManifestHash"
                ),
                "supersedesDecisionId": _id(
                    registry.latest_decision_id
                ),
                "requestKey": request_key,
                "requestHash": request_hash,
                "decidedBy": str(admin.pk),
                "decidedAt": now.isoformat(),
                "reason": reason,
            }
        )
        decision = SourceRegistryDecision(
            id=decision_id,
            registry=registry,
            version=version,
            decision=decision_value,
            expected_row_version=data["expectedRowVersion"],
            expected_manifest_hash=data["expectedManifestHash"],
            expected_current_head_registry_id=(
                data.get("expectedCurrentHeadRegistryId")
            ),
            expected_current_head_version=data.get(
                "expectedCurrentHeadVersion"
            ),
            expected_current_head_manifest_hash=data.get(
                "expectedCurrentHeadManifestHash"
            ),
            supersedes_decision=latest_decision,
            request_key=request_key,
            request_hash=request_hash,
            decision_hash=decision_hash,
            decided_by=admin,
            decided_at=now,
            reason=reason,
        )
        decision.full_clean()
        decision.save(using=alias)

        if decision_value == SourceRegistryDecision.Decision.APPROVED:
            for member in memberships:
                if not member.enabled:
                    continue
                source = locked_sources[member.source_definition_id]
                snapshot = locked_snapshots[member.source_snapshot_id]
                if snapshot.state == SourceDefinitionSnapshot.State.DRAFT:
                    previous_approved = list(
                        SourceDefinitionSnapshot.objects.using(alias)
                        .select_for_update()
                        .filter(
                            source=source,
                            state=SourceDefinitionSnapshot.State.APPROVED,
                        )
                        .exclude(pk=snapshot.pk)
                    )
                    for previous in previous_approved:
                        previous.state = (
                            SourceDefinitionSnapshot.State.RETIRED
                        )
                        previous.retired_at = now
                        previous.save(
                            update_fields=["state", "retired_at"],
                            using=alias,
                        )
                    snapshot.state = SourceDefinitionSnapshot.State.APPROVED
                    snapshot.approved_by = admin
                    snapshot.approved_at = now
                    snapshot.retired_at = None
                    snapshot.save(
                        update_fields=[
                            "state",
                            "approved_by",
                            "approved_at",
                            "retired_at",
                        ],
                        using=alias,
                    )
                source.latest_approved_snapshot_version = snapshot.version
                if source.latest_draft_snapshot_id == snapshot.id:
                    source.latest_draft_snapshot = None
                    source.latest_draft_snapshot_version = None
                    source.latest_draft_config_hash = None
                source.save(
                    update_fields=[
                        "latest_approved_snapshot_version",
                        "latest_draft_snapshot",
                        "latest_draft_snapshot_version",
                        "latest_draft_config_hash",
                        "updated_at",
                    ],
                    using=alias,
                )

            if prior_head is not None:
                prior_before = _registry_audit_material(prior_head)
                prior_head.state = SourceRegistrySnapshot.State.RETIRED
                prior_head.retired_at = now
                prior_head.row_version += 1
                prior_head.save(
                    update_fields=[
                        "state",
                        "retired_at",
                        "row_version",
                    ],
                    using=alias,
                )
                record_audit_event(
                    context=audit_context,
                    action="source_registry.superseded",
                    entity=prior_head,
                    identity_key=_hash(
                        {
                            "requestKey": request_key,
                            "priorRegistryId": str(prior_head.id),
                            "newRegistryId": str(registry.id),
                        }
                    ),
                    material_schema_version=REGISTRY_AUDIT_SCHEMA_V2,
                    metadata_schema_version="2",
                    before_material=prior_before,
                    after_material=_registry_audit_material(prior_head),
                    metadata={
                        "request_hash": request_hash,
                        "result": "superseded",
                        "registry_id": str(prior_head.id),
                        "replacement_registry_id": str(registry.id),
                    },
                )

            registry.state = SourceRegistrySnapshot.State.APPROVED
            registry.approved_by = admin
            registry.approved_at = now
            registry.retired_at = None
            head.current_approved_registry = registry
            head.current_approved_version = registry.version
            head.current_approved_manifest_hash = registry.manifest_hash
        else:
            registry.state = SourceRegistrySnapshot.State.RETIRED
            registry.retired_at = now
            head.current_approved_registry = None
            head.current_approved_version = None
            head.current_approved_manifest_hash = None

        registry.latest_decision = decision
        registry.row_version += 1
        registry.save(
            update_fields=[
                "state",
                "row_version",
                "latest_decision",
                "approved_by",
                "approved_at",
                "retired_at",
            ],
            using=alias,
        )
        head.row_version += 1
        head.save(using=alias)
        record_audit_event(
            context=audit_context,
            action=f"source_registry.{decision_value}",
            entity=registry,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=REGISTRY_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            before_material=before_material,
            after_material=_registry_audit_material(registry),
            metadata={
                "request_hash": request_hash,
                "result": "decided",
                "decision": decision_value,
                "decision_hash": decision.decision_hash,
                "registry_id": str(registry.id),
                "manifest_hash": registry.manifest_hash,
                "version": decision.version,
                "reauth_proof_id": str(data["reauthProofId"]),
                "prior_head_registry_id": _id(
                    prior_head.id if prior_head else None
                ),
            },
        )
        return decision, True


def normalize_registry_import(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping) or any(
        field not in data
        for field in ("topicCode", "title", "policyVersion", "freshnessMinutes", "policy", "sources")
    ):
        raise InvalidInput("Registry imports require explicit topic policy material.")
    freshness_minutes = data["freshnessMinutes"]
    if isinstance(freshness_minutes, bool) or not isinstance(freshness_minutes, int) or not 1 <= freshness_minutes <= 525_600:
        raise InvalidInput("freshnessMinutes is invalid.")
    topic_policy = data["policy"]
    if not isinstance(topic_policy, Mapping):
        raise InvalidInput("Topic policy must be an object.")
    allowed_authority_tiers = topic_policy.get("allowedAuthorityTiers")
    valid_tiers = set(SourceDefinition.AuthorityTier.values) | {"primary_regulatory", "trusted_industry"}
    if (not isinstance(allowed_authority_tiers, list) or not allowed_authority_tiers
            or any(not isinstance(tier, str) or tier not in valid_tiers for tier in allowed_authority_tiers)):
        raise InvalidInput("Topic policy requires allowedAuthorityTiers.")
    normalized_topic_policy = dict(topic_policy)
    normalized_topic_policy["allowedAuthorityTiers"] = sorted(set(allowed_authority_tiers))
    if not isinstance(data["sources"], list) or not data["sources"]:
        raise InvalidInput("Registry imports require sources.")
    sources: list[dict[str, Any]] = []
    for raw in data["sources"]:
        if not isinstance(raw, Mapping):
            raise InvalidInput(
                "Repository sources must be objects."
            )
        source_key = raw.get("key")
        if (
            not isinstance(source_key, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", source_key)
            is None
        ):
            raise InvalidInput(
                "Repository source keys must be lowercase slugs."
            )
        required_source_fields = {
            "displayName", "ownerName", "authorityTier",
            "baseUrl", "accessMethod", "independenceGroup", "adapter", "entrypoints",
            "recordHosts", "attachmentContentTypes", "allowedContentTypes",
            "accessPolicy", "rightsPolicy", "pollMinutes",
            "rateLimitPerMinute", "maxConcurrency", "burst", "rightsStatus", "enabled",
        }
        adapter_key = raw.get("adapter")
        if adapter_key in {"housing_applyhome", "housing_lh"}:
            required_source_fields.update({"apiContentTypes", "detailContentTypes"})
        elif adapter_key == "open_data_json":
            required_source_fields.add("apiContentTypes")
        elif raw.get("accessMethod") == "rss_atom":
            required_source_fields.update({"feedContentTypes", "detailContentTypes"})
        else:
            required_source_fields.update({"listContentTypes", "detailContentTypes"})
        if not required_source_fields.issubset(raw):
            raise InvalidInput("Registry sources require explicit policy and MIME material.")
        if raw.get("authorityTier") not in normalized_topic_policy["allowedAuthorityTiers"]:
            raise InvalidInput("Source authorityTier is not allowed by topic policy.")
        material = _normalize_source_material(
            {
                "topic": data["topicCode"],
                "name": raw["displayName"],
                "publisher": raw.get("publisher", raw["ownerName"]),
                "authorityTier": raw["authorityTier"],
                "independenceGroupId": raw["independenceGroup"],
                "ownerName": raw["ownerName"],
                "editorialControlName": raw.get(
                    "editorialControlName",
                    raw["ownerName"],
                ),
                "baseUrl": raw["baseUrl"],
                "accessMethod": raw["accessMethod"],
                "adapterKey": raw.get("adapter", "public_html"),
                "externalConfig": {
                    "entrypoints": raw.get("entrypoints", []),
                    **{
                        field_name: raw[field_name]
                        for field_name in (
                            "maxPages",
                            "pageSize",
                            "detailEntrypoint",
                            "supplyEntrypoint",
                            "recordHosts",
                            "reconciliationDays",
                            "maxRequests",
                            "maxElapsedSeconds",
                            "sourceCheckDays",
                            "apiContentTypes",
                            "detailContentTypes",
                            "attachmentContentTypes",
                            "feedContentTypes",
                            "listContentTypes",
                            "identityNamespace",
                            "mediaDownloadPolicy",
                            "issuerCodes",
                            "accessPolicy",
                            "rightsPolicy",
                        )
                        if field_name in raw
                    },
                },
                "secretRef": raw.get("secretRef"),
                "allowedMimeTypes": raw.get(
                    "allowedContentTypes",
                    [],
                ),
                "defaultRightsStatus": raw["rightsStatus"],
                "termsUrl": raw.get("termsUrl"),
                "robotsUrl": raw.get("robotsUrl"),
                "licenseUrl": raw.get("licenseUrl"),
                "pollIntervalSeconds": int(raw["pollMinutes"]) * 60,
                "rateLimitPolicy": {
                    "maxConcurrency": int(raw["maxConcurrency"]),
                    "requestsPerMinute": int(raw["rateLimitPerMinute"]),
                    "burst": int(raw["burst"]),
                },
                "enabled": raw["enabled"],
            }
        )
        sources.append({"key": source_key, "material": material})
    source_keys = [item["key"] for item in sources]
    if len(set(source_keys)) != len(source_keys):
        raise InvalidInput(
            "Repository source keys must be unique within one topic."
        )
    return {
        "topic_code": data["topicCode"],
        "title": data["title"],
        "policy_version": int(data["policyVersion"]),
        "freshness_minutes": freshness_minutes,
        "policy": normalized_topic_policy,
        "sources": sources,
    }


def _approved_registry_matches_import(
    registry: SourceRegistrySnapshot,
    normalized: Mapping[str, Any],
    *,
    using: str,
) -> bool:
    if (
        registry.state != SourceRegistrySnapshot.State.APPROVED
        or registry.topic_code != normalized["topic_code"]
    ):
        return False
    memberships = list(
        SourceRegistryMembership.objects.using(using)
        .select_related("source_definition", "source_snapshot")
        .filter(registry=registry)
        .order_by("source_definition_id")
    )
    if (
        registry_manifest_hash_for_memberships(memberships)
        != registry.manifest_hash
    ):
        return False
    expected = {
        item["key"]: {
            "config_hash": source_snapshot_hash(item["material"]),
            "enabled": bool(item["material"]["enabled"]),
            "display_order": display_order,
        }
        for display_order, item in enumerate(normalized["sources"])
    }
    if len(expected) != len(normalized["sources"]):
        raise InvalidInput(
            "Repository source keys must be unique within one topic."
        )
    if {
        membership.source_definition.key
        for membership in memberships
    } != set(expected):
        return False
    for membership in memberships:
        source = membership.source_definition
        snapshot = membership.source_snapshot
        target = expected[source.key]
        if (
            source.topic_code != registry.topic_code
            or snapshot.source_id != source.id
            or snapshot.topic_code != registry.topic_code
            or (
                membership.enabled
                and snapshot.state
                != SourceDefinitionSnapshot.State.APPROVED
            )
            or membership.enabled != target["enabled"]
            or membership.display_order != target["display_order"]
            or not _is_verifiable_source_snapshot(
                snapshot,
                expected_config_hash=target["config_hash"],
            )
        ):
            return False
    return True


def _topic_policy_matches_import(
    normalized: Mapping[str, Any],
    *,
    using: str,
) -> bool:
    policy = (
        TopicPolicy.objects.using(using)
        .select_for_update()
        .filter(
            code=normalized["topic_code"],
            version=normalized["policy_version"],
        )
        .first()
    )
    expected_hash = _hash(normalized["policy"])
    return policy is not None and all(
        (
            policy.active,
            policy.title == normalized["title"],
            policy.freshness_minutes
            == normalized["freshness_minutes"],
            policy.policy == normalized["policy"],
            policy.policy_hash == expected_hash,
        )
    )


def import_registry_manifest(
    path: str | Path,
    *,
    audit_context: AuditContext,
) -> RegistryImportResult:
    if audit_context.actor_type != AuditEvent.ActorType.SYSTEM:
        raise ValueError(
            "Source registry import requires explicit system provenance."
        )
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    normalized = normalize_registry_import(data)
    topic_code = normalized["topic_code"]
    if topic_code not in TopicCode.values:
        raise InvalidInput("Unsupported registry topic.")
    request_material = {
        "schemaVersion": "source-registry-import-v2",
        **normalized,
    }
    repository_hash = _hash(request_material)
    alias = audit_context.database_alias

    with transaction.atomic(using=alias):
        head = _lock_topic_head(topic_code, using=alias)
        if head.current_approved_registry_id is not None:
            current = (
                SourceRegistrySnapshot.objects.using(alias)
                .select_for_update()
                .get(pk=head.current_approved_registry_id)
            )
            if (
                current.version == head.current_approved_version
                and current.manifest_hash
                == head.current_approved_manifest_hash
                and _approved_registry_matches_import(
                    current,
                    normalized,
                    using=alias,
                )
                and _topic_policy_matches_import(
                    normalized,
                    using=alias,
                )
            ):
                return RegistryImportResult(
                    topic_code=topic_code,
                    registry_id=str(current.id),
                    source_count=len(normalized["sources"]),
                    created=False,
                )
        request_hash = _hash(
            {
                **request_material,
                "baseRegistryId": _id(
                    head.current_approved_registry_id
                ),
                "baseVersion": head.current_approved_version,
                "baseManifestHash": (
                    head.current_approved_manifest_hash
                ),
            }
        )
        legacy_request_key = f"seed:{request_hash}"
        request_key = "seed:" + _hash(
            {
                "schemaVersion": (
                    "source-registry-import-generation-v1"
                ),
                "requestHash": request_hash,
                "headRowVersion": head.row_version,
            }
        )
        existing = SourceRegistrySnapshot.objects.using(alias).filter(
            topic_code=topic_code,
            state=SourceRegistrySnapshot.State.DRAFT,
            draft_request_key__in=(
                request_key,
                legacy_request_key,
            ),
        ).first()
        if existing is not None:
            require_idempotent_match(
                stored_hash=existing.draft_request_hash or "",
                expected_hash=request_hash,
            )
            require_audit_replay(
                context=audit_context,
                action="source_registry.imported",
                entity=existing,
                identity_key=existing.draft_request_key,
                request_hash=request_hash,
            )
            return RegistryImportResult(
                topic_code=topic_code,
                registry_id=str(existing.id),
                source_count=existing.memberships.count(),
                created=False,
            )

        policy_hash = _hash(normalized["policy"])
        policy, policy_created = TopicPolicy.objects.using(alias).get_or_create(
            code=topic_code,
            version=normalized["policy_version"],
            defaults={
                "title": normalized["title"],
                "freshness_minutes": normalized["freshness_minutes"],
                "policy": normalized["policy"],
                "policy_hash": policy_hash,
                "active": True,
            },
        )
        if not policy_created and any(
            (
                not policy.active,
                policy.title != normalized["title"],
                policy.freshness_minutes
                != normalized["freshness_minutes"],
                policy.policy != normalized["policy"],
                policy.policy_hash != policy_hash,
            )
        ):
            raise Conflict(
                "The topic policy version already has different material."
            )

        selected: list[
            tuple[SourceDefinition, SourceDefinitionSnapshot, bool, int]
        ] = []
        for display_order, item in enumerate(normalized["sources"]):
            source = (
                SourceDefinition.objects.using(alias)
                .select_for_update()
                .filter(topic_code=topic_code, key=item["key"])
                .first()
            )
            if source is None:
                source = SourceDefinition(
                    topic_code=topic_code,
                    key=item["key"],
                )
                _apply_source_projection(source, item["material"])
                source.save(using=alias)
            target_hash = source_snapshot_hash(item["material"])
            snapshot = None
            if (
                source.latest_draft_snapshot_id
                and source.latest_draft_config_hash == target_hash
            ):
                snapshot = (
                    SourceDefinitionSnapshot.objects.using(alias)
                    .select_for_update()
                    .get(pk=source.latest_draft_snapshot_id)
                )
                if (
                    snapshot.state
                    != SourceDefinitionSnapshot.State.DRAFT
                    or not _is_verifiable_source_snapshot(
                        snapshot,
                        expected_config_hash=target_hash,
                    )
                ):
                    snapshot = None
            if snapshot is None:
                snapshot = (
                    SourceDefinitionSnapshot.objects.using(alias)
                    .select_for_update()
                    .filter(
                        source=source,
                        state=SourceDefinitionSnapshot.State.APPROVED,
                        config_hash=target_hash,
                    )
                    .order_by("-version")
                    .first()
                )
                if (
                    snapshot is not None
                    and not _is_verifiable_source_snapshot(
                        snapshot,
                        expected_config_hash=target_hash,
                    )
                ):
                    snapshot = None
            if snapshot is None:
                _retire_current_draft(source, using=alias)
                version = _next_source_snapshot_version(
                    source,
                    using=alias,
                )
                snapshot = _create_draft_snapshot(
                    source=source,
                    material=item["material"],
                    version=version,
                    request_key=None,
                    request_hash=None,
                    using=alias,
                )
            elif snapshot.state == SourceDefinitionSnapshot.State.APPROVED:
                _retire_current_draft(source, using=alias)
            _apply_source_projection(source, item["material"])
            source.current_snapshot_version = snapshot.version
            if snapshot.state == SourceDefinitionSnapshot.State.DRAFT:
                source.latest_draft_snapshot = snapshot
                source.latest_draft_snapshot_version = snapshot.version
                source.latest_draft_config_hash = snapshot.config_hash
            else:
                source.latest_draft_snapshot = None
                source.latest_draft_snapshot_version = None
                source.latest_draft_config_hash = None
            source.save(using=alias)
            selected.append(
                (
                    source,
                    snapshot,
                    bool(item["material"]["enabled"]),
                    display_order,
                )
            )

        membership_material = [
            {
                "source_definition_id": source.id,
                "source_snapshot_id": snapshot.id,
                "config_hash": snapshot.config_hash,
                "enabled": enabled,
                "display_order": display_order,
            }
            for source, snapshot, enabled, display_order in selected
        ]
        manifest_hash = registry_manifest_hash_for_memberships(
            membership_material
        )
        base = None
        if head.current_approved_registry_id:
            base = SourceRegistrySnapshot.objects.using(alias).get(
                pk=head.current_approved_registry_id
            )
        registry = SourceRegistrySnapshot.objects.using(alias).create(
            topic_code=topic_code,
            version=_next_registry_version(topic_code, using=alias),
            state=SourceRegistrySnapshot.State.DRAFT,
            manifest_hash=manifest_hash,
            row_version=1,
            base_approved_registry=base,
            base_approved_version=head.current_approved_version,
            base_approved_manifest_hash=(
                head.current_approved_manifest_hash
            ),
            draft_request_key=request_key,
            draft_request_hash=request_hash,
        )
        SourceRegistryMembership.objects.using(alias).bulk_create(
            [
                SourceRegistryMembership(
                    registry=registry,
                    source_definition=source,
                    source_snapshot=snapshot,
                    enabled=enabled,
                    display_order=display_order,
                )
                for source, snapshot, enabled, display_order in selected
            ]
        )
        record_audit_event(
            context=audit_context,
            action="source_registry.imported",
            entity=registry,
            identity_key=request_key,
            material_schema_version=REGISTRY_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            after_material=_registry_audit_material(registry),
            metadata={
                "request_hash": request_hash,
                "result": "imported",
                "repository_hash": repository_hash,
                "registry_id": str(registry.id),
                "manifest_hash": registry.manifest_hash,
                "version": registry.version,
                "membership_count": len(selected),
            },
        )
        return RegistryImportResult(
            topic_code=topic_code,
            registry_id=str(registry.id),
            source_count=len(selected),
            created=True,
        )


def _source_check_snapshot(
    source: SourceDefinition,
    *,
    using: str,
) -> SourceDefinitionSnapshot:
    if source.latest_draft_snapshot_id is not None:
        return SourceDefinitionSnapshot.objects.using(using).get(
            pk=source.latest_draft_snapshot_id
        )
    if source.latest_approved_snapshot_version is not None:
        return SourceDefinitionSnapshot.objects.using(using).get(
            source=source,
            version=source.latest_approved_snapshot_version,
            state=SourceDefinitionSnapshot.State.APPROVED,
        )
    raise StateConflict(
        "The source has no current draft or approved snapshot to check."
    )


def request_source_check(
    source_id: Any,
    *,
    admin: Any,
    request_key: str,
    audit_context: AuditContext,
) -> Any:
    from wisdome_writer.infrastructure.outbox import enqueue_event

    if not request_key:
        raise ValueError("A source check request key is required.")
    reason = "source access check"
    _require_admin_context(
        audit_context=audit_context,
        admin=admin,
        request_key=request_key,
        reason=reason,
    )
    alias = audit_context.database_alias
    with transaction.atomic(using=alias):
        source = (
            SourceDefinition.objects.using(alias)
            .select_for_update()
            .get(pk=source_id)
        )
        snapshot = _source_check_snapshot(source, using=alias)
        check_id = uuid.uuid4()
        event = enqueue_event(
            topic="source.check_requested",
            aggregate_type="SourceDefinition",
            aggregate_id=source.id,
            message_key=f"source.check_requested:{source.id}:{check_id}",
            correlation_id=audit_context.correlation_id,
            job_id=check_id,
            operation="check",
            payload={
                "source_id": str(source.id),
                "source_snapshot_id": str(snapshot.id),
                "source_config_hash": snapshot.config_hash,
                "check_id": str(check_id),
            },
            max_attempts=3,
        )
        record_audit_event(
            context=audit_context,
            action="source_definition.check_requested",
            entity=source,
            identity_key=source_registry_audit_request_key(
                request_key
            ),
            material_schema_version=SOURCE_AUDIT_SCHEMA_V2,
            metadata_schema_version="2",
            before_material=_source_audit_material(source),
            after_material=_source_audit_material(source),
            metadata={
                "result": "accepted",
                "source_id": str(source.id),
                "snapshot_id": str(snapshot.id),
                "config_hash": snapshot.config_hash,
                "job_id": str(event.job_id),
            },
        )
        return event


def record_source_check_result(
    *,
    source_id: Any,
    source_snapshot_id: Any,
    source_config_hash: str,
    status: str,
    record_count: int,
    error_code: str | None,
    etag: str | None = None,
    last_modified: str | None = None,
    content_hash: str | None = None,
    rights_decision: str | None = None,
) -> dict[str, Any]:
    if status not in {"passed", "failed"}:
        raise ValueError("Unsupported source check status.")
    safe_error_code = (
        re.sub(r"[^a-z0-9_.-]+", "_", str(error_code).lower())[:120]
        if error_code
        else None
    )
    with transaction.atomic():
        source = (
            SourceDefinition.objects.select_for_update().get(pk=source_id)
        )
        snapshot = SourceDefinitionSnapshot.objects.get(
            pk=source_snapshot_id,
            source=source,
        )
        if snapshot.config_hash != source_config_hash:
            raise Conflict("The source check snapshot hash is inconsistent.")
        current_snapshot = _source_check_snapshot(source, using="default")
        frozen = snapshot.frozen_config
        if not _is_verifiable_source_snapshot(snapshot):
            raise Conflict("The source check snapshot is not policy-verifiable.")
        if content_hash is not None and (
            not isinstance(content_hash, str)
            or re.fullmatch(r"[a-f0-9]{64}", content_hash) is None
        ):
            raise ValueError("Source check content_hash must be a SHA-256 hash.")
        for field_name, value in (("etag", etag), ("last_modified", last_modified)):
            if value is not None and (not isinstance(value, str) or len(value) > 500):
                raise ValueError(f"Source check {field_name} is invalid.")
        expected_rights = frozen["defaultRightsStatus"]
        if rights_decision is not None and rights_decision != expected_rights:
            raise Conflict("Source check rights decision is inconsistent.")
        result = {
            "taxonomyVersion": "source-check-taxonomy-v1",
            "snapshotId": str(snapshot.id),
            "configHash": snapshot.config_hash,
            "accessPolicyHash": frozen["accessPolicyHash"],
            "rightsPolicyHash": frozen["rightsPolicyHash"],
            "status": status,
            "checkedAt": timezone.now().isoformat(),
            "recordCount": max(0, min(int(record_count), 1_000_000)),
            "etag": etag,
            "lastModified": last_modified,
            "contentHash": content_hash,
            "rightsDecision": expected_rights,
            "errorCode": safe_error_code,
        }
        if current_snapshot.id == snapshot.id:
            source.last_health = result
            source.save(
                update_fields=["last_health", "updated_at"],
            )
        return result


def current_registry(
    topic_code: str,
    *,
    for_update: bool = False,
) -> SourceRegistrySnapshot:
    if (
        for_update
        and not transaction.get_connection().in_atomic_block
    ):
        raise RuntimeError(
            "A locked registry lookup requires an active transaction."
        )
    head_queryset = TopicRegistryHead.objects
    if for_update:
        head_queryset = head_queryset.select_for_update()
    try:
        head = head_queryset.get(topic_code=topic_code)
    except TopicRegistryHead.DoesNotExist as exc:
        raise StateConflict(
            "No approved source registry head exists for this topic."
        ) from exc
    if head.current_approved_registry_id is None:
        raise StateConflict(
            "No consistent approved source registry exists for this topic."
        )
    registry_queryset = SourceRegistrySnapshot.objects
    if for_update:
        registry_queryset = registry_queryset.select_for_update()
    registry = registry_queryset.get(
        pk=head.current_approved_registry_id
    )
    if (
        registry.state != SourceRegistrySnapshot.State.APPROVED
        or registry.version != head.current_approved_version
        or registry.manifest_hash
        != head.current_approved_manifest_hash
    ):
        raise StateConflict(
            "No consistent approved source registry exists for this topic."
        )
    memberships = _verify_registry_manifest(
        registry,
        using=registry._state.db or "default",
    )
    for member in memberships:
        if not member.enabled:
            continue
        snapshot = member.source_snapshot
        if (
            snapshot.state != SourceDefinitionSnapshot.State.APPROVED
            or not _is_verifiable_source_snapshot(snapshot)
        ):
            raise Conflict(
                "An approved source registry contains an unverifiable snapshot."
            )
    return (
        SourceRegistrySnapshot.objects.prefetch_related(
            "memberships__source_definition",
            "memberships__source_snapshot",
        )
        .select_related("latest_decision", "base_approved_registry")
        .get(pk=registry.pk)
    )
