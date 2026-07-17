# Tasks: 주제 기반 자동 블로그 발행

**Input**: `specs/001-automated-content-publishing/`의 spec, plan, research, data-model, contracts

**Execution policy**: 사용자 요청에 따라 구현을 먼저 완료하고 자동화 테스트는 Phase 7에서 후속 수행한다.

## Phase 1: Setup

**Purpose**: 실행 가능한 Django/Celery 프로젝트와 로컬 인프라를 만든다.

- [X] T001 Python 3.12/Django/Celery/PaddleOCR 의존성과 도구 설정을 `pyproject.toml`에 작성
- [X] T002 [P] Django 프로젝트·settings·URL·Celery 부트스트랩을 `src/manage.py`와 `src/wisdome_writer/`에 구현
- [X] T003 [P] PostgreSQL·Redis·MinIO·web·worker·beat 실행 구성을 `compose.yaml`과 `deploy/containers/`에 구현
- [X] T004 [P] 환경 변수 예시와 저장소 제외 규칙을 `.env.example`, `.gitignore`, `.dockerignore`에 작성

## Phase 2: Foundational

**Purpose**: 모든 사용자 스토리가 공유하는 관리자 보안, 감사, 저장소, 작업 기반을 구현한다.

- [X] T005 관리자 계정·재인증 proof와 session/CSRF 정책을 `src/apps/accounts/`에 구현
- [X] T006 [P] append-only AuditEvent와 요청 멱등성·JCS hash 도구를 `src/apps/audit/`와 `src/wisdome_writer/domain/`에 구현
- [X] T007 [P] S3 객체 저장소·비밀 참조·outbox 포트를 `src/adapters/storage/`와 `src/wisdome_writer/infrastructure/`에 구현
- [X] T008 공통 문제 응답, health endpoint와 Admin API 라우팅을 `src/wisdome_writer/api/`와 `src/wisdome_writer/urls.py`에 구현
- [X] T009 전체 앱의 초기 데이터베이스 모델과 migration을 `src/apps/*/models.py`와 `src/apps/*/migrations/`에 생성

## Phase 3: User Story 1 - 출처가 검증된 글 초안 생성 (Priority: P1) 🎯 MVP

**Goal**: 두 주제의 승인 출처에서 자료를 수집하고 PaddleOCR 포함 멀티모달 근거를 생성해 출처 연결 초안을 만든다.

**Independent Test**: 관리자가 주제와 시간창을 지정하면 run 상세에 수집 항목·근거·주장·초안이 나타나고 저신뢰 핵심 값은 게시 불가다.

- [x] T010 [P] [US1] TopicPolicy·SourceDefinition·registry snapshot 모델과 승인 서비스를 `src/apps/topics/`에 구현
- [x] T011 [P] [US1] 청약·반도체 초기 출처 registry와 정책 manifest를 `config/source-registry/`와 `config/editorial-policies/`에 작성
- [x] T012 [P] [US1] 청약홈·LH·공공데이터 source adapter를 `src/adapters/sources/housing/`에 구현
- [x] T013 [P] [US1] 정부·기업 IR·거래소·신뢰 뉴스 source adapter를 `src/adapters/sources/semiconductor/`에 구현
- [x] T014 [US1] CollectionRun·SourceItem·RunSourceItem 수집 orchestration과 Celery task를 `src/apps/collection/`에 구현
- [X] T015 [P] [US1] EvidenceAsset·DocumentExtraction·ExtractionRun·review decision 모델을 `src/apps/evidence/models.py`에 구현
- [X] T016 [P] [US1] PaddleOCR 3.7.0 PP-StructureV3 profile manifest와 로컬 worker adapter를 `config/extraction-profiles/`와 `src/adapters/extractors/paddleocr/`에 구현
- [X] T017 [P] [US1] native PDF·HWPX·legacy HWP→PDF·HTML·spreadsheet generic extractor를 `src/adapters/extractors/`에 구현
- [X] T018 [US1] 문서 페이지 routing·locator·confidence·권리·publishability 처리를 `src/apps/evidence/services.py`와 `src/apps/evidence/tasks.py`에 구현
- [x] T019 [P] [US1] DraftArticle·ArticleRevision·Claim·citation·visualization 모델을 `src/apps/editorial/models.py`에 구현
- [x] T020 [US1] 출처 기반 한국어 초안 생성·사실/해석 분리·품질 gate를 `src/apps/editorial/services.py`와 `src/adapters/generators/`에 구현
- [X] T021 [US1] run 생성·상세·근거 검토·초안 조회/개정 API를 `src/apps/collection/api.py`, `src/apps/evidence/api.py`, `src/apps/editorial/api.py`에 구현
- [X] T022 [US1] 관리자 실행·근거 검토·초안 화면을 `src/templates/admin_console/`와 `src/static/admin_console/`에 구현

## Phase 4: User Story 2 - 검증된 글을 블로그 채널에 발행 (Priority: P2)

