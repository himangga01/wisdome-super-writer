"""Validated, atomic, immutable filesystem bundles for local housing articles."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import secrets
import stat
import unicodedata
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from apps.local_content.images import (
    GENERIC_HERO_CAPTION,
    GENERIC_HERO_SHA256,
    ArticleImage,
    ImageRenderError,
    ImageSet,
    canonical_card_renderer_input,
    card_accessibility_material,
    card_renderer_fingerprint,
    rerender_card_bytes,
)
from apps.local_content.rendering import RenderedArticle

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?")
_IMAGE_MIME_BY_FORMAT = {"PNG": "image/png", "WEBP": "image/webp"}
_IMAGE_EXTENSION_BY_MIME = {"image/png": ".png", "image/webp": ".webp"}
_IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".webp",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".ico",
        ".avif",
        ".heic",
        ".heif",
        ".svg",
    }
)
_ALLOWED_IMAGE_PATHS = frozenset(
    {"assets/hero.png", "assets/summary-card.webp", "assets/timeline.webp"}
)
_RESERVED_PATHS = frozenset({"manifest.json", "manifest.sha256"})
_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{value}" for value in range(1, 10)),
        *(f"lpt{value}" for value in range(1, 10)),
        *(f"com{value}" for value in "¹²³"),
        *(f"lpt{value}" for value in "¹²³"),
    }
)
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_BUNDLE_FILE_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_MAX_IMAGE_EDGE = 4096
_MAX_IMAGE_PIXELS = 16_000_000
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 10_000
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_ALLOWED_WSW_COMMENT = re.compile(
    r"<!-- WSW:(?:block|endblock|slot):[a-z0-9_-]+ -->"
)
_RAW_HTML = re.compile(
    r"<\s*(?:!doctype\b|/?[a-z][a-z0-9:-]*(?:\s|/?>))",
    re.IGNORECASE,
)


class BundleValidationError(ValueError):
    """Raised before publication when a bundle fails a safety invariant."""


class BundlePublishError(BundleValidationError):
    """Raised when an otherwise-valid bundle cannot be published atomically."""


@dataclass(frozen=True)
class _ExistingBundleValidation:
    bundle_hash: str
    root_fingerprint: tuple[object, ...]
    file_hashes: tuple[tuple[str, str], ...]


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
        if image_metadata is not None:
            checksum = image_metadata.sha256
        else:
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
        self._writer_token = secrets.token_hex(8)

    def write(
        self,
        bundle: ArticleBundle,
        *,
        run_directory: str | None = None,
    ) -> Path:
        """Validate and atomically publish one immutable article bundle."""

        root = self._safe_root()
        validated = self._validate(bundle)
        run_name = _validated_run_directory(bundle.run_date, run_directory)
        date_root = root / run_name
        self._ensure_directory(date_root)
        primary = self._safe_child(date_root, bundle.slug)
        target, existing = self._publication_target(primary, bundle.bundle_hash)
        if existing is not None:
            _require_validated_existing_root(target, existing, "exact existing bundle")
            return target

        staging, owned_stage_fingerprint = self._create_owned_staging(
            root,
            target,
            bundle.bundle_hash,
        )
        try:
            for item, payload in validated:
                destination = self._safe_child(staging, item.path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _write_bytes_fsynced(destination, payload)
            _fsync_tree_directories(staging)
            checksum_document = "".join(
                f"{item.sha256}  {item.path}\n"
                for item, _payload in sorted(validated, key=lambda value: value[0].path)
            )
            _write_bytes_fsynced(staging / "manifest.sha256", checksum_document.encode("utf-8"))
            _fsync_directory(staging)
            manifest = self._manifest(bundle, validated)
            manifest_payload = _json_bytes(manifest)
            _write_bytes_fsynced(staging / "manifest.json", manifest_payload)
            _fsync_directory(staging)
            self._validate_owned_article_stage(
                staging,
                owned_stage_fingerprint,
                bundle,
            )
            try:
                os.replace(staging, target)
            except OSError as exc:
                if target.is_dir():
                    winner = self._existing_hash(target)
                    if winner.bundle_hash == bundle.bundle_hash:
                        _require_validated_existing_root(
                            target,
                            winner,
                            "concurrent winning bundle",
                        )
                        return target
                raise BundlePublishError(f"atomic bundle publish failed: {target}") from exc
            _require_owned_identity(
                target,
                owned_stage_fingerprint,
                "published bundle",
            )
            if self._existing_hash(target).bundle_hash != bundle.bundle_hash:
                raise BundlePublishError(f"published bundle does not match owned stage: {target}")
            _fsync_directory(date_root)
            _fsync_directory(root)
            _require_owned_identity(
                target,
                owned_stage_fingerprint,
                "fully validated published bundle",
            )
            return target
        except BundlePublishError:
            raise
        except OSError as exc:
            raise BundlePublishError(f"bundle write or publish failed: {target}") from exc

    def write_run(
        self,
        run_date: date,
        weekly_index: RenderedArticle | str,
        bundles: tuple[ArticleBundle, ...] | list[ArticleBundle] = (),
        *,
        run_directory: str | None = None,
    ) -> Path:
        """Publish article bundles, then atomically replace the run index and manifest."""

        root = self._safe_root()
        run_name = _validated_run_directory(run_date, run_directory)
        run_root = root / run_name
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
            path = self.write(bundle, run_directory=run_name)
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
        manifest_payload = _json_bytes(run_manifest)
        try:
            index_tmp, index_fingerprint = self._write_owned_run_temp(
                run_root,
                "index.md",
                index_hash,
                index_payload,
            )
            manifest_tmp, manifest_fingerprint = self._write_owned_run_temp(
                run_root,
                "manifest.json",
                hashlib.sha256(manifest_payload).hexdigest(),
                manifest_payload,
            )
            _fsync_directory(run_root)
            self._validate_owned_run_temp(
                index_tmp,
                index_fingerprint,
                index_payload,
                "run index temp",
            )
            index_target = run_root / "index.md"
            manifest_target = run_root / "manifest.json"
            os.replace(index_tmp, index_target)
            self._validate_owned_run_temp(
                index_target,
                index_fingerprint,
                index_payload,
                "published run index",
            )
            _fsync_directory(run_root)
            self._validate_owned_run_temp(
                manifest_tmp,
                manifest_fingerprint,
                manifest_payload,
                "run manifest temp",
            )
            os.replace(manifest_tmp, manifest_target)
            self._validate_owned_run_temp(
                manifest_target,
                manifest_fingerprint,
                manifest_payload,
                "published run manifest",
            )
            _fsync_directory(run_root)
            _fsync_directory(root)
        except BundlePublishError:
            raise
        except OSError as exc:
            raise BundlePublishError(f"run publish failed: {run_root}") from exc
        validated_run = (
            self.validate_run(run_date)
            if run_directory is None
            else self.validate_run(run_date, run_directory=run_name)
        )
        _require_owned_identity(
            index_target,
            index_fingerprint,
            "fully validated published run index",
        )
        _require_owned_identity(
            manifest_target,
            manifest_fingerprint,
            "fully validated published run manifest",
        )
        return validated_run

    def validate_run(
        self,
        run_date: date,
        *,
        run_directory: str | None = None,
    ) -> Path:
        """Validate the run manifest commit marker before any reader uses the index."""

        root = self._safe_root()
        run_name = _validated_run_directory(run_date, run_directory)
        run_root = self._safe_child(root, run_name)
        _require_regular_directory(run_root, "run directory", BundlePublishError)
        ancestor_snapshot = _snapshot_ancestors(run_root, BundlePublishError)
        run_root_fingerprint = _path_fingerprint(
            run_root,
            "run directory",
            BundlePublishError,
        )
        manifest_path = run_root / "manifest.json"
        index_path = run_root / "index.md"
        if not _is_regular_file_no_links(manifest_path):
            raise BundlePublishError(f"run commit marker is missing or unsafe: {run_root}")
        if not _is_regular_file_no_links(index_path):
            raise BundlePublishError(f"run index is missing or unsafe: {run_root}")
        manifest_fingerprint = _path_fingerprint(
            manifest_path,
            "run manifest",
            BundlePublishError,
        )
        index_fingerprint = _path_fingerprint(index_path, "run index", BundlePublishError)
        manifest_payload = _read_bound_file(
            manifest_path,
            manifest_fingerprint,
            max_bytes=_MAX_MANIFEST_BYTES,
            label="run manifest",
        )
        manifest = _parse_strict_json_object(manifest_payload, "run")
        if set(manifest) != {"schema_version", "run_date", "index_sha256", "articles"}:
            raise BundlePublishError(f"run JSON manifest violates closed schema: {run_root}")
        index_payload = _read_bound_file(
            index_path,
            index_fingerprint,
            max_bytes=_MAX_BUNDLE_FILE_BYTES,
            label="run index",
        )
        try:
            index_payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BundlePublishError(f"run index is not UTF-8: {run_root}") from exc
        if (
            manifest.get("schema_version") != 1
            or manifest.get("run_date") != run_date.isoformat()
            or not isinstance(manifest.get("index_sha256"), str)
            or not isinstance(manifest.get("articles"), dict)
        ):
            raise BundlePublishError(f"run commit marker has invalid fields: {run_root}")
        if hashlib.sha256(index_payload).hexdigest() != manifest["index_sha256"]:
            raise BundlePublishError(f"run index checksum does not match commit marker: {run_root}")
        articles = manifest["articles"]
        seen_articles: set[str] = set()
        for article_name, expected_hash in articles.items():
            if not isinstance(article_name, str) or not isinstance(expected_hash, str):
                raise BundlePublishError(f"run article commit material is invalid: {run_root}")
            _validate_article_directory_name(article_name)
            if _SHA256.fullmatch(expected_hash) is None:
                raise BundlePublishError(f"run article hash is invalid: {article_name}")
            alias = unicodedata.normalize("NFC", article_name).casefold()
            if alias in seen_articles:
                raise BundlePublishError(f"run article directory alias is invalid: {article_name}")
            seen_articles.add(alias)
        for article_name, expected_hash in articles.items():
            article_path = self._safe_child(run_root, article_name)
            if self._existing_hash(article_path).bundle_hash != expected_hash:
                raise BundlePublishError(f"run article hash does not match: {article_path}")
        if _path_fingerprint(run_root, "run directory", BundlePublishError) != run_root_fingerprint:
            raise BundlePublishError(f"run directory changed during validation: {run_root}")
        _require_stable_snapshot(
            ancestor_snapshot,
            _snapshot_ancestors(run_root, BundlePublishError),
            "run ancestor",
        )
        return run_root

    def validated_run_article_metadata(
        self,
        run_date: date,
        *,
        run_directory: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Return verification JSON only from bundles referenced by a valid run."""

        root = self._safe_root()
        run_name = _validated_run_directory(run_date, run_directory)
        run_root = self._safe_child(root, run_name)
        manifest_path = run_root / "manifest.json"
        manifest_fingerprint = _path_fingerprint(
            manifest_path,
            "run manifest",
            BundlePublishError,
        )
        manifest_payload = _read_bound_file(
            manifest_path,
            manifest_fingerprint,
            max_bytes=_MAX_MANIFEST_BYTES,
            label="run manifest",
        )
        manifest = _parse_strict_json_object(manifest_payload, "run")
        self.validate_run(run_date, run_directory=run_name)
        _require_unchanged_fingerprint(
            manifest_path,
            manifest_fingerprint,
            BundlePublishError,
        )
        articles = manifest.get("articles")
        if not isinstance(articles, dict):
            raise BundlePublishError("validated run has invalid article references")
        result: list[dict[str, object]] = []
        for article_name, expected_bundle_hash in sorted(articles.items()):
            if not isinstance(article_name, str) or not isinstance(
                expected_bundle_hash,
                str,
            ):
                raise BundlePublishError("validated run has invalid article references")
            article_root = self._safe_child(run_root, article_name)
            verification_path = article_root / "verification.json"
            verification_fingerprint = _path_fingerprint(
                verification_path,
                "article verification",
                BundlePublishError,
            )
            verification_payload = _read_bound_file(
                verification_path,
                verification_fingerprint,
                max_bytes=_MAX_MANIFEST_BYTES,
                label="article verification",
            )
            validation = self._existing_hash(article_root)
            declared_hashes = dict(validation.file_hashes)
            if (
                validation.bundle_hash != expected_bundle_hash
                or declared_hashes.get("verification.json")
                != hashlib.sha256(verification_payload).hexdigest()
            ):
                raise BundlePublishError("validated article metadata hash does not match")
            _require_unchanged_fingerprint(
                verification_path,
                verification_fingerprint,
                BundlePublishError,
            )
            result.append(_parse_strict_json_object(verification_payload, "verification"))
        self.validate_run(run_date, run_directory=run_name)
        _require_unchanged_fingerprint(
            manifest_path,
            manifest_fingerprint,
            BundlePublishError,
        )
        return tuple(result)

    def _validate(self, bundle: ArticleBundle) -> tuple[tuple[BundleFile, bytes], ...]:
        if (
            _SLUG.fullmatch(bundle.slug) is None
            or _is_windows_device_name(bundle.slug)
            or "--rev-" in bundle.slug
        ):
            raise BundleValidationError("bundle slug is not a portable safe path segment")
        if not bundle.files:
            raise BundleValidationError("bundle must contain at least one file")
        seen: set[str] = set()
        for item in bundle.files:
            self._validate_relative_path(item.path)
            key = unicodedata.normalize("NFC", item.path).casefold()
            if key in seen:
                raise BundleValidationError(f"duplicate bundle path: {item.path}")
            seen.add(key)
            if item.path.casefold() in _RESERVED_PATHS:
                raise BundleValidationError(f"reserved manifest path: {item.path}")
        validated: list[tuple[BundleFile, bytes]] = []
        for item in bundle.files:
            if item.source_path is not None:
                self._assert_no_symlink(item.source_path)
                source_metadata = _lstat_no_links(
                    item.source_path,
                    "bundle source",
                    BundleValidationError,
                )
                if not stat.S_ISREG(source_metadata.st_mode):
                    raise BundleValidationError(f"source path is not a file: {item.source_path}")
                source_size = source_metadata.st_size
                source_is_declared_image = (
                    item.mime_type.startswith("image/")
                    or PurePosixPath(item.path).suffix.casefold() in _IMAGE_EXTENSIONS
                    or item.image_metadata is not None
                )
                if source_size > _MAX_BUNDLE_FILE_BYTES or (
                    source_is_declared_image and source_size > _MAX_IMAGE_BYTES
                ):
                    raise BundlePublishError(f"bundle source byte limit exceeded: {item.path}")
            try:
                payload = item.payload_bytes()
            except OSError as exc:
                raise BundleValidationError(f"cannot read bundle source: {item.path}") from exc
            if len(payload) > _MAX_BUNDLE_FILE_BYTES:
                raise BundlePublishError(f"bundle file byte limit exceeded: {item.path}")
            if _SHA256.fullmatch(item.sha256) is None:
                raise BundleValidationError(f"invalid checksum declaration: {item.path}")
            actual = hashlib.sha256(payload).hexdigest()
            if actual != item.sha256:
                raise BundleValidationError(f"checksum mismatch: {item.path}")
            self._validate_payload_semantics(item, payload)
            validated.append((item, payload))
        return tuple(validated)

    def _validate_payload_semantics(self, item: BundleFile, payload: bytes) -> None:
        extension = PurePosixPath(item.path).suffix.casefold()
        signature_mime = _sniff_image_mime(payload)
        image_like = (
            item.path.startswith("assets/")
            or signature_mime is not None
            or extension in _IMAGE_EXTENSIONS
            or item.mime_type.startswith("image/")
            or item.image_metadata is not None
        )
        if image_like:
            if signature_mime is not None and not item.mime_type.startswith("image/"):
                raise BundleValidationError(
                    f"image signature cannot use non-image MIME declaration: {item.path}"
                )
            self._validate_image(item, payload)
            return
        if extension == ".md" and item.mime_type == "text/markdown":
            document = _decode_utf8(payload, item.path)
            _reject_raw_html(document, item.path, allow_wsw_comments=True)
            return
        if extension == ".json" and item.mime_type == "application/json":
            document = _decode_utf8(payload, item.path, error_type=BundlePublishError)
            _parse_strict_json_document(document, f"JSON file {item.path}")
            return
        if extension == ".ndjson" and item.mime_type == "application/x-ndjson":
            document = _decode_utf8(payload, item.path, error_type=BundlePublishError)
            remaining_nodes = _MAX_JSON_NODES
            for line in document.splitlines():
                if line.strip():
                    _parsed, observed_nodes = _parse_strict_json_document(
                        line,
                        f"NDJSON file {item.path}",
                        max_nodes=remaining_nodes,
                    )
                    remaining_nodes -= observed_nodes
            return
        raise BundleValidationError(f"bundle file type is not allowed: {item.path}")

    def _validate_image(self, item: BundleFile, payload: bytes) -> None:
        metadata = item.image_metadata
        if metadata is None:
            raise BundleValidationError(f"image metadata is required: {item.path}")
        renderer_input = _parse_renderer_input_pairs(metadata.renderer_input, item.path)
        if item.path not in _ALLOWED_IMAGE_PATHS:
            raise BundleValidationError(f"image asset path is not approved: {item.path}")
        expected_extension = _IMAGE_EXTENSION_BY_MIME.get(item.mime_type)
        if (
            expected_extension is None
            or PurePosixPath(item.path).suffix.casefold() != expected_extension
        ):
            raise BundleValidationError(f"image MIME and extension do not match: {item.path}")
        if len(payload) > _MAX_IMAGE_BYTES:
            raise BundlePublishError(f"image exceeds source byte limit: {item.path}")
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
            metadata.renderer_fingerprint,
        )
        if not all(required) or metadata.width <= 0 or metadata.height <= 0:
            raise BundleValidationError(f"image metadata is incomplete: {item.path}")
        if (
            metadata.width > _MAX_IMAGE_EDGE
            or metadata.height > _MAX_IMAGE_EDGE
            or metadata.width * metadata.height > _MAX_IMAGE_PIXELS
        ):
            raise BundlePublishError(f"image dimensions exceed limit: {item.path}")
        if metadata.rights_status not in {"owned", "generated"}:
            raise BundleValidationError(f"image rights are not publishable: {item.path}")
        if metadata.sha256 != item.sha256 or metadata.mime_type != item.mime_type:
            raise BundleValidationError(f"image metadata checksum or MIME mismatch: {item.path}")
        if metadata.bundle_path != item.path:
            raise BundleValidationError(f"image metadata asset path mismatch: {item.path}")
        signature_mime = _sniff_image_mime(payload)
        if signature_mime != item.mime_type:
            raise BundleValidationError(f"image MIME does not match actual signature: {item.path}")
        if item.path == "assets/hero.png":
            if (
                item.sha256 != GENERIC_HERO_SHA256
                or metadata.rights_status != "generated"
                or metadata.caption != GENERIC_HERO_CAPTION
                or metadata.creator != "OpenAI ImageGen"
                or metadata.source != "이 저장소를 위해 생성한 일반 주거 이미지"
                or metadata.source_url is not None
                or metadata.rights_basis
                != "이 저장소 전용 생성 이미지이며 공식 공고 첨부물을 사용하지 않음"
                or metadata.attribution != "OpenAI ImageGen으로 생성"
                or metadata.renderer_fingerprint
                != hashlib.sha256(
                    f"openai-imagegen:{GENERIC_HERO_SHA256}".encode("ascii")
                ).hexdigest()
                or renderer_input != {"kind": "hero", "sha256": GENERIC_HERO_SHA256}
            ):
                raise BundleValidationError(
                    "generic hero provenance does not match repository asset"
                )
        elif metadata.rights_status != "owned":
            raise BundleValidationError(f"derived card must have owned rights: {item.path}")
        elif (
            metadata.renderer_fingerprint != card_renderer_fingerprint()
            or (metadata.width, metadata.height) != (1200, 630)
            or metadata.creator != "Wisdome Super Writer / Pillow"
            or metadata.source != "정규화된 공식 공고 사실"
            or metadata.source_url is not None
            or metadata.rights_basis != "저장소 코드가 정규화된 사실만으로 직접 렌더링함"
            or metadata.attribution != "Wisdome Super Writer"
        ):
            raise BundleValidationError(f"derived card provenance is invalid: {item.path}")
        else:
            expected_kind = "summary" if item.path.endswith("summary-card.webp") else "timeline"
            try:
                canonical_input = canonical_card_renderer_input(
                    renderer_input,
                    expected_kind=expected_kind,
                )
                rerendered = rerender_card_bytes(canonical_input)
                expected_alt, expected_caption = card_accessibility_material(canonical_input)
            except ImageRenderError as exc:
                raise BundlePublishError(
                    f"derived card renderer or rerender failed: {item.path}"
                ) from exc
            if rerendered != payload:
                raise BundleValidationError(
                    f"derived card bytes do not match deterministic rerender: {item.path}"
                )
            if metadata.alt != expected_alt or metadata.caption != expected_caption:
                raise BundleValidationError(
                    f"derived card accessibility does not match renderer input: {item.path}"
                )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as image:
                    actual_mime = _IMAGE_MIME_BY_FORMAT.get(image.format or "")
                    size = image.size
                    width, height = size
                    if (
                        width > _MAX_IMAGE_EDGE
                        or height > _MAX_IMAGE_EDGE
                        or width * height > _MAX_IMAGE_PIXELS
                    ):
                        raise BundlePublishError(f"image dimensions exceed limit: {item.path}")
                    image.verify()
        except BundlePublishError:
            raise
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise BundlePublishError(f"image decompression bomb rejected: {item.path}") from exc
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            raise BundlePublishError(f"image content is invalid: {item.path}") from exc
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
        existed = self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(self.root)
        root = Path(os.path.abspath(self.root))
        if not existed:
            _fsync_directory(root.parent)
        return root

    def _ensure_directory(self, path: Path) -> None:
        self._assert_no_symlink(path)
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(path)
        if not existed:
            _fsync_directory(path.parent)

    def _safe_child(self, parent: Path, relative: str) -> Path:
        child = parent.joinpath(*PurePosixPath(relative).parts)
        self._assert_child(parent, child)
        return child

    @staticmethod
    def _assert_child(parent: Path, child: Path) -> None:
        try:
            parent_absolute = os.path.normcase(os.path.abspath(parent))
            child_absolute = os.path.normcase(os.path.abspath(child))
            if os.path.commonpath((parent_absolute, child_absolute)) != parent_absolute:
                raise ValueError
        except (OSError, ValueError) as exc:
            raise BundleValidationError(f"path escapes configured root: {child}") from exc
        ArticleBundleWriter._assert_no_symlink(parent)

    @staticmethod
    def _validate_relative_path(value: str) -> None:
        if (
            not value
            or "\\" in value
            or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
        ):
            raise BundleValidationError(f"invalid bundle path: {value!r}")
        if unicodedata.normalize("NFC", value) != value:
            raise BundleValidationError(f"non-canonical Unicode bundle path: {value}")
        path = PurePosixPath(value)
        canonical = "/".join(path.parts)
        if (
            path.is_absolute()
            or value != canonical
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise BundleValidationError(f"unsafe bundle path: {value}")
        for part in path.parts:
            if (
                ":" in part
                or part.endswith((".", " "))
                or _is_windows_device_name(part)
            ):
                raise BundleValidationError(f"non-portable bundle path: {value}")

    @staticmethod
    def _assert_no_symlink(path: Path) -> None:
        path = Path(path)
        candidates = [path, *path.parents]
        for candidate in candidates:
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise BundleValidationError(f"cannot inspect path ancestor: {candidate}") from exc
            if stat.S_ISLNK(metadata.st_mode) or (
                getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise BundleValidationError(f"symlink or reparse path is not allowed: {path}")

    def _create_owned_staging(
        self,
        root: Path,
        target: Path,
        bundle_hash: str,
    ) -> tuple[Path, tuple[object, ...]]:
        for _attempt in range(10):
            nonce = secrets.token_hex(8)
            staging = target.with_name(
                f".{target.name}.tmp-{bundle_hash[:12]}-{self._writer_token}-{nonce}"
            )
            self._assert_child(root, staging)
            try:
                staging.mkdir()
            except FileExistsError:
                continue
            fingerprint = _path_identity_fingerprint(
                staging,
                "owned staging",
                BundlePublishError,
            )
            _fsync_directory(staging.parent)
            return staging, fingerprint
        raise BundlePublishError("could not allocate a unique writer-owned staging directory")

    def _validate_owned_article_stage(
        self,
        staging: Path,
        owned_fingerprint: tuple[object, ...],
        bundle: ArticleBundle,
    ) -> None:
        _require_owned_identity(staging, owned_fingerprint, "owned staging")
        observed = self._existing_hash(
            staging,
            expected_slug=bundle.slug,
            allow_staging_name=True,
        )
        if observed.bundle_hash != bundle.bundle_hash:
            raise BundlePublishError(f"owned staging manifest does not match bundle: {staging}")
        _require_owned_identity(
            staging,
            owned_fingerprint,
            "fully validated owned staging",
        )

    def _write_owned_run_temp(
        self,
        run_root: Path,
        label: str,
        content_hash: str,
        payload: bytes,
    ) -> tuple[Path, tuple[object, ...]]:
        for _attempt in range(10):
            nonce = secrets.token_hex(8)
            path = self._safe_child(
                run_root,
                f".{label}.tmp-{content_hash[:12]}-{self._writer_token}-{nonce}",
            )
            try:
                _write_bytes_fsynced(path, payload)
            except FileExistsError:
                continue
            fingerprint = _path_identity_fingerprint(
                path,
                "owned run temp",
                BundlePublishError,
            )
            return path, fingerprint
        raise BundlePublishError("could not allocate a unique writer-owned run temp file")

    @staticmethod
    def _validate_owned_run_temp(
        path: Path,
        owned_fingerprint: tuple[object, ...],
        expected_payload: bytes,
        label: str,
    ) -> None:
        _require_owned_identity(path, owned_fingerprint, label)
        current_fingerprint = _path_fingerprint(path, label, BundlePublishError)
        observed = _read_bound_file(
            path,
            current_fingerprint,
            max_bytes=max(len(expected_payload), 1),
            label=label,
        )
        if observed != expected_payload:
            raise BundlePublishError(f"{label} bytes were substituted: {path}")
        _require_owned_identity(path, owned_fingerprint, f"handle-validated {label}")

    def _publication_target(
        self,
        primary: Path,
        bundle_hash: str,
    ) -> tuple[Path, _ExistingBundleValidation | None]:
        self._assert_no_symlink(primary)
        if not _path_exists_no_follow(primary):
            return primary, None
        existing = self._existing_hash(primary)
        if existing.bundle_hash == bundle_hash:
            return primary, existing
        revision = primary.with_name(f"{primary.name}--rev-{bundle_hash[:12]}")
        self._assert_no_symlink(revision)
        if _path_exists_no_follow(revision):
            existing = self._existing_hash(revision)
            if existing.bundle_hash == bundle_hash:
                return revision, existing
            raise BundlePublishError(f"revision hash collision: {revision}")
        return revision, None

    def _existing_hash(
        self,
        path: Path,
        *,
        expected_slug: str | None = None,
        allow_staging_name: bool = False,
    ) -> _ExistingBundleValidation:
        ancestor_snapshot = _snapshot_ancestors(path, BundlePublishError)
        actual_files, actual_directories, entry_snapshot = _snapshot_tree(
            path,
            BundlePublishError,
        )
        manifest_path = path / "manifest.json"
        _require_regular_directory(path, "existing bundle", BundlePublishError)
        if not _is_regular_file_no_links(manifest_path):
            raise BundlePublishError(f"existing bundle is incomplete or unsafe: {path}")
        manifest_fingerprint = entry_snapshot.get("manifest.json")
        if manifest_fingerprint is None:
            raise BundlePublishError(f"existing bundle manifest was not snapshotted: {path}")
        manifest_payload = _read_bound_file(
            manifest_path,
            manifest_fingerprint,
            max_bytes=_MAX_MANIFEST_BYTES,
            label="article manifest",
        )
        manifest = _parse_strict_json_object(manifest_payload, "article")
        if set(manifest) != {"schema_version", "run_date", "slug", "bundle_hash", "files"}:
            raise BundlePublishError(f"article JSON manifest violates closed schema: {path}")
        bundle_hash = manifest.get("bundle_hash")
        if not isinstance(bundle_hash, str) or _SHA256.fullmatch(bundle_hash) is None:
            raise BundlePublishError(f"existing bundle manifest has no valid hash: {path}")
        manifest_slug = manifest.get("slug")
        expected_names = (
            manifest_slug,
            f"{manifest_slug}--rev-{bundle_hash[:12]}",
        )
        if (
            manifest.get("schema_version") != 1
            or not isinstance(manifest_slug, str)
            or _SLUG.fullmatch(manifest_slug) is None
            or _is_windows_device_name(manifest_slug)
            or manifest.get("run_date") != _run_date_from_directory(path.parent.name)
            or (expected_slug is not None and manifest_slug != expected_slug)
            or (not allow_staging_name and path.name not in expected_names)
        ):
            raise BundlePublishError(f"existing bundle identity or slug is invalid: {path}")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            raise BundlePublishError(f"existing bundle manifest has no files: {path}")
        seen_paths: set[str] = set()
        for relative in files:
            if not isinstance(relative, str):
                raise BundlePublishError(f"existing bundle path key is invalid: {path}")
            try:
                ArticleBundleWriter._validate_relative_path(relative)
            except BundleValidationError as exc:
                message = f"existing bundle contains a non-canonical path alias: {path}"
                raise BundlePublishError(message) from exc
            alias = unicodedata.normalize("NFC", relative).casefold()
            if alias in seen_paths:
                raise BundlePublishError(f"existing bundle contains a path alias: {relative}")
            if alias in _RESERVED_PATHS:
                raise BundlePublishError(f"existing bundle contains a reserved path: {relative}")
            seen_paths.add(alias)
        checksum_lines: list[str] = []
        hash_material: list[dict[str, object]] = []
        for relative, entry in sorted(files.items()):
            if not isinstance(relative, str) or not isinstance(entry, dict):
                raise BundlePublishError(f"existing bundle file manifest is invalid: {path}")
            expected_entry_keys = {"sha256", "mime_type", "bytes"}
            if "image" in entry:
                expected_entry_keys.add("image")
            if set(entry) != expected_entry_keys:
                raise BundlePublishError(
                    f"existing file manifest violates closed schema: {relative}"
                )
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
            if not _is_regular_file_no_links(source):
                raise BundlePublishError(f"existing bundle file is missing or unsafe: {source}")
            expected_fingerprint = entry_snapshot.get(relative)
            if expected_fingerprint is None:
                raise BundlePublishError(f"existing bundle entry was not snapshotted: {source}")
            payload = _read_bound_file(
                source,
                expected_fingerprint,
                max_bytes=_MAX_BUNDLE_FILE_BYTES,
                label="bundle entry",
            )
            if hashlib.sha256(payload).hexdigest() != declared:
                raise BundlePublishError(f"existing bundle checksum mismatch: {source}")
            if entry.get("bytes") != len(payload):
                raise BundlePublishError(f"existing bundle byte count mismatch: {source}")
            image_metadata = _article_image_from_manifest(entry.get("image"), source)
            replay_file = BundleFile(
                path=relative,
                content=payload,
                mime_type=mime_type,
                sha256=declared,
                image_metadata=image_metadata,
            )
            try:
                self._validate_payload_semantics(replay_file, payload)
            except BundleValidationError as exc:
                raise BundlePublishError(
                    f"existing bundle semantic provenance is invalid: {source}: {exc}"
                ) from exc
            checksum_lines.append(f"{declared}  {relative}\n")
            hash_material.append(
                {
                    "path": relative,
                    "sha256": declared,
                    "mime_type": mime_type,
                    "image": entry.get("image"),
                }
            )
        expected_files = set(files) | _RESERVED_PATHS
        expected_directories = _parent_directories(expected_files)
        if actual_files != expected_files or actual_directories != expected_directories:
            raise BundlePublishError(f"existing bundle contains unmanifested material: {path}")
        checksum_path = path / "manifest.sha256"
        if not _is_regular_file_no_links(checksum_path):
            raise BundlePublishError(f"existing checksum manifest is missing or unsafe: {path}")
        checksum_fingerprint = entry_snapshot.get("manifest.sha256")
        if checksum_fingerprint is None:
            raise BundlePublishError(f"existing checksum manifest was not snapshotted: {path}")
        checksum_payload = _read_bound_file(
            checksum_path,
            checksum_fingerprint,
            max_bytes=_MAX_MANIFEST_BYTES,
            label="checksum manifest",
        )
        try:
            checksum_document = checksum_payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
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
        _require_stable_snapshot(
            entry_snapshot,
            _snapshot_tree(path, BundlePublishError)[2],
            "bundle entry",
        )
        _require_stable_snapshot(
            ancestor_snapshot,
            _snapshot_ancestors(path, BundlePublishError),
            "bundle ancestor",
        )
        return _ExistingBundleValidation(
            bundle_hash=bundle_hash,
            root_fingerprint=entry_snapshot["."],
            file_hashes=tuple(
                sorted(
                    (relative, entry["sha256"])
                    for relative, entry in files.items()
                    if isinstance(relative, str)
                    and isinstance(entry, dict)
                    and isinstance(entry.get("sha256"), str)
                )
            ),
        )


def _parse_renderer_input_pairs(value: object, path: str) -> dict[str, str]:
    if not isinstance(value, tuple):
        raise BundlePublishError(f"renderer input is not a closed pair sequence: {path}")
    result: dict[str, str] = {}
    for pair in value:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise BundlePublishError(f"renderer input has a malformed pair: {path}")
        key, item = pair
        if not isinstance(key, str) or not isinstance(item, str):
            raise BundlePublishError(f"renderer input pair types are invalid: {path}")
        if key in result:
            raise BundlePublishError(f"renderer input contains a duplicate key: {path}")
        result[key] = item
    return result


def _decode_utf8(
    payload: bytes,
    path: str,
    *,
    error_type: type[BundleValidationError] = BundleValidationError,
) -> str:
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise error_type(f"text file is not valid UTF-8: {path}") from exc


def _reject_raw_html(
    document: str,
    path: str,
    *,
    allow_wsw_comments: bool = False,
) -> None:
    inspected = _ALLOWED_WSW_COMMENT.sub("", document) if allow_wsw_comments else document
    if "<!--" in inspected or _RAW_HTML.search(inspected):
        raise BundleValidationError(f"raw HTML or SVG is not allowed: {path}")


def _reject_html_in_json_values(
    value: object,
    path: str,
    *,
    max_nodes: int = _MAX_JSON_NODES,
) -> int:
    stack: list[tuple[object, int]] = [(value, 0)]
    observed_nodes = 0
    while stack:
        current, depth = stack.pop()
        observed_nodes += 1
        if depth > _MAX_JSON_DEPTH:
            raise BundlePublishError(f"JSON depth resource limit exceeded: {path}")
        if observed_nodes > max_nodes:
            raise BundlePublishError(f"JSON node resource limit exceeded: {path}")
        if isinstance(current, str):
            _reject_raw_html(current, path)
        elif isinstance(current, dict):
            if observed_nodes + len(stack) + (len(current) * 2) > max_nodes:
                raise BundlePublishError(f"JSON node resource limit exceeded: {path}")
            for key, item in current.items():
                stack.append((item, depth + 1))
                stack.append((key, depth + 1))
        elif isinstance(current, list):
            if observed_nodes + len(stack) + len(current) > max_nodes:
                raise BundlePublishError(f"JSON node resource limit exceeded: {path}")
            stack.extend((item, depth + 1) for item in current)
    return observed_nodes


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _strict_json_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key}")
        result[key] = value
    return result


def _parse_strict_json_document(
    document: str,
    label: str,
    *,
    max_nodes: int = _MAX_JSON_NODES,
) -> tuple[object, int]:
    try:
        _reject_raw_html(document, label)
        value = json.loads(
            document,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_strict_json_object_pairs,
        )
        observed_nodes = _reject_html_in_json_values(
            value,
            label,
            max_nodes=max_nodes,
        )
    except BundlePublishError:
        raise
    except BundleValidationError as exc:
        raise BundlePublishError(str(exc)) from exc
    except (RecursionError, ValueError) as exc:
        raise BundlePublishError(f"{label} is invalid") from exc
    return value, observed_nodes


def _parse_strict_json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        document = payload.decode("utf-8", errors="strict")
        value, _observed_nodes = _parse_strict_json_document(
            document,
            f"{label} JSON manifest",
        )
    except BundlePublishError:
        raise
    except UnicodeDecodeError as exc:
        raise BundlePublishError(f"{label} JSON manifest is invalid") from exc
    if not isinstance(value, dict):
        raise BundlePublishError(f"{label} JSON manifest must be an object")
    return value


def _article_image_from_manifest(value: object, source: Path) -> ArticleImage | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BundlePublishError(f"existing image manifest is invalid: {source}")
    required_strings = (
        "path",
        "sha256",
        "mime_type",
        "alt",
        "caption",
        "creator",
        "source",
        "rights_status",
        "rights_basis",
        "attribution",
        "renderer_fingerprint",
    )
    expected_keys = set(required_strings) | {
        "width",
        "height",
        "source_url",
        "renderer_input",
    }
    if set(value) != expected_keys:
        raise BundlePublishError(f"existing image manifest violates closed schema: {source}")
    if any(not isinstance(value.get(key), str) for key in required_strings):
        raise BundlePublishError(f"existing image manifest has invalid strings: {source}")
    width = value.get("width")
    height = value.get("height")
    source_url = value.get("source_url")
    renderer_input_value = value.get("renderer_input")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or (source_url is not None and not isinstance(source_url, str))
    ):
        raise BundlePublishError(f"existing image manifest has invalid dimensions: {source}")
    if not isinstance(renderer_input_value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in renderer_input_value.items()
    ):
        raise BundlePublishError(f"existing renderer input material is invalid: {source}")
    renderer_input = tuple(renderer_input_value.items())
    return ArticleImage(
        path=source,
        bundle_path=value["path"],
        sha256=value["sha256"],
        mime_type=value["mime_type"],
        width=width,
        height=height,
        alt=value["alt"],
        caption=value["caption"],
        creator=value["creator"],
        source=value["source"],
        source_url=source_url,
        rights_status=value["rights_status"],
        rights_basis=value["rights_basis"],
        attribution=value["attribution"],
        renderer_fingerprint=value["renderer_fingerprint"],
        renderer_input=renderer_input,
    )


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
    except OSError as exc:
        if _directory_fsync_is_unsupported(exc):
            return
        raise BundlePublishError(f"directory fsync open failed: {path}") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if not _directory_fsync_is_unsupported(exc):
            raise BundlePublishError(f"directory fsync failed: {path}") from exc
    finally:
        os.close(descriptor)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_fsync_is_unsupported(exc: OSError) -> bool:
    unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}
    if hasattr(errno, "EOPNOTSUPP"):
        unsupported.add(errno.EOPNOTSUPP)
    if os.name == "nt":
        unsupported.add(errno.EACCES)
    return exc.errno in unsupported


