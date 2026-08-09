from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any, Mapping

from django.conf import settings
from django.db import transaction

from wisdome_writer.domain.hashing import (
    CANONICAL_HASH_SCHEMA_V1,
    canonical_hash,
)


POLICY_SCHEMA_VERSION = "editorial-policy-v1"
REQUIRED_GATE_CODES = frozenset(
    {
        "all_publishable_claims_grounded",
        "high_risk_verification_satisfied",
        "claim_independence_satisfied",
        "source_freshness_satisfied",
        "evidence_publish_eligibility_current",
        "quotation_limits_satisfied",
        "claim_types_separated_and_attributed",
        "duplicate_or_conflict_resolved",
        "korean_readability_and_repetition",
        "no_exaggeration_or_false_experience",
    }
)
VISUAL_GATE_CODE = "visual_rights_and_alt_text"
CLAIM_TYPES = frozenset(
    {"fact", "company_claim", "interpretation", "outlook"}
)
_POLICY_FIELDS = {
    "schemaVersion",
    "policyKey",
    "topicCode",
    "policyVersion",
    "language",
    "claimTypes",
    "requiredBlockTypes",
    "highImpactFields",
    "checks",
}
_CHECK_FIELDS = {"code", "version", "blocking", "config"}
_CHECK_CONFIG_FIELDS = {
    "quotation_limits_satisfied": ({"maxCitationChars"}, {"maxCitationChars"}),
    "claim_types_separated_and_attributed": (set(), set()),
    "duplicate_or_conflict_resolved": (set(), set()),
    "no_exaggeration_or_false_experience": ({"forbiddenPhrases"}, {"forbiddenPhrases"}),
    "source_freshness_satisfied": ({"clock"}, {"clock"}),
    "high_risk_verification_satisfied": ({"required"}, {"required"}),
    "claim_independence_satisfied": (
        {"primaryOrIndependentCount"},
        {"primaryOrIndependentCount", "corporateSelfClaimCounts"},
    ),
    "korean_readability_and_repetition": (
        {"maxSentenceChars", "maxParagraphChars"},
        {"maxSentenceChars", "maxParagraphChars"},
    ),
    "evidence_publish_eligibility_current": ({"allowed"}, {"allowed"}),
    "all_publishable_claims_grounded": (
        {"allClaimsRequireEvidence"},
        {"allClaimsRequireEvidence"},
    ),
}

_EDITORIAL_IMPLEMENTATION_FILES = (
    "src/adapters/generators/base.py",
    "src/adapters/generators/template.py",
    "src/apps/editorial/policies.py",
    "src/apps/editorial/quality.py",
    "src/apps/editorial/services.py",
    "src/apps/editorial/clustering.py",
    "src/apps/evidence/models.py",
    "src/apps/evidence/services.py",
    "src/wisdome_writer/domain/hashing.py",
)


class EditorialPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class EditorialPolicy:
    document: Mapping[str, Any]
    material_hash: str
    release_document_hash: str = ""
    config_hash: str = ""
    implementation_manifest: Mapping[str, Any] = field(default_factory=dict)
    implementation_manifest_hash: str = ""

    @property
    def topic_code(self) -> str:
        return str(self.document["topicCode"])

    @property
    def policy_key(self) -> str:
        return str(self.document["policyKey"])

    @property
    def policy_version(self) -> str:
        return str(self.document["policyVersion"])

    def check(self, code: str) -> Mapping[str, Any]:
        return next(row for row in self.document["checks"] if row["code"] == code)


