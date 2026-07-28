# Wisdome Super Writer 남은 작업 인수인계

**작성일**: 2026-07-17  
**기준 브랜치/커밋**: `main` / `34d81fe`  
**기준 문서**: `spec.md`, `plan.md`, `tasks.md`, Constitution 1.0.1  
**정본 작업 목록**: [`tasks.md`](tasks.md)

이 문서는 다음 작업자가 구현을 바로 이어가기 위한 실행용 요약이다. 완료 여부와 task ID는
항상 `tasks.md`를 정본으로 사용한다. 이번 수렴 감사에서 발견한 모든 잔여 작업은 기존
`T039~T057`에 이미 추적되고 있으므로 새 task를 추가하지 않았다.

## 1. 현재 상태

구현된 기반:

- Django/Celery/PostgreSQL/Redis/MinIO 기반 프로젝트와 관리자 전용 콘솔
- 주제·출처 registry, collection run, evidence, article revision 모델
- native PDF 및 PaddleOCR adapter 골격, HWP/HWPX/HTML/spreadsheet 추출기
- WordPress 대표 원문과 Google Blogger 보조 배포 adapter
- publication intent, target별 승인, 미리보기, 재시도와 reconcile 기반
- cron schedule, `skip`/`queue_one`, 전역 kill switch
- 정정 case와 발행 계획 골격, 보존 batch 골격, append-only AuditEvent

현재 기본 확인 결과:

- `python src/manage.py check`: 통과
- `python src/manage.py makemigrations --check --dry-run`: 변경 없음
- Python compile: 통과
- 자동화 테스트: **미구현** (`tests/` 디렉터리 없음)
- 로컬 DB의 두 topic registry와 source snapshot은 감사 시점에 모두 `draft`이므로 실제 run은
  fail-closed 상태이다.

## 2. 반드시 유지할 결정

- 작업에는 **Spec Kit 스킬만 사용**한다. Superpower 및 marketing 스킬은 호출하지 않는다.
- 사용자는 Django staff 관리자 한 종류만 지원한다.
- OCR은 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) `PP-StructureV3`를 사용한다.
  다른 OCR 엔진으로 자동 우회하지 않는다.
- WordPress가 대표 원문과 canonical URL을 먼저 확정하고 Blogger가 그 URL을 포함해 후속 발행한다.
- 모든 사실 주장과 게시 시각 자료는 독자가 접근 가능한 출처, 권리 상태, locator/alt text를 가진다.
- 자동발행은 승인된 registry/profile, 통과한 품질 gate, 고정된 target validation/activation이
  모두 일치할 때만 허용한다.
- 테스트 구현은 사용자 지시에 따라 기능 구현 뒤로 미루되, 각 batch마다 `check`, migration check,
  compile은 수행한다.

## 3. 권장 실행 순서

병렬 작업이 가능하면 다음 wave로 나눈다.

| Wave | 작업 | 목적 |
|---|---|---|
| A | `T046`, `T049`, `T052` | 승인된 입력과 격리 실행 기반 확정 |
| B | `T045`, `T050`, `T053` | 실제 수집·OCR 검토·worker 시작 gate 완성 |
| C | `T047`, `T051` | 중복/충돌/속보 검증과 편집 품질 완성 |
| D | `T055`, `T056`, `T057` | 예약 자동발행, 정정, 보존·운영 마감 |
| E | `T039~T044` | release-blocking 자동화 검증 |

핵심 의존 관계:

```text
T046 ──> T045 ──> T047 ──> T051 ──> T055 ──> T056
  │          │        │        │        │        │
  └──────────┴────────┴──> T053│        └────────┴──> T057
T049 ──> T050 ────────────┘
T052 ─────────────────> T053
T045~T057 완료 ───────────────────────────────> T039~T044
```

## 4. 상세 잔여 작업

### T046 — 출처 정책과 불변 Registry 기반

**상태**: 부분 구현이지만 Constitution II 및 계획 계약과 충돌. 가장 먼저 처리한다.

현재 문제:

