# Round 1 review A: security, privacy, and Windows local lifecycle

- Review date: 2026-10-03, Asia/Seoul.
- Baseline: `main`, `7eb33310665dfffafa15561d66d6d1a2d4b48e6b`.
- Initial working tree: modified `README.md`; untracked `AGENTS.md`, `CLAUDE.md`, `docs/code-analysis.md`, and `docs/service-analysis.md` from the preceding analysis. These files were preserved.
- Authority read first: project `AGENTS.md`, `C:/Users/강지혜/.agents/analysis-policy.md`, [code analysis](../code-analysis.md), and [service analysis](../service-analysis.md).
- Scope: the assigned common runtime/API/infrastructure, accounts, audit/retention, local-content workflow, storage/publishers, shared source HTTP adapters, Windows and acceptance scripts, deployment/configuration, local preview assets, and related tests. This is reviewer A's report for the parent-led full review, not the combined canonical conclusion.
- Reviewer writes: this report and isolated `.local/reviews/round1-security-local/` diagnostics only. No product, test, dependency, instruction, or historical acceptance edits were made by this reviewer.

The service is an administrator-only evidence and publishing system with an additional synchronous local housing writer. Distributed execution uses PostgreSQL, Celery/Redis, object storage, and publisher adapters; local execution uses SQLite, protected loopback humanization, immutable article bundles, and a Windows process supervisor. This pass found concrete defects in configuration, cleanup ownership, credential revocation, and verification tooling. No new live service acceptance is claimed.

The parent is implementing independently verified findings. In particular, the parent reported an A1 repair while this review continued; its new tests were visible in the later test-outline pass. Findings below describe the reviewed pre-repair behavior. This reviewer has not independently approved parent fixes or treated their existence as completed acceptance.

## Findings

Priorities describe user effect: P1 blocks an intended configured workflow; P2 causes data loss in a narrower supported helper/CLI boundary or weakens reliable verification. No speculative exploit or stylistic lint issue is counted.

### A1 — P2: failed S3 download deletes a destination it did not create

- Evidence: `src/adapters/storage/s3.py:247` and `:259-260` at baseline; `ObjectStorage.get_file` in `src/adapters/storage/base.py:41-49`.
- Trigger: call `get_file` with an existing destination, or receive invalid object metadata before the exclusive file open. The `xb` open is expected to reject collision without modifying the caller's file. The exception cleanup instead unconditionally unlinks that file.
- Reproduced now: mock object downloads against two isolated caller-owned files. `FileExistsError` and pre-open `ValueError` both left the existing destination absent. Original results: `.local/reviews/round1-security-local/s3-existing/results.json`.
- Remedy/test: track successful creation and the opened file identity; remove only a still-owned newly created destination. Also verify identity before successful return/chmod. Cover an existing file, invalid response headers, checksum failure of a newly created file, and substitution of the pathname during body streaming. A boolean alone does not establish cleanup ownership after a pathname replacement.
- Contract/counterargument: the current production caller at `src/apps/evidence/tasks.py:2474-2494` supplies `input<suffix>` in a newly created `TemporaryDirectory` for legacy HWP. That makes the collision unlikely in that caller and HWP admission is closed. It does not make destructive collision handling safe for the storage protocol's new-file helper.

### A2 — P2: the full-setup test copies a Windows venv launcher without its configuration

- Evidence: `tests/unit/test_local_scripts.py:143-145`, with the failing assertion at `:648`.
- Trigger: the prescribed `.venv/Scripts/python.exe -m pytest` runner makes `sys.executable` a Windows venv launcher. `_full_setup_fixture` copies only that executable to another fake venv. It cannot start without a corresponding `pyvenv.cfg`.
- Reproduced now: the single setup-boundary test, using a fresh confined ASCII pytest directory, development/local mode, UTF-8, and removed inherited root overrides, failed with `No pyvenv.cfg file`, command exit 106; pytest reported **1 failed in 1.37 s**. Log: `.local/reviews/round1-security-local/fixture-interpreter.log`.
- Remedy/test: prepare a valid fixture interpreter using the original interpreter/venv metadata or a lightweight venv from the base Python. Keep the real manage-command subprocess boundary. Run this test through the project venv and a standalone Python 3.12 runner.
- Counterargument: this is a test preparation defect, not evidence that actual application migration/root creation failed. The earlier canonical report records a valid disposable migration successfully executing.