**Goal**: 승인된 revision을 WordPress 대표 원문으로 발행한 뒤 Blogger에 배포하고 채널별 상태를 복구한다.

**Independent Test**: 승인된 초안을 draft로 발행하면 WordPress publication과 Blogger publication이 각각 원격 ID/URL/상태를 보존한다.

- [x] T023 [P] [US2] PublicationTarget·Intent·Attempt·Approval·Media 모델을 `src/apps/publishing/models.py`에 구현
- [x] T024 [P] [US2] WordPress REST/Application Password publisher adapter를 `src/adapters/publishers/wordpress/`에 구현
- [x] T025 [P] [US2] Google Blogger OAuth/API publisher adapter를 `src/adapters/publishers/blogger/`에 구현
- [x] T026 [US2] WordPress 선발행·Blogger 후속발행·멱등 reconcile orchestration을 `src/apps/publishing/services.py`와 `src/apps/publishing/tasks.py`에 구현
- [x] T027 [US2] target 연결·preflight·승인·발행·재시도 API를 `src/apps/publishing/api.py`에 구현
- [x] T028 [US2] 채널 설정·미리보기·승인·발행 상태 화면을 `src/templates/admin_console/publishing/`에 구현

## Phase 5: User Story 3 - 일정 기반 반복 실행과 운영 모니터링 (Priority: P3)

**Goal**: 관리자 일정에 따라 전체 파이프라인을 반복하고 중복, 중지, 부분 실패를 운영 화면에서 제어한다.

**Independent Test**: Asia/Seoul cron 일정이 한 run만 dispatch하고 kill switch 또는 중복 정책에 따라 skip/queue 상태를 기록한다.

- [x] T029 [P] [US3] Schedule·ScheduleDispatch·kill-switch 모델을 `src/apps/scheduling/models.py`에 구현
- [x] T030 [US3] cron dispatch·skip/queue_one locking·run 생성 서비스를 `src/apps/scheduling/services.py`와 `src/apps/scheduling/tasks.py`에 구현
- [x] T031 [US3] 일정 CRUD·kill switch·실행 중지/재시도·audit 조회 API를 `src/apps/scheduling/api.py`와 `src/apps/audit/api.py`에 구현
- [x] T032 [US3] 일정·실행 모니터링·오류 복구 화면을 `src/templates/admin_console/operations/`에 구현
- [X] T033 [US3] 정정·철회 감지와 기존 채널 update/unpublish 흐름을 `src/apps/editorial/corrections.py`와 `src/apps/publishing/corrections.py`에 구현
- [x] T034 [US3] 90일 raw/1년 audit 보존과 hold-aware 정리 task를 `src/apps/audit/retention.py`와 `config/retention/`에 구현

## Phase 6: Integration & Runnable MVP

**Purpose**: 앱 간 배선과 관리 명령을 연결해 실제 실행 가능한 MVP를 만든다.

- [X] T035 extraction profile/source registry import·검증·bootstrap 명령을 `src/apps/topics/management/commands/`와 `src/apps/evidence/management/commands/`에 구현
- [X] T036 Celery queue routing·worker startup·beat schedule을 `src/wisdome_writer/celery.py`와 `src/wisdome_writer/settings/`에 연결
- [X] T037 Admin console 홈과 전체 URL/앱 구성을 `src/wisdome_writer/urls.py`, `src/templates/admin_console/index.html`, `src/apps/*/admin.py`에 연결
- [X] T038 로컬 실행·자격 증명·PaddleOCR 모델 준비 절차를 `README.md`에 작성

## Phase 7: Deferred Automated Verification (사용자 요청에 따라 후속)

**Purpose**: 구현 완료 뒤 Constitution의 자동화 품질 게이트를 충족한다.

- [ ] T039 [P] 수집·중복·출처/권리·citation 테스트를 `tests/unit/`와 `tests/integration/test_collection_pipeline.py`에 구현
- [ ] T040 [P] PaddleOCR/HWP/locator/저신뢰 차단 계약 테스트를 `tests/contract/test_evidence_extractor.py`에 구현
- [ ] T041 [P] WordPress/Blogger 멱등·부분 실패·reconcile 계약 테스트를 `tests/contract/test_publishers.py`에 구현
- [ ] T042 [P] 일정 locking·kill switch·재시도·보존 테스트를 `tests/integration/test_scheduling_operations.py`에 구현
- [ ] T043 [P] 관리자 인증·CSRF·비밀 redaction 테스트를 `tests/integration/test_security_audit.py`에 구현
- [ ] T044 전체 수동실행→초안→승인→두 채널 발행 E2E를 `tests/e2e/test_admin_journey.py`에 구현

## Dependencies & Execution Order

- Phase 1 → Phase 2 → Phase 3의 실행 가능한 MVP 순서가 기본이다.
- T002/T003/T004, T006/T007, T010~T013, T015~T017/T019, T023~T025, T039~T043은 파일 경계가 달라 병렬 가능하다.
- US2는 ArticleRevision을 입력으로 사용하므로 US1 모델 계약에 의존한다.
- US3 schedule dispatch는 CollectionRun과 publication orchestration에 의존하지만 CRUD와 모델은 병렬 구현 가능하다.
- Phase 7은 사용자 요청에 따라 코드 구현 뒤 수행한다.

