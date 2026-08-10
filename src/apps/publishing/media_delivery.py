from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

from adapters.publishers.wordpress import WordPressPublisher
from adapters.storage import S3ObjectStorage

from .contracts import PublisherError, PublishResult
from .models import (
    MediaDeliveryOperation,
    PublicationMedia,
    PublishedEvidenceSnapshot,
)
from .services import MediaDeliveryOperationFence, publisher_for_target


@dataclass(frozen=True, slots=True)
class FrozenMediaMaterial:
    object_key: str
    object_version: str
    checksum: str
    presentation_hash: str
    mime_type: str
    byte_size: int
    alt_text: str
    caption: str

    @property
    def filename(self) -> str:
        return PurePosixPath(self.object_key).name or "asset"


def _binding_material(operation: MediaDeliveryOperation) -> FrozenMediaMaterial:
    filters: dict[str, Any] = {}
    if operation.publication_attempt_id:
        filters["publication_id"] = operation.publication_attempt.publication_id
    if operation.remote_media_id:
        filters["remote_media_id"] = operation.remote_media_id
    else:
        filters["public_delivery_asset_id"] = operation.public_delivery_asset_id
    bindings = list(
        PublicationMedia.objects.select_related(
            "published_evidence_snapshot",
            "published_visualization_snapshot",
        )
        .filter(**filters)
        .order_by("id")
    )
    if not bindings:
        raise ValueError("media delivery has no frozen source binding")
    materials: list[FrozenMediaMaterial] = []
    for binding in bindings:
        snapshot = (
            binding.published_evidence_snapshot
            or binding.published_visualization_snapshot
        )
        checksum = (
            snapshot.asset_checksum
            if isinstance(snapshot, PublishedEvidenceSnapshot)
            else snapshot.output_checksum
        )
        materials.append(
            FrozenMediaMaterial(
                object_key=snapshot.object_key,
                object_version=snapshot.object_version,
                checksum=checksum,
                presentation_hash=snapshot.presentation_hash,
                mime_type=snapshot.mime_type,
                byte_size=snapshot.byte_size,
                alt_text=binding.alt_text_snapshot,
                caption=binding.caption_snapshot,
            )
        )
    material = materials[0]
    if any(row != material for row in materials[1:]):
        raise ValueError("media delivery source bindings are ambiguous")
    mapping = operation.remote_media or operation.public_delivery_asset
    if (
        mapping.asset_checksum != material.checksum
        or mapping.presentation_hash != material.presentation_hash
    ):
        raise ValueError("media delivery mapping differs from frozen source")
    return material


def _wordpress_result(result: PublishResult) -> dict[str, Any]:
    if result.status == "succeeded":
        return {
            "status": "succeeded",
            "remote_media_id": result.remote_post_id,
            "remote_url": result.remote_url,
            "http_status": result.http_status,
        }
    if result.status in {"not_found", "unknown_outcome", "retryable_failed"}:
        return {
            "status": "unknown_outcome",
            "error_code": result.error_code or "remote_media_outcome_unknown",
            "http_status": result.http_status,
        }
    return {
        "status": "manual_required",
        "error_code": result.error_code or "remote_media_manual_required",
        "http_status": result.http_status,
    }


def _description_marker(operation: MediaDeliveryOperation) -> str:
    mapping = operation.remote_media
    return (
        "wisdome-media-v1:"
        f"{mapping.id}:{mapping.asset_checksum}:{mapping.presentation_hash}"
    )


def perform_media_delivery_operation(
    operation: MediaDeliveryOperation,
    *,
    fence: MediaDeliveryOperationFence,
    write_guard: Callable[[], None] | None = None,
    storage: S3ObjectStorage | None = None,
    publisher=None,
) -> dict[str, Any]:
    if operation.id != fence.operation_id or operation.generation != fence.generation:
        raise ValueError("media delivery fence differs from operation")
    storage = storage or S3ObjectStorage()
    guard = write_guard or (lambda: None)

    if operation.remote_media_id:
        mapping = operation.remote_media
        owns_publisher = publisher is None
        if publisher is None:
            publisher = publisher_for_target(
                mapping.target,
                write_guard=guard,
            )
            if not isinstance(publisher, WordPressPublisher):
                raise ValueError("remote media operation requires WordPress")
        try:
            if operation.action == MediaDeliveryOperation.Action.DELETE:
                if not mapping.remote_media_id:
                    return {"status": "succeeded"}
                return _wordpress_result(
                    publisher.delete_media(mapping.remote_media_id)
                )
            material = _binding_material(operation)
            content = storage.get_bounded_bytes(
                key=material.object_key,
                version_id=material.object_version,
                max_bytes=material.byte_size,
            )
            if (
                len(content) != material.byte_size
                or hashlib.sha256(content).hexdigest() != material.checksum
            ):
                raise ValueError("frozen media source bytes differ from snapshot")
            marker = _description_marker(operation)
            if operation.action == MediaDeliveryOperation.Action.UPLOAD:
                result = publisher.upload_media(
                    content=content,
                    filename=material.filename,
                    mime_type=material.mime_type,
                    remote_lookup_key=mapping.remote_lookup_key,
                    alt_text=material.alt_text,
                    caption=material.caption,
                    description_marker=marker,
                )
            elif operation.action == MediaDeliveryOperation.Action.RECONCILE:
                result = publisher.find_media(
                    remote_lookup_key=mapping.remote_lookup_key,
                    description_marker=marker,
                    expected_alt_text=material.alt_text,
                    expected_caption=material.caption,
                    expected_checksum=material.checksum,
                    expected_mime_type=material.mime_type,
                )
            else:
                raise ValueError("unsupported WordPress media action")
            return _wordpress_result(result)
        except PublisherError as exc:
            return {
                "status": (
                    "manual_required"
                    if exc.category == "permanent"
                    else "unknown_outcome"
                ),
                "error_code": exc.code,
                "http_status": exc.http_status,
            }
        finally:
            if owns_publisher:
                publisher.close()

    mapping = operation.public_delivery_asset
    if operation.action == MediaDeliveryOperation.Action.DELETE:
        guard()
        storage.delete_exact_version(
            key=mapping.delivery_object_key,
            version_id=mapping.delivery_object_version,
        )
        return {"status": "succeeded"}
    material = _binding_material(operation)
    if operation.action == MediaDeliveryOperation.Action.PREPARE:
        content = storage.get_bounded_bytes(
            key=material.object_key,
            version_id=material.object_version,
            max_bytes=material.byte_size,
        )
        if (
            len(content) != material.byte_size
            or hashlib.sha256(content).hexdigest() != material.checksum
        ):
            raise ValueError("frozen delivery source bytes differ from snapshot")
        guard()
        info, public_url = storage.put_public_bytes_exact(
            key=mapping.delivery_object_key,
            data=content,
            content_type=material.mime_type,
            checksum_sha256=material.checksum,
        )
    elif operation.action == MediaDeliveryOperation.Action.RECONCILE:
        info, public_url = storage.verify_public_bytes_exact(
            key=mapping.delivery_object_key,
            version_id=mapping.delivery_object_version,
            checksum_sha256=material.checksum,
            expected_size=material.byte_size,
            content_type=material.mime_type,
        )
    else:
        raise ValueError("unsupported public delivery action")
    return {
        "status": "succeeded",
        "asset_checksum": info.checksum_sha256,
        "presentation_hash": material.presentation_hash,
        "mime_type": info.content_type,
        "byte_size": info.size,
        "object_version": info.version_id,
        "public_url": public_url,
    }