### A3 — P1: Compose development selects the local runtime

- Evidence: `compose.yaml:3-30`, `.env.example:3`, `src/wisdome_writer/settings/__init__.py:43-49`, `:367-368`; the documented distributed quick start uses the development environment from the example.
- Trigger: launch the distributed stack with its documented development environment. Compose does not pass `WISDOME_RUNTIME_MODE`; settings default development to local mode. The web disables external publishing, local readiness probes a nonexistent sibling humanizer, and task configuration enables eager execution.
- Reproduced now: YAML inspection confirmed the missing setting. A settings-only child probe with the same development selection returned `mode=local`, `eager=true`, `publishing=false`. No database connection or Compose service was launched. Original result: `.local/reviews/round1-security-local/compose-runtime.json`.
- Remedy/test: explicitly select `distributed` in the shared Compose Django environment; assert the inherited web/worker/migrate/profile-admin environments. Keep the Windows local launchers explicitly local.
- Counterargument/limit: production already defaults distributed. The settings probe in this checkout can also load the existing local environment, whereas the image copies neither `.env` nor `.env.local`; the source's unconditional development default independently establishes the container mismatch. No deployed-container result is claimed.

### A4 — P1: WordPress self-revocation is misreported as a still-present credential

- Evidence: `src/adapters/publishers/wordpress/client.py:518-555`; the existing mock in `tests/unit/test_publisher_credentials.py:270-298` assumes a post-delete 404.
- Trigger: introspect the application's current password, delete that exact UUID successfully, then verify it using the deleted Basic Auth password. Core rejects that authentication before serving the password-resource route. The adapter accepts only 404/410 and raises `wordpress_application_password_still_present` for the expected 401.
- Reproduced now: an exact `deleted=true`/matching `previous.uuid` response followed by a Core-shaped `incorrect_password`/401 caused `PublisherError`, category `unknown_outcome`. Result: `.local/reviews/round1-security-local/wordpress-self-revoke.json`.
- Remedy/test: bind the successful DELETE result to the introspected UUID and its exact `previous.uuid`, and handle expected authentication invalidation after that bound deletion; alternatively use the validated Core deletion response as completion proof. Do not blindly accept every 401/403 or an unrelated deletion response. Test successful self-revocation, invalid or wrong-UUID deletion results, transport uncertainty, and a genuinely still-authenticating credential.
- Primary evidence: Core [application-password authentication](https://developer.wordpress.org/reference/functions/wp_authenticate_application_password/) fails when the supplied password no longer matches, [REST authentication error handling](https://developer.wordpress.org/reference/functions/rest_application_password_check_errors/) supplies 401, and [the deletion controller](https://developer.wordpress.org/reference/classes/wp_rest_application_passwords_controller/delete_item/) returns deletion and previous-record material. The expected remote response is inferred from this official source and locally reproduced with a mock. No actual WordPress account was used.

### A5 — P2: generated PowerShell test files and state reads assume an ASCII path

- Evidence: `tests/unit/test_local_scripts.py:787-802`, `:925-931`, `:1061-1063`, and analogous generated `-File` scripts/default `Get-Content` state reads throughout that module.
- Trigger: pytest's temporary path includes Korean characters on Windows PowerShell 5.1. Python writes interpolated PowerShell scripts as BOM-less UTF-8, which Windows PowerShell reads using its default legacy encoding; state JSON written as UTF-8 is also read without an explicit encoding.
- Reproduced now: the same existing Korean directory was not found by a BOM-less UTF-8 script (exit 9), but was found by an UTF-8-BOM script (exit 0). Default JSON `Get-Content` also failed that path; explicit `-Encoding UTF8` passed. Result: `.local/reviews/round1-security-local/encoding/results.json`.
- Remedy/test: use `utf-8-sig` for generated PowerShell 5.1 files, explicit `-Encoding UTF8` for JSON reads, and explicit UTF-8 child console output where the harness decodes output as UTF-8. Exercise an actual non-ASCII temporary directory, not only ASCII `--basetemp`.
- Counterargument: the application supervisor writes correct UTF-8 state; application environment/template reads already specify UTF-8. This finding concerns false test failures, not a newly demonstrated product reparse or process-ownership failure.

### A6 — P1: local ApplyHome API reconciliation rejects supported filtered count envelopes

- Evidence: `src/apps/local_content/api_reconciliation.py:207-255`, especially `:217`; contrast `src/adapters/sources/http.py:381-406`, which validates and prefers ODCloud `matchCount`.
- Trigger: date-filtered response contains `totalCount=200`, `matchCount=1`, and its one matching notice. Local observation uses totalCount as the pagination target, fetches an empty second page, and fails rather than returning the matching notice. Credential-enabled reconciliation then reports a source failure and blocks a complete local run.
- Reproduced now: a supported filtered-envelope mock failed with `OFFICIAL_API_PAGINATION_FAILED`, requested pages `[1, 2]`, while the existing distributed parser returned the expected count `1`. Result: `.local/reviews/round1-security-local/applyhome-matchcount.json`.
- Remedy/test: share the validated ODCloud envelope/count interpretation or equivalently prefer valid matchCount, retain totalCount fallback, and reject invalid or changing counts. Cover zero matches, fewer matches than the global total, malformed counts, and changing pagination.
- Limits/counterargument: no API credential was read or live request made. The [provider listing](https://www.data.go.kr/data/15098547/openapi.do) points to REB technical documentation, but its DOCX contents were not extracted by the browser tool in this pass. The precisely verified defect is incompatibility with envelopes already supported by the repository's distributed parser, not a newly observed provider outage. Default no-key HTML-only collection is unaffected.

### A7 — P2: a changed filename can become a Ruff option

- Evidence: `scripts/ci_changed_python.py:177-179` and the duplicate boundary `src/apps/local_content/acceptance_runner.py:170-175`.
- Trigger: a repository contains a changed Python file named `--config=relax.py`. Since filenames are appended without `--`, Ruff interprets that filename as a config option. A valid Python/TOML `relax.py` containing `lint.ignore = ["ALL"]` causes ordinary lint findings to disappear.
- Reproduced now: isolated `bad.py` with an unused import, `relax.py`, and the empty option-shaped filename. Current-style argv exited 0/`All checks passed!`; identical argv with an option delimiter exited 1 and reported F401/F821. Result: `.local/reviews/round1-security-local/ruff-flags/results.json`.
- Remedy/test: insert `--` before all repository-provided Ruff paths, including the acceptance command plan. Add a regression for leading-hyphen filenames. Update exact-plan expectations while preserving historical acceptance records as historical evidence.
- Counterargument: this is argument interpretation and a false lint verdict, not shell injection or privilege escalation. Existing branch review still governs changes to CI/config files.

### A8 — P2: acceptance target discovery silently omits non-ASCII changed filenames

- Evidence: `src/apps/local_content/acceptance_runner.py:250-267`.
- Trigger: Git's default quoted filename output for a changed/untracked `한글 review.py` is processed through `splitlines()`, `strip()`, and `endswith('.py')`. The quoted line does not identify the existing pathname and the file is omitted.
- Reproduced now: an isolated diagnostic Git repository with a baseline commit, a local-content fixture, and an untracked Korean Python filename. Discovery selected only the local-content fixture. The pinned baseline constant was substituted only in memory for this fixture. Result: `.local/reviews/round1-security-local/git-paths/result.json`.
- Remedy/test: request `-z` for all four Git queries and decode raw NUL-separated paths using the existing CI selector's `os.fsdecode` approach. Include type changes (`T`) in the diff filter and avoid stripping legitimate whitespace. Cover Korean, leading-hyphen, and (where supported) quote/newline filenames.
- Limits: only the isolated diagnostic repository was initialized/committed. The main checkout was not committed. Current `scripts/ci_changed_python.py` already handles NUL-separated paths; this defect is in the acceptance runner's duplicate discovery.

### A9 — P2: HWP exact CLI rejection deletes pre-existing output/report files

- Evidence: `deploy/containers/hwp-worker/wisdome-hwp-sandbox:939-940`, `:995-1005`.
- Trigger: exact CLI receives existing output/report paths and correctly rejects them as not-new, then its exception handlers unlink both caller-owned files.
- Reproduced now: import the wrapper without starting a service and call `run_exact_cli` with two existing isolated sentinels. It returned invalid-input exit 20 and deleted both files before converter execution or Linux isolation. Result: `.local/reviews/round1-security-local/hwp-existing/result.json`.
- Remedy/test: track which output/report files the invocation actually created and verify ownership before cleanup; preserve pre-existing or substituted paths. Test collision and invalid-input cases independently for output and report, plus genuine newly created partial output cleanup.
- Counterargument/limit: the normal sidecar invokes this CLI in a new private workspace. HWP activation is intentionally hard-false; this is a direct CLI/pre-release cleanup defect, not evidence of an active HWP conversion exploit. Keep that admission block closed.

### A10 — P2: final acceptance can combine an artifact audit from another run

- Evidence: `src/apps/local_content/acceptance.py:216-248`; artifact verifier `:413-485` independently re-audits its referenced root and returns its run name, but the caller does not bind that run to `report.run_path` or the workflow's run.
- Trigger: attach a valid older run's artifact-evidence reference to a later workflow report. Workflow and browser evidence may refer to the later run while the artifact verifier passes for the older run. The combiner uses the independent pass booleans without checking their shared subject.
- Reproduced now: a narrow composition probe supplied individually passing verifier results for workflow `2026-10-03` and artifact `2026-10-02`; final acceptance returned `overall_passed=true`. Result: `.local/reviews/round1-security-local/acceptance-run-binding.json`.
- Remedy/test: pass the expected run identity into artifact verification and reject mismatch; bind related counts/window and relevant generation/material identities where the report requires them. Test two independently valid different runs and matching evidence for one run.
- Verification limit: this probe mocks verifier outputs to isolate composition. No complete pair of live filesystem runs or browser evidence was executed. The source establishes that real artifact verification validates its own referenced run independently and omits the expected-run input. Parent must independently verify the actionable boundary before implementing.

### A11 — P2: the executable-lock test races startup and can corrupt its own fixture

- Evidence: `tests/unit/test_local_scripts.py:802-806` at baseline.
- Trigger: the test sleeps exactly 0.4 seconds after starting PowerShell, then asserts writing the copied executable is denied. PowerShell/module initialization can take longer than that; `process.poll() is None` does not establish that the verified file handle is open. If the guard is not yet acquired, `write_bytes` replaces the fixture and the test fails for its own scheduling race.
- Evidence reused per policy: the same-baseline preceding analysis's clean focused run failed this test; its independent non-mutating probe first observed denial at **0.453 seconds**, then recorded **32 denied samples** with an unchanged final hash. See [code analysis, verification diagnosis](../code-analysis.md#actual-verification-and-failure-investigation). This pass inspected the exact unsynchronized source but did not rerun that timing probe or present the earlier result as a new test.
- Remedy/test: wait with a bounded timeout for an observable child-start/guard-ready signal or harmless open denial; attempt a non-truncating open while probing, then test denial during the held interval. Avoid writing replacement bytes before synchronization. Exercise a deliberately delayed start and ensure the helper/executable hash remain intact.
- Counterargument: the earlier positive probe supports correct product file-sharing behavior. A11 is a flaky/unsafe test timing assumption, not evidence the guard itself is missing.

## Reproduction commands and results

A saved convenience harness is `.local/reviews/round1-security-local/reproduce_findings.py`. It sets development/local mode, UTF-8, and removes the three inherited local-root overrides; it uses only disposable files and mock transports. Its outputs will change as the parent fixes the product. It was written after the initial probes, so the original JSON/log artifacts above are the evidence of their actual pre-repair outcomes.

```powershell
$env:PYTHONUTF8='1'
.venv/Scripts/python.exe .local/reviews/round1-security-local/reproduce_findings.py A1
.venv/Scripts/python.exe .local/reviews/round1-security-local/reproduce_findings.py A2
# Select A3 through A10 individually using the same command.
```

| Check executed during this pass | Actual result |
| --- | --- |
| Git baseline/status | Expected main/HEAD; pre-existing documentation changes preserved |
| Product/config text inventory | 135 assigned files, including one binary asset; all enumerated and inspected, with generated DDL review limits below |
| S3 collision and invalid-header mock | Both pre-existing caller-owned files removed before repair |
| One full-setup fixture pytest test | 1 failed in 1.37 s; venv launcher exit 106 |
| Compose YAML/settings-only probe | Runtime mode absent in Compose; local/eager/disabled publishing under development selection |
| WordPress self-revoke mock | Successful bound deletion followed by 401 incorrectly raised unknown_outcome |
| PowerShell Unicode path/script/JSON probe | BOM-less/default decoding failed; BOM/explicit UTF-8 passed |
| ODCloud filtered-envelope mock | Local pagination failed; existing distributed parser expected count 1 |
| Ruff filename option probe | No delimiter passed; delimiter reported actual F401/F821 findings |
| Isolated Git path discovery | Existing Korean changed file omitted |
| HWP exact CLI early-rejection mock | Exit 20 and both existing sentinels deleted |
| Acceptance composition probe | Mismatched passing run subjects produced overall_passed=true |
| Saved harness A3 smoke check | Executed successfully; confirmed missing Compose mode at that moment |
| Hero binary check | PNG, 1730×909; SHA-256 matches the renderer's fixed repository asset |

No full pytest run, new Django migration/check pass, whole-repository Ruff run, application server, Compose deployment, Redis/PostgreSQL/S3 integration, source collection with a credential, live humanizer, real publisher, PaddleOCR model download/corpus, HWP Linux sandbox, or hosted CI was executed by this reviewer. Only the stated single pytest test and isolated repros ran. Existing canonical report/test results remain historical starting evidence.

## Safeguards and deferred questions

Inspected safeguards include staff/session/CSRF, scoped reauthentication, closed input contracts, secret references and bounded audit redaction, public-DNS pinning and redirect checks, durable event material and consumer/terminal capabilities, local generation leases and hash inventories, loopback-only preview, escaped Markdown with security headers, and Windows suspended-process job ownership. Their existence is not blanket security certification.

The HWP hard-false activation gate must remain closed; unsigned/golden metadata cannot substitute for signed release acceptance. No finding asks to open that gate.

Deferred ideas/hypotheses, not counted defects:

- Define local history retention and migration of renderer/font fingerprints; long histories and changed fonts can make integrity-preserving replay expensive or intentionally unavailable.
- Clarify publisher destination/network trust and readiness objectives before changing configuration/address admission policy. No new SSRF exploit was demonstrated.
- Define the precise admission instant for a new legal hold or reference racing remote retention deletion, then verify it on PostgreSQL with actual concurrent writers. SQLite or source reading cannot establish those guarantees.
- Make acceptance browser executable/environment selection portable when the historical Task 12 contract is replaced; preserve the dated machine-specific acceptance evidence.
- Larger HTTP/JSON/decompression and filesystem race/resource benchmarks could extend this review; they were not run and no speculative vulnerability is reported.

## File coverage appendix

The initial inventory contained 162 files; three relevant cross-interface test files were added, for 165 entries. Text inspection used numbered range reads; small/empty modules were included. Repeated generated model-field declarations and backend DDL were structurally sampled, with custom migration/backfill/guard functions inspected. No PostgreSQL trigger execution is implied. Related tests were enumerated/AST-outlined with targeted fixtures/assertions and defect-relevant bodies read; this is explicitly **test sampling**, not a line-by-line or executed test audit. The later outline encountered parent-added A1 regressions, without executing them.

- `S`: product/config text inspected (all named assigned product files).
- `M`: migration text inspected; generated declaration/backend DDL review sampling and execution limits apply.
- `T`: related test outline/assertion/body sampling; only A2's named pytest test executed.
- `B`: binary header/dimensions/digest inspected; not a textual or visual design audit.
- Unread assigned product/config files: **none**. Unread test bodies beyond the explicitly stated samples remain a verification limit.

| File | Initial lines | Coverage |
| --- | ---: | --- |
| `.env.example` | 67 | S |
| `.env.local.example` | 12 | S |
| `.github/workflows/quality.yml` | 70 | S |
| `compose.yaml` | 488 | S |
| `config/retention/default.json` | 21 | S |
| `deploy/compose-deploy.ps1` | 366 | S |
| `deploy/containers/app/Dockerfile` | 26 | S |
| `deploy/containers/app/entrypoint.sh` | 11 | S |
| `deploy/containers/hwp-worker/build_manifest.py` | 158 | S |
| `deploy/containers/hwp-worker/Dockerfile` | 92 | S |
| `deploy/containers/hwp-worker/fonts.conf` | 7 | S |
| `deploy/containers/hwp-worker/wisdome-hwp-sandbox` | 1615 | S |
| `deploy/containers/paddleocr-model-bootstrap/bootstrap.py` | 159 | S |
| `deploy/containers/paddleocr-model-bootstrap/Dockerfile` | 23 | S |
| `deploy/containers/paddleocr-worker/Dockerfile` | 25 | S |
| `deploy/containers/profile-admin/Dockerfile` | 25 | S |
| `pyproject.toml` | 69 | S |
| `scripts/audit_task12_live_run.py` | 101 | S |
| `scripts/ci_changed_python.py` | 184 | S |
| `scripts/finalize_task12_acceptance.py` | 49 | S |
| `scripts/local-process-guard.psm1` | 896 | S |
| `scripts/local-toolchain.psm1` | 262 | S |
| `scripts/prepare_ci_sqlite.py` | 40 | S |
| `scripts/run_task12_deterministic.py` | 155 | S |
| `scripts/run_task12_live_workflow.py` | 144 | S |
| `scripts/setup-local.ps1` | 766 | S |
| `scripts/start-local.ps1` | 281 | S |
| `scripts/toolchain-lock.json` | 14 | S |
| `scripts/verify_task12_brave.py` | 420 | S |
| `src/adapters/publishers/__init__.py` | 2 | S |
| `src/adapters/publishers/blogger/__init__.py` | 4 | S |
| `src/adapters/publishers/blogger/client.py` | 479 | S |
| `src/adapters/publishers/blogger/oauth.py` | 160 | S |
| `src/adapters/publishers/wordpress/__init__.py` | 4 | S |
| `src/adapters/publishers/wordpress/client.py` | 644 | S |
| `src/adapters/sources/__init__.py` | 25 | S |
| `src/adapters/sources/base.py` | 368 | S |
| `src/adapters/sources/errors.py` | 68 | S |
| `src/adapters/sources/http.py` | 2104 | S |
| `src/adapters/sources/manifests.py` | 185 | S |
| `src/adapters/storage/__init__.py` | 5 | S |
| `src/adapters/storage/base.py` | 62 | S |
| `src/adapters/storage/s3.py` | 434 | S |
| `src/apps/accounts/__init__.py` | 2 | S |
| `src/apps/accounts/admin.py` | 56 | S |
| `src/apps/accounts/api.py` | 29 | S |
| `src/apps/accounts/apps.py` | 9 | S |
| `src/apps/accounts/forms.py` | 16 | S |
| `src/apps/accounts/managers.py` | 28 | S |
| `src/apps/accounts/migrations/0001_initial.py` | 61 | M |
| `src/apps/accounts/migrations/0002_adminaccount_reauthentication_throttle.py` | 25 | M |
| `src/apps/accounts/migrations/__init__.py` | 0 | M |
| `src/apps/accounts/models.py` | 57 | S |
| `src/apps/accounts/services.py` | 233 | S |
| `src/apps/accounts/urls.py` | 8 | S |
| `src/apps/audit/__init__.py` | 2 | S |
| `src/apps/audit/admin.py` | 72 | S |
| `src/apps/audit/api.py` | 395 | S |
| `src/apps/audit/apps.py` | 9 | S |
| `src/apps/audit/cursor.py` | 93 | S |
| `src/apps/audit/management/__init__.py` | 1 | S |
| `src/apps/audit/management/commands/__init__.py` | 1 | S |
| `src/apps/audit/management/commands/configure_runtime_database_role.py` | 357 | S |
| `src/apps/audit/migrations/0001_initial.py` | 98 | M |
| `src/apps/audit/migrations/0002_auditevent_append_only.py` | 206 | M |
| `src/apps/audit/migrations/0003_retention_object_tombstone.py` | 220 | M |
| `src/apps/audit/migrations/__init__.py` | 0 | M |
| `src/apps/audit/models.py` | 358 | S |
| `src/apps/audit/redaction.py` | 1001 | S |
| `src/apps/audit/retention.py` | 1337 | S |
| `src/apps/audit/services.py` | 661 | S |
| `src/apps/audit/urls.py` | 31 | S |
| `src/apps/local_content/__init__.py` | 1 | S |
| `src/apps/local_content/acceptance.py` | 1611 | S |
| `src/apps/local_content/acceptance_runner.py` | 1021 | S |
| `src/apps/local_content/api_reconciliation.py` | 396 | S |
| `src/apps/local_content/apps.py` | 7 | S |
| `src/apps/local_content/bundles.py` | 2203 | S |
| `src/apps/local_content/contracts.py` | 92 | S |
| `src/apps/local_content/dates.py` | 21 | S |
| `src/apps/local_content/http.py` | 296 | S |
| `src/apps/local_content/humanizer.py` | 1141 | S |
| `src/apps/local_content/images.py` | 669 | S |
| `src/apps/local_content/management/__init__.py` | 1 | S |
| `src/apps/local_content/management/commands/__init__.py` | 1 | S |
| `src/apps/local_content/management/commands/collect_recent_housing.py` | 477 | S |
| `src/apps/local_content/rendering.py` | 687 | S |
| `src/apps/local_content/selection.py` | 139 | S |
| `src/apps/local_content/sources/__init__.py` | 1 | S |
| `src/apps/local_content/sources/applyhome.py` | 691 | S |
| `src/apps/local_content/sources/lh.py` | 708 | S |
| `src/apps/local_content/status.py` | 161 | S |
| `src/apps/local_content/urls.py` | 27 | S |
| `src/apps/local_content/views.py` | 622 | S |
| `src/apps/local_content/workflow.py` | 1769 | S |
| `src/static/local_articles/generic-housing-hero.png` | binary | B |
| `src/static/local_articles/preview.css` | 116 | S |
| `src/templates/local_articles/article.html` | 20 | S |
| `src/templates/local_articles/index.html` | 31 | S |
| `src/templates/local_articles/run.html` | 16 | S |
| `src/wisdome_writer/__init__.py` | 4 | S |
| `src/wisdome_writer/api/__init__.py` | 9 | S |
| `src/wisdome_writer/api/health.py` | 325 | S |
| `src/wisdome_writer/api/middleware.py` | 338 | S |
| `src/wisdome_writer/api/openapi.py` | 1125 | S |
| `src/wisdome_writer/api/pagination.py` | 279 | S |
| `src/wisdome_writer/api/problems.py` | 181 | S |
| `src/wisdome_writer/api/urls.py` | 6 | S |
| `src/wisdome_writer/asgi.py` | 7 | S |
| `src/wisdome_writer/celery.py` | 222 | S |
| `src/wisdome_writer/console.py` | 38 | S |
| `src/wisdome_writer/domain/__init__.py` | 13 | S |
| `src/wisdome_writer/domain/concurrency.py` | 145 | S |
| `src/wisdome_writer/domain/errors.py` | 222 | S |
| `src/wisdome_writer/domain/hashing.py` | 76 | S |
| `src/wisdome_writer/domain/models.py` | 19 | S |
| `src/wisdome_writer/external_publishing.py` | 66 | S |
| `src/wisdome_writer/infrastructure/__init__.py` | 10 | S |
| `src/wisdome_writer/infrastructure/apps.py` | 9 | S |
| `src/wisdome_writer/infrastructure/event_routes.py` | 1082 | S |
| `src/wisdome_writer/infrastructure/http_safety.py` | 620 | S |
| `src/wisdome_writer/infrastructure/migrations/0001_initial.py` | 55 | M |
| `src/wisdome_writer/infrastructure/migrations/0002_outboxconsumerreceipt_and_more.py` | 207 | M |
| `src/wisdome_writer/infrastructure/migrations/0003_outbox_terminal_reservation.py` | 329 | M |
| `src/wisdome_writer/infrastructure/migrations/__init__.py` | 0 | M |
| `src/wisdome_writer/infrastructure/models.py` | 152 | S |
| `src/wisdome_writer/infrastructure/outbox.py` | 1427 | S |
| `src/wisdome_writer/infrastructure/queues.py` | 19 | S |
| `src/wisdome_writer/infrastructure/secrets.py` | 206 | S |
| `src/wisdome_writer/infrastructure/tasks.py` | 304 | S |
| `src/wisdome_writer/observability.py` | 567 | S |
| `src/wisdome_writer/runtime_mode.py` | 11 | S |
| `src/wisdome_writer/settings/__init__.py` | 465 | S |
| `src/wisdome_writer/urls.py` | 48 | S |
| `src/wisdome_writer/wsgi.py` | 7 | S |
| `tests/conftest.py` | 95 | S (fixture teardown read) |
| `tests/integration/test_local_housing_workflow.py` | 2056 | T |
| `tests/unit/test_applyhome_public_html.py` | 579 | T |
| `tests/unit/test_lh_public_html.py` | 610 | T |
| `tests/unit/test_local_api_reconciliation.py` | 229 | T |
| `tests/unit/test_local_content_acceptance.py` | 570 | T |
| `tests/unit/test_local_content_bundles.py` | 2202 | T |
| `tests/unit/test_local_content_dates.py` | 197 | T |
| `tests/unit/test_local_content_http.py` | 482 | T |
| `tests/unit/test_local_content_humanizer.py` | 1350 | T |
| `tests/unit/test_local_content_images.py` | 306 | T |
| `tests/unit/test_local_content_preview.py` | 304 | T |
| `tests/unit/test_local_content_rendering.py` | 346 | T |
| `tests/unit/test_local_content_selection.py` | 294 | T |
| `tests/unit/test_local_publishing_policy.py` | 90 | T |
| `tests/unit/test_local_runtime.py` | 209 | T |
| `tests/unit/test_local_scripts.py` | 2040 | T |
| `tests/unit/test_operations_admin_contract.py` | 426 | T |
| `tests/unit/test_outbox_terminal_reservation.py` | 108 | T |
| `tests/unit/test_paddleocr_model_bootstrap.py` | 293 | T |
| `tests/unit/test_publisher_credentials.py` | 609 | T |
| `tests/unit/test_queue_configuration.py` | 18 | T |
| `tests/unit/test_retention_dependency_graph.py` | 697 | T |
| `tests/unit/test_s3_streaming_upload.py` | 90 | T |
| `tests/unit/test_sqlite_test_cleanup.py` | 163 | T |
| `tests/unit/test_task12_deterministic_runner.py` | 399 | T |
| `tests/unit/test_wordpress_media_delivery.py` | 193 | T |
| `tests/unit/test_extraction_safety_boundaries.py` | 988 | T |
| `tests/unit/test_run_control.py` | 346 | T |
| `tests/unit/test_legacy_hwp_sandbox.py` | 1129 | T |

Additional interface reads: `src/apps/evidence/tasks.py:2460-2510`, `src/apps/publishing/services.py:1315-1365`, `:3100-3160`, `:3698-3760`, the revocation call in `src/apps/publishing/tasks.py`, README's distributed quick start, relevant OpenAPI input schemas, and the canonical starting reports. These cross-reads do not claim full coverage of the other reviewers' areas.

## Handoff log

| Date | Entry |
| --- | --- |
| 2026-10-03 | Reviewer A round 1: baseline preserved; A1–A10 provided to parent with narrow evidence; A11 retained as a distinct same-baseline historical test-timing diagnosis; product/config inventory and test/DDL sampling limits recorded. Parent owns independent verification, fixes, integration, and canonical code/service report updates. |
