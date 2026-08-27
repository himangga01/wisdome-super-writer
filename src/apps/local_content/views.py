"""Read-only, loopback-only preview for validated local article bundles."""

from __future__ import annotations

import html
import ipaddress
import os
import re
import stat
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.safestring import SafeString, mark_safe

from apps.local_content.bundles import (
    ArticleBundleWriter,
    BundlePublishError,
    BundleValidationError,
    _parse_strict_json_object,
    _path_fingerprint,
    _read_bound_file,
    _require_unchanged_fingerprint,
)

_RUN_NAME = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})(?:--run-[0-9a-f]{12})?")
_ARTICLE_NAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?(?:--rev-[0-9a-f]{12})?"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOCAL_ARTICLE_LINK = re.compile(
    r"\./(?P<article>[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?"
    r"(?:--rev-[0-9a-f]{12})?)/article\.md"
)
_INLINE = re.compile(
    r"!\[(?P<image_alt>[^\]\n]*)\]\((?P<image_target>[^)\n]+)\)"
    r"|\[(?P<link_text>[^\]\n]+)\]\((?P<link_target>[^)\n]+)\)"
    r"|`(?P<code>[^`\n]+)`"
)
_TABLE_SEPARATOR = re.compile(r"^\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?$")
_WSW_COMMENT = re.compile(r"^<!-- WSW:(?:block|endblock|slot):[a-z0-9_-]+ -->$")
_OFFICIAL_HOSTS = frozenset({"www.applyhome.co.kr", "apply.lh.or.kr", "www.data.go.kr"})
_ALLOWED_IMAGE_MIMES = frozenset({"image/png", "image/webp"})
_MAX_TEXT_BYTES = 16 * 1024 * 1024
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_REPARSE_POINT = 0x400
_CSP = (
    "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
    "object-src 'none'; img-src 'self'; style-src 'self'"
)


def local_article_index(request: HttpRequest) -> HttpResponse:
    _require_local_request(request)
    runs = _list_validated_runs()
    return _page_response(
        render(request, "local_articles/index.html", {"runs": runs}),
    )


def local_article_run(request: HttpRequest, run_name: str) -> HttpResponse:
    _require_local_request(request)
    run_root, writer, manifest = _validated_run(run_name)
    final_articles = _final_articles(run_root, writer, manifest)
    index_payload = _read_validated_file(run_root / "index.md", max_bytes=_MAX_TEXT_BYTES)
    try:
        index_markdown = index_payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise Http404 from exc
    _revalidate_run(writer, run_name)
    rendered = render_markdown_preview(
        index_markdown,
        run_name=run_name,
        article_name=None,
        article_files={},
        final_articles=frozenset(final_articles),
    )
    return _page_response(
        render(
            request,
            "local_articles/run.html",
            {"run_name": run_name, "article_html": rendered},
        )
    )


def local_article_detail(
    request: HttpRequest,
    run_name: str,
    article_name: str,
) -> HttpResponse:
    _require_local_request(request)
    run_root, writer, manifest = _validated_run(run_name)
    final_articles = _final_articles(run_root, writer, manifest)
    if article_name not in final_articles:
        raise Http404
    article_root = _confined_existing(run_root, article_name)
    files = _article_manifest_files(article_root, writer, run_name)
    if "article.md" not in files:
        raise Http404
    markdown_payload = _read_validated_file(
        article_root / "article.md",
        max_bytes=_MAX_TEXT_BYTES,
    )
    try:
        markdown = markdown_payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise Http404 from exc
    _revalidate_run(writer, run_name)
    rendered = render_markdown_preview(
        markdown,
        run_name=run_name,
        article_name=article_name,
        article_files=files,
        final_articles=frozenset(final_articles),
    )
    return _page_response(
        render(
            request,
            "local_articles/article.html",
            {
                "run_name": run_name,
                "article_name": article_name,
                "article_html": rendered,
            },
        )
    )


