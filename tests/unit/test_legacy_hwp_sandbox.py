from __future__ import annotations

import hashlib
import json
import os
import runpy
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import yaml
from django.core.exceptions import ValidationError

os.environ.setdefault("WISDOME_ENVIRONMENT", "development")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wisdome_writer.settings")

import django

django.setup()

from adapters.extractors.base import ExtractorError
from adapters.extractors.legacy_hwp import LegacyHwpConverter, probe_legacy_hwp_sandbox
from apps.evidence import tasks
from apps.evidence.profiles import load_profile_documents


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_SCRIPT = REPOSITORY_ROOT / "deploy/containers/hwp-worker/wisdome-hwp-sandbox"
MANIFEST_SCRIPT = REPOSITORY_ROOT / "deploy/containers/hwp-worker/build_manifest.py"
MAGIC = b"WSHWP001"
PROTOCOL_VERSION = "wisdome-hwp-uds-v1"
MANIFEST_HASH = "1" * 64
HWP_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1test-hwp"
PDF_BYTES = b"%PDF-1.7\n% sandbox test\n%%EOF\n"


def _read_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise EOFError("peer closed the framed message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _receive_request(connection: socket.socket) -> dict[str, Any]:
    if _read_exact(connection, len(MAGIC)) != MAGIC:
        raise AssertionError("client sent the wrong protocol magic")
    header_length = struct.unpack("!I", _read_exact(connection, 4))[0]
    request = json.loads(_read_exact(connection, header_length).decode("utf-8"))
    if connection.recv(1) != b"":
        raise AssertionError("client did not half-close or sent trailing request bytes")
    return request


def _report_for(request: dict[str, Any], **changes: Any) -> dict[str, Any]:
    report = {
        "schema_version": "v1",
        "protocol_version": PROTOCOL_VERSION,
        "attempt_id": request["attempt_id"],
        "generation": request["generation"],
        "nonce": request["nonce"],
        "input_checksum_sha256": request["input"]["checksum_sha256"],
        "input_byte_size": request["input"]["byte_size"],
        "output_pdf_checksum_sha256": hashlib.sha256(PDF_BYTES).hexdigest(),
        "output_pdf_byte_size": len(PDF_BYTES),
        "page_count": 7,
        "pdf_mime": "application/pdf",
        "qpdf_validated": True,
        "converter_manifest_hash": MANIFEST_HASH,
        "sandbox_policy": {
            "network_allowed": False,
            "read_only_rootfs": True,
            "read_only_input": True,
            "private_tmpfs": True,
            "non_root": True,
            "resource_limits_enforced": True,
        },
        "warnings": [],
        "font_substitutions": [],
        "fallback_used": False,
        "partial_text_used": False,
        "stdout_evidence_used": False,
    }
    report.update(changes)
    return report


class _OneShotServer:
    def __init__(
        self,
        connection: socket.socket,
        responder: Callable[[socket.socket, dict[str, Any]], None],
    ) -> None:
        self._connection = connection
        self._connection.settimeout(5)
        self._responder = responder
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            with self._connection:
                request = _receive_request(self._connection)
                self._responder(self._connection, request)
        except BaseException as exc:  # surfaced in the owning test thread
            self.error = exc
        finally:
            self._connection.close()

    def finish(self) -> None:
        self.thread.join(timeout=6)
        if self.thread.is_alive():
            self._connection.close()
            self.thread.join(timeout=1)
            raise AssertionError("fake UDS server did not finish")
        if self.error is not None:
            raise self.error


def _send_response(
    connection: socket.socket,
    request: dict[str, Any],
    *,
    exit_code: int = 0,
    report: dict[str, Any] | None = None,
    raw_report: bytes | None = None,
    pdf: bytes = PDF_BYTES,
    header_changes: dict[str, Any] | None = None,
    trailing: bytes = b"",
) -> None:
    if exit_code:
        pdf = b""
        raw_report = b""
    elif raw_report is None:
        raw_report = json.dumps(
            report or _report_for(request),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    header = {
        "schema_version": "v1",
        "protocol_version": PROTOCOL_VERSION,
        "attempt_id": request["attempt_id"],
        "generation": request["generation"],
        "nonce": request["nonce"],
        "exit_code": exit_code,
        "report_bytes": len(raw_report or b""),
        "pdf_bytes": len(pdf),
    }
    header.update(header_changes or {})
    header_bytes = json.dumps(
        header,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    connection.sendall(
        MAGIC
        + struct.pack("!I", len(header_bytes))
        + header_bytes
        + (raw_report or b"")
        + pdf
        + trailing
    )


class LegacyHwpClientTests(unittest.TestCase):
    def _config(self, socket_path: Path, staging_root: Path) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "sandbox_socket_path": str(socket_path.resolve()),
            "input_staging_root": str(staging_root.resolve()),
            "converter_manifest_hash": MANIFEST_HASH,
            "golden_corpus_approved": True,
            "staging_owner_uid": 65532,
            "staging_owner_gid": 65532,
            "timeout_seconds": 3,
            "max_input_bytes": 1024,
            "max_output_bytes": 4096,
            "max_report_bytes": 4096,
            "max_pages": 100,
        }

    def _run_with_responder(
        self,
        responder: Callable[[socket.socket, dict[str, Any]], None],
    ):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            socket_path = root / "hwp.sock"
            staging_root = root / "staging"
            staging_root.mkdir()
            input_path = root / "sample.hwp"
            input_path.write_bytes(HWP_BYTES)
            client_socket, server_socket = socket.socketpair()
            server = _OneShotServer(server_socket, responder)
            try:
                output = LegacyHwpConverter(
                    self._config(socket_path, staging_root),
                    socket_factory=lambda _path, _timeout: client_socket,
                    staging_owner_setter=lambda _path, _uid, _gid: None,
                ).extract(
                    input_path,
                    attempt_id="11111111-1111-4111-8111-111111111111",
                    generation=1,
                )
                remaining = list(staging_root.iterdir())
                durable_bytes = Path(output.records[0].object_path).read_bytes()
            finally:
                client_socket.close()
                server.finish()
            return output, remaining, durable_bytes

    def test_valid_bounded_uds_result_produces_only_a_verified_pdf(self) -> None:
        """Removing identity/report checks must make a successful conversion fail this test."""
        output, remaining, durable_bytes = self._run_with_responder(
            lambda connection, request: _send_response(connection, request)
        )

        self.assertEqual(output.engine, "legacy_hwp_converter")
        self.assertEqual(len(output.records), 1)
        record = output.records[0]
        self.assertEqual(record.mime_type, "application/pdf")
        self.assertIsNone(record.text)
        self.assertEqual(record.structured_data["converted_page_count"], 7)
        self.assertEqual(
            record.structured_data["follow_up_engine_allowlist"],
            ["native_pdf", "paddleocr_ppstructurev3"],
        )
        self.assertEqual(durable_bytes, PDF_BYTES)
        self.assertEqual(record.structured_data["conversion_report"]["nonce"], record.locator["nonce"])
        self.assertEqual(record.locator["input_byte_size"], len(HWP_BYTES))
        self.assertEqual(record.locator["output_pdf_byte_size"], len(PDF_BYTES))
        report_bytes = json.dumps(
            record.structured_data["conversion_report"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(record.locator["sandbox_report_hash"], hashlib.sha256(report_bytes).hexdigest())
        self.assertFalse(output.metadata["partial_text_used"])
        self.assertEqual(remaining, [])

    def test_duplicate_unknown_and_bool_as_int_report_values_fail_closed(self) -> None:
        """A permissive JSON/report parser would accept ambiguous or type-confused provenance."""
        mutations: list[Callable[[dict[str, Any]], bytes | dict[str, Any]]] = [
            lambda request: json.dumps(_report_for(request) | {"unexpected": "value"}).encode(),
            lambda request: json.dumps(_report_for(request, page_count=True)).encode(),
            lambda request: json.dumps(_report_for(request)).replace(
                '"page_count": 7', '"page_count": 7, "page_count": 8'
            ).encode(),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                def responder(connection: socket.socket, request: dict[str, Any]) -> None:
                    raw = mutation(request)
                    _send_response(
                        connection,
                        request,
                        raw_report=(
                            raw
                            if isinstance(raw, bytes)
                            else json.dumps(raw).encode("utf-8")
                        ),
                    )

                with self.assertRaises(ExtractorError) as caught:
                    self._run_with_responder(responder)
                self.assertEqual(caught.exception.code, "legacy_hwp_report_invalid")
                self.assertFalse(caught.exception.retryable)

    def test_warning_manifest_mismatch_and_trailing_bytes_fail_closed(self) -> None:
        """Warnings, self-approved material, or data outside the frame must never be promoted."""
        responders = (
            lambda connection, request: _send_response(
                connection, request, report=_report_for(request, warnings=["missing font"])
            ),
            lambda connection, request: _send_response(
                connection,
                request,
                report=_report_for(request, converter_manifest_hash="2" * 64),
            ),
            lambda connection, request: _send_response(connection, request, trailing=b"extra"),
        )
        for index, responder in enumerate(responders):
            with self.subTest(index=index):
                with self.assertRaises(ExtractorError) as caught:
                    self._run_with_responder(responder)
                self.assertFalse(caught.exception.retryable)

    def test_only_socket_disconnect_is_retryable(self) -> None:
        """A daemon loss must be retried without making converter/report failures retryable."""
        def disconnect(connection: socket.socket, request: dict[str, Any]) -> None:
            del connection, request

        with self.assertRaises(ExtractorError) as caught:
            self._run_with_responder(disconnect)
        self.assertEqual(caught.exception.code, "legacy_hwp_sandbox_unavailable")
        self.assertTrue(caught.exception.retryable)

    def test_wrapper_exit_codes_are_terminal_and_preserved(self) -> None:
        """Collapsing exact wrapper outcomes would hide invalid/resource-limited inputs."""
        expected = {
            20: "legacy_hwp_input_invalid",
            21: "legacy_hwp_conversion_failed",
            22: "legacy_hwp_resource_limit",
        }
        for exit_code, error_code in expected.items():
            with self.subTest(exit_code=exit_code):
                with self.assertRaises(ExtractorError) as caught:
                    self._run_with_responder(
                        lambda connection, request, value=exit_code: _send_response(
                            connection, request, exit_code=value
                        )
                    )
                self.assertEqual(caught.exception.code, error_code)
                self.assertFalse(caught.exception.retryable)

    def test_profile_stays_inactive_before_golden_corpus_approval(self) -> None:
        """A deployment manifest alone must not activate the unaccepted HWP subset."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = self._config(root / "missing.sock", root / "staging")
            config["golden_corpus_approved"] = False
            with self.assertRaises(ExtractorError) as caught:
                LegacyHwpConverter(config)
        self.assertEqual(caught.exception.code, "legacy_hwp_profile_inactive")

    def test_staged_input_is_owned_by_the_supervisor_and_read_only(self) -> None:
        """A root-owned 0400 file is unreadable to the UID 65532 sidecar supervisor."""
        observed: list[tuple[str, int, int, int]] = []

        def set_owner(path: Path, uid: int, gid: int) -> None:
            observed.append((path.name, stat.S_IMODE(path.stat().st_mode), uid, gid))

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "sample.hwp"
            source.write_bytes(HWP_BYTES)
            staging = root / "staging"
            staging.mkdir()
            converter = LegacyHwpConverter(
                self._config(root / "unused.sock", staging),
                socket_factory=lambda _path, _timeout: socket.socketpair()[0],
                staging_owner_setter=set_owner,
            )
            staged = staging / "material.hwp"
            converter._prepare_staging_root()
            converter._stage_input(source, staged)

            expected_mode = 0o444 if os.name == "nt" else 0o400
            expected_directory_mode = 0o777 if os.name == "nt" else 0o700
            self.assertEqual(
                observed,
                [
                    ("staging", expected_directory_mode, 65532, 65532),
                    ("material.hwp", expected_mode, 65532, 65532),
                ],
            )

    def test_identity_probe_requires_manifest_policy_and_exact_eof(self) -> None:
        client, server = socket.socketpair()
        observed_error: list[BaseException] = []

        def respond() -> None:
            try:
                with server:
                    self.assertEqual(_read_exact(server, 8), b"WSHWPP01")
                    length = struct.unpack("!I", _read_exact(server, 4))[0]
                    request = json.loads(_read_exact(server, length))
                    self.assertEqual(server.recv(1), b"")
                    response = {
                        "schema_version": "v1",
                        "protocol_version": PROTOCOL_VERSION,
                        "nonce": request["nonce"],
                        "converter_manifest_hash": MANIFEST_HASH,
                        "sandbox_policy": {
                            "network_allowed": False,
                            "read_only_rootfs": True,
                            "read_only_input": True,
                            "private_tmpfs": True,
                            "non_root": True,
                            "resource_limits_enforced": True,
                        },
                        "ok": True,
                    }
                    raw = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
                    server.sendall(b"WSHWPP01" + struct.pack("!I", len(raw)) + raw)
            except BaseException as exc:
                observed_error.append(exc)

        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        probe_legacy_hwp_sandbox(
            "/unused.sock",
            MANIFEST_HASH,
            socket_factory=lambda _path, _timeout: client,
        )
        thread.join(timeout=5)
        self.assertEqual(observed_error, [])


class LegacyHwpProfileTests(unittest.TestCase):
    def _manifest(self) -> dict[str, Any]:
        roles = (
            "converter", "qpdf", "wrapper", "config", "font", "fontconfig", "runtime", "library"
        )
        return {
            "schema_version": "v1",
            "converter": {
                "name": "rhwp",
                "version": "0.8.2",
                "source_commit": "9b16aa9e23f476e2b335d7c029fc9f24a199d63c",
                "rust_version": "1.93.1",
                "cargo_locked": True,
            },
            "files": [
                {
                    "role": role,
                    "path": f"/opt/wisdome/{role}.bin",
                    "sha256": str(index + 1) * 64,
                    "byte_size": index + 1,
                }
                for index, role in enumerate(roles)
            ],
        }

    def _profile_root(self, root: Path, manifest_path: Path, expected_hash: str) -> Path:
        profiles = root / "profiles"
        profiles.mkdir()
        (profiles / "legacy.json").write_text(
            json.dumps(
                {
                    "profile_key": "legacy-hwp-test",
                    "profile_version": "1.1.0",
                    "engine": "legacy_hwp_converter",
                    "config": {
                        "converter_manifest_path": str(manifest_path.resolve()),
                        "converter_manifest_hash": expected_hash,
                    },
                }
            ),
            encoding="utf-8",
        )
        return profiles

    def test_profile_loader_preserves_and_compares_external_expected_hash(self) -> None:
        """Reintroducing runtime self-approval must accept the wrong hash and fail this test."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / "converter-manifest.json"
            manifest_path.write_text(json.dumps(self._manifest()), encoding="utf-8")
            profiles = self._profile_root(root, manifest_path, "f" * 64)
            with self.assertRaises(ValidationError):
                load_profile_documents(profiles)

    def test_profile_loader_accepts_exact_reviewed_manifest_bytes_without_overwriting_hash(self) -> None:
        """Changing any reviewed manifest byte must invalidate the external deployment digest."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / "converter-manifest.json"
            manifest_path.write_text(json.dumps(self._manifest()), encoding="utf-8")
            expected_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            profiles = self._profile_root(root, manifest_path, expected_hash)

            documents = load_profile_documents(profiles)

        self.assertEqual(documents[0]["config"]["converter_manifest_hash"], expected_hash)
        self.assertEqual(
            documents[0]["config"]["converter_manifest"]["converter"]["source_commit"],
            "9b16aa9e23f476e2b335d7c029fc9f24a199d63c",
        )


class LegacyHwpSandboxArtifactTests(unittest.TestCase):
    def test_manifest_builder_hashes_sorted_actual_bytes_for_every_required_role(self) -> None:
        """Omitting a converter dependency or hashing metadata instead of bytes must fail."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            entries = []
            for index, role in enumerate(
                (
                    "converter", "qpdf", "wrapper", "config", "font", "fontconfig", "runtime", "library"
                ), start=1
            ):
                path = root / f"{role}.bin"
                path.write_bytes(bytes([index]) * index)
                entries.extend(("--entry", f"{role}={path}"))
            output = root / "manifest.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(MANIFEST_SCRIPT),
                    "--output",
                    str(output),
                    "--rhwp-version",
                    "0.8.2",
                    "--rhwp-commit",
                    "9b16aa9e23f476e2b335d7c029fc9f24a199d63c",
                    "--rust-version",
                    "1.93.1",
                    *entries,
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], "v1")
        self.assertEqual(
            [entry["path"] for entry in manifest["files"]],
            sorted(entry["path"] for entry in manifest["files"]),
        )
        self.assertEqual({entry["role"] for entry in manifest["files"]}, {
            "converter", "qpdf", "wrapper", "config", "font", "fontconfig", "runtime", "library"
        })
        self.assertTrue(all(entry["byte_size"] > 0 for entry in manifest["files"]))

    def test_exact_wrapper_cli_rejects_invalid_input_with_exit_20_and_no_partial_output(self) -> None:
        """Invalid HWP bytes must never leave a report or a promotable partial PDF."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "invalid.hwp"
            output_path = root / "out.pdf"
            report_path = root / "report.json"
            input_path.write_bytes(b"not an HWP file")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SANDBOX_SCRIPT),
                    "--network=none",
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--report",
                    str(report_path),
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 20, completed.stderr)
        self.assertFalse(output_path.exists())
        self.assertFalse(report_path.exists())

    def test_sandbox_input_open_rejects_escape_hardlink_and_non_regular_files(self) -> None:
        """Path-only validation must not permit aliases or special filesystem objects."""
        namespace = runpy.run_path(str(SANDBOX_SCRIPT))
        validate = namespace["validate_relative_input"]
        open_verified = namespace["open_verified_input"]
        invalid = namespace["SandboxInvalidInput"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.hwp"
            source.write_bytes(HWP_BYTES)
            hardlink = root / "hardlink.hwp"
            os.link(source, hardlink)

            for value in ("../escape.hwp", "/absolute.hwp", "nested/input.hwp"):
                with self.subTest(value=value), self.assertRaises(invalid):
                    validate(value)
            with self.assertRaises(invalid):
                open_verified(root, hardlink.name, len(HWP_BYTES) + 1, "0" * 64)
            with self.assertRaises(invalid):
                open_verified(root, ".", 1, "0" * 64)

    def test_input_and_pdf_snapshots_stream_to_new_bounded_inodes(self) -> None:
        """Holding complete untrusted input/PDF bytes in memory crosses the sidecar budget."""
        namespace = runpy.run_path(str(SANDBOX_SCRIPT))
        snapshot_input = namespace["snapshot_verified_input"]
        snapshot_pdf = namespace["snapshot_untrusted_pdf"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_root = root / "input"
            input_root.mkdir()
            name = "11111111-1111-4111-8111-111111111111-1-" + "a" * 64 + ".hwp"
            source = input_root / name
            source.write_bytes(HWP_BYTES)
            private_input = root / "private.hwp"
            input_hash = hashlib.sha256(HWP_BYTES).hexdigest()
            observed_hash, observed_size = snapshot_input(
                input_root, name, len(HWP_BYTES), input_hash, private_input
            )
            untrusted_pdf = root / "untrusted.pdf"
            untrusted_pdf.write_bytes(PDF_BYTES)
            trusted_pdf = root / "trusted.pdf"
            snapshot = snapshot_pdf(untrusted_pdf, trusted_pdf, 4096)

            self.assertEqual((observed_hash, observed_size), (input_hash, len(HWP_BYTES)))
            self.assertEqual(private_input.read_bytes(), HWP_BYTES)
            self.assertNotEqual(untrusted_pdf.stat().st_ino, trusted_pdf.stat().st_ino)
            self.assertEqual(snapshot["sha256"], hashlib.sha256(PDF_BYTES).hexdigest())
            self.assertEqual(snapshot["byte_size"], len(PDF_BYTES))
            self.assertEqual(stat.S_IMODE(trusted_pdf.stat().st_mode) & 0o222, 0)

    def test_response_streams_pdf_chunks_instead_of_concatenating_the_frame(self) -> None:
        """One report+PDF sendall allocation would duplicate the entire converted document."""
        namespace = runpy.run_path(str(SANDBOX_SCRIPT))
        send_response = namespace["_send_response"]

        class BoundedConnection:
            def __init__(self) -> None:
                self.lengths: list[int] = []

            def sendall(self, value: bytes) -> None:
                self.lengths.append(len(value))
                if len(value) > 64 * 1024:
                    raise AssertionError("response chunk exceeded the streaming bound")

        request = {
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "generation": 1,
            "nonce": "a" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            pdf_path = Path(temporary_directory) / "large.pdf"
            pdf_path.write_bytes(b"%PDF-" + b"x" * (2 * 1024 * 1024))
            connection = BoundedConnection()
            send_response(
                connection,
                request,
                exit_code=0,
                report=b"{}",
                pdf_path=pdf_path,
                pdf_size=pdf_path.stat().st_size,
            )

        self.assertGreater(len(connection.lengths), 3)
        self.assertLessEqual(max(connection.lengths), 64 * 1024)

    def test_log_limit_terminates_the_job_before_wall_clock_timeout(self) -> None:
        """A post-exit stat check cannot stop an output-flooding child promptly."""
        namespace = runpy.run_path(str(SANDBOX_SCRIPT))
        run_bounded = namespace["_run_bounded"]
        resource_limit = namespace["SandboxResourceLimit"]
        limits = {
            "cpu_seconds": 20,
            "address_space_bytes": 256 * 1024 * 1024,
            "file_bytes": 16 * 1024 * 1024,
            "open_files": 64,
            "processes": 16,
            "log_bytes": 1024,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            started = time.monotonic()
            with self.assertRaises(resource_limit):
                run_bounded(
                    [
                        sys.executable,
                        "-c",
                        "import sys,time; sys.stdout.write('x'*4096); sys.stdout.flush(); time.sleep(8)",
                    ],
                    cwd=Path(temporary_directory),
                    environment=os.environ,
                    timeout_seconds=10,
                    limits=limits,
                    process_group=True,
                )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)

    def test_compose_declares_a_credential_free_networkless_bounded_sidecar(self) -> None:
        """Removing any container isolation boundary must be visible in parsed Compose config."""
        compose = yaml.safe_load((REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8"))
        service = compose["services"]["hwp-converter"]

        self.assertEqual(service["network_mode"], "none")
        self.assertTrue(service["read_only"])
        self.assertEqual(service["user"], "65532:65532")
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", service["security_opt"])
        self.assertGreater(service["pids_limit"], 0)
        self.assertEqual(service["cap_add"], ["SETUID", "SETGID"])
        self.assertEqual(service["memswap_limit"], service["mem_limit"])
        self.assertEqual(service["environment"]["HWP_CHILD_UID"], "65533")
        self.assertEqual(service["environment"]["HWP_VALIDATOR_UID"], "65534")
        self.assertIn("/work", service["tmpfs"][0])
        serialized_environment = json.dumps(service.get("environment", {})).lower()
        for forbidden in ("database", "redis", "broker", "aws", "minio", "secret", "password"):
            self.assertNotIn(forbidden, serialized_environment)
        self.assertEqual(
            compose["services"]["worker-extract"]["command"][-1],
            "--concurrency=1",
        )
        profile_admin = compose["services"]["profile-admin"]
        self.assertIn("paddle-models:/models/paddleocr:ro", profile_admin["volumes"])
        self.assertIn("hwp-sandbox-socket:/run/wisdome-hwp:ro", profile_admin["volumes"])
        self.assertIn("HWP_CONVERTER_MANIFEST_SHA256", profile_admin["environment"])
        app_dockerfile = (
            REPOSITORY_ROOT / "deploy/containers/app/Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("COPY deploy/containers/hwp-worker ./deploy/containers/hwp-worker", app_dockerfile)

    def test_supervisor_drops_child_identity_and_validates_only_a_trusted_snapshot(self) -> None:
        source = SANDBOX_SCRIPT.read_text(encoding="utf-8")
        for boundary in (
            "os.setgroups([])",
            "os.setgid(gid)",
            "os.setuid(uid)",
            "os.umask(child_umask)",
            "untrusted.chmod(0o2770)",
            "child_umask=0o027",
            "snapshot_via_identity(\n            output,\n            trusted_output",
            '"snapshot",',
            "uid=validator_uid",
            "pass_fds=pass_fds",
        ):
            self.assertIn(boundary, source)
        self.assertLess(
            source.index("snapshot_via_identity(\n            output,\n            trusted_output"),
            source.index("uid=validator_uid"),
        )


class LegacyHwpPageCountTests(unittest.TestCase):
    @staticmethod
    def _bound_record() -> dict[str, Any]:
        request = {
            "attempt_id": "11111111-1111-4111-8111-111111111111",
            "generation": 1,
            "nonce": "a" * 64,
            "input": {
                "checksum_sha256": hashlib.sha256(HWP_BYTES).hexdigest(),
                "byte_size": len(HWP_BYTES),
            },
        }
        report = _report_for(request)
        report_bytes = json.dumps(
            report, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return {
            "locator": {
                "attempt_id": report["attempt_id"],
                "generation": report["generation"],
                "nonce": report["nonce"],
                "input_checksum": report["input_checksum_sha256"],
                "input_byte_size": report["input_byte_size"],
                "output_pdf_checksum": report["output_pdf_checksum_sha256"],
                "output_pdf_byte_size": report["output_pdf_byte_size"],
                "converter_manifest_hash": report["converter_manifest_hash"],
                "sandbox_report_hash": hashlib.sha256(report_bytes).hexdigest(),
                "page_count": report["page_count"],
            },
            "structured_data": {
                "converted_page_count": report["page_count"],
                "sandbox_policy": report["sandbox_policy"],
                "follow_up_engine_allowlist": ["native_pdf", "paddleocr_ppstructurev3"],
                "qpdf_validated": True,
                "warnings": [],
                "font_substitutions": [],
                "fallback_used": False,
                "conversion_report": report,
            },
        }

    def test_verified_conversion_page_count_drives_follow_up_document(self) -> None:
        """Falling back to one page would silently omit converted HWP pages."""
        from apps.evidence import tasks

        structured = {"records": [self._bound_record()]}
        self.assertEqual(tasks._verified_legacy_hwp_page_count(structured), 7)
        with self.assertRaises(ExtractorError):
            tasks._verified_legacy_hwp_page_count(
                {"records": [{"structured_data": {"converted_page_count": True}}]}
            )

    def test_pdf_upload_material_is_bound_to_locator_and_exact_report(self) -> None:
        """Changing the PDF after adapter validation must prevent storage and evidence creation."""
        record = self._bound_record()
        self.assertEqual(tasks._verified_legacy_hwp_record_material(record, PDF_BYTES), 7)
        with self.assertRaises(ExtractorError):
            tasks._verified_legacy_hwp_record_material(record, PDF_BYTES + b"tamper")
        report_tamper = json.loads(json.dumps(record))
        report_tamper["structured_data"]["conversion_report"]["unknown"] = "field"
        tampered_report = json.dumps(
            report_tamper["structured_data"]["conversion_report"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        report_tamper["locator"]["sandbox_report_hash"] = hashlib.sha256(
            tampered_report
        ).hexdigest()
        with self.assertRaises(ExtractorError):
            tasks._verified_legacy_hwp_record_material(report_tamper, PDF_BYTES)

    def test_reused_document_requires_complete_page_and_object_identity(self) -> None:
        """A checksum-only match must not reuse a one-page or wrong-object projection."""
        document = SimpleNamespace(
            run_source_item_id="run-source",
            source_item_id="source",
            input_asset_id="asset",
            input_object_key="key",
            input_object_version="version",
            input_kind="pdf",
            input_mime_type="application/pdf",
            input_frame_count=None,
            input_checksum="a" * 64,
            input_page_count=7,
            expected_page_indices=list(range(7)),
        )
        kwargs = {
            "run_source_item_id": "run-source",
            "source_item_id": "source",
            "input_asset_id": "asset",
            "input_object_key": "key",
            "input_object_version": "version",
            "input_kind": "pdf",
            "input_mime_type": "application/pdf",
            "input_frame_count": None,
            "input_checksum": "a" * 64,
            "input_page_count": 7,
            "expected_page_indices": list(range(7)),
        }
        tasks._validate_reused_document_identity(document, **kwargs)
        document.input_page_count = 1
        with self.assertRaises(Exception):
            tasks._validate_reused_document_identity(document, **kwargs)

    def test_only_legacy_hwp_attachment_failure_sets_required_manual_marker(self) -> None:
        """Optional attachments stay partial while a required legacy HWP blocks run.evidence_ready."""
        self.assertTrue(tasks._is_required_legacy_hwp_attachment({"filename": "required.HWP"}))
        self.assertFalse(tasks._is_required_legacy_hwp_attachment({"filename": "optional.pdf"}))
        marker = tasks._legacy_hwp_failure_marker(
            [{"requiredLegacyHwp": True, "code": "profile_not_approved"}]
        )
        self.assertEqual(
            marker,
            {"legacyHwpRequiredFailures": 1, "legacyHwpRequiredFailureCodes": ["profile_not_approved"]},
        )


if __name__ == "__main__":
    unittest.main()
