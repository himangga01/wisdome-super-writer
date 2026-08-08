# Generic Evidence Extractor Contract

## 범위

이 계약은 PDF·독립 이미지가 아닌 HTML, 구조화 데이터, 스프레드시트, HWPX, legacy HWP의
격리 PDF 변환, 허용된 브라우저 캡처, 미디어 메타데이터와 수동 입력을 `other_derived`
EvidenceAsset으로 만드는 경로다.
문서·이미지 OCR은 이 계약에서 실행하지 않고 `evidence-extractor.md`의 PaddleOCR 계약만
사용한다.

## 이벤트 경계

생산자는 DB에 `GenericExtractionAttempt(state=queued)`를 먼저 만든 뒤
`evidence.other_extract_requested`를 보낸다. payload는 다음 ID·지문만 가진다.

| 필드 | 계약 |
|---|---|
| `run_id`, `run_source_item_id` | CollectionRun과 그 실행의 SourceItem join UUID |
| `generic_extraction_attempt_id` | 사전 생성된 UUID |
| `source_item_id`, `input_asset_id?` | 불변 입력 참조 |
| `profile_snapshot_id` | DB의 승인된 불변 ExtractionProfileSnapshot UUID |
| `profile_material_hash` | snapshot 전체 nullable material의 NFC+RFC 8785 JCS SHA-256 |
| `engine`, `extractor_version`, `config_hash` | 승인 구현 snapshot |
| `validation_mode` | `deterministic/calibrated/manual` |
| `calibration_profile_key/version/hash` | calibrated일 때 필수, 그 외 null |
| `fingerprint_schema_version`, `extraction_fingerprint` | `v1`과 정규 작업 지문 |

성공·저신뢰 소비자는 attempt와 EvidenceAsset을 같은 트랜잭션에 저장하고 outbox로
`evidence.other_ready`를 만든다. ready payload는 `run_id`, `run_source_item_id`,
`source_item_id`, `generic_extraction_attempt_id`,
`evidence_asset_id`, `engine`, `locator_type`, `validation_mode`, `result_checksum`,
`low_confidence_reasons_hash?`, `calibration_profile_key/version/hash?`,
`extraction_fingerprint`를 가진다.
소비자는 DB의 provenance와 아래 매트릭스를 다시 검증한다. 실패 attempt는 ready 이벤트나
EvidenceAsset을 만들지 않는다.
요청·ready의 `run_id`, `run_source_item_id`, `source_item_id`는 attempt가 참조하는
RunSourceItem 계보와 정확히 같아야 하며 payload 값만으로 실행 소유권을 정하지 않는다.

## Engine·locator·검증 매트릭스

| engine | 허용 locator | 허용 validation mode |
|---|---|---|
| `html_parser` | `html_dom` | `deterministic/calibrated` |
| `structured_parser` | `structured_path` | `deterministic/calibrated` |
| `spreadsheet_parser` | `spreadsheet_cell` | `deterministic/calibrated` |
| `hwpx_parser` | `hwpx_path` | `deterministic` |
| `legacy_hwp_converter` | `hwp_conversion` | `deterministic` |
| `browser_capture` | `html_dom/image_region` | `deterministic/calibrated` |
| `media_parser` | `media_time/image_region` | `deterministic/calibrated` |
| `manual_entry` | `manual` | `manual` |

`deterministic/manual`은 confidence가 null이다. `calibrated`는 승인된 골든 표본 ID, metric,
임계값과 평가 결과를 담은 불변 manifest의 key/version/SHA-256이 모두 있어야 하며 그때만
0..1 confidence를 기록한다. enum 선언만으로 calibrated가 될 수 없고, 비활성·누락·hash
불일치 profile은 실행 전에 `failed/calibration_profile_invalid`로 끝난다.

