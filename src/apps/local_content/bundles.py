"""Validated, atomic, immutable filesystem bundles for local housing articles."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from PIL import Image

from apps.local_content.images import ArticleImage, ImageSet
from apps.local_content.rendering import RenderedArticle

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?")
_TEXT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/x-ndjson",
        "application/yaml",
    }
)
_IMAGE_MIME_BY_FORMAT = {"PNG": "image/png", "WEBP": "image/webp"}
_RESERVED_PATHS = frozenset({"manifest.json", "manifest.sha256"})


class BundleValidationError(ValueError):
    """Raised before publication when a bundle fails a safety invariant."""


class BundlePublishError(RuntimeError):
    """Raised when an otherwise-valid bundle cannot be published atomically."""


@dataclass(frozen=True)
class BundleFile:
    """One declared file, optionally backed by a non-symlink source path."""

    path: str
    content: str | bytes | None
    mime_type: str
    sha256: str
    source_path: Path | None = None
    image_metadata: ArticleImage | None = None

    @classmethod
    def text(
        cls,
        path: str,
        content: str,
        mime_type: str = "text/plain",
    ) -> BundleFile:
        payload = content.encode("utf-8")
        return cls(
            path=path,
            content=content,
            mime_type=mime_type,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    @classmethod
    def from_path(
        cls,
        path: str,
        source_path: Path,
        mime_type: str,
        *,
        image_metadata: ArticleImage | None = None,
    ) -> BundleFile:
        source_path = Path(source_path)
        try:
            checksum = _file_sha256(source_path)
        except OSError:
            checksum = "0" * 64
        return cls(
            path=path,
            content=None,
            mime_type=mime_type,
            sha256=checksum,
            source_path=source_path,
            image_metadata=image_metadata,
        )

    def payload_bytes(self) -> bytes:
        if self.source_path is not None:
            return self.source_path.read_bytes()
        if isinstance(self.content, str):
            return self.content.encode("utf-8")
        if isinstance(self.content, bytes):
            return self.content
        raise BundleValidationError(f"bundle file has no content: {self.path}")


@dataclass(frozen=True)
class ArticleBundle:
    """A content-addressed article directory for one KST run date."""

    run_date: date
    slug: str
    files: tuple[BundleFile, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))

    @property
    def bundle_hash(self) -> str:
        material = [
            {
                "path": item.path,
                "sha256": item.sha256,
                "mime_type": item.mime_type,
                "image": (
                    item.image_metadata.as_manifest(path=item.path)
                    if item.image_metadata is not None
                    else None
                ),
            }
            for item in sorted(self.files, key=lambda value: value.path)
        ]
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def build_article_bundle(
    run_date: date,
    article: RenderedArticle,
    images: ImageSet,
) -> ArticleBundle:
    """Create a draft bundle without copying or fetching source attachments."""

    source_document = json.dumps(
        [source.as_dict() for source in article.sources],
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    files = [
        BundleFile.text(
            "article.draft.md",
            article.to_markdown(),
            mime_type="text/markdown",
        ),
        BundleFile.text("sources.json", source_document, mime_type="application/json"),
    ]
    files.extend(
        BundleFile.from_path(
            image.bundle_path,
            image.path,
            image.mime_type,
            image_metadata=image,
        )
        for image in images.images
    )
    return ArticleBundle(run_date=run_date, slug=article.slug, files=tuple(files))


class ArticleBundleWriter:
    """Publish complete bundle directories by sibling-stage and atomic replace."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def write(self, bundle: ArticleBundle) -> Path:
        """Validate and atomically publish one immutable article bundle."""

        root = self._safe_root()
        validated = self._validate(bundle)
        date_root = root / bundle.run_date.isoformat()
        self._ensure_directory(date_root)
        primary = self._safe_child(date_root, bundle.slug)
        target = self._publication_target(primary, bundle.bundle_hash)
        if target.exists():
            return target

        staging = target.with_name(f".{target.name}.tmp-{bundle.bundle_hash[:12]}")
        self._assert_child(root, staging)
        if staging.exists() or staging.is_symlink():
            raise BundlePublishError(f"staging path already exists: {staging}")
        staging.mkdir()
        try:
            for item, payload in validated:
                destination = self._safe_child(staging, item.path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _write_bytes_fsynced(destination, payload)
            checksum_document = "".join(
                f"{item.sha256}  {item.path}\n"
                for item, _payload in sorted(validated, key=lambda value: value[0].path)
            )
            _write_bytes_fsynced(staging / "manifest.sha256", checksum_document.encode("utf-8"))
            manifest = self._manifest(bundle, validated)
            manifest_payload = _json_bytes(manifest)
            _write_bytes_fsynced(staging / "manifest.json", manifest_payload)
            _fsync_directory(staging)
            try:
                os.replace(staging, target)
            except OSError as exc:
                if target.is_dir() and self._existing_hash(target) == bundle.bundle_hash:
                    return target
                raise BundlePublishError(f"atomic bundle publish failed: {target}") from exc
            _fsync_directory(date_root)
            return target
        except Exception:
            # An interrupted/failed write remains identifiable by its .tmp- name.
            raise

    def write_run(
        self,
        run_date: date,
        weekly_index: RenderedArticle | str,
        bundles: tuple[ArticleBundle, ...] | list[ArticleBundle] = (),
    ) -> Path:
        """Publish article bundles, then atomically replace the run index and manifest."""

        root = self._safe_root()
        run_root = root / run_date.isoformat()
        self._ensure_directory(run_root)
        index_markdown = (
            weekly_index.to_markdown()
            if isinstance(weekly_index, RenderedArticle)
            else weekly_index
        )
        if not isinstance(index_markdown, str):
            raise BundleValidationError("weekly index must be UTF-8 text")

        published: dict[str, str] = {}
        for bundle in bundles:
            if bundle.run_date != run_date:
                raise BundleValidationError("article bundle run date does not match run index")
            path = self.write(bundle)
            published[path.name] = bundle.bundle_hash
            if path.name != bundle.slug:
                index_markdown = index_markdown.replace(
                    f"./{bundle.slug}/article.md",
                    f"./{path.name}/article.md",
                )

        index_payload = index_markdown.encode("utf-8")
        index_payload.decode("utf-8", errors="strict")
        index_hash = hashlib.sha256(index_payload).hexdigest()
        run_manifest = {
            "schema_version": 1,
            "run_date": run_date.isoformat(),
            "index_sha256": index_hash,
            "articles": dict(sorted(published.items())),
        }
        index_tmp = self._safe_child(run_root, f".index.md.tmp-{index_hash[:12]}")
        manifest_tmp = self._safe_child(
            run_root,
            f".manifest.json.tmp-{hashlib.sha256(_json_bytes(run_manifest)).hexdigest()[:12]}",
        )
        if index_tmp.exists() or manifest_tmp.exists():
            raise BundlePublishError("run staging file already exists")
        _write_bytes_fsynced(index_tmp, index_payload)
        _write_bytes_fsynced(manifest_tmp, _json_bytes(run_manifest))
        os.replace(index_tmp, run_root / "index.md")
        os.replace(manifest_tmp, run_root / "manifest.json")
        _fsync_directory(run_root)
        return run_root

    def _validate(self, bundle: ArticleBundle) -> tuple[tuple[BundleFile, bytes], ...]:
        if _SLUG.fullmatch(bundle.slug) is None:
            raise BundleValidationError("bundle slug is not a portable safe path segment")
        if not bundle.files:
            raise BundleValidationError("bundle must contain at least one file")
        validated: list[tuple[BundleFile, bytes]] = []
        seen: set[str] = set()
        for item in bundle.files:
            self._validate_relative_path(item.path)
            key = item.path.casefold()
            if key in seen:
                raise BundleValidationError(f"duplicate bundle path: {item.path}")
            seen.add(key)
            if item.path.casefold() in _RESERVED_PATHS:
                raise BundleValidationError(f"reserved manifest path: {item.path}")
            if item.source_path is not None:
                self._assert_no_symlink(item.source_path)
                if not item.source_path.is_file():
                    raise BundleValidationError(f"source path is not a file: {item.source_path}")
            try:
                payload = item.payload_bytes()
            except OSError as exc:
                raise BundleValidationError(f"cannot read bundle source: {item.path}") from exc
            if _SHA256.fullmatch(item.sha256) is None:
                raise BundleValidationError(f"invalid checksum declaration: {item.path}")
            actual = hashlib.sha256(payload).hexdigest()
            if actual != item.sha256:
                raise BundleValidationError(f"checksum mismatch: {item.path}")
            if _is_text_file(item):
                try:
                    payload.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    message = f"text file is not valid UTF-8: {item.path}"
                    raise BundleValidationError(message) from exc
            if item.mime_type.startswith("image/"):
                self._validate_image(item, payload)
            elif item.image_metadata is not None:
                raise BundleValidationError(f"image metadata on non-image file: {item.path}")
            validated.append((item, payload))
        return tuple(validated)

    def _validate_image(self, item: BundleFile, payload: bytes) -> None:
        metadata = item.image_metadata
        if metadata is None:
            raise BundleValidationError(f"image metadata is required: {item.path}")
        required = (
            metadata.sha256,
            metadata.mime_type,
            metadata.alt,
            metadata.caption,
            metadata.creator,
            metadata.source,
            metadata.rights_status,
            metadata.rights_basis,
            metadata.attribution,
        )
        if not all(required) or metadata.width <= 0 or metadata.height <= 0:
            raise BundleValidationError(f"image metadata is incomplete: {item.path}")
        if metadata.rights_status not in {"owned", "generated"}:
            raise BundleValidationError(f"image rights are not publishable: {item.path}")
        if metadata.sha256 != item.sha256 or metadata.mime_type != item.mime_type:
            raise BundleValidationError(f"image metadata checksum or MIME mismatch: {item.path}")
        try:
            with Image.open(io.BytesIO(payload)) as image:
                actual_mime = _IMAGE_MIME_BY_FORMAT.get(image.format or "")
                size = image.size
                image.verify()
        except OSError as exc:
            raise BundleValidationError(f"image content is invalid: {item.path}") from exc
        if actual_mime != item.mime_type or size != (metadata.width, metadata.height):
            raise BundleValidationError(f"image MIME or dimensions do not match: {item.path}")

    def _manifest(
        self,
        bundle: ArticleBundle,
        validated: tuple[tuple[BundleFile, bytes], ...],
    ) -> dict[str, object]:
        files: dict[str, object] = {}
        for item, payload in sorted(validated, key=lambda value: value[0].path):
            entry: dict[str, object] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "mime_type": item.mime_type,
                "bytes": len(payload),
            }
            if item.image_metadata is not None:
                entry["image"] = item.image_metadata.as_manifest(path=item.path)
            files[item.path] = entry
        return {
            "schema_version": 1,
            "run_date": bundle.run_date.isoformat(),
            "slug": bundle.slug,
            "bundle_hash": bundle.bundle_hash,
            "files": files,
        }

    def _safe_root(self) -> Path:
        self._assert_no_symlink(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(self.root)
        return self.root.resolve(strict=True)

    def _ensure_directory(self, path: Path) -> None:
        if path.is_symlink():
            raise BundleValidationError(f"output path contains a symlink: {path}")
        path.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(path)

    def _safe_child(self, parent: Path, relative: str) -> Path:
        child = parent.joinpath(*PurePosixPath(relative).parts)
        self._assert_child(parent, child)
        return child

    @staticmethod
    def _assert_child(parent: Path, child: Path) -> None:
        try:
            child.resolve(strict=False).relative_to(parent.resolve(strict=True))
        except ValueError as exc:
            raise BundleValidationError(f"path escapes configured root: {child}") from exc

    @staticmethod
    def _validate_relative_path(value: str) -> None:
        if not value or "\\" in value or "\x00" in value:
            raise BundleValidationError(f"invalid bundle path: {value!r}")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise BundleValidationError(f"unsafe bundle path: {value}")
        if any(":" in part for part in path.parts):
            raise BundleValidationError(f"non-portable bundle path: {value}")

    @staticmethod
    def _assert_no_symlink(path: Path) -> None:
        path = Path(path)
        candidates = [path, *path.parents]
        for candidate in candidates:
            if candidate.is_symlink():
                raise BundleValidationError(f"symlink path is not allowed: {path}")

    def _publication_target(self, primary: Path, bundle_hash: str) -> Path:
        if primary.is_symlink():
            raise BundleValidationError(f"bundle target is a symlink: {primary}")
        if not primary.exists():
            return primary
        existing_hash = self._existing_hash(primary)
        if existing_hash == bundle_hash:
            return primary
        revision = primary.with_name(f"{primary.name}--rev-{bundle_hash[:12]}")
        if revision.is_symlink():
            raise BundleValidationError(f"revision target is a symlink: {revision}")
        if revision.exists():
            if self._existing_hash(revision) == bundle_hash:
                return revision
            raise BundlePublishError(f"revision hash collision: {revision}")
        return revision

    @staticmethod
    def _existing_hash(path: Path) -> str:
        manifest_path = path / "manifest.json"
        if not path.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
            raise BundlePublishError(f"existing bundle is incomplete or unsafe: {path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BundlePublishError(f"existing bundle manifest is invalid: {path}") from exc
        bundle_hash = manifest.get("bundle_hash")
        if not isinstance(bundle_hash, str) or _SHA256.fullmatch(bundle_hash) is None:
            raise BundlePublishError(f"existing bundle manifest has no valid hash: {path}")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise BundlePublishError(f"existing bundle manifest has no files: {path}")
        checksum_lines: list[str] = []
        hash_material: list[dict[str, object]] = []
        for relative, entry in sorted(files.items()):
            if not isinstance(relative, str) or not isinstance(entry, dict):
                raise BundlePublishError(f"existing bundle file manifest is invalid: {path}")
            try:
                ArticleBundleWriter._validate_relative_path(relative)
            except BundleValidationError as exc:
                message = f"existing bundle contains an unsafe path: {path}"
                raise BundlePublishError(message) from exc
            declared = entry.get("sha256")
            mime_type = entry.get("mime_type")
            if (
                not isinstance(declared, str)
                or _SHA256.fullmatch(declared) is None
                or not isinstance(mime_type, str)
                or not mime_type
            ):
                raise BundlePublishError(f"existing bundle checksum metadata is invalid: {path}")
            source = path.joinpath(*PurePosixPath(relative).parts)
            if source.is_symlink() or not source.is_file():
                raise BundlePublishError(f"existing bundle file is missing or unsafe: {source}")
            try:
                payload = source.read_bytes()
            except OSError as exc:
                raise BundlePublishError(f"existing bundle file cannot be read: {source}") from exc
            if hashlib.sha256(payload).hexdigest() != declared:
                raise BundlePublishError(f"existing bundle checksum mismatch: {source}")
            if entry.get("bytes") != len(payload):
                raise BundlePublishError(f"existing bundle byte count mismatch: {source}")
            checksum_lines.append(f"{declared}  {relative}\n")
            hash_material.append(
                {
                    "path": relative,
                    "sha256": declared,
                    "mime_type": mime_type,
                    "image": entry.get("image"),
                }
            )
        checksum_path = path / "manifest.sha256"
        if checksum_path.is_symlink() or not checksum_path.is_file():
            raise BundlePublishError(f"existing checksum manifest is missing or unsafe: {path}")
        try:
            checksum_document = checksum_path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            raise BundlePublishError(f"existing checksum manifest is invalid: {path}") from exc
        if checksum_document != "".join(checksum_lines):
            raise BundlePublishError(f"existing checksum manifest does not match: {path}")
        computed_hash = hashlib.sha256(
            json.dumps(
                hash_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if computed_hash != bundle_hash:
            raise BundlePublishError(f"existing bundle hash does not match its manifest: {path}")
        return bundle_hash


def _is_text_file(item: BundleFile) -> bool:
    return item.mime_type.startswith("text/") or item.mime_type in _TEXT_MIME_TYPES


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _write_bytes_fsynced(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
