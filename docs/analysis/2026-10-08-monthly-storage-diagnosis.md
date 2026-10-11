# Monthly funding storage integrity diagnosis

Latest checkpoint: the first retry established an exact `0x10000000` lstat
attribute delta with unchanged device/inode/type. Real funding fixture runs then
reproduced disagreement with native attributes. A narrowly verified identity
reconciliation is implemented below; actual live retry acceptance remains owned
by the parent. The archive-only repair and earlier unknown-cause findings are
preserved as dated historical steps, not the explanation for this later delta.

## Scope and source baseline

- Analysis date: 2026-10-08, Asia/Seoul.
- Purpose: diagnose the failed local October funding collection commit without
  weakening directory ownership, inode, root-containment or reparse defenses.
- Baseline: `codex/policy-funding` at
  `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc`, with pre-existing uncommitted
  funding/portal implementation and documentation changes. The writer already
  contains the uncommitted validated run metadata reader; its identity functions
  are unchanged from the baseline commit.
- Initial working tree included modified `AGENTS.md`, README, canonical analysis
  reports, the remediation plan, `bundles.py`, templates/settings/URLs, and
  untracked funding/portal code, tests and dated reports. Initial diagnosis did
  not change production files. A subsequently approved narrow repair changes
  only the bundle identity implementation and targeted bundle tests; failed live
  data, dependencies and Git history remain untouched.
- Exact writer bytes during reproduction: SHA-256
  `48bfe9fba69b7f189ebba61d8ed0b8cade0452775f6ec1f3fdf005b64a4ad0ba`.
- Runtime: existing `.venv/Scripts/python.exe`, Python 3.12.10 on Windows.
- Starting authorities: project `AGENTS.md`, the shared
  `C:/Users/강지혜/.agents/analysis-policy.md`, and
  [monthly update progress](2026-10-08-monthly-data-update.md).

## Implemented persistence behavior

Funding collection publishes immutable generation runs via
[`publish_collection`](../../src/apps/policy_funding/workflow.py). It reserves a
writer-owned run directory, builds a beginner guide and one article/verification
bundle per notice, then commits the run index and declared notice/observation
metadata. Validated readback is part of that workflow. Housing and funding roots
remain separate.

[`ArticleBundleWriter`](../../src/apps/local_content/bundles.py) creates an exclusive
sibling staging directory, captures its identity, writes and synchronizes the
declared files and manifests, validates the staging bytes, and replaces the
target atomically. Its ownership guard compares four fields:

`(stat.S_IFMT(st_mode), st_dev, st_ino, st_file_attributes)`.

At the original failure this tuple retained all attribute bits. The approved
repair below now excludes only the mutable archive bit from identity tuples.
The directory fingerprint is captured immediately after `staging.mkdir()` in
`_create_owned_staging`; `_validate_owned_article_stage` checks it after writing
the payloads and manifests. `_lstat_no_links` independently rejects symlinks and
the Windows reparse attribute. Full replay fingerprints additionally retain
size, timestamps and attributes. The failure intentionally leaves staged
material in place. Existing tests exercise directory/file replacement, inode
changes, handle binding and reparse rejection; historical suite acceptance is
not evidence that this new live commit succeeded.

## Actual live failure and missing evidence

The authorized funding command used `--pages 3 --max-notices 40 --seconds 180
--as-of 2026-10-08`. The saved log
`.local/reviews/monthly-update/funding-collection.log` records HTTP reads followed
by exit 1 and:

> owned staging owned identity, type, or reparse state was substituted

The failed staging name is
`.funding-1dc2d2eff9d36483bee630f8423c4eb757af96f4d4686d916e602270f95011ff.tmp-bd2f6a168492-f4307605348f2fa2-341fe7213445cf88`
under `output/policy-funding/2026-10-08/`. This is a failed commit, not a completed
October data update.

The log does not record the expected tuple, observed tuple or differing fields.
Consequently the actual live field difference cannot be reconstructed from that
log. A subsequent read-only inspection observed:

| Field | Failed staging directory, inspected now |
| --- | --- |
| File type | 16384 (`S_IFDIR`) |
| Device | 14913894758346402705 |
| Inode | 2814749767984200 |
| Windows attributes | 16 (`0x10`) |
| Reparse attribute / tag | false / 0 |
| Child names | `article.md`, `verification.json`, `manifest.sha256`, `manifest.json` |

The run and funding-root directories also currently have attributes 16 and no
reparse flag. These snapshots do not prove their state at the failed comparison,
nor establish that content was valid, nor establish that substitution did not
occur. No content from the live failed folder was changed or deleted.