producer는 신규 attempt 생성 시 `approval_state=approved`인 ExtractionProfileSnapshot ID와
hint를 함께 고정한다. worker는 snapshot ID로 DB 기준 행을 다시 읽고 event의 engine,
extractor/package/runtime/pipeline version, implementation manifest, config와 calibration
key/version/hash, 저장된 manifest checksum과 로컬 parser 설정을 대조한다. 불일치는
추론·파싱 전에 실패하며 event 값만으로 profile을 승인하지 않는다.

HWPX는 ZIP central directory를 먼저 검사해 압축 폭탄/경로 탈출/중첩 archive/허용 밖 MIME과
active content를 차단한 뒤 로컬 XML만 파싱한다. locator는 section XML path, paragraph ID,
table/row/column/cell과 embedded object ID를 보존하고 외부 link를 따라가지 않는다.

legacy HWP는 원본을 직접 해석 결과로 게시하지 않는다. 승인된 `legacy-hwp-v1` profile의 pinned
converter binary/implementation checksum을 no-network, read-only input, disposable filesystem,
CPU/memory/time/process 제한 sandbox에서 실행해 PDF와 conversion report를 만든다. macro/OLE/
external link는 실행하지 않는다. output PDF checksum/MIME/page count와 report hash가 유효할
때만 converted attachment EvidenceAsset을 만들고, 이어 새 DocumentExtraction이 그 PDF를
native extraction 또는 **PaddleOCR PP-StructureV3**로 인식한다. converter 미설치·손상·timeout·
지원 불가 문서는 `manual_required`이며 부분 text나 자동게시 결과를 만들지 않는다.

T015의 legacy HWP 경계는 일반 worker가 새 queue/event 없이 synchronous UDS client로
`network_mode: none` sidecar를 호출하는 구조다. 고정 magic `WSHWP001`, protocol version과
bounded length framing을 쓰며 request의 attempt ID, `generation=1`, nonce, input basename/
checksum/byte size와 resource bounds를 final report가 exact echo한다. 이 generation은 T015
호환 identity일 뿐 DB stale-result fence가 아니며, 영속 generation/lease/fencing은 T016 범위다.

sidecar는 DB·Redis·MinIO 환경변수/credential이 없고 read-only rootfs/input/manifest, private
bounded tmpfs, root 신뢰 supervisor와 `cap_drop: ALL` 뒤 CHOWN/KILL/SETUID/SETGID만 재부여,
no-new-privileges, PID/memory/CPU/time/file-size/
open-file/process 제한을 함께 적용한다. basename만 `O_NOFOLLOW`로 열어 regular file,
single hardlink, size와 hash를 검사한 뒤 private tmpfs로 복사한다. 기본 Compose는 gVisor를
강제하지 않으며 지원 Linux 운영환경에서만 선택적 override를 둘 수 있다.

UDS supervisor는 UID/GID 0이며 startup에서 UID/GID, NoNewPrivs와 exact effective/permitted capability
mask, zero inheritable/ambient를 확인한다. untrusted exact-CLI child는 supplementary group과 모든
capability 없이 65533으로, qpdf validator는 65531로 내리고 exec 이후 identity probe로 다시 확인한다.
socket/input root는 supervisor만 접근한다. supervisor-owned input directory는 child group read-only,
child-owned output directory는 write-only trust 영역으로 분리한다. PR_SET_CHILD_SUBREAPER와 descendant
sweep으로 setsid escape까지 TERM→KILL→reap하고 zero descendant를 확인한 뒤, untrusted PDF를 새
supervisor inode로 streaming snapshot한다. validator는 root:validator 0440 snapshot path만 reopen하며
supervisor만 final report를 만든다. one-shot bootstrap은 named socket/input volume을 root:root 0700으로
초기화한다.

