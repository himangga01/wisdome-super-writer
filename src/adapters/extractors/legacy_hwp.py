from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import struct
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Mapping

from adapters.extractors.base import (
    ExtractorError,
    GenericEvidenceRecord,
    GenericExtractionOutput,
    sha256_file,
)


PROTOCOL_MAGIC = b"WSHWP001"
PROBE_MAGIC = b"WSHWPP01"
PROTOCOL_VERSION = "wisdome-hwp-uds-v1"
MAX_HEADER_BYTES = 64 * 1024
STREAM_MARGIN_SECONDS = 10.0
_RESPONSE_FIELDS = frozenset(
    {
        "schema_version",
        "protocol_version",
        "attempt_id",
        "generation",
        "nonce",
        "exit_code",
        "report_bytes",
        "pdf_bytes",
    }
)
_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "protocol_version",
        "attempt_id",
        "generation",
        "nonce",
        "input_checksum_sha256",
        "input_byte_size",
        "output_pdf_checksum_sha256",
        "output_pdf_byte_size",
        "page_count",
        "pdf_mime",
        "qpdf_validated",
        "converter_manifest_hash",
        "sandbox_policy",
        "warnings",
        "font_substitutions",
        "fallback_used",
        "partial_text_used",
        "stdout_evidence_used",
    }
)
_POLICY = {
    "network_allowed": False,
    "read_only_rootfs": True,
    "read_only_input": True,
    "private_tmpfs": True,
    "non_root": True,
    "resource_limits_enforced": True,
}
_EXIT_ERRORS = {
    20: ("legacy_hwp_input_invalid", "Legacy HWP input is invalid or unsupported"),
    21: ("legacy_hwp_conversion_failed", "Sandboxed HWP conversion failed"),
    22: ("legacy_hwp_resource_limit", "Legacy HWP conversion exceeded a resource limit"),
}
_PROBE_RESPONSE_FIELDS = frozenset(
    {"schema_version", "protocol_version", "nonce", "converter_manifest_hash", "sandbox_policy", "ok"}
)


class _SocketDisconnected(Exception):
    pass


class _ProtocolViolation(Exception):
    pass


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) != {"0"}
        and all(character in "0123456789abcdef" for character in value)
    )