def local_article_asset(
    request: HttpRequest,
    run_name: str,
    article_name: str,
    checksum: str,
    asset_path: str,
) -> HttpResponse:
    _require_local_request(request)
    if _SHA256.fullmatch(checksum) is None:
        raise Http404
    run_root, writer, manifest = _validated_run(run_name)
    final_articles = _final_articles(run_root, writer, manifest)
    if article_name not in final_articles:
        raise Http404
    article_root = _confined_existing(run_root, article_name)
    files = _article_manifest_files(article_root, writer, run_name)
    entry = files.get(asset_path)
    if (
        not isinstance(entry, dict)
        or entry.get("sha256") != checksum
        or entry.get("mime_type") not in _ALLOWED_IMAGE_MIMES
        or not asset_path.startswith("assets/")
    ):
        raise Http404
    asset = _confined_existing(article_root, asset_path)
    payload = _read_validated_file(asset, max_bytes=_MAX_IMAGE_BYTES)
    _revalidate_run(writer, run_name)
    response = HttpResponse(payload, content_type=str(entry["mime_type"]))
    return _secure_response(response, cache_control="public, max-age=31536000, immutable")


def local_article_status(request: HttpRequest) -> JsonResponse:
    _require_local_request(request)
    runs = _list_validated_runs()
    response = JsonResponse(
        {
            "status": "ok",
            "workflow": {
                "validRunCount": len(runs),
                "latestRun": runs[0]["name"] if runs else None,
            },
            "humanizer": _humanizer_status(),
        }
    )
    return _secure_response(response, cache_control="no-store")  # type: ignore[return-value]


def render_markdown_preview(
    markdown: str,
    *,
    run_name: str,
    article_name: str | None,
    article_files: dict[str, object],
    final_articles: frozenset[str],
) -> SafeString:
    """Render the generated Markdown subset after escaping every text token."""

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0] == "---":
        try:
            closing = lines.index("---", 1)
        except ValueError:
            closing = -1
        if closing > 0:
            lines = lines[closing + 1 :]

    output: list[str] = []
    paragraph: list[str] = []
    in_list = False
    index = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            body = " ".join(value.strip() for value in paragraph)
            rendered_body = _render_inline(
                body,
                run_name,
                article_name,
                article_files,
                final_articles,
            )
            output.append(f"<p>{rendered_body}</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            output.append("</ul>")
            in_list = False

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            close_list()
            index += 1
            continue
        if _WSW_COMMENT.fullmatch(stripped):
            flush_paragraph()
            close_list()
            index += 1
            continue
        heading = re.fullmatch(r"(#{1,3})\s+(.+)", stripped)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            body = _render_inline(
                heading.group(2), run_name, article_name, article_files, final_articles
            )
            output.append(f"<h{level}>{body}</h{level}>")
            index += 1
            continue
        if (
            "|" in stripped
            and index + 1 < len(lines)
            and _TABLE_SEPARATOR.fullmatch(lines[index + 1].strip())
        ):
            flush_paragraph()
            close_list()
            headers = _table_cells(stripped)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_table_cells(lines[index].strip()))
                index += 1
            output.append("<div class=\"table-scroll\"><table><thead><tr>")
            for cell in headers:
                rendered_cell = _render_inline(
                    cell, run_name, article_name, article_files, final_articles
                )
                output.append(f"<th>{rendered_cell}</th>")
            output.append("</tr></thead><tbody>")
            for row in rows:
                output.append("<tr>")
                for cell in row:
                    rendered_cell = _render_inline(
                        cell, run_name, article_name, article_files, final_articles
                    )
                    output.append(f"<td>{rendered_cell}</td>")
                output.append("</tr>")
            output.append("</tbody></table></div>")
            continue
        list_item = re.fullmatch(r"\s*-\s+(.+)", line)
        if list_item:
            flush_paragraph()
            if not in_list:
                output.append("<ul>")
                in_list = True
            body = _render_inline(
                list_item.group(1), run_name, article_name, article_files, final_articles
            )
            output.append(f"<li>{body}</li>")
            index += 1
            continue
        paragraph.append(line)
        index += 1

    flush_paragraph()
    close_list()
    return mark_safe("".join(output))