stdout/stderr는 pipe를 동시에 drain해 합계 1 MiB를 넘는 즉시 전체 job group을 종료한다. request는
client `SHUT_WR` 뒤 exact EOF여야 하고 response는 header/report/PDF를 64 KiB 이하 chunk로 보낸다.
tmpfs 768 MiB는 128 MiB input + 300 MiB untrusted PDF + 300 MiB trusted snapshot + bounded log/metadata
여유이며 container memory/memswap은 1536 MiB로 같다. 기본 extract worker concurrency는 1이고
scale-out은 worker마다 전용 socket/input volume/converter가 있을 때만 허용한다.

변환기는 `rhwp 0.8.2` full commit
`9b16aa9e23f476e2b335d7c029fc9f24a199d63c`, Rust 1.93.1과 locked `Cargo.lock`로 고정하고
qpdf `11.3.0-1+deb12u1`은 pinned Bookworm runtime과 배포/ABI 경계를 맞추고 binary와 loaded
library bytes를 manifest에 포함해 PDF 구조와 실제 page count를 검증한다. 자동 secondary converter fallback과
기본 `--text-as-paths`를 금지한다. 승인 font bytes만 쓰며 warning, missing-font,
font substitution 또는 fallback 징후는 terminal 실패다.

manifest schema v1은 rhwp/qpdf/Python interpreter와 runtime loaded library/font/fixed fontconfig,
rhwp LICENSE/Cargo.lock/build metadata/Compose security policy/wrapper config의 normalized absolute
path, SHA-256과 byte size를 기록한다. expected manifest SHA-256은 release/deploy 외부 입력이고
profile loader가 read-only manifest bytes와 exact compare한다. image 내부 runtime material로
expected hash를 덮어쓰는 self-approval은 금지한다.

JSON duplicate key/NaN/unknown field/bool-as-int, 잘못된 magic/version/length/trailing bytes,
identity·manifest·policy·checksum·size·MIME·qpdf page count 불일치는 fail closed한다. socket/
daemon 단절만 retryable이다. unsupported/warning/missing-font, exit 20/21/22 또는 tamper는
permanent `failed`이며 converted/derived EvidenceAsset, `evidence.other_ready`, DocumentExtraction을
0건 만든다. raw input은 감사용으로 보존하되 `manual_required/publishable=false`로 격리한다.
run은 typed counter와 failure count를 보존하고 `manual_required` recovery로 넘긴다.
`legacy-hwp-v1@1.1.0`은 immutable
`golden_corpus_approved=false` draft다. T032가 acceptance artifact와 새
`legacy-hwp-v1@1.2.0(golden_corpus_approved=true)`을 만들고 승인한 뒤에도 1.1.0은 retire하지 않고
immutable superseded draft로 보존한다. golden=true profile은 acceptance object key/version/SHA-256,
target OCI digest, converter manifest hash, schema와 all-pass를 모두 결속해야 한다. 실제 artifact
fetch/signature trust root, hermetic vendored/offline build, pinned source/vendor/deb/SBOM과 signed release
attestation은 T032/release material blocker다. 현재 activation gate는 reference 형식과 runtime
self-report가 그럴듯해도 항상 false다. T032가 exact versioned object bytes를 fetch하여
SHA-256·schema·subject·OCI·manifest·all-results를 검증하고 외부 trust root로 release signature를
검증하는 구현을 제공하기 전에는 golden=true profile import/verify/converter construction을 모두
거절한다. runtime self-report와 hard-coded policy는 admission 증거가 아니다.

## 멱등성과 상태

fingerprint v1은 run source item ID, source/input asset ID와 checksum, profile snapshot ID와
profile material hash,
engine, extractor version, config hash,
validation mode, calibration profile hash의 null 표현까지 Unicode NFC 후 RFC 8785 JCS로
직렬화한 SHA-256이다. 같은 입력 범위와 fingerprint는 DB 고유 제약으로 하나만 실행한다.

- `succeeded`: 결과 checksum과 locator가 유효하며 EvidenceAsset 생성 가능
- `low_confidence`: calibrated 결과와 reason hash를 보존하고 EvidenceAsset은
  `manual_required/publishable=false`; 승인 후에도 attempt 상태는 불변