## Later live retry: exact field difference and primary source meaning

The parent's subsequent live retry still failed after the archive-only repair.
Its saved log `.local/reviews/monthly-update/funding-collection-retry.log` now
records both tuples for staging under
`output/policy-funding/2026-10-08--run-fd965062917d/`:

| Field | Expected at creation | Observed at ownership guard |
| --- | --- | --- |
| File type | 16384 | 16384 |
| Device | 14913894758346402705 | 14913894758346402705 |
| Inode | 4785074604941172 | 4785074604941172 |
| Attributes | 268435472 (`0x10000010`) | 16 (`0x10`) |

The XOR is exactly `0x10000000`, not archive (`0x20`). The original first failure
still lacks its tuples; this later failure establishes the actual observed
delta for the retry, not retroactive proof of the first failure's cause.

Microsoft documents `0x10000000` as `FILE_FLAG_RANDOM_ACCESS`, an input caching
hint to `CreateFileW`. Its documented file attribute list does not define that
value as an attribute. Sharing a numerical value with an open flag alone is not
proof that an arbitrary observed bit is safe to discard.
[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew),
[File Attribute Constants](https://learn.microsoft.com/en-us/windows/win32/fileio/file-attribute-constants).

Microsoft defines `GetFileAttributesW` as a query of filesystem attributes,
including the link itself when the path names a symbolic link.
[GetFileAttributesW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfileattributesw).

CPython 3.12.10's Windows `lstat` uses
`GetFileInformationByName(FileStatBasicByNameInfo)` where available, and copies
the resulting `FileAttributes` into `st_file_attributes`; its fallback queries a
handle. This is source evidence about the runtime query route, not proof of
which runtime, OS, filesystem or filter layer generated the anomalous bit.
[CPython posixmodule.c](https://github.com/python/cpython/blob/v3.12.10/Modules/posixmodule.c#L1976),
[CPython fileutils.c](https://github.com/python/cpython/blob/v3.12.10/Python/fileutils.c#L1071).
Microsoft describes the by-name API as returning information without an opened
handle. [GetFileInformationByName](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyname).

The project's `_fsync_directory` contains no direct native CreateFile flags:
it calls `os.open(path, os.O_RDONLY)`, `os.fsync` and `os.close`; the existing
Windows unsupported-directory-open case returns without a descriptor. Code
inspection alone therefore does not establish flag contamination by this helper.

## Native comparison experiments and exact natural reproduction

All material below was newly created under `.local/reviews/monthly-update/storage`.
Neither live failed generation was changed or deleted.

1. `storage_native_diagnose.py` created 100 staging-shaped directories and captured
   300 snapshots after mkdir, parent fsync, and payload/directory fsync. Each
   snapshot took lstat, native GetFileAttributesW and lstat again. All values were
   16; no discrepancy. Evidence:
   `native-d2cbdd0f3c6c42f5be1c215417f7dfb8/evidence.json`.
2. Direct native opens on a retained fixture with random-access,
   sequential-scan and write-through combinations returned attributes 16 before,
   during and after the handle lifetime. This did not reproduce the issue.
   Evidence: the same root's `native-open-flags.json`.
3. Direct by-name queries with buffers initialized to zero, `0x10`, `0xA5` and
   `0xFF` returned attributes 16, including a later inspection of a failed
   research stage. No initialization-related field difference was observed.
   Evidence: `by-name-initialization.json` in the first native root, and
   `funding-native-0138737ad87142b6bb92b2978cda4f41/failed-stage-byname-initialization.json`.
4. The real policy funding publisher was then exercised with two planned fixture
   generations of 49 notices each, preserving fixture provenance and validated
   readback. No transport was supplied or invoked. On the second generation,
   the real unchanged guard failed after 16,191 identity checks:
   `(16384, 14913894758346402705, 4503599628231448, 268435472)`.
   Native GetFileAttributesW immediately returned 16, and the next lstat returned
   16 with the same inode. This reproduces the exact live retry delta and proves
   observed disagreement between lstat and native attributes in the relevant
   workflow. Evidence:
   `funding-native-6ef2d747fdc6444cbeb2b87ef8fac248/evidence.json`.
5. A first conditional repair required the second lstat to clear the bit. Natural
   fixture verification exposed a further variant and correctly failed closed:
   initial and second lstat both had attributes 268435472; native attributes were
   16, and **all** other fingerprint fields were identical. Two earlier stages
   reconciled; the third refused to guess. Evidence:
   `funding-native-0138737ad87142b6bb92b2978cda4f41/evidence.json`.

Observed proof is the same-object metadata/native discrepancy and both captured
variants. The inference is that the lstat query result carries a non-persistent
query artifact for this bit; the internal cause is unproven. The repair below
relies on native verification for each occurrence rather than trusting that
inference or globally masking undocumented attributes.

## Bounded natural reproduction

Research script:
`.local/reviews/monthly-update/storage_diagnose.py`.

The script wraps staging creation and the ownership guard only to capture
metadata. Every production guard still runs, and no exception is suppressed.
It writes 100 unique tiny two-file bundles using the real writer and a writer
issued run lease in a new disposable research root. Each bundle contains a short
Markdown article and `{}` JSON verification file; it performs no HTTP requests,
image generation, browser work or server operations.

Command executed with `PYTHONUTF8=1`, `WISDOME_ENVIRONMENT=development` and
`WISDOME_RUNTIME_MODE=local`:

```powershell
.venv/Scripts/python.exe .local/reviews/monthly-update/storage_diagnose.py
```

Actual result: exit 0; **100 attempted, 100 published, zero failures, zero
identity mismatches**, 5.430 seconds. Every captured fresh directory had file
type 16384 and Windows attributes 16. This experiment did not reproduce the live
failure.

Retained evidence and fixture directories:
`.local/reviews/monthly-update/storage/natural-1bf448b1ef6549e5bfb2a78e323ed067/evidence.json`.
The evidence includes creation metadata, counts, source hash and runtime version.
It is ignored local research material; this Markdown report is the portable
analysis authority.

## Controlled archive-bit red characterization

A separate test deliberately changed the Windows archive bit on a new, otherwise
unchanged directory using `SetFileAttributesW`. This was one directory probe,
not a second natural bundle batch and not an action on live data.

Research test:
`.local/reviews/monthly-update/test_storage_archive_characterization.py`.

```powershell
.venv/Scripts/python.exe .local/reviews/monthly-update/test_storage_archive_characterization.py
```

Actual result: exit 1; one assertion failure. The current ownership guard rejected
the same directory solely for an archive metadata change:

| Field | Expected before change | Observed after change |
| --- | --- | --- |
| File type | 16384 | 16384 |
| Device | 14913894758346402705 | 14913894758346402705 |
| Inode | 3377699721408007 | 3377699721408007 |
| Windows attributes | 16 (`0x10`) | 48 (`0x30`) |
| Reparse attribute | false | false |

The real difference is exactly `0x20`. No type, device, inode or reparse state
changed. The test catches `BundlePublishError` and asserts that no rejection
occurred; it fails with "same owned directory was rejected solely for archive
metadata". This proves that full attribute equality can produce a false
substitution alarm for a real mutable Windows attribute. **It does not prove that
an archive-bit change caused the live failure.** That inference is unsupported,
especially because the later failed-folder snapshot has attributes 16.

Retained evidence:
`.local/reviews/monthly-update/storage/archive-90bcd3713e6840a9a1d8947463ddce09/evidence.json`.

## Findings, priority and proposed next action

1. **P1: the live funding update remains incomplete and its precise integrity
   mismatch remains unknown.** Preserve the failed run and collect the original
   expected/observed tuple plus differing field names at a future authorized
   failure. A current `lstat` or a successful fixture batch cannot adjudicate the
   old mismatch. Keep the guard fail-closed; do not convert this error into
   success, automatically retry through it, recapture identity after failure, or
   delete abandoned material.
2. **P2: the identity comparison includes a mutable attribute that is not object
   identity.** The controlled red test supplies a verified, separate Windows
   false-positive case. A narrowly scoped candidate fix is to exclude only
   `FILE_ATTRIBUTE_ARCHIVE` (`0x20`) consistently from the attribute component of
   identity-only tuples (`_identity_fingerprint_metadata` and
   `_identity_from_fingerprint`). Preserve file type, device, inode, every other
   attribute, the explicit symlink/reparse guard, root containment, handle-bound
   reads and complete replay fingerprints. This candidate was subsequently
   approved and implemented for its independently demonstrated false-positive
   case, as recorded below. It must not be presented as the diagnosed
   live-failure fix.
3. The diagnostic production improvement is additional
   evidence at the rejection boundary: report sanitized expected/observed tuples
   or field differences while preserving the rejection. It enables a later
   field-specific decision instead of broad attribute masking. No secret values
   are needed for this evidence.

The archive-only regression is now in the maintained suite, alongside existing
stage/root/inode replacement and reparse regressions.
Do not mask all file attributes or alter full immutable replay fingerprints
without additional evidence. The real live mismatch still requires separate
adjudication and successful collection/readback acceptance.

## Approved narrow repair and executed regression verification

The parent approved the bounded design already proposed above: normalize only
`FILE_ATTRIBUTE_ARCHIVE` (`0x20`) in identity tuples, preserve full read/replay
fingerprints and all other protections, and include expected/observed tuples in
the remaining ownership rejection message. The pre-existing validated metadata
reader remains intact.

Implemented changes in `src/apps/local_content/bundles.py`:

- `_identity_fingerprint_metadata` and `_identity_from_fingerprint` apply
  `attributes & ~0x20`. File type, device, inode and every other attribute bit
  remain part of owned identity.
- `_fingerprint_metadata`, `_opened_handle_fingerprint` and full immutable
  replay/read checks retain the complete attributes unchanged.
- `_require_owned_identity` still rejects unequal identities and now reports
  `expected=(...)` and `observed=(...)`. These tuples contain filesystem type,
  device, inode and normalized attributes, not credentials or notice contents.
- Symlink/reparse rejection, root containment, reservations, atomic publication,
  failed-material retention and file/manifest validation are unchanged.

Maintained regressions in `tests/unit/test_local_content_bundles.py` exercise
the real writer flow with an actual Windows archive-bit change immediately after
stage creation, plus a portable complete stat-object control. They verify
successful publication of the expected article bytes while the owned directory
object remains the same. Separate writer-flow cases reject changes to inode,
device, file type, readonly, hidden and reparse metadata. Bound-read cases reject
archive metadata changes at both path snapshot and opened-handle boundaries;
these are real reader calls with controlled stat input, not assertions on the
implementation text.

RED before the implementation: after correcting an initial test-fixture MIME
declaration, the focused command reported **7 failed, 2 passed**. Both portable
and actual Windows archive publication cases failed at the production ownership
guard. Five security cases rejected substitution as intended but lacked the new
expected/observed diagnostics; reparse and full-read controls passed.

GREEN after the implementation: the final focused command reported
**10 passed, 142 deselected** in 0.26 seconds:

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_local_content_bundles.py -k 'bound_read_still or archive or security_metadata' --tb=short
```

Scoped Ruff on the two edited code/test files reported **All checks passed**
(exit 0). The initial scoped regression run completed **217 passed** in 36.18
seconds. The final run, including both improved bound-reader controls, completed
**218 passed** in 37.14 seconds (exit 0). It included the full bundle, local
housing workflow and funding workflow files.

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_local_content_bundles.py tests/integration/test_local_housing_workflow.py tests/unit/test_policy_funding_workflow.py --tb=short
.venv/Scripts/python.exe -m ruff check src/apps/local_content/bundles.py tests/unit/test_local_content_bundles.py
```

Both test commands used development/local mode and `PYTHONUTF8=1`, removing
inherited `LOCAL_STATE_ROOT`, `LOCAL_OBJECT_ROOT`, `LOCAL_ARTICLE_ROOT` and obsolete
`LOCAL_OUTPUT_ROOT` variables. Final test log:
`.local/reviews/monthly-update/storage-scope-tests-final.log`.

Writer source SHA-256 after the historical archive-only repair:
`1d61cc1107ef6ed65d22466ef521b45c85e8a1963524189e193a39e0829e78b8`.
Targeted test source SHA-256:
`3dacacdd5447c1aa6a2e27253fd3b552e35de0d341481a23335dcb4a099bb4ae`.

## Current native-validated identity repair and final verification

The parent approved a narrow conditional native reconciliation, then its
evidence-supported extension for the second-lstat-still-high variant. The
implemented `_path_identity_fingerprint` now performs extra work only when the
first path snapshot contains `0x10000000`:

- Native attributes equal to the first raw attributes are retained, including a
  genuine native high bit. An unsupported platform leaves the raw value intact.
- A correction requires the first raw/native XOR to be **exactly** `0x10000000`.
- A second no-link lstat must keep **all** non-attribute fingerprint fields
  unchanged: type, device, inode, size, mtime and ctime. Its attribute XOR with
  native must be zero or exactly the same demonstrated bit; every other flag
  remains bound.
- Two native GetFileAttributesW queries must return identical attributes. A
  native error, changed native value, reparse flag, changed lower/other flag or
  changed object/content metadata raises the supplied validation error.
- Only after these checks does the **identity snapshot** use verified native
  attributes. Existing archive-only identity normalization then applies.
  Full path/content/replay/read fingerprints and source-file fstat snapshots
  remain unchanged and strict.

The native helper uses Unicode GetFileAttributesW with extended absolute paths,
including the UNC extended-path form. It does not write filesystem attributes,
follow a recovery path through failed staging or alter existing content. Existing
ownership rejection diagnostics remain available for any remaining failure.

Maintained regression file: `tests/unit/test_bundle_windows_metadata.py`.
Before the initial conditional implementation, the new tests reported
**12 failed, 4 passed**; the publication case failed with the exact captured
expected-high/observed-low tuple. Before the validated extension, tests reported
**6 failed, 18 passed**, including the second-raw-still-high publication case and
missing second-native error/change rejection. After the final implementation,
**24 passed** in 0.18 seconds. Controls cover raw inode/device/type/size/time and
lower-flag changes, reparse rejection, native errors on either query, native
disagreement, genuine native bit retention, unsupported platforms, and unchanged
full path/handle read binding.

Natural funding verification of the final code then published and validated
**both** 49-notice fixture generations: 100 bundles including the two guides,
26,144 identity checks, zero errors, and 8 matching native queries across four
actual anomalous identity captures. Duration: 5.036 seconds. This exercises the
reconciliation during real fixture publication, not merely a mock. Evidence:
`.local/reviews/monthly-update/storage/funding-native-79db03b5ec1548488936fd4166fe1b2d/evidence.json`.
It establishes local fixture persistence and readback, not official live source
coverage or financial program acceptance.

Final scoped regression command:

```powershell
.venv/Scripts/python.exe -m pytest -q tests/unit/test_local_content_bundles.py tests/unit/test_bundle_windows_metadata.py tests/integration/test_local_housing_workflow.py tests/unit/test_policy_funding_workflow.py --tb=short
```

Actual result: **242 passed** in 38.36 seconds, exit 0. Log:
`.local/reviews/monthly-update/storage-native-scope-tests-final.log`.
Scoped Ruff on the writer and both targeted test files reported
**All checks passed**, exit 0. These checks used the same local/development and
root-override removal settings described above. No full suite was run here.

Final writer SHA-256:
`1d1979fb4b0a9202ee1aface4434e61b7c086859f7cd5b1362e5ee6932dd5d4a`.
New native regression file SHA-256:
`3d06bd2e81e407d1d837bbe3051ce4d5da0b6a17332debd3b6f43921276a193d`.

## Verification limits and continuation questions

- The independent archive false positive and proven native-disagreement identity
  cases are repaired with scoped regression verification recorded above. No live
  collection retry, browser/port
  operation, dependency change, commit or full suite was performed by this
  investigation. The parent owns any new collection and acceptance evidence.
- The 100-bundle batch exercises the writer with tiny in-memory files on this
  Windows workspace. It does not reproduce live payload sizes, background
  interference, original timing or the original process state.
- The controlled archive mutation is explicit test input. Natural Windows
  mutation between the original guard snapshots was not observed.
- Transient lstat/native disagreement is now established in the real local
  funding fixture workflow. Its internal runtime/OS/filesystem cause remains
  unproven. The original first failure's fields remain unknown. A future full
  content/read fingerprint failure requires separate evidence and adjudication;
  this repair does not normalize those fingerprints.
- Product/operations question: should diagnostic mismatch evidence be persisted
  alongside failed local collection logs before the next live retry? Keep
  collection outcome status failed until validated run readback succeeds.
- AGENTS canonical report registration is delegated to the parent to avoid a
  concurrent edit of project instructions. Future investigation should read this
  report and monthly progress first, compare the current writer bytes, then
  update this report with actual mismatch evidence or newly executed acceptance.

## Update log

- 2026-10-08: recorded the live failure evidence limit, a 100/100 natural fixture
  result, the deliberately induced archive-only red test, and a conditional
  minimal candidate without changing production defenses or failed material.
- 2026-10-08 follow-up: implemented the parent-approved archive-only identity
  normalization and safe mismatch diagnostics; preserved all other protections,
  recorded maintained RED/GREEN tests and scoped verification. The actual
  original live mismatch remains unknown.
- 2026-10-08 later retry: recorded exact live `0x10000000` delta, primary API
  references, successful natural native-disagreement reproduction, and both raw
  variants. Implemented the parent-approved twice-native-verified identity-only
  reconciliation; natural fixture publication/readback succeeded, final scoped
  tests passed 242 and Ruff passed. Parent owns the next live retry and outcome.