def _strict_json_object(raw: bytes, *, fields: frozenset[str]) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise _ProtocolViolation(f"non-finite JSON value: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _ProtocolViolation("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ProtocolViolation("invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != fields:
        raise _ProtocolViolation("JSON object has missing or unknown fields")
    return value


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _SocketDisconnected("sandbox response deadline expired")
    return remaining


def _read_exact(
    connection: socket.socket,
    length: int,
    *,
    deadline: float | None = None,
) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        try:
            if deadline is not None:
                connection.settimeout(_remaining_timeout(deadline))
            chunk = connection.recv(remaining)
        except (OSError, TimeoutError, socket.timeout) as exc:
            raise _SocketDisconnected("sandbox socket read failed") from exc
        if not chunk:
            raise _SocketDisconnected("sandbox disconnected during a framed response")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _positive_int(value: object, *, maximum: int | None = None) -> bool:
    return type(value) is int and value > 0 and (maximum is None or value <= maximum)


def _bounded_int(config: Mapping[str, Any], key: str, default: int, maximum: int) -> int:
    value = config.get(key, default)
    if not _positive_int(value, maximum=maximum):
        raise ExtractorError("legacy_hwp_profile_invalid", f"{key} must be a bounded positive integer")
    return int(value)


def _bounded_uid(config: Mapping[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    if type(value) is not int or value < 0 or value > 2**31 - 1:
        raise ExtractorError(
            "legacy_hwp_profile_invalid",
            f"{key} must be a bounded non-negative integer",
        )
    return value


def legacy_hwp_acceptance_reference_is_well_formed(
    config: Mapping[str, Any],
) -> bool:
    """Validate reference shape only; this is never admission evidence."""
    approved = config.get("golden_corpus_approved")
    acceptance = config.get("golden_corpus_acceptance")
    if approved is not True:
        return False
    expected_manifest = config.get("converter_manifest_hash")
    expected_fields = {
        "schema_version",
        "object_key",
        "object_version",
        "sha256",
        "target_oci_image_digest",
        "converter_manifest_hash",
        "all_pass",
    }
    return bool(
        isinstance(acceptance, Mapping)
        and set(acceptance) == expected_fields
        and acceptance.get("schema_version") == "legacy-hwp-golden-acceptance-v1"
        and isinstance(acceptance.get("object_key"), str)
        and bool(acceptance.get("object_key"))
        and isinstance(acceptance.get("object_version"), str)
        and bool(acceptance.get("object_version"))
        and _is_sha256(acceptance.get("sha256"))
        and acceptance.get("target_oci_image_digest", "").startswith("sha256:")
        and _is_sha256(str(acceptance.get("target_oci_image_digest", ""))[7:])
        and acceptance.get("converter_manifest_hash") == expected_manifest
        and _is_sha256(expected_manifest)
        and acceptance.get("all_pass") is True
    )


def validate_legacy_hwp_activation_config(config: Mapping[str, Any]) -> bool:
    """Fail closed until T032 implements signed artifact-byte verification."""
    if not legacy_hwp_acceptance_reference_is_well_formed(config):
        return False
    # Reference metadata and runtime self-report are attacker-controlled inputs. T032 must
    # fetch the exact versioned bytes, validate their hash/schema/subject/results/OCI/manifest,
    # and verify a release signature against an external trust root before this can return true.
    return False


def _default_socket_factory(path: str, timeout: float) -> socket.socket:
    if not hasattr(socket, "AF_UNIX"):
        raise OSError("Unix-domain sockets are unavailable on this platform")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(path)
    except BaseException:
        connection.close()
        raise
    return connection


def _default_staging_owner_setter(path: Path, uid: int, gid: int) -> None:
    """Transfer a staged input to the credential-free sidecar supervisor."""
    if not hasattr(os, "chown"):
        raise OSError("staging ownership transfer is unavailable on this platform")
    os.chown(path, uid, gid)


def probe_legacy_hwp_sandbox(
    socket_path: str,
    converter_manifest_hash: str,
    *,
    timeout_seconds: float = 2.0,
    socket_factory: Callable[[str, float], socket.socket] | None = None,
) -> None:
    """Perform an identity-bearing bounded probe; a connect-only check is insufficient."""
    if not _is_sha256(converter_manifest_hash):
        raise ExtractorError("legacy_hwp_converter_unavailable", "Probe manifest hash is invalid")
    nonce = secrets.token_hex(32)
    request = {
        "schema_version": "v1",
        "protocol_version": PROTOCOL_VERSION,
        "nonce": nonce,
        "converter_manifest_hash": converter_manifest_hash,
    }
    raw = json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    factory = socket_factory or _default_socket_factory
    try:
        connection = factory(socket_path, timeout_seconds)
        connection.settimeout(timeout_seconds)
        with connection:
            connection.sendall(PROBE_MAGIC + struct.pack("!I", len(raw)) + raw)
            connection.shutdown(socket.SHUT_WR)
            if _read_exact(connection, len(PROBE_MAGIC)) != PROBE_MAGIC:
                raise _ProtocolViolation("wrong probe response magic")
            length = struct.unpack("!I", _read_exact(connection, 4))[0]
            if not 0 < length <= MAX_HEADER_BYTES:
                raise _ProtocolViolation("probe response length is outside policy")
            response = _strict_json_object(
                _read_exact(connection, length), fields=_PROBE_RESPONSE_FIELDS
            )
            if (
                response.get("schema_version") != "v1"
                or response.get("protocol_version") != PROTOCOL_VERSION
                or response.get("nonce") != nonce
                or response.get("converter_manifest_hash") != converter_manifest_hash
                or response.get("sandbox_policy") != _POLICY
                or response.get("ok") is not True
                or connection.recv(1) != b""
            ):
                raise _ProtocolViolation("probe response identity mismatch")
    except (OSError, TimeoutError, socket.timeout, _SocketDisconnected, _ProtocolViolation) as exc:
        raise ExtractorError(
            "legacy_hwp_converter_unavailable",
            "Legacy HWP sandbox identity probe failed",
        ) from exc


class LegacyHwpConverter:
    """Synchronous client for the credential-free, no-network HWP sidecar."""

    engine = "legacy_hwp_converter"

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        socket_factory: Callable[[str, float], socket.socket] | None = None,
        staging_owner_setter: Callable[[Path, int, int], None] | None = None,
    ) -> None:
        self.config = dict(config)
        if not validate_legacy_hwp_activation_config(self.config):
            raise ExtractorError(
                "legacy_hwp_profile_inactive",
                "Legacy HWP remains inactive until the T032 golden corpus is approved",
            )
        self.protocol_version = str(self.config.get("protocol_version", ""))
        self.socket_path = str(self.config.get("sandbox_socket_path", ""))
        self.input_staging_root = Path(str(self.config.get("input_staging_root", "")))
        self.converter_manifest_hash = str(self.config.get("converter_manifest_hash", ""))
        self.timeout_seconds = _bounded_int(self.config, "timeout_seconds", 180, 600)
        self.max_input_bytes = _bounded_int(
            self.config, "max_input_bytes", 128 * 1024 * 1024, 512 * 1024 * 1024
        )
        self.max_output_bytes = _bounded_int(
            self.config, "max_output_bytes", 300 * 1024 * 1024, 512 * 1024 * 1024
        )
        self.max_report_bytes = _bounded_int(self.config, "max_report_bytes", 64 * 1024, 1024 * 1024)
        self.max_pages = _bounded_int(self.config, "max_pages", 2000, 10000)
        self.staging_owner_uid = _bounded_uid(self.config, "staging_owner_uid", 0)
        self.staging_owner_gid = _bounded_uid(self.config, "staging_owner_gid", 0)
        self._socket_factory = socket_factory or _default_socket_factory
        self._staging_owner_setter = staging_owner_setter or _default_staging_owner_setter
        if (
            self.protocol_version != PROTOCOL_VERSION
            or not self.socket_path
            or "$" in self.socket_path
            or "%" in self.socket_path
            or not Path(self.socket_path).is_absolute()
            or not self.input_staging_root.is_absolute()
            or not _is_sha256(self.converter_manifest_hash)
        ):
            raise ExtractorError(
                "legacy_hwp_converter_unavailable",
                "Approved UDS path, staging root, protocol, and converter manifest are required",
            )

    def extract(
        self,
        path: Path,
        *,
        attempt_id: str,
        generation: int = 1,
    ) -> GenericExtractionOutput:
        try:
            normalized_attempt_id = str(uuid.UUID(str(attempt_id)))
        except (ValueError, AttributeError) as exc:
            raise ExtractorError("legacy_hwp_request_invalid", "Attempt ID must be a UUID") from exc
        if normalized_attempt_id != str(attempt_id) or not _positive_int(generation):
            raise ExtractorError(
                "legacy_hwp_request_invalid", "Attempt identity and generation must be canonical"
            )
        if not path.is_file() or path.is_symlink():
            raise ExtractorError("legacy_hwp_input_invalid", "Legacy HWP input is not a regular file")
        if path.stat().st_size < 1 or path.stat().st_size > self.max_input_bytes:
            raise ExtractorError("legacy_hwp_input_invalid", "Legacy HWP input size is outside policy")

        nonce = secrets.token_hex(32)
        self._prepare_staging_root()
        staged_name = f"{normalized_attempt_id}-{generation}-{nonce}.hwp"
        staged_path = self.input_staging_root / staged_name
        output_temp: Path | None = None
        try:
            input_checksum, input_size = self._stage_input(path, staged_path)
            request = {
                "schema_version": "v1",
                "protocol_version": self.protocol_version,
                "attempt_id": normalized_attempt_id,
                "generation": generation,
                "nonce": nonce,
                "converter_manifest_hash": self.converter_manifest_hash,
                "input": {
                    "relative_path": staged_name,
                    "checksum_sha256": input_checksum,
                    "byte_size": input_size,
                },
                "limits": {
                    "timeout_seconds": self.timeout_seconds,
                    "max_input_bytes": self.max_input_bytes,
                    "max_output_bytes": self.max_output_bytes,
                    "max_report_bytes": self.max_report_bytes,
                    "max_pages": self.max_pages,
                },
            }
            output_temp, report_bytes, output_checksum, output_size = self._request_conversion(
                request, path
            )
            report = self._validate_report(
                report_bytes,
                request=request,
                output_path=output_temp,
                output_checksum=output_checksum,
                output_size=output_size,
            )
            report_hash = hashlib.sha256(report_bytes).hexdigest()
            durable_output = Path(str(path) + f".{output_checksum}.converted.pdf")
            if durable_output.exists():
                if (
                    not durable_output.is_file()
                    or durable_output.is_symlink()
                    or durable_output.stat().st_size != output_size
                    or sha256_file(durable_output) != output_checksum
                ):
                    raise ExtractorError(
                        "legacy_hwp_output_invalid", "Existing converted PDF differs from verified output"
                    )
                output_temp.unlink()
            else:
                os.replace(output_temp, durable_output)
            output_temp = None
            record = GenericEvidenceRecord(
                kind="attachment",
                locator_type="hwp_conversion",
                locator={
                    "locator_type": "hwp_conversion",
                    "attempt_id": normalized_attempt_id,
                    "generation": generation,
                    "nonce": nonce,
                    "input_checksum": input_checksum,
                    "input_byte_size": input_size,
                    "output_pdf_checksum": output_checksum,
                    "output_pdf_byte_size": output_size,
                    "converter_manifest_hash": self.converter_manifest_hash,
                    "sandbox_report_hash": report_hash,
                    "page_count": report["page_count"],
                },
                object_path=str(durable_output),
                mime_type="application/pdf",
                structured_data={
                    "converted_page_count": report["page_count"],
                    "sandbox_policy": dict(report["sandbox_policy"]),
                    "follow_up_engine_allowlist": ["native_pdf", "paddleocr_ppstructurev3"],
                    "qpdf_validated": True,
                    "warnings": [],
                    "font_substitutions": [],
                    "fallback_used": False,
                    "conversion_report": report,
                },
            )
            return GenericExtractionOutput(
                engine=self.engine,
                extractor_version=str(self.config.get("extractor_version", "legacy-hwp-v1.1.0")),
                validation_mode="deterministic",
                records=[record],
                metadata={
                    "partial_text_used": False,
                    "stdout_evidence_used": False,
                    "network_allowed": False,
                    "generation_boundary": "t015-static-1",
                },
            )
        finally:
            if output_temp is not None:
                output_temp.unlink(missing_ok=True)
            if staged_path.exists():
                try:
                    staged_path.chmod(0o600)
                except OSError:
                    pass
                staged_path.unlink(missing_ok=True)

    def _stage_input(self, source: Path, destination: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as reader, destination.open("xb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > self.max_input_bytes:
                        raise ExtractorError(
                            "legacy_hwp_input_invalid", "Legacy HWP input exceeds its byte limit"
                        )
                    digest.update(chunk)
                    writer.write(chunk)
            destination.chmod(0o400)
            self._staging_owner_setter(
                destination,
                self.staging_owner_uid,
                self.staging_owner_gid,
            )
        except FileExistsError as exc:
            raise ExtractorError("legacy_hwp_request_invalid", "Staging identity already exists") from exc
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise ExtractorError(
                "legacy_hwp_converter_unavailable",
                "Staged HWP ownership could not be transferred to the sandbox supervisor",
            ) from exc
        return digest.hexdigest(), size

    def _prepare_staging_root(self) -> None:
        try:
            self.input_staging_root.mkdir(parents=True, exist_ok=True)
            if self.input_staging_root.is_symlink() or not self.input_staging_root.is_dir():
                raise OSError("staging root is not a real directory")
            self.input_staging_root.chmod(0o700)
            self._staging_owner_setter(
                self.input_staging_root,
                self.staging_owner_uid,
                self.staging_owner_gid,
            )
        except OSError as exc:
            raise ExtractorError(
                "legacy_hwp_converter_unavailable",
                "HWP staging root cannot be assigned to the sandbox supervisor",
            ) from exc

    def _request_conversion(
        self,
        request: dict[str, Any],
        source_path: Path,
    ) -> tuple[Path, bytes, str, int]:
        request_bytes = json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(request_bytes) > MAX_HEADER_BYTES:
            raise ExtractorError("legacy_hwp_request_invalid", "Sandbox request header is too large")
        output_handle = tempfile.NamedTemporaryFile(
            prefix="wisdome-hwp-result-", suffix=".pdf", delete=False, dir=source_path.parent
        )
        output_path = Path(output_handle.name)
        output_handle.close()
        try:
            try:
                socket_budget = float(self.timeout_seconds) + STREAM_MARGIN_SECONDS
                deadline = time.monotonic() + socket_budget
                connection = self._socket_factory(self.socket_path, socket_budget)
                connection.settimeout(_remaining_timeout(deadline))
                with connection:
                    connection.sendall(PROTOCOL_MAGIC + struct.pack("!I", len(request_bytes)) + request_bytes)
                    connection.shutdown(socket.SHUT_WR)
                    response = self._receive_header(connection, request, deadline=deadline)
                    exit_code = response["exit_code"]
                    if exit_code != 0:
                        self._require_end_of_stream(connection, deadline=deadline)
                        error = _EXIT_ERRORS.get(exit_code)
                        if error is None:
                            raise _ProtocolViolation("unknown sandbox exit code")
                        raise ExtractorError(error[0], error[1])
                    report_bytes = _read_exact(
                        connection,
                        response["report_bytes"],
                        deadline=deadline,
                    )
                    digest = hashlib.sha256()
                    remaining = response["pdf_bytes"]
                    with output_path.open("wb") as output:
                        while remaining:
                            chunk = _read_exact(
                                connection,
                                min(1024 * 1024, remaining),
                                deadline=deadline,
                            )
                            digest.update(chunk)
                            output.write(chunk)
                            remaining -= len(chunk)
                    self._require_end_of_stream(connection, deadline=deadline)
                return output_path, report_bytes, digest.hexdigest(), response["pdf_bytes"]
            except ExtractorError:
                raise
            except _SocketDisconnected as exc:
                raise ExtractorError(
                    "legacy_hwp_sandbox_unavailable",
                    "Legacy HWP sandbox disconnected",
                    retryable=True,
                ) from exc
            except _ProtocolViolation as exc:
                raise ExtractorError(
                    "legacy_hwp_report_invalid", "Sandbox response violated its framing contract"
                ) from exc
            except (OSError, TimeoutError, socket.timeout) as exc:
                raise ExtractorError(
                    "legacy_hwp_sandbox_unavailable",
                    "Legacy HWP sandbox socket is unavailable",
                    retryable=True,
                ) from exc
        except BaseException:
            output_path.unlink(missing_ok=True)
            raise

    def _receive_header(
        self,
        connection: socket.socket,
        request: Mapping[str, Any],
        *,
        deadline: float,
    ) -> dict[str, Any]:
        if _read_exact(connection, len(PROTOCOL_MAGIC), deadline=deadline) != PROTOCOL_MAGIC:
            raise _ProtocolViolation("wrong response magic")
        header_length = struct.unpack("!I", _read_exact(connection, 4, deadline=deadline))[0]
        if not 0 < header_length <= MAX_HEADER_BYTES:
            raise _ProtocolViolation("response header length is outside policy")
        response = _strict_json_object(
            _read_exact(connection, header_length, deadline=deadline),
            fields=_RESPONSE_FIELDS,
        )
        identity = ("protocol_version", "attempt_id", "generation", "nonce")
        if response.get("schema_version") != "v1" or any(
            response.get(field) != request.get(field) for field in identity
        ):
            raise _ProtocolViolation("response identity mismatch")
        if type(response.get("exit_code")) is not int or response["exit_code"] not in {0, 20, 21, 22}:
            raise _ProtocolViolation("invalid sandbox exit code")
        if type(response.get("report_bytes")) is not int or type(response.get("pdf_bytes")) is not int:
            raise _ProtocolViolation("response lengths must be integers")
        if response["exit_code"] == 0:
            if not (
                0 < response["report_bytes"] <= self.max_report_bytes
                and 0 < response["pdf_bytes"] <= self.max_output_bytes
            ):
                raise _ProtocolViolation("successful response lengths are outside policy")
        elif response["report_bytes"] != 0 or response["pdf_bytes"] != 0:
            raise _ProtocolViolation("failed response contains partial output")
        return response

    @staticmethod
    def _require_end_of_stream(connection: socket.socket, *, deadline: float) -> None:
        try:
            connection.settimeout(_remaining_timeout(deadline))
            trailing = connection.recv(1)
        except (OSError, TimeoutError, socket.timeout) as exc:
            raise _ProtocolViolation("sandbox did not close the exact response frame") from exc
        if trailing:
            raise _ProtocolViolation("sandbox response has trailing bytes")

    def _validate_report(
        self,
        report_bytes: bytes,
        *,
        request: Mapping[str, Any],
        output_path: Path,
        output_checksum: str,
        output_size: int,
    ) -> dict[str, Any]:
        try:
            report = _strict_json_object(report_bytes, fields=_REPORT_FIELDS)
            if report_bytes != json.dumps(
                report,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"):
                raise _ProtocolViolation("conversion report is not the exact canonical frame")
            identity = ("protocol_version", "attempt_id", "generation", "nonce")
            if report.get("schema_version") != "v1" or any(
                report.get(field) != request.get(field) for field in identity
            ):
                raise _ProtocolViolation("report identity mismatch")
            request_input = request["input"]
            if (
                report.get("input_checksum_sha256") != request_input["checksum_sha256"]
                or report.get("input_byte_size") != request_input["byte_size"]
                or type(report.get("input_byte_size")) is not int
                or report.get("output_pdf_checksum_sha256") != output_checksum
                or report.get("output_pdf_byte_size") != output_size
                or type(report.get("output_pdf_byte_size")) is not int
                or report.get("converter_manifest_hash") != self.converter_manifest_hash
                or report.get("pdf_mime") != "application/pdf"
                or report.get("qpdf_validated") is not True
                or report.get("sandbox_policy") != _POLICY
                or report.get("warnings") != []
                or report.get("font_substitutions") != []
                or report.get("fallback_used") is not False
                or report.get("partial_text_used") is not False
                or report.get("stdout_evidence_used") is not False
                or not _positive_int(report.get("page_count"), maximum=self.max_pages)
                or output_size < 1
                or output_size > self.max_output_bytes
                or output_path.stat().st_size != output_size
            ):
                raise _ProtocolViolation("conversion report material mismatch")
            with output_path.open("rb") as output:
                if output.read(5) != b"%PDF-":
                    raise _ProtocolViolation("converted output is not a PDF")
        except (OSError, _ProtocolViolation) as exc:
            raise ExtractorError(
                "legacy_hwp_report_invalid", "Conversion report or PDF did not match the request"
            ) from exc
        return report