- `failed`: GenericExtractionAttempt와 RunStep에 오류를 기록하고 EvidenceAsset·ready 없음

## 검토·보안

저신뢰 결과의 review subject v1은 attempt ID/fingerprint, result checksum, reason hash와
calibration profile hash를 포함하고 mutable 게시·검토 projection은 제외한다. 관리자
결정은 Admin API의 subject version/hash 및 expected latest decision CAS를 통과해야 한다.
profile 승인/retire 결정은 검증 보고서 object key/version/hash를 decision hash와 audit metadata에
동결한다. approved/retired report API는 latest decision envelope와 profile projection을 exact
비교하고 불일치하면 409다. 기존 결정은 migration 시 현재 projection으로 backfill하며, projection이
불완전하면 migration을 중단하고 당시 envelope를 복원했다고 주장하지 않는다.

워커는 발행 자격 증명에 접근하지 않고, 허용된 객체와 parser/capture capability만 가진다.
매크로·실행 파일, 로그인·유료벽·CAPTCHA 우회와 임의 외부 URL fetch는 금지한다. 원문,
추출 본문, 셀 전체와 이미지 bytes는 이벤트나 로그에 넣지 않는다.

low-confidence attempt는 원문을 복제하지 않은 impact, code, field/block ref, 관측
confidence, threshold와 비민감 설명의 전체 정규 reason manifest를 객체 저장소에 보존하고
key/version/hash를 attempt에 기록한다. high-impact 우선 뒤 scope/ref/code/value/threshold/
message tie-break로 안정 정렬한 전체 배열을 EvidenceAsset과 승인 화면에 제공하며 그 JCS
hash가 provenance reason hash와 일치해야 한다. 전체 배열을 로드·검증하지 못하면 승인을
금지한다.

## 복수 출처 시각화 렌더러 경계

시각화는 단일 RunSourceItem을 요구하는 GenericExtractionAttempt와
`evidence.other_extract_requested/ready`를 사용하지 않는다. producer는 ArticleRevision 아래
VisualizationRender와 stable-order VisualizationInputClaim/VisualizationInputEvidence join을
먼저 만들고 `visualization.render_requested`에는 render ID만 보낸다. worker는 join의 모든
input claim/evidence ID·checksum, 단위·기준일·결측/반올림·변환식 transform spec과
renderer/version/config hash를 정렬 manifest로 다시 읽는다. 결과·`visualization.render_ready`
outbox를 같은 트랜잭션에 저장한다.

새 수치를 추론하거나 원본 권리를 확대할 수 없고 output checksum·rights manifest·alt text를
VisualizationRender와 `visualization_derived` EvidenceAsset에 함께 저장한다. 생성 자산은
`visualization_render_id`만 provenance FK로 가지며 단일 source/run-source와 document/generic
FK는 null이다. locator의 render/input/transform hash는 DB join과 같아야 한다.

## 계약 테스트

- 각 engine의 허용 locator 양성 예와 모든 교차 불일치 음성 예
- `manual_entry+calibrated`, calibrated profile 누락/비활성/hash 불일치 거절
- 존재하지 않거나 draft/retired인 profile snapshot, event/config hint 불일치 거절
- deterministic/manual confidence 숫자 거절, deterministic/manual `low_confidence` 상태 거절,
  calibrated confidence 누락 거절
- 같은 fingerprint 100회 전달 시 attempt와 ready side effect 하나
- failed attempt의 EvidenceAsset·`evidence.other_ready` 0건
- low-confidence attempt의 승인 없는 publishable 응답 거절과 stale review CAS 409
- visualization input/transform/renderer/output hash 재현, 단위·기준일·출처 표시와 alt/rights
  필수; input 값/transform 변조 시 output 재사용·게시 거절