- `config/source-registry/*.json`에 `termsUrl`, `robotsUrl`, `licenseUrl` 근거가 없다.
- [`src/adapters/sources/http.py`](../../src/adapters/sources/http.py)는 HTTPS, host, DNS, timeout,
  12 MiB만 확인하며 robots, 약관, 실제 rate/poll limit, `Retry-After`, 허용 MIME을 강제하지 않는다.
- [`src/apps/topics/services.py`](../../src/apps/topics/services.py)는 `SourceDefinition`을
  `update_or_create`로 변경하고 snapshot/version을 단조 증가시키지 않는다.
- registry 승인에 재인증 proof, expected manifest/head CAS, 사유, mutation/decision, AuditEvent가 없다.
- OpenAPI의 registry draft/membership/decision API와
  `verify_source_registry_snapshots --require-approved-mvp` 명령이 없다.

남은 구현:

1. source manifest에 약관·robots·license·access evidence, MIME, poll/rate, adapter version/hash를
   승인 필수 material로 추가한다.
2. source별 robots allow, 분산 rate/concurrency 제한, poll 간격, bounded backoff,
   `Retry-After`, redirect host와 MIME 검사를 실제 HTTP 요청 앞에 강제한다.
3. 불변 SourceDefinitionSnapshot, 단조 version, full carry-forward registry draft와 head CAS를 구현한다.
4. append-only mutation/decision, idempotency, reauthentication, reason, AuditEvent를 한 transaction에 묶는다.
5. 승인/retire된 snapshot은 model/service/admin에서 변경 불가하게 만든다.
6. OpenAPI와 일치하는 API 및 배포 검증 management command를 구현한다.

완료 조건:

- robots deny는 외부 HTTP 요청 없이 차단된다.
- stale CAS는 409, 동일 request replay는 같은 결과를 반환한다.
- 재인증 없는 approve/retire는 거부되고 정확히 한 AuditEvent가 남는다.
- 한 source 변경 시 나머지 membership이 새 registry version에 그대로 carry-forward된다.
- 정확히 승인된 두 MVP registry가 있을 때만 verify command가 0을 반환한다.

### T045 — 실제 주제별 수집기와 변경 상태

**상태**: 부분 구현. RSS 시간창 필터 외 핵심 실수집은 미구현.

현재 문제:

- [`src/adapters/sources/housing/__init__.py`](../../src/adapters/sources/housing/__init__.py)의
  ApplyHome/LH adapter는 빈 `PublicHtmlAdapter` subclass다.
- `PublicHtmlAdapter.collect()`는 목록 page 전체를 SourceItem 하나로 저장하고 상세, pagination,
  `since/until`을 처리하지 않는다.
- `OpenDataJsonAdapter`는 첫 payload의 `items`만 읽고 pagination, 기간, API secret ref가 없다.
- `SourceItem` status는 항상 `active`, discovery는 `new_version|unchanged`뿐이다.
- MOTIE/KRX/SIA도 일반 목록 page 단위이며 Samsung/SK hynix RSS만 item 단위다.

남은 구현:

1. ApplyHome, LH, MOTIE, KRX, SIA별 목록→상세→첨부 parser를 구현한다.
2. 안정적인 external ID/canonical URL, 게시·수정 시각, pagination/cursor, 기간 필터,
   early stop/max pages와 구조 변경 감지를 추가한다.
3. PDF/HWP/HWPX/XLS(X)/이미지/차트/표의 content type, URL, 권리 metadata를 evidence 단계로 전달한다.
4. `corrected`, `retracted`, `unavailable`, `restored` 상태와 append-only source version lineage를 구현한다.
5. 출처별 고정 fixture와 parse failure 격리를 준비한다.

완료 조건:

- 모든 enabled source가 목록 item별 상세 record를 만든다.
- window 밖 자료는 0건이며 두 page 이상을 안정적으로 순회한다.
- 같은 자료 재수집은 `unchanged`, 본문 변경은 새 version, 철회/복원은 정확한 상태가 된다.

### T047 — 중복·충돌·글 identity·반도체 속보 검증

**상태**: 기능적으로 미구현. `EventCluster` schema 골격만 존재한다.

현재 문제:

- [`src/apps/editorial/models.py`](../../src/apps/editorial/models.py)의 `EventCluster`는 사용처가 없다.
- article identity가 `{run UUID, topic}` hash라 동일 공고/사건도 run마다 새 글이 된다.
- 주택은 항상 `housing_notice`, 반도체는 항상 `semiconductor_daily_digest`다.
- generator가 모든 claim을 `fact`로 만들고 선택·병합·충돌·제외 이유를 기록하지 않는다.

남은 구현:

1. origin/syndication identity, EventClusterItem, immutable verification, conflict/selection/exclusion 모델과
   migration을 추가한다.
2. housing은 공식 notice/correction ID를 중심으로 묶고 제목만으로 merge하지 않는다.
3. 최신 정정 PDF → 운영기관 상세 → 구조화 API → 집계 순서로 충돌을 해결하고 이유를 보존한다.
4. semiconductor는 사건 주체·행위·시간·공식 ID와 원 전재 기준으로 clustering한다.
5. 정책의 4개 속보 범주와 `공식 1차 1곳 또는 독립 origin 2곳` 조건을 검증한다.
6. 미달 사건은 `daily_digest_candidate/held`, 통과 사건만 `verified_breaking`으로 만든다.
7. housing identity는 notice/correction ID, breaking은 cluster key, digest는 KST date+policy version으로 고정한다.

완료 조건:

- 동일 notice는 여러 run에서 DraftArticle 하나만 유지한다.
- 제목만 같은 다른 공고는 합쳐지지 않는다.
- 같은 기사의 전재 두 건은 독립 출처 하나로 계산한다.
- company-only 또는 단일 secondary 자료는 속보로 발행되지 않는다.
- 충돌 선택·제외 결과가 DB snapshot으로 재현 가능하다.

### T049 — PaddleOCR 모델 bootstrap과 실제 checksum

**상태**: 부분 구현, 현재 profile은 운영 실행 불가.

현재 문제:

- profile은 외부 model manifest를 요구하지만 model bootstrap/volume population service가 없다.
- `PADDLEOCR_MODEL_MANIFEST_ROOT`가 Compose와 `.env.example`에 없다.
- layout/text/table/formula/chart 전체 model directory binding과 checksum 생성기가 없다.
- adapter가 일부 model만 전달하고 생성 manifest의 `schema_version`과 hash가 profile material과
  일치하지 않을 수 있다.

남은 구현:

1. 공식 PaddleOCR/PaddleX model을 build/deploy 단계에서 받는 `ocr-model-bootstrap`을 만든다.
2. model 파일별 SHA-256, package/runtime/config, schema version을 실제 bytes에서 생성한다.
3. 전체 PP-StructureV3 model directory를 명시적으로 adapter에 binding한다.
4. read-only volume을 worker에 제공하고 runtime model download/network fallback을 차단한다.
5. manifest root와 고정 환경 변수를 `.env.example`, Compose, README에 동기화한다.

완료 조건:

- network가 없는 OCR worker에서 model 로딩과 골든 문서 추출이 성공한다.
- model byte 하나가 바뀌면 startup/verify가 fail-closed 한다.
- `verify_ocr_manifest`와 approved profile 검증이 실제 checksum 기준으로 통과한다.

### T050 — profile 승인·locator·검토 transaction 마감

**상태**: 대부분 구현됐으나 부분 결함과 실제 검증이 남음.

이미 존재하는 기반:

- 실패한 verification report 승인 차단
- profile CAS와 model gate
- OCR locator 필수화
- 저신뢰 검토 transaction/outbox 기반

남은 구현:

1. 중복 `@transaction.atomic`을 정리한다.
2. 문서 aggregate가 DB `publishable` 값을 갱신한 뒤 객체를 refresh하지 않아 outbox payload가
   stale할 수 있는 경로를 수정한다.
3. T049의 실제 model/report로 approve/revoke/CAS와 locator 누락/저신뢰 차단을 검증한다.
4. review decision, projection, outbox가 한 transaction에서 정확히 한 번 반영되는지 확인한다.

완료 조건:

- failed report, locator 누락, 저신뢰 고위험 값은 승인/자동발행이 불가능하다.
- stale review subject는 409이며 outbox payload가 최종 projection과 일치한다.

### T052 — legacy HWP 격리 sandbox

**상태**: 미구현. adapter가 외부 wrapper를 호출하는 골격만 있다.

남은 구현:

1. 실제 `/usr/local/bin/wisdome-hwp-sandbox`와 converter image/toolchain을 만든다.
2. no-network, read-only rootfs/input, 제한된 writable tmp, CPU/memory/time/process/file-size 한도를 강제한다.
3. converter binary/image/config의 build-time manifest와 실제 SHA-256 생성기를 만든다.
4. zero hash인 `legacy-hwp-v1.json`을 실제 manifest path/hash로 교체한다.
5. 변환 성공 PDF만 native/Paddle routing에 넘기고 부분 text는 폐기한다.

완료 조건:

- 현재 profile import의 fail-closed 상태가 실제 manifest로 해소된다.
- 손상/폭탄/timeout fixture가 sandbox 밖 자원과 network에 접근하지 못한다.

### T053 — worker 시작 gate와 비밀 격리

**상태**: 미구현.

현재 문제:

- collect/scheduler/OCR worker가 approved Registry/Profile 검증 없이 시작한다.
- OCR worker가 전체 `.env`를 받아 WordPress/Blogger 발행 비밀에 접근할 수 있다.
- worker별 외부 network/egress 경계가 없다.

남은 구현:

1. T046/T049/T052 verify command를 실행하는 worker startup preflight를 만든다.
2. 실패하면 worker가 queue를 consume하기 전에 non-zero로 종료하도록 한다.
3. service별 환경 변수를 allowlist하고 OCR worker에서 publisher/OAuth 자격 증명을 제거한다.
4. OCR/HWP service에 필요한 DB/Redis/S3만 허용하는 network 경계를 구성한다.

완료 조건:

- draft/retired/mismatch registry/profile이면 collect/OCR/scheduler가 시작하지 않는다.
- OCR container 환경과 filesystem에서 발행 secret을 조회할 수 없다.

### T051 — 정책 기반 편집과 수동 개정 재검증

**상태**: 부분 구현. 모델은 있으나 핵심 동작이 없다.

현재 문제:

- [`src/apps/editorial/services.py`](../../src/apps/editorial/services.py)는 모든 publishable evidence를
  사용하고 세 가지 단순 check만 수행한다.
- `config/editorial-policies/*.json`을 읽거나 version/hash를 고정하지 않는다.
- generator가 모든 claim을 `fact`로 생성한다.
- `VisualizationRender`는 골격뿐이며 본문 배치, source/rights/alt 검증이 없다.
- 수동 revision은 `quality_state=pending`으로 생성된 뒤 재검증 경로가 없다.

남은 구현:

1. topic별 editorial policy loader와 immutable version/hash를 generation attempt에 고정한다.
2. `fact`, `company_claim`, `interpretation`, `outlook`을 분리하고 본문 섹션과 주의 문구를 정책화한다.
3. 청약 날짜·가격·자격 등 high-impact field에 primary evidence/locator/conflict gate를 적용한다.
4. 시각자료의 본문 위치, provenance, rights, alt text, caption을 revision에 연결한다.
5. source coverage, 사실/해석 분리, 고위험 값, 권리, 중복/충돌, 읽기 품질을 blocking check로 만든다.
6. 관리자 수정 시 claim을 다시 추출·연결하고 모든 gate를 재실행하는 task/API를 구현한다.

완료 조건:

- pending/failed manual revision은 preview/approval/publish로 넘어갈 수 없다.
- 모든 high-impact claim과 게시 visual이 승인된 evidence/locator/rights에 연결된다.

### T055 — 예약 Run과 자동발행의 최종 상태

**상태**: 부분 구현. schedule→intent→attempt 연결은 있으나 종료 집계가 없다.

이미 존재하는 기반:

- validated-auto schedule이 draft 완료 후 publication task를 호출한다.
- 자동 intent, target별 approval, publication attempt 생성 경로가 있다.
- `queue_one` release helper가 있다.