def _render_inline(
    value: str,
    run_name: str,
    article_name: str | None,
    article_files: dict[str, object],
    final_articles: frozenset[str],
) -> str:
    output: list[str] = []
    cursor = 0
    for match in _INLINE.finditer(value):
        output.append(html.escape(value[cursor : match.start()], quote=True))
        if match.group("code") is not None:
            output.append(f"<code>{html.escape(match.group('code'), quote=True)}</code>")
        elif match.group("image_alt") is not None:
            target, title = _markdown_target(match.group("image_target"))
            entry = article_files.get(target)
            if (
                article_name is not None
                and target.startswith("assets/")
                and isinstance(entry, dict)
                and entry.get("mime_type") in _ALLOWED_IMAGE_MIMES
                and isinstance(entry.get("sha256"), str)
                and _SHA256.fullmatch(str(entry["sha256"]))
            ):
                url = reverse(
                    "local-article-asset",
                    kwargs={
                        "run_name": run_name,
                        "article_name": article_name,
                        "checksum": entry["sha256"],
                        "asset_path": target,
                    },
                )
                title_attribute = (
                    f' title="{html.escape(title, quote=True)}"' if title is not None else ""
                )
                output.append(
                    f'<figure><img src="{html.escape(url, quote=True)}" '
                    f'alt="{html.escape(match.group("image_alt"), quote=True)}"'
                    f"{title_attribute}></figure>"
                )
            else:
                output.append(html.escape(match.group(0), quote=True))
        else:
            label = html.escape(match.group("link_text"), quote=True)
            target, _title = _markdown_target(match.group("link_target"))
            official = _official_https_url(target)
            local_match = _LOCAL_ARTICLE_LINK.fullmatch(target)
            if official:
                output.append(
                    f'<a href="{html.escape(official, quote=True)}" '
                    'rel="noopener noreferrer">'
                    f"{label}</a>"
                )
            elif local_match and local_match.group("article") in final_articles:
                url = reverse(
                    "local-article-detail",
                    kwargs={
                        "run_name": run_name,
                        "article_name": local_match.group("article"),
                    },
                )
                output.append(f'<a href="{html.escape(url, quote=True)}">{label}</a>')
            else:
                output.append(label)
        cursor = match.end()
    output.append(html.escape(value[cursor:], quote=True))
    return "".join(output)


def _markdown_target(value: str) -> tuple[str, str | None]:
    value = value.strip()
    title: str | None = None
    if value.startswith("<"):
        closing = value.find(">")
        if closing < 0:
            return value, None
        target = value[1:closing]
        remainder = value[closing + 1 :].strip()
    else:
        target, separator, remainder = value.partition(" ")
        remainder = remainder.strip() if separator else ""
    if len(remainder) >= 2 and remainder[0] == remainder[-1] == '"':
        title = remainder[1:-1]
    return target, title


def _official_https_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _OFFICIAL_HOSTS
        or parsed.username
        or parsed.password
        or (port is not None and port != 443)
        or not parsed.path.startswith("/")
    ):
        return None
    return value


def _table_cells(value: str) -> list[str]:
    return [cell.strip() for cell in value.strip().strip("|").split("|")]