def _fsync_tree_directories(root: Path) -> None:
    _files, directories = _scan_tree(root, BundlePublishError)
    for relative in sorted(directories, key=lambda value: value.count("/"), reverse=True):
        _fsync_directory(root.joinpath(*PurePosixPath(relative).parts))
    _fsync_directory(root)


def _sniff_image_mime(payload: bytes) -> str | None:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload.startswith(b"BM"):
        return "image/bmp"
    if payload.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if payload.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if len(payload) >= 12 and payload[4:8] == b"ftyp":
        brand = payload[8:12]
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
    text_prefix = payload[:512].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if text_prefix.startswith(b"<svg") or b"<svg" in text_prefix[:256]:
        return "image/svg+xml"
    return None


def _is_windows_device_name(value: str) -> bool:
    stem = value.split(".", 1)[0].casefold()
    return stem in _WINDOWS_DEVICE_NAMES


def _validate_article_directory_name(value: str) -> None:
    base, marker, revision = value.partition("--rev-")
    if (
        _SLUG.fullmatch(base) is None
        or _is_windows_device_name(base)
        or (marker and re.fullmatch(r"[0-9a-f]{12}", revision) is None)
    ):
        raise BundlePublishError(f"run article directory name is unsafe: {value}")


def _validated_run_directory(run_date: date, value: str | None) -> str:
    expected = run_date.isoformat()
    run_directory = expected if value is None else value
    if not isinstance(run_directory, str) or re.fullmatch(
        rf"{re.escape(expected)}(?:--run-[0-9a-f]{{12}})?",
        run_directory,
    ) is None:
        raise BundlePublishError("run directory name is unsafe")
    return run_directory