## Parallel Examples

- US1: housing adapter, semiconductor adapter, PaddleOCR adapter, editorial models를 병렬 구현한다.
- US2: WordPress와 Blogger adapter를 병렬 구현한 뒤 publication service에서 결합한다.
- US3: schedule 모델과 correction/retention 모듈을 병렬 구현한다.

## Implementation Strategy

1. Phase 1~2로 웹/worker가 부팅되는 기반을 만든다.
2. US1의 수동 run→근거→초안을 가장 먼저 연결한다.
3. US2 공식 발행 adapter, US3 일정/운영을 병렬로 추가한다.
4. Phase 6에서 관리 명령과 전체 URL을 연결한다.
5. Phase 7 자동화 검증은 구현 우선 요청에 따라 후속 수행한다.

## Phase 8: Convergence

- [ ] T045 CRITICAL 청약·반도체 출처별 목록/상세/pagination/기간 필터와 변경 상태 수집을 `src/adapters/sources/`와 `config/source-registry/`에 구현 per US1/AC1-2, FR-004, FR-005 (partial)
- [ ] T046 CRITICAL robots·약관·호출 제한 강제와 Registry 불변 버전·재인증 승인·감사·배포 검증을 `src/apps/topics/`, `src/adapters/sources/http.py`, `src/apps/topics/management/commands/`에 구현 per Constitution II, CR-005, plan: 출처 레지스트리 부트스트랩 (contradicts)
- [ ] T047 CRITICAL 중복·충돌 선택 기록, 청약 공고별 identity, 반도체 사건 clustering·속보 범주·독립 출처 검증을 `src/apps/collection/`와 `src/apps/editorial/`에 구현 per FR-007, FR-010, FR-022, SC-010 (missing)
- [X] T048 CRITICAL Markdown citation을 안전한 링크 HTML과 채널 render `source_links`로 보존하도록 `src/apps/publishing/services.py`를 구현 per CR-001 (contradicts)
- [ ] T049 CRITICAL PaddleOCR 전체 모델을 빌드 시 준비하고 실제 checksum·경로를 고정해 runtime network 없이 실행하도록 `deploy/containers/paddleocr-worker/`, `config/extraction-profiles/paddleocr/`, `compose.yaml`을 구현 per FR-024, US1/AC5, plan: Paddle profile (missing)
- [ ] T050 CRITICAL 실패한 profile 보고서 승인 차단, OCR locator 필수화와 저신뢰 검토 CAS·projection·outbox 단일 트랜잭션을 `src/apps/evidence/`와 `src/adapters/extractors/paddleocr/`에 구현 per FR-024, plan: 추출 프로필 승인 게이트 (contradicts)
- [ ] T051 정책 기반 사실/기업주장/해석/전망 구조, 고위험 값 gate, 시각 자료 배치와 수동 개정 재검증을 `src/apps/editorial/`, `src/adapters/generators/`, `config/editorial-policies/`에 구현 per FR-008, FR-009, CR-006, CR-007 (partial)
- [ ] T052 승인된 converter manifest를 사용하는 no-network·read-only·resource-limited legacy HWP sandbox를 `deploy/containers/`와 `src/adapters/extractors/legacy_hwp.py`에 구현 per FR-005, plan: legacy HWP sandbox (missing)
- [ ] T053 승인된 Registry/Profile 시작 게이트와 OCR worker 발행 비밀 격리를 `compose.yaml`, `deploy/containers/`, worker bootstrap 명령에 구현 per plan: 실행 경계와 worker startup (contradicts)
- [X] T054 관리자가 intent 생성→채널 미리보기→target별 승인→WordPress/Blogger 발행·재시도까지 완료하는 화면을 `src/templates/admin_console/publishing/`와 `src/static/admin_console/`에 구현 per US2, FR-011, FR-013 (partial)
- [ ] T055 예약 Run에서 승인 모드에 따라 자동발행을 연결하고 queue_one 해제·Run 최종 상태를 처리하도록 `src/apps/scheduling/`, `src/apps/editorial/tasks.py`, `src/apps/publishing/`을 구현 per US3, FR-014, FR-018 (missing)
- [ ] T056 원문 변경 감시→관리자 검증→새 개정→WordPress 우선 update/unpublish→Blogger 반영을 `src/apps/editorial/corrections.py`, `src/apps/publishing/corrections.py`, Celery task와 관리자 API에 연결 per FR-021, SC-009 (missing)
- [ ] T057 draft/published snapshot/audit/public asset 보존·실제 객체 삭제, 전 단계 AuditEvent와 일정 CRUD·실패 복구 화면을 `src/apps/audit/`, `src/apps/*/services.py`, `src/templates/admin_console/operations/`에 구현 per FR-019, FR-020, Constitution V, Constitution VI (partial)