def _list_validated_runs() -> list[dict[str, object]]:
    root = _article_root(required=False)
    if root is None:
        return []
    writer = ArticleBundleWriter(root)
    runs: list[dict[str, object]] = []
    try:
        entries = tuple(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        parsed = _parse_run_name(entry.name)
        if parsed is None or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            run_root = writer.validate_run(parsed, run_directory=entry.name)
            manifest = _run_manifest(run_root)
            final_articles = _final_articles(run_root, writer, manifest)
        except (BundlePublishError, BundleValidationError, OSError, ValueError):
            continue
        runs.append(
            {
                "name": entry.name,
                "date": parsed.isoformat(),
                "generation": entry.name != parsed.isoformat(),
                "article_count": len(final_articles),
                "url": reverse("local-article-run", kwargs={"run_name": entry.name}),
            }
        )
    return sorted(runs, key=lambda item: str(item["name"]), reverse=True)


def _validated_run(
    run_name: str,
) -> tuple[Path, ArticleBundleWriter, dict[str, object]]:
    run_date = _parse_run_name(run_name)
    if run_date is None:
        raise Http404
    root = _article_root(required=True)
    assert root is not None
    writer = ArticleBundleWriter(root)
    try:
        run_root = writer.validate_run(run_date, run_directory=run_name)
        _confined_existing(root, run_name)
        manifest = _run_manifest(run_root)
    except (BundlePublishError, BundleValidationError, OSError, ValueError) as exc:
        raise Http404 from exc
    return run_root, writer, manifest


def _final_articles(
    run_root: Path,
    writer: ArticleBundleWriter,
    manifest: dict[str, object],
) -> dict[str, dict[str, object]]:
    articles = manifest.get("articles")
    if not isinstance(articles, dict):
        raise Http404
    final: dict[str, dict[str, object]] = {}
    for article_name in articles:
        if not isinstance(article_name, str) or _ARTICLE_NAME.fullmatch(article_name) is None:
            raise Http404
        article_root = _confined_existing(run_root, article_name)
        validation = writer._existing_hash(article_root)  # noqa: SLF001
        file_hashes = dict(validation.file_hashes)
        if "article.md" in file_hashes:
            final[article_name] = {"bundle_hash": validation.bundle_hash}
    return final


def _article_manifest_files(
    article_root: Path,
    writer: ArticleBundleWriter,
    run_name: str,
) -> dict[str, object]:
    validation = writer._existing_hash(article_root)  # noqa: SLF001
    manifest_payload = _read_validated_file(
        article_root / "manifest.json", max_bytes=_MAX_TEXT_BYTES
    )
    try:
        manifest = _parse_strict_json_object(manifest_payload, "preview article")
    except BundlePublishError as exc:
        raise Http404 from exc
    files = manifest.get("files")
    if manifest.get("bundle_hash") != validation.bundle_hash or not isinstance(files, dict):
        raise Http404
    _revalidate_run(writer, run_name)
    return files


def _run_manifest(run_root: Path) -> dict[str, object]:
    payload = _read_validated_file(run_root / "manifest.json", max_bytes=_MAX_TEXT_BYTES)
    try:
        return _parse_strict_json_object(payload, "preview run")
    except BundlePublishError as exc:
        raise Http404 from exc


def _read_validated_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        fingerprint = _path_fingerprint(path, "preview file", BundlePublishError)
        payload = _read_bound_file(
            path,
            fingerprint,
            max_bytes=max_bytes,
            label="preview file",
        )
        _require_unchanged_fingerprint(path, fingerprint, BundlePublishError)
    except (BundlePublishError, OSError, ValueError) as exc:
        raise Http404 from exc
    return payload


def _revalidate_run(writer: ArticleBundleWriter, run_name: str) -> None:
    run_date = _parse_run_name(run_name)
    if run_date is None:
        raise Http404
    try:
        writer.validate_run(run_date, run_directory=run_name)
    except (BundlePublishError, BundleValidationError, OSError, ValueError) as exc:
        raise Http404 from exc


def _article_root(*, required: bool) -> Path | None:
    configured = Path(settings.LOCAL_ARTICLE_ROOT)
    absolute = Path(os.path.abspath(configured))
    if not absolute.exists():
        if required:
            raise Http404
        return None
    try:
        resolved = absolute.resolve(strict=True)
        if os.path.normcase(str(resolved)) != os.path.normcase(str(absolute)):
            raise ValueError("article root resolves through a link")
        _reject_links_and_reparse(resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Http404 from exc
    return resolved


def _confined_existing(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    unsafe_part = any(part in {"", ".", ".."} for part in pure.parts)
    if pure.is_absolute() or "\\" in relative or unsafe_part:
        raise Http404
    candidate = root.joinpath(*pure.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        _reject_links_and_reparse(resolved, stop=root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Http404 from exc
    return resolved


def _reject_links_and_reparse(path: Path, *, stop: Path | None = None) -> None:
    current = path
    while True:
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
        ):
            raise ValueError("linked preview path")
        if stop is not None and current == stop:
            return
        if current.parent == current:
            return
        current = current.parent


def _parse_run_name(value: str) -> date | None:
    match = _RUN_NAME.fullmatch(value)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group("date"))
    except ValueError:
        return None


def _require_local_request(request: HttpRequest) -> None:
    if not settings.IS_LOCAL_RUNTIME:
        raise Http404
    remote = request.META.get("REMOTE_ADDR", "")
    try:
        address = ipaddress.ip_address(remote)
    except ValueError as exc:
        raise Http404 from exc
    if not address.is_loopback:
        raise Http404


def _humanizer_status() -> dict[str, str]:
    endpoint = "http://127.0.0.1:3210"
    if settings.HUMANIZER_BASE_URL != endpoint:
        return {"status": "misconfigured", "endpoint": endpoint}
    try:
        response = httpx.get(f"{endpoint}/api/health", timeout=0.5)
        payload = response.json()
        ready = response.status_code == 200 and payload.get("status") == "ready"
    except (httpx.HTTPError, ValueError, TypeError):
        ready = False
    return {"status": "ready" if ready else "unavailable", "endpoint": endpoint}


def _page_response(response: HttpResponse) -> HttpResponse:
    return _secure_response(response, cache_control="no-store")


def _secure_response(response: HttpResponse, *, cache_control: str) -> HttpResponse:
    response["Content-Security-Policy"] = _CSP
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = cache_control
    return response