def _run_date_from_directory(value: str) -> str | None:
    match = re.fullmatch(r"(?P<date>\d{4}-\d{2}-\d{2})(?:--run-[0-9a-f]{12})?", value)
    return match.group("date") if match is not None else None


def _path_exists_no_follow(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BundleValidationError(f"cannot inspect path: {path}") from exc
    return True


def _lstat_no_links(
    path: Path,
    label: str,
    error_type: type[BundleValidationError],
) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise error_type(f"{label} is missing or cannot be inspected: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or (
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise error_type(f"{label} contains a symlink or reparse point: {path}")
    return metadata


def _fingerprint_metadata(metadata: os.stat_result) -> tuple[object, ...]:
    return (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        getattr(metadata, "st_file_attributes", 0),
    )


def _path_fingerprint(
    path: Path,
    label: str,
    error_type: type[BundleValidationError],
) -> tuple[object, ...]:
    return _fingerprint_metadata(_lstat_no_links(path, label, error_type))


def _path_identity_fingerprint(
    path: Path,
    label: str,
    error_type: type[BundleValidationError],
) -> tuple[object, ...]:
    fingerprint = _path_fingerprint(path, label, error_type)
    file_type, device, inode, _size, _mtime, _ctime, attributes = fingerprint
    return (file_type, device, inode, attributes)


def _require_owned_identity(
    path: Path,
    expected: tuple[object, ...],
    label: str,
) -> None:
    observed = _path_identity_fingerprint(path, label, BundlePublishError)
    if observed != expected:
        raise BundlePublishError(
            f"{label} owned identity, type, or reparse state was substituted: {path}"
        )


def _require_validated_existing_root(
    path: Path,
    validation: _ExistingBundleValidation,
    label: str,
) -> None:
    observed = _path_fingerprint(path, label, BundlePublishError)
    if observed != validation.root_fingerprint:
        raise BundlePublishError(
            f"{label} identity, type, or reparse state was substituted: {path}"
        )


def _require_unchanged_fingerprint(
    path: Path,
    expected: tuple[object, ...],
    error_type: type[BundleValidationError],
) -> None:
    if _path_fingerprint(path, "bundle entry", error_type) != expected:
        raise error_type(f"bundle entry identity changed during replay: {path}")


def _opened_handle_fingerprint(metadata: os.stat_result) -> tuple[object, ...]:
    return (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        getattr(metadata, "st_file_attributes", 0),
    )


def _opened_handle_fingerprint_from_path(
    fingerprint: tuple[object, ...],
) -> tuple[object, ...]:
    file_type, device, inode, size, _mtime, _ctime, attributes = fingerprint
    return (file_type, device, inode, size, attributes)


def _read_bound_file(
    path: Path,
    expected_fingerprint: tuple[object, ...],
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    _require_unchanged_fingerprint(path, expected_fingerprint, BundlePublishError)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BundlePublishError(f"{label} could not be opened safely: {path}") from exc
    try:
        before = _opened_handle_fingerprint(os.fstat(descriptor))
        expected_handle = _opened_handle_fingerprint_from_path(expected_fingerprint)
        if before != expected_handle or before[0] != stat.S_IFREG:
            raise BundlePublishError(
                f"{label} opened identity differs from lstat: {path}; "
                f"expected={expected_handle!r}; opened={before!r}"
            )
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, max_bytes + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > max_bytes:
                raise BundlePublishError(f"{label} exceeds bounded read limit: {path}")
        after = _opened_handle_fingerprint(os.fstat(descriptor))
        if after != before:
            raise BundlePublishError(f"{label} opened identity changed during read: {path}")
    except OSError as exc:
        raise BundlePublishError(f"{label} handle read failed: {path}") from exc
    finally:
        os.close(descriptor)
    _require_unchanged_fingerprint(path, expected_fingerprint, BundlePublishError)
    return b"".join(chunks)


def _snapshot_ancestors(
    path: Path,
    error_type: type[BundleValidationError],
) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for candidate in (path, *path.parents):
        try:
            fingerprint = _path_identity_fingerprint(candidate, "bundle ancestor", error_type)
        except BundleValidationError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                continue
            raise
        snapshot[os.path.normcase(os.path.abspath(candidate))] = fingerprint
    return snapshot


def _require_stable_snapshot(
    before: dict[str, tuple[object, ...]],
    after: dict[str, tuple[object, ...]],
    label: str,
) -> None:
    if before != after:
        raise BundlePublishError(f"{label} identity changed during replay TOCTOU validation")


def _require_regular_directory(
    path: Path,
    label: str,
    error_type: type[BundleValidationError],
) -> None:
    metadata = _lstat_no_links(path, label, error_type)
    if not stat.S_ISDIR(metadata.st_mode):
        raise error_type(f"{label} is not a directory: {path}")


def _is_regular_file_no_links(path: Path) -> bool:
    try:
        metadata = _lstat_no_links(path, "file", BundlePublishError)
    except BundlePublishError:
        return False
    return stat.S_ISREG(metadata.st_mode)


def _scan_tree(
    root: Path,
    error_type: type[BundleValidationError],
) -> tuple[set[str], set[str]]:
    files, directories, _fingerprints = _snapshot_tree(root, error_type)
    return files, directories


def _snapshot_tree(
    root: Path,
    error_type: type[BundleValidationError],
) -> tuple[set[str], set[str], dict[str, tuple[object, ...]]]:
    files: set[str] = set()
    directories: set[str] = set()
    root_fingerprint = _path_fingerprint(root, "bundle tree root", error_type)
    fingerprints: dict[str, tuple[object, ...]] = {".": root_fingerprint}

    def visit(directory: Path, relative_parent: PurePosixPath | None = None) -> None:
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise error_type(f"cannot inspect bundle tree: {directory}") from exc
        for entry in entries:
            relative = (
                PurePosixPath(entry.name)
                if relative_parent is None
                else relative_parent / entry.name
            )
            value = relative.as_posix()
            entry_path = Path(entry.path)
            metadata = _lstat_no_links(entry_path, "bundle tree", error_type)
            fingerprint = _fingerprint_metadata(metadata)
            fingerprints[value] = fingerprint
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(value)
                visit(entry_path, relative)
            elif stat.S_ISREG(metadata.st_mode):
                files.add(value)
            else:
                raise error_type(f"bundle tree contains a non-regular entry: {entry.path}")
            if _path_fingerprint(entry_path, "bundle tree", error_type) != fingerprint:
                raise error_type(f"bundle tree entry changed while scanning: {entry.path}")

    visit(root)
    if _path_fingerprint(root, "bundle tree root", error_type) != root_fingerprint:
        raise error_type(f"bundle tree root changed while scanning: {root}")
    return files, directories, fingerprints


def _parent_directories(paths: set[str] | frozenset[str]) -> set[str]:
    result: set[str] = set()
    for value in paths:
        parent = PurePosixPath(value).parent
        while parent != PurePosixPath("."):
            result.add(parent.as_posix())
            parent = parent.parent
    return result