def _reject_duplicate_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EditorialPolicyError(
                f"editorial policy contains duplicate JSON member {key!r}"
            )
        result[key] = value
    return result


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_policy_document(document: object, topic_code: str) -> dict:
    if not isinstance(document, dict) or set(document) != _POLICY_FIELDS:
        raise EditorialPolicyError("editorial policy fields are not exact")
    if document["schemaVersion"] != POLICY_SCHEMA_VERSION:
        raise EditorialPolicyError("editorial policy schemaVersion is unsupported")
    if document["topicCode"] != topic_code:
        raise EditorialPolicyError("editorial policy topicCode does not match its file")
    if not all(
        _nonempty(document[field])
        for field in ("policyKey", "policyVersion", "language")
    ):
        raise EditorialPolicyError("editorial policy identity is incomplete")
    claim_types = document["claimTypes"]
    if (
        not isinstance(claim_types, list)
        or len(claim_types) != len(set(claim_types))
        or set(claim_types) != CLAIM_TYPES
    ):
        raise EditorialPolicyError("editorial policy claimTypes are not exact")
    for name in ("requiredBlockTypes", "highImpactFields"):
        values = document[name]
        if (
            not isinstance(values, list)
            or not values
            or len(values) != len(set(values))
            or not all(_nonempty(value) for value in values)
        ):
            raise EditorialPolicyError(f"editorial policy {name} is invalid")
    checks = document["checks"]
    if not isinstance(checks, list):
        raise EditorialPolicyError("editorial policy checks must be a list")
    codes = []
    for row in checks:
        if not isinstance(row, dict) or set(row) != _CHECK_FIELDS:
            raise EditorialPolicyError("editorial policy check fields are not exact")
        code = row["code"]
        if not _nonempty(code) or not _nonempty(row["version"]):
            raise EditorialPolicyError("editorial policy check identity is invalid")
        if row["blocking"] is not True or not isinstance(row["config"], dict):
            raise EditorialPolicyError("editorial policy checks must be blocking")
        required_config, allowed_config = _CHECK_CONFIG_FIELDS.get(
            code, (set(), set())
        )
        config = row["config"]
        if not required_config <= set(config) or not set(config) <= allowed_config:
            raise EditorialPolicyError(
                f"editorial policy {code} config fields are not exact"
            )
        if code == "quotation_limits_satisfied" and (
            type(config["maxCitationChars"]) is not int
            or not 1 <= config["maxCitationChars"] <= 160
        ):
            raise EditorialPolicyError("editorial policy citation config is unsafe")
        if code == "no_exaggeration_or_false_experience" and (
            not isinstance(config["forbiddenPhrases"], list)
            or not config["forbiddenPhrases"]
            or len(config["forbiddenPhrases"])
            != len(set(config["forbiddenPhrases"]))
            or not all(_nonempty(value) for value in config["forbiddenPhrases"])
        ):
            raise EditorialPolicyError("editorial policy exaggeration config is invalid")
        if code == "source_freshness_satisfied" and config["clock"] != "current_publication_time":
            raise EditorialPolicyError("editorial policy freshness clock is unsupported")
        if code == "high_risk_verification_satisfied" and config["required"] is not True:
            raise EditorialPolicyError("editorial policy locator gate cannot be disabled")
        if code == "claim_independence_satisfied" and (
            type(config["primaryOrIndependentCount"]) is not int
            or config["primaryOrIndependentCount"] < 2
            or (
                "corporateSelfClaimCounts" in config
                and config["corporateSelfClaimCounts"] is not False
            )
        ):
            raise EditorialPolicyError("editorial policy independence config is unsafe")
        if code == "korean_readability_and_repetition" and any(
            type(config[field]) is not int or config[field] <= 0
            for field in ("maxSentenceChars", "maxParagraphChars")
        ):
            raise EditorialPolicyError("editorial policy readability config is invalid")
        if code == "evidence_publish_eligibility_current" and config["allowed"] != [
            "allowed", "attribution_required"
        ]:
            raise EditorialPolicyError("editorial policy rights config is unsafe")
        if (
            code == "all_publishable_claims_grounded"
            and config["allClaimsRequireEvidence"] is not True
        ):
            raise EditorialPolicyError("editorial policy coverage gate cannot be disabled")
        codes.append(code)
    missing = sorted(REQUIRED_GATE_CODES - set(codes))
    extra = sorted(set(codes) - REQUIRED_GATE_CODES)
    if missing:
        raise EditorialPolicyError(
            "editorial policy is missing required gate " + ",".join(missing)
        )
    if extra or len(codes) != len(set(codes)):
        raise EditorialPolicyError("editorial policy gate codes are not exact")
    return document


def _implementation_manifest() -> dict[str, Any]:
    root = settings.REPOSITORY_ROOT.resolve()
    files = []
    for relative_name in _EDITORIAL_IMPLEMENTATION_FILES:
        relative = Path(relative_name)
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise EditorialPolicyError("editorial implementation path escaped repository") from exc
        if not path.is_file():
            raise EditorialPolicyError(
                f"editorial implementation file is missing: {relative.as_posix()}"
            )
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {
        "schemaVersion": "editorial-implementation-manifest-v1",
        "files": sorted(files, key=lambda row: row["path"]),
    }


def load_editorial_policy(
    topic_code: str,
    *,
    root: str | Path | None = None,
) -> EditorialPolicy:
    policy_root = Path(
        root if root is not None else settings.EDITORIAL_POLICY_ROOT
    ).resolve()
    path = (policy_root / f"{topic_code}.json").resolve()
    if path.parent != policy_root:
        raise EditorialPolicyError("editorial policy path escapes its root")
    try:
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode("utf-8")
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_members)
    except EditorialPolicyError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EditorialPolicyError("editorial policy document cannot be loaded") from exc
    document = _validate_policy_document(document, topic_code)
    config_hash = canonical_hash(
        document,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    release_document_hash = hashlib.sha256(raw_bytes).hexdigest()
    implementation_manifest = _implementation_manifest()
    implementation_manifest_hash = canonical_hash(
        implementation_manifest,
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    material_hash = canonical_hash(
        {
            "schemaVersion": "editorial-policy-release-material-v1",
            "releaseDocumentHash": release_document_hash,
            "configHash": config_hash,
            "implementationManifestHash": implementation_manifest_hash,
        },
        schema_version=CANONICAL_HASH_SCHEMA_V1,
    )
    return EditorialPolicy(
        document=document,
        material_hash=material_hash,
        release_document_hash=release_document_hash,
        config_hash=config_hash,
        implementation_manifest=implementation_manifest,
        implementation_manifest_hash=implementation_manifest_hash,
    )


def resolve_editorial_policy_snapshot(
    topic_code: str,
    *,
    root: str | Path | None = None,
    using: str = "default",
):
    from .models import EditorialPolicySnapshot

    policy = load_editorial_policy(topic_code, root=root)
    with transaction.atomic(using=using):
        snapshot, _ = (
            EditorialPolicySnapshot.objects.using(using)
            .select_for_update()
            .get_or_create(
                topic_code=policy.topic_code,
                policy_key=policy.policy_key,
                policy_version=policy.policy_version,
                defaults={
                    "document": dict(policy.document),
                    "material_hash": policy.material_hash,
                    "release_document_hash": policy.release_document_hash,
                    "config_hash": policy.config_hash,
                    "implementation_manifest": dict(policy.implementation_manifest),
                    "implementation_manifest_hash": policy.implementation_manifest_hash,
                },
            )
        )
        if (
            snapshot.material_hash != policy.material_hash
            or snapshot.document != policy.document
            or snapshot.release_document_hash != policy.release_document_hash
            or snapshot.config_hash != policy.config_hash
            or snapshot.implementation_manifest != policy.implementation_manifest
            or snapshot.implementation_manifest_hash
            != policy.implementation_manifest_hash
        ):
            raise EditorialPolicyError(
                "editorial policy version was reused with changed bytes"
            )
        return snapshot