남은 구현:

1. attempt 전체 결과를 집계해 `CollectionRun.PUBLISHING`을 `COMPLETED` 또는 `FAILED`로 종료한다.
2. 성공/영구 실패 모든 terminal 경로에서 `completed_at`, error summary와 영향 target을 기록한다.
3. 모든 terminal 결과에서 `release_waiting_for_topic()`을 호출해 `queue_one`을 해제한다.
4. [`src/apps/publishing/tasks.py`](../../src/apps/publishing/tasks.py)의 `finished_at` 사용을
   `CollectionRun.completed_at`으로 교정한다.
5. dispatch 시 schedule의 validation/activation refs를 run/dispatch에 불변 snapshot으로 고정하고
   automation이 mutable schedule을 다시 읽지 않게 한다.
6. manual approval schedule도 awaiting approval→publish→terminal lifecycle을 끝까지 연결한다.

완료 조건:

- WordPress 성공 후 Blogger 대기 중에는 run이 terminal이 되지 않는다.
- 전체 성공 시 COMPLETED, 복구 불가 실패 시 FAILED이며 대기한 한 건만 다음 run으로 시작한다.

### T056 — 정정·철회 end-to-end orchestration

**상태**: 부분 구현. detector와 publication plan만 있고 호출/검증/개정이 없다.

현재 문제:

- `detect_correction_cases()`를 호출하는 task/beat가 없다.
- 관리자 verify/reject API/UI가 없다.
- 검증된 변경으로 새 revision을 만들고 T051 gate를 재실행하지 않는다.
- retraction 시 Blogger가 WordPress terminal보다 먼저 처리될 수 있다.
- channel render의 correction history가 채워지지 않는다.

남은 구현:

1. T045 change lineage를 감시하는 주기 task와 idempotent case 생성을 연결한다.
2. diff/evidence를 보여주는 관리자 verify/reject API/UI와 AuditEvent를 구현한다.
3. 검증된 case에서 corrected revision을 만들고 T051 revalidation을 통과시킨다.
4. 공개 본문 상단의 정정/철회 이력과 source diff를 channel render에 넣는다.
5. update, mark-withdrawn, unpublish 모두 WordPress terminal 후 Blogger가 실행되도록 dependency를 고정한다.
6. dispatch, retry/reconcile, 30분 SLA timestamp와 실패 복구를 연결한다.

완료 조건:

- 감지 자체로 외부 글을 변경하지 않고 관리자 검증 이후에만 실행한다.
- 두 채널의 기존 remote post ID/URL을 유지하면서 같은 최신 사실과 이력을 반영한다.

### T057 — 보존·감사·운영 UI 마감

**상태**: 부분 구현.

현재 문제:

- retention preview는 오래된 `SourceItem.body_text`와 `EvidenceAsset` 일부만 대상으로 한다.
- DB의 `object_key`를 지우지만 S3의 실제 versioned object를 삭제하지 않는다.
- draft, published snapshot, audit, attachment, public delivery asset 보존이 연결되지 않았다.
- AuditEvent가 evidence 일부와 publishing에만 있고 collection/editorial/scheduling/correction/retention에 없다.
- operations UI는 kill switch와 일정 목록만 보여주며 일정 생성/수정/중지, 실패 복구, retention UI가 없다.

남은 구현:

1. raw/draft 90일, published/audit 365일, delivery grace 정책별 candidate handler를 구현한다.
2. legal hold, active publication/media binding, correction dependency와 reference protection을 적용한다.
3. S3 version-aware 실제 삭제, retry/idempotency와 DB tombstone을 연결한다.
4. 모든 상태 변경에 actor, correlation ID, before/after hash, reason/result AuditEvent를 추가한다.
5. 일정 CRUD/활성화, 실패 단계·영향 target, safe retry/reconcile 화면을 구현한다.
6. retention preview→재인증 승인→실행→실패 재개 UI와 감사 기록을 구현한다.

완료 조건:

- hold/active binding 자산은 삭제되지 않는다.
- DB와 S3가 같은 최종 삭제 상태가 되며 재실행해도 안전하다.
- 관리자가 콘솔에서 실패 원인과 안전한 다음 동작을 확인할 수 있다.

## 5. 후속 자동화 검증 — T039~T044

현재 `tests/` 디렉터리가 없다. 위 기능 구현이 끝난 뒤 다음 순서로 작성한다.

| Task | 테스트 범위 | 예정 파일 |
|---|---|---|
| T039 | 수집, window/pagination, 중복·충돌, 권리, citation | `tests/unit/`, `tests/integration/test_collection_pipeline.py` |
| T040 | PaddleOCR/HWP, locator, page coverage, 저신뢰 차단 | `tests/contract/test_evidence_extractor.py` |
| T041 | WordPress/Blogger 멱등성, 부분 실패, reconcile | `tests/contract/test_publishers.py` |
| T042 | schedule lock, `queue_one`, kill switch, retry, retention | `tests/integration/test_scheduling_operations.py` |
| T043 | 관리자 인증, CSRF, reauth, secret redaction, AuditEvent | `tests/integration/test_security_audit.py` |
| T044 | 수동 run→evidence→revision→승인→두 채널 발행 | `tests/e2e/test_admin_journey.py` |

최종 release gate에는 다음 표본을 반드시 포함한다.

- 동일 publish 요청 100회에도 채널별 remote post가 하나만 존재
- text/scan/rotation/table PDF 골든 30 page에서 locator 누락과 저신뢰 자동발행 0건
- 조건 미달 semiconductor 사건의 breaking 발행 0건
- 검증된 correction/retraction의 WordPress 우선 및 Blogger 후속 반영
- secret/cookie/token이 API 문제 응답, 로그, AuditEvent에 노출되지 않음

## 6. 빠른 검증 명령

기능 batch마다 테스트 대신 최소한 다음을 실행한다.

```powershell
.\.venv\Scripts\python.exe src\manage.py check
.\.venv\Scripts\python.exe src\manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe -m compileall -q src deploy
docker compose config
```

T046/T049/T052/T053 완료 후:

```powershell
docker compose run --rm web python src/manage.py verify_source_registry_snapshots --require-approved-mvp
docker compose run --rm ocr-model-bootstrap
docker compose run --rm ocr-worker python src/manage.py verify_ocr_manifest
docker compose run --rm ocr-worker python src/manage.py verify_extraction_profile_snapshots --require-approved-mvp
# hwp-worker를 추가한 뒤 실행
docker compose run --rm hwp-worker python src/manage.py verify_extraction_profile_files
```

Phase 7 구현 후:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit -q
.\.venv\Scripts\python.exe -m pytest tests/contract -q
.\.venv\Scripts\python.exe -m pytest tests/integration -q
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_admin_journey.py -q
.\.venv\Scripts\python.exe src\manage.py check --deploy
```

## 7. 다음 세션 시작 절차

1. `git pull origin main`
2. 이 문서와 `tasks.md`, `plan.md`, Constitution을 읽는다.
3. `.specify/scripts/powershell/check-prerequisites.ps1 -Json -RequireTasks -IncludeTasks`를 실행한다.
4. `T046`을 in-progress로 잡고 실제 registry policy/version/approval gate부터 구현한다.
5. 병렬 agent가 가능하면 `T049`와 `T052`를 독립 파일 경계로 분리한다.
6. 완료한 task만 `tasks.md`에서 `[X]`로 바꾸고 위 최소 검증 결과를 남긴다.
7. 각 wave 뒤 `$speckit-converge`를 다시 실행해 남은 차이를 줄인다.

다음 세션에 그대로 사용할 요청문:

```text
Spec Kit 스킬만 사용하고 Superpower/marketing 스킬은 사용하지 마라.
specs/001-automated-content-publishing/REMAINING_WORK.md와 tasks.md를 읽고
권장 Wave A부터 구현을 계속하라. 기능 구현 속도를 우선하고 T039~T044 테스트는 마지막에 수행하라.
PaddleOCR는 PP-StructureV3만 사용하고 완료한 task만 tasks.md에서 체크하라.
```