- 복수 source input에서도 생성 EvidenceAsset의 임의 단일 source 귀속이 없고 input join 전체가
  source manifest에 포함되며 generic extraction event/attempt가 0건
- HWPX zip-slip/zip-bomb/active-content fixture 거절, paragraph/table/cell locator 재현
- legacy HWP sandbox의 network/process escape 0건, output PDF/report checksum 변조 거절,
  변환 성공 뒤 인식 엔진은 native PDF 또는 PaddleOCR뿐이며 converter 실패 시 자동게시 0건

## English / AI-readable — T015 legacy HWP isolation

- The normal extraction worker synchronously calls a credential-free Docker sidecar over a
  bounded Unix-domain-socket protocol. The sidecar uses `network_mode: none`; no HWP-specific
  Celery queue or domain event is added.
- The request and response use fixed `WSHWP001` magic/version/length framing and strict JSON.
  Duplicate keys, NaN, unknown fields, bool-as-int, identity mismatch, or trailing bytes fail
  closed. The echoed T015 `generation=1` is compatibility identity, not DB fencing; T016 owns
  persisted generations, leases, and stale-result fencing.
- The sidecar accepts a basename-only, `O_NOFOLLOW`, regular, single-link input whose size and
  checksum match, copies it into private tmpfs, and runs the exact no-network wrapper CLI.
  Rootfs/input/manifest are read-only; tmpfs, UID, capabilities, no-new-privileges, PID, memory,
  CPU, wall-time, file-size, descriptor, and process limits are bounded at container and process
  boundaries. Base Compose does not mandate gVisor; a supported Linux deployment may add it as
  an override.
- The trusted UID 0 supervisor has exactly CHOWN/KILL/SETUID/SETGID. Parser 65533 and validator
  65531 clear all capability sets and are verified after exec. A child subreaper sweeps escaped
  descendants, input/output directories are separated, and qpdf reopens only a validator-readable
  supervisor snapshot. Requests require client SHUT_WR/exact EOF; logs and response chunks are hard
  bounded. Default extract-worker concurrency is one for one converter.
- Converter material is `rhwp 0.8.2` commit
  `9b16aa9e23f476e2b335d7c029fc9f24a199d63c`, Rust 1.93.1, locked Cargo, pinned qpdf, approved
  fonts, loaded libraries, wrapper, and policy config. No automatic fallback and no default
  `--text-as-paths` are permitted.
- The release-pinned expected manifest SHA-256 is an external deployment trust input. The
  profile loader exact-compares it with read-only manifest bytes and never replaces it with a
  runtime-computed self-approval value.
- Only a warning-free, no-font-substitution, qpdf-validated PDF with a complete matching report
  may create one converted EvidenceAsset and a follow-up DocumentExtraction using the verified
  page count. Follow-up engines are native PDF or PaddleOCR only; stdout/partial text is never
  evidence.
- Only UDS infrastructure disconnect is retryable. Unsupported input, warnings, missing fonts,
  fallback, exact exits 20/21/22, or tamper is permanent and produces zero converted/derived
  EvidenceAsset, `evidence.other_ready`, or DocumentExtraction. Raw input remains quarantined and
  non-publishable. Profile 1.1.0 remains an immutable superseded draft after T032 approves 1.2.0.
  Golden activation requires the immutable acceptance object, target OCI and converter manifest
  digests, schema, and all-pass. Artifact signature trust and hermetic signed build inputs remain
  mandatory T032/release blockers. The activation gate remains unconditionally false until T032
  implements exact versioned artifact-byte/schema/subject/result verification and release-signature
  verification against an external trust root. Runtime self-report and hard-coded policy are not
  admission evidence.
- Profile approve/retire decisions freeze the report object key/version/hash in the decision hash
  and audit metadata. The approved/retired report API returns 409 if the latest decision envelope
  differs from the profile projection. Migration 0004 backfills legacy chains only from the
  available current projection and aborts on incomplete material; it does not claim unavailable
  historical reconstruction.
