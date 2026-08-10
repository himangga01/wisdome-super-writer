# Tasks: 주제 기반 자동 블로그 발행 — 재정비 백로그

## 한국어 실행 계획

**입력 문서**: `specs/001-automated-content-publishing/`의 `spec.md`, `plan.md`,
`research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**재작성 기준**: 2026-07-28 전체 정적 코드 리뷰 결과를 기준으로 기존 T001~T057의
완료 표시를 폐기하고, 실제 남은 작업만 실행 순서에 맞춰 다시 작성한다.

**ID 정책**: 앞서 승인된 WSW-001~WSW-033 범위를 Spec Kit 실행 형식에 맞게
T001~T033으로 재배치한다.

**완료 정책**:

- 모든 항목은 현재 미완료 상태에서 시작한다.
- 코드나 모델이 존재한다는 이유만으로 완료 처리하지 않는다.
- 각 작업의 설명과 아래 독립 확인 기준을 모두 충족해야 체크할 수 있다.
- 자동화 테스트 작성과 성공 기준 측정은 T032에서 수행한다. 이 파일을 생성하는 현재
  작업에서는 테스트를 실행하지 않는다.

**조직 방식**: 공통 안전 기반을 먼저 완성한 뒤 사용자 스토리 US1→US2→US3 순서로
구현하고, 마지막에 전체 자동 검증과 문서 정합성을 닫는다.

---

## Phase 1: Setup — 안전한 실행 경계

**목적**: 외부 네트워크, hash, 배포 환경처럼 모든 사용자 스토리가 공유하는 실행 경계를
먼저 고정한다.

- [X] T001 [P] DNS 재해석·사설 IP·redirect를 차단하고 streaming 크기·시간·메모리 제한과 URL 비밀 redaction을 제공하는 공통 outbound HTTP 보호 계층을 `src/wisdome_writer/infrastructure/http_safety.py`, `src/adapters/sources/http.py`, `src/apps/evidence/tasks.py`, `src/adapters/publishers/wordpress/client.py`에 구현
- [X] T002 [P] Unicode NFC와 RFC 8785 JCS를 따르는 단일 canonical hash 구현으로 앱별 hash 함수를 통합하고 hash schema version을 `src/wisdome_writer/domain/hashing.py`, `src/apps/topics/services.py`, `src/apps/editorial/services.py`, `src/apps/audit/retention.py`에 적용
- [X] T003 [P] production 환경 변수 fail-fast, WSGI 실행, migration/static 시작 절차, dependency·container image 고정, DB·MinIO credential 교체, worker별 비밀 격리와 network 경계를 `src/wisdome_writer/settings/__init__.py`, `compose.yaml`, `deploy/containers/`, `pyproject.toml`, `.env.example`에 구현

**Checkpoint**: 외부 입력과 production 실행 환경이 이후 기능에서 재사용할 수 있는 안전한
기본 경계를 제공한다.

---

## Phase 2: Foundational — 상태·트랜잭션·감사 기반

**목적**: 승인, 멱등성, 비동기 전달과 감사가 모든 사용자 스토리에서 동일한 규칙으로
동작하게 한다.

**중요**: 이 Phase가 완료되기 전에는 외부 production 채널 쓰기를 허용하지 않는다.

- [X] T004 [P] 9개 고위험 action scope를 중앙 재인증 서비스로 통일하고 session binding, entity/action binding, 만료, 단회 소비와 동일 요청 replay 규칙을 `src/apps/accounts/services.py`, `src/apps/accounts/api.py`, `src/apps/publishing/services.py`, `src/apps/scheduling/api.py`, `src/apps/evidence/api.py`, `src/apps/audit/api.py`에 구현
- [X] T005 [P] transaction commit 이후 event를 전달하는 outbox dispatcher, lease, consumer dedupe, retry, dead-letter, event envelope version·causation·correlation을 `src/wisdome_writer/infrastructure/outbox.py`, `src/wisdome_writer/infrastructure/models.py`, `src/wisdome_writer/celery.py`, `src/apps/*/tasks.py`에 구현하고 직접 `.delay()` 전달 공백을 제거
- [X] T006 관리자 actor·reason·request/correlation metadata를 보존하고 감사 실패를 은폐하지 않으며 DB 수준 append-only와 업무 mutation의 원자성을 보장하도록 `src/apps/audit/models.py`, `src/apps/audit/services.py`, `src/apps/publishing/services.py`, `src/apps/topics/services.py`, `src/apps/scheduling/services.py`, `src/apps/editorial/services.py`를 정비 (depends on T004, T005)
- [X] T007 [P] OpenAPI 입력 검증, enum·UUID·필수 필드·additionalProperties 처리, 일관된 problem response, cursor pagination, request-key payload hash와 expected-version CAS 공통 계층을 `src/wisdome_writer/api/`, `src/wisdome_writer/domain/errors.py`, `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml`에 구현
- [X] T008 HTTP에서 Celery까지 correlation을 전파하고 run/step/channel duration, retry, terminal impact, outbox backlog와 recovery 가능 상태를 기록하는 관측 기반을 `src/wisdome_writer/observability.py`, `src/wisdome_writer/celery.py`, `src/apps/collection/models.py`, `src/apps/publishing/models.py`, `src/wisdome_writer/api/health.py`에 구현 (depends on T005)

**Checkpoint**: 재인증, 비동기 전달, 감사, API 오류와 관측 정보가 공통 규칙으로 동작한다.

---

## Phase 3: User Story 1 — 출처가 검증된 글 초안 생성 (Priority: P1) 🎯 MVP

**목표**: 승인된 출처의 목록·상세·첨부 자료를 수집하고 중복·충돌·권리·신뢰도를 검증한
뒤 주장과 원출처가 연결된 한국어 초안을 만든다.

**독립 확인 기준**: 관리자가 주제와 시간창을 지정하면 공고 또는 사건 identity별로 하나의
run 결과가 생성되고, 사용·제외·충돌 근거와 주장별 citation을 확인할 수 있어야 한다.
저신뢰 핵심 값, 권리 불명 시각 자료와 속보 검증 미달 자료는 게시 가능 상태가 되면 안 된다.

- [X] T009 [US1] `TopicRegistryHead`, `SourceRegistryDecision`, `SourceRegistryMutation`과 immutable source snapshot·membership·CAS projection을 `src/apps/topics/models.py`, `src/apps/topics/services.py`, `src/apps/topics/api.py`, `src/apps/topics/urls.py`, `src/templates/admin_console/`에 구현하고 source/registry/check/membership/decision 계약 endpoint를 완성 (depends on T002, T004, T006, T007)
- [X] T010 [US1] 청약홈·LH·공공데이터의 목록/상세/pagination/기간 필터, 첨부 원문, stable identity와 corrected/retracted/unavailable/restored 상태 수집을 `src/adapters/sources/housing/`, `src/adapters/sources/http.py`, `config/source-registry/housing_subscription.json`에 구현 (depends on T001, T009)
- [X] T011 [US1] 정부·규제기관·거래소·기업 IR·신뢰 뉴스의 RSS+Atom·상세·pagination·기간 필터와 source tier·independence group·origin identity 수집을 `src/adapters/sources/semiconductor/`, `src/adapters/sources/http.py`, `config/source-registry/semiconductor_news.json`에 구현 (depends on T001, T009)
- [X] T012 [US1] robots·이용약관·license·허용 MIME·poll/rate limit·Retry-After·freshness·authority와 수집 실패 분류를 실제 요청 경로에서 강제하고 권리 근거 fallback을 제거하도록 `src/apps/topics/services.py`, `src/apps/collection/services.py`, `src/adapters/sources/http.py`, `config/source-registry/`를 정비 (depends on T001, T009, T010, T011)
- [X] T013 [US1] `EventClusterItem`, `EventClusterVerification`과 공고·사건 canonical identity, duplicate/conflict/exclusion decision, 독립 출처 검증, 청약 신규·정정 공고별 글 및 반도체 일일 요약·중요 속보 분기를 `src/apps/collection/`, `src/apps/editorial/models.py`, `src/apps/editorial/services.py`에 구현 (depends on T002, T009, T010, T011, T012)
- [X] T014 [P] [US1] 모든 extraction profile이 web·worker에서 동일하게 import되도록 PaddleOCR model manifest root·실제 checksum·S3 object version·runtime-network 차단·worker 시작 gate를 `config/extraction-profiles/`, `src/apps/evidence/profiles.py`, `src/apps/evidence/management/commands/`, `deploy/containers/paddleocr-worker/`, `compose.yaml`에 구현 (depends on T002, T003)
- [ ] T015 [US1] 승인된 converter manifest를 실제 Python/rhwp/qpdf/library/font/fontconfig/LICENSE/Cargo.lock/build/Compose bytes와 대조하고, exact CHOWN/KILL/SETUID/SETGID만 가진 root supervisor·zero-cap child 65533·validator 65531, subreaper descendant cleanup, split input/output, streaming snapshot, exact UDS EOF/report/object/page 결속, absolute deadline, concurrency 1·전용 OCR profile-admin·volume bootstrap을 갖는 no-network legacy HWP sandbox를 `src/adapters/extractors/legacy_hwp.py`, `src/apps/evidence/profiles.py`, `src/apps/evidence/tasks.py`, `config/extraction-profiles/generic/legacy-hwp-v1.json`, `deploy/containers/`, `compose.yaml`에 구현한다. 1.1.0은 immutable golden=false draft로 유지하고 profile-before-attempt 영구 실패도 raw quarantine, typed counter/failure count와 manual recovery로 보존한다. (depends on T001, T003, T014)
- [ ] T016 [US1] DocumentExtraction·ExtractionRun·GenericExtractionAttempt의 uniqueness, row lock, generation fencing, retry/finalizer 순서, 예상 밖 예외 terminal 처리, stop hook와 outbox 전이를 `src/apps/evidence/models.py`, `src/apps/evidence/services.py`, `src/apps/evidence/tasks.py`에 구현. receipt lease generation, atomic DLQ terminal callback, exact child provenance, stop/stale 분리, evidence-native object-write ledger, historical event/receipt rearm을 collection 0009/evidence 0005에 포함 (depends on T005, T014, T015; T015 외부 승인 blocker가 해소되기 전에는 완료 표시 금지)
- [ ] T017 [US1] browser/media/manual profile을 실제 routing에 연결하거나 계약에서 제거하고 XML parser fail-closed, PDF/image dimension·decompression·S3 read 제한, locator·confidence 필수 보존을 `src/apps/evidence/tasks.py`, `src/adapters/extractors/`, `src/adapters/storage/s3.py`에 구현 (depends on T001, T014, T015, T016)
- [ ] T018 [US1] release JSON으로 현재 정책을 해석하되 approval/head 없이 append-only `EditorialPolicySnapshot`을 만들고 같은 key/version의 변경 bytes를 거부한다. `bodyBlocks`를 revision 정본으로 삼아 사실·기업주장·해석·전망 atomic claim을 분리하고, 정렬 multi-verification/input-evidence/excluded·duplicate·conflict snapshot을 고정한다. `all_publishable_claims_grounded`, `high_risk_verification_satisfied`, `claim_independence_satisfied`, `source_freshness_satisfied`, `evidence_publish_eligibility_current`, `quotation_limits_satisfied`, `claim_types_separated_and_attributed`, `duplicate_or_conflict_resolved`, `korean_readability_and_repetition`, `no_exaggeration_or_false_experience`의 exact 10개 gate와 visual 사용 시 `visual_rights_and_alt_text`를 적용한다. current policy/evidence publish eligibility 재확인, `editorial.revalidate_requested` 수동 개정 재검증, 주장·근거·제외 자료·runtime eligibility 관리자 화면을 `src/apps/editorial/`, `src/adapters/generators/`, `config/editorial-policies/`, `src/templates/admin_console/`, `src/static/admin_console/`에 구현한다. (depends on T002, T013, T016, T017; T015~T017 외부 blocker가 해소되기 전에는 완료 표시 금지)

**Checkpoint**: US1만으로도 외부 채널에 쓰지 않고, 검증 가능한 근거와 안전한 초안을
독립적으로 생성·검토할 수 있다.

---

## Phase 4: User Story 2 — 검증된 글을 WordPress와 Blogger에 발행 (Priority: P2)

**목표**: 최신 승인을 받은 하나의 revision을 WordPress 대표 원문으로 먼저 발행하고,
정확히 동결된 대표 URL을 Blogger에 전달하며 중복·부분 실패를 안전하게 복구한다.

**독립 확인 기준**: 같은 request와 Celery message를 반복해도 채널별 원격 게시물은 하나만
존재해야 한다. 승인 철회는 다음 외부 쓰기를 즉시 막고, update/unpublish reconcile은 실제
원격 action·상태·본문 hash가 맞을 때만 성공해야 한다.

- [ ] T019 [US2] Approval을 append-only 결정으로 유지하면서 `(expectedLatestApprovalId, expectedHeadVersion)` 이중 CAS의 latest-decision projection과 approve/reject/revoke 전이를 구현한다. 결정 사유·결정 hash·material version·head version, non-null 결정 소유 관리자와 실제 admin/worker actor provenance를 불변 저장하고, revoked와 approved-unpublish에만 용도 결속 재인증을 요구하며, reject/revoke가 intent·dispatch·실행 직전 gate를 즉시 차단하게 `src/apps/publishing/models.py`, `src/apps/publishing/services.py`, `src/apps/publishing/automation.py`를 정비한다. 기존 approval POST의 exact request/response serializer·OpenAPI status(신규 201, 동일 replay 200, 재인증 403, stale/불법 전이/CAS 409, 본문 불일치 422)와 집중 계약 테스트는 이 작업에 포함하되 새 route, 결정 이력 pagination과 관리자 UI는 T026에 남긴다. (depends on T004, T006, T018; T018 및 선행 외부 blocker가 해소되기 전에는 완료 표시 금지)
- [ ] T020 [US2] intent 생성과 dispatch를 서로 다른 append-only 멱등 경계로 구현한다. intent는 article-scoped request identity와 versioned request hash/head projection을 사용해 mutable CAS보다 replay를 먼저 판정하고 target snapshot·command·validation·activation ref를 각각 최대 20개로 제한하며 semantic target ID 중복은 422로 거부한다. 유효한 UUID는 body와 article path 모두 case-insensitive로 받은 뒤 canonical lowercase로 hash·저장·응답하고, `revisionNo`는 API와 직접 관리자/worker 호출 모두 1~9007199254740991로 제한하며, 모든 caller가 필수 nonblank·trimmed·audit-safe `reason`을 제공한다. 최초 intent는 201, exact canonical replay는 200, 같은 key의 변경 material은 409다. dispatch는 생략/null `publishAt`을 같은 즉시 실행 요청으로 canonicalize하고 non-null은 `T/t`·초·`Z/z|±HH:MM`을 가진 strict RFC 3339를 받아 구분자를 대문자 `T`, 동일 instant를 UTC `Z`로 hash·schedule한다. ledger와 frozen target별 정확히 하나의 최초 attempt·outbox·AuditEvent를 한 transaction에 만들며 최초 202, exact replay 200, 변경 material 409, 잘못된 구조·의미는 422를 반환한다. DB는 조건 없는 `(publication_intent, publication)` unique로 논리 attempt row 하나를 유지하고 retry counter만 전진시킨다. API는 mutable state/history와 mutable retry counter 없이 bounded `PublicationDispatchResult`의 acceptance `attemptNo=1`만 반환하고 focused 계약·DB 테스트를 추가한다. route 완성·cursor history·관리자 UI·E2E는 T026 범위이며 T019와 선행 blocker가 끝나기 전에는 완료 표시하지 않는다. (depends on T005, T019)
- [ ] T021 [US2] T020 acceptance `attemptNo=1`, business `executionAttemptNo` 1~5, domain `executionGeneration`, receipt `leaseGeneration`, dispatcher delivery `attempt`, read-only `reconcileAttemptNo` 1~5를 분리한다. 신규 `publication.requested@2` exact payload는 `publication_attempt_id+execution_attempt_no`, reconcile@2는 `publication_attempt_id+reconcile_attempt_no`이며 public ABI에 raw capability를 추가하지 않는다. v1은 exact bound replay 또는 proven virgin generation 1만 결속하고 나머지는 `legacy-unverifiable-v1` no-op quarantine한다. PublicationAttempt/ExecutionObservation에는 `publication-execution-v1` raw internal capability+audit-only hash lease envelope와 write marker를 동결하고, reconcile claim/reclaim은 append-only `PublicationReconcileDeliveryObservation`을 남기며 late result는 execution observation XOR reconcile delivery observation exact FK를 가진다. mutating HTTP 직전 statement-time pre-I/O fence가 current·미만료 raw capability와 승인·intent·target·kill switch를 재검증하고, 결과 정산은 higher reclaim이 없는 current raw token/generation을 clock expiry만으로 거부하지 않는다. pre-write read failure는 write marker 0건과 `external_write_authorized=false`로 정산하며 `Retry-After` delay-seconds/IMF-fixdate를 보존한다. create는 schema-complete bounded 조회의 exact target/blog·complete pagination·0 match만 mutate하고, mutating 2xx/reconcile 성공은 actual provider body의 canonical hash·exact remote ID·target/blog·state와 content action의 title을 증명한다. ambiguous mutating timeout/408/429/5xx는 direct create retry 없이 reconcile한다. 0건은 최대 5회 bounded read-only reconcile, 복수건은 즉시 manual, Blogger는 live/draft/scheduled 합산 15 page·750 item·wall 30초(`maxResults=50/page`) 내 `nextPageToken`을 따른다. running reconcile reclaim은 같은 generation을 재개한다. terminal callback은 processing receipt에 raw `terminal_lease_token`을 포함한 all-or-none set-once 5필드 reservation을 먼저 남기며 예약된 receipt 삭제·성공 전이를 금지하고 exact callback·`delivery_failed/manual_required`·AuditEvent·DLQ를 한 transaction으로 처리한다. raw token은 내부 authoritative 비교 전용이고 event·audit·log·API는 hash만 노출한다. AuditEvent는 source event·domain/reconcile/receipt generation·consumer·token hash·write authorization/marker·delivery observation·terminal reservation hash/error를 결속한다. remote URL은 최대 1000자 `http(s)`만 저장하고 invalid/초과 raw 값은 SHA-256 사실만 남긴다. retry/reconcile는 immutable T020 Dispatch-ledger cohort를 바꾸지 않으며 OpenAPI history/retry route, cursor pagination, 관리자 UI와 E2E는 T026 범위다. T020 및 선행 blocker가 끝나기 전 완료 표시하지 않는다. (depends on T020)
- [ ] T022 [US2] revision별 append-only `PublishedAssetCohort`와 `PublishedEvidenceSnapshot`/`PublishedVisualizationSnapshot`을 동결하고 bounded channel media manifest를 approval·dispatch·attempt와 결속한다. `PublicationMedia`는 exact snapshot을 content-addressed WordPress `RemoteMedia` 또는 Blogger `PublicDeliveryAsset`에 연결하며 current rights/source/render와 available/active 상태 전에는 외부 쓰기를 차단한다. prepare/reconcile/delete는 exact v2 event와 receipt capability에 결속된 1~5 append-only `MediaDeliveryOperation` generation으로 실행하고, WordPress는 0/1/many·메타데이터·공개 bytes hash를, Blogger는 exact S3 object version/HEAD/익명 HTTPS bytes를 검증한다. 삭제는 binding·attempt·remote-body·correction·hold를 잠금 재집계해 30일 연속 0건일 때만 exact ID/version으로 실행하며 재참조는 미실행 delete를 supersede한다. `src/apps/publishing/models.py`, `src/apps/publishing/migrations/0012_published_asset_snapshots.py`, `src/apps/publishing/services.py`, `src/apps/publishing/tasks.py`, `src/adapters/publishers/wordpress/client.py`, `src/adapters/storage/s3.py`와 focused tests에 구현한다. 선행 dependency 상태가 완료되기 전에는 체크하지 않는다. (depends on T002, T018, T020, T021)
- [X] T023 [US2] versioned secret bundle과 target snapshot credential version을 저장하고, 관리자·session·target snapshot에 결속된 Blogger OAuth connect·단일 refresh·scope 검증·refresh-token revoke 및 WordPress Application Password의 정확한 remote delete/부재 확인을 `src/wisdome_writer/infrastructure/secrets.py`, `src/apps/publishing/models.py`, `src/apps/publishing/services.py`, `src/apps/publishing/tasks.py`, `src/adapters/publishers/blogger/`, `src/adapters/publishers/wordpress/client.py`와 migration에 구현한다. 원격 결과가 불명확하면 local credential을 지우지 않는다. (depends on T001, T003, T004)
- [ ] T024 [US2] `PublicationAttempt.depends_on_attempt`와 `publication-dependency-v1` hash로 같은 intent/revision/article/environment의 exact primary WordPress attempt를 Blogger create/update/mark-withdrawn/unpublish에 동결한다. dispatch/correction은 WordPress attempt를 먼저 생성하고 release·실행 gate·final render·reconcile은 FK만 따라가며, 검증 URL은 Blogger final render의 canonical URL/body/content hash에 별도로 동결한다. 다른 target·test URL·불명확 legacy cohort는 fail-closed하도록 `src/apps/publishing/models.py`, `src/apps/publishing/migrations/0014_publication_dependency.py`, `src/apps/publishing/services.py`, `src/apps/publishing/automation.py`, `src/apps/publishing/corrections.py`를 정비한다. 선행 T022가 완료 표시되기 전에는 체크하지 않는다. (depends on T020, T021, T022, T023)
- [ ] T025 [US2] client는 `topic/requestKey/reason`만 제출하고 서버가 current target/credential, approved registry와 source-adapter manifest, approved extraction profile 결정·보고서, topic/editorial/generation/quality/render/publisher release, exact canary/pilot evidence를 `auto-publish-validation-material-v2`로 동결한다. test canary는 서버 고정 policy로 6단계와 원격 cleanup ID의 deleted 상태를 보존하고 production은 same-channel current canary와 approved public pilot을 결속한다. registry/profile 결정과 target snapshot 변경은 validation stale·auto-disable을 투영하며 activation/intent/attempt/pre-I/O gate는 현재 material을 재계산한다. legacy client material은 영구 fail-closed한다. 구현 파일은 `src/apps/publishing/models.py`, migration 0015, services/tasks/api, `src/apps/evidence/profiles.py`, `src/apps/topics/services.py`, event route와 focused tests다. 선행 T022가 완료 표시되기 전에는 체크하지 않는다. (depends on T004, T009, T014, T018, T019, T021, T022, T023, T024)
- [ ] T026 [US2] target 연결·preflight·OAuth·server-derived validation·activation·exact preview·approve/reject/revoke·dispatch·retry/reconcile·disconnect의 모든 route를 OpenAPI operation에 결속한다. current intent는 authoritative IntentHead만 반환하고, approval/publication/attempt 이력은 parent filter·내림차순 timestamp/UUID·limit에 결속된 signed cursor page로 분리한다. Approval UI는 historical approved를 권한으로 추론하지 않고 `currentHead.latestApprovalId+version+dispatchEligible`만 사용하며 revoke는 `approval_revoke`, validation은 `validation_decision`, activation은 `auto_publish_change`, disconnect는 `credential_disconnect` 재인증 proof를 즉시 획득한다. preview HTML은 sandboxed frame, 나머지 동적 값과 링크는 DOM text node와 HTTP(S)-only URL 정책으로 표시한다. `src/apps/publishing/api.py`, `src/apps/publishing/urls.py`, `src/templates/admin_console/publishing/`, `src/static/admin_console/`, OpenAPI와 focused contract tests에 구현한다. T019가 고정한 approval domain/POST serializer 계약을 재정의하지 않고, 선행 T022/T025가 완료 표시되기 전에는 체크하지 않는다. (depends on T007, T019, T020, T021, T022, T023, T024, T025)

**Checkpoint**: US1 초안을 입력으로 받아 두 채널을 중복 없이 발행하고 승인 철회·부분 실패·
unknown outcome을 안전하게 복구할 수 있다.

---

## Phase 5: User Story 3 — 일정 실행·중지·정정·보존 운영 (Priority: P3)

**목표**: 일정 실행의 입력을 불변으로 동결하고, 관리자가 중지·재개·정정·철회·보존 삭제의
상태와 채널 영향을 추적하고 통제할 수 있게 한다.

**독립 확인 기준**: 같은 schedule tick은 하나의 dispatch만 만들고 다른 일정과 run을 공유하지
않아야 한다. 중지 요청 후 새 외부 쓰기가 발생하면 안 되며 정상·실패·중지 run은 terminal
상태와 영향 채널을 기록해야 한다. 검증된 정정은 WordPress와 Blogger의 동일 기존 글에
반영되고, retention은 보호 중인 게시 근거를 삭제하면 안 된다.

- [ ] T027 [US3] scheduler가 schedule row와 target fence/target, approved registry head·topic policy를 잠근 한 transaction에서 `schedule-dispatch-material-v1`을 만들고, exact target snapshot·credential version·approval mode·validation/activation·owner를 `ScheduleDispatch`에 불변 동결한다. `(schedule, scheduledFor)` tick은 schedule version/enable 변경보다 먼저 기존 audit row로 replay하고 due scan은 조회 시각의 `nextRunAt`을 명시적으로 전달한다. queue-one은 queued root 하나의 최대 100개 정렬 tick ref·union window·manifest/version을 원자 전진시키며 실제 release에서 `schedule-execution-material-v1`과 그 hash를 set-once 동결해 CollectionRun request fingerprint에 결속한다. publishing automation은 live Schedule을 읽지 않고 frozen dispatch/run exact 일치와 current target·auto-publish safety gate만 검증한다. legacy material은 replay 가능한 terminal ledger 외에는 release하지 않도록 `src/apps/scheduling/models.py`, migration 0003, services/tasks, `src/apps/collection/services.py`, `src/apps/publishing/automation.py`와 focused tests에 구현한다. 선행 dependency가 완료되기 전에는 체크하지 않는다. (depends on T002, T004, T005, T006, T009)
- [ ] T028 [US3] worker의 stop 상태 재조회와 queued 작업 취소, run/step/channel terminal 집계, all-source failure 보존, queue-one 정상 해제, kill-switch payload idempotency와 선택 단계·target 재시도를 `src/apps/collection/`, `src/apps/evidence/tasks.py`, `src/apps/editorial/tasks.py`, `src/apps/publishing/tasks.py`, `src/apps/scheduling/services.py`에 구현 (depends on T005, T006, T020, T021, T027)
- [ ] T029 [US3] 원문 변경 감시 beat→관리자 verify/reject→새 revision·재검증→정확한 WordPress update/unpublish→Blogger 반영→정정/철회 이력·감사를 intent별 attempt 기준으로 `src/apps/editorial/corrections.py`, `src/apps/publishing/corrections.py`, `src/apps/editorial/tasks.py`, `src/apps/publishing/tasks.py`에 구현 (depends on T005, T006, T013, T018, T021, T022, T024, T028)
- [ ] T030 [US3] raw·draft·published snapshot·audit·public delivery·WordPress media 전체 category에 immutable preview item과 checksum/object-version precondition, hold dependency graph, 실제 S3/원격 객체 삭제와 tombstone을 `src/apps/audit/retention.py`, `src/apps/audit/models.py`, `src/apps/publishing/models.py`, `config/retention/default.json`에 구현 (depends on T005, T006, T009, T022, T029)
- [ ] T031 [US3] schedule CRUD·expected-version CAS, kill switch, run stop·selective retry, correction 검증, retention preview/detail/items/approve/execute와 audit 조회 API·관리자 화면을 OpenAPI와 일치하도록 `src/apps/scheduling/api.py`, `src/apps/audit/api.py`, `src/apps/editorial/api.py`, `src/templates/admin_console/operations/`, `src/static/admin_console/operations.js`에 구현 (depends on T007, T027, T028, T029, T030)

**Checkpoint**: 예약 실행, 운영 중지, 정정과 보존 정책을 관리자 화면에서 일관된 식별자와
감사 기록으로 통제할 수 있다.

---

## Phase 6: Release Gate — 자동 검증과 문서 정합성

**목적**: 구현 완료를 체크박스가 아니라 자동화된 계약·통합·E2E와 SC 측정 결과로 증명한다.

- [ ] T032 수집·권리·중복·citation, PDF/HWP/locator, 승인·멱등·fencing·reconcile, 일정 locking·중지·정정·보존, 재인증·CSRF·SSRF·비밀 redaction, outbox·audit와 전체 관리자 여정을 `tests/unit/`, `tests/contract/test_evidence_extractor.py`, `tests/contract/test_publishers.py`, `tests/integration/test_collection_pipeline.py`, `tests/integration/test_scheduling_operations.py`, `tests/integration/test_security_audit.py`, `tests/e2e/test_admin_journey.py`에 구현하고 `spec.md`의 SC-001~SC-012 및 PDF/HWP golden 결과를 기록한다. Legacy HWP acceptance artifact는 immutable object key/version/SHA-256, target OCI digest, converter manifest hash, schema/all-pass와 지원 corpus 및 unsupported/warning/missing-font/exit20/21/22/tamper의 zero-derived-output/raw-quarantine/manual recovery를 증명한다. 현재 hard-false activation gate를 열기 전에 exact versioned artifact bytes fetch, SHA-256/schema/subject/OCI/manifest/all-results 검증과 외부 signed-release trust root 검증을 반드시 구현한다. 형식상 올바른 reference나 runtime self-report/hard-coded policy는 admission 증거로 인정하지 않는다. hermetic vendored/offline source·crate·deb·SBOM와 signed release attestation도 필수 구현한다. 이를 결속한 새 1.2.0 golden=true profile을 승인하되 1.1.0은 immutable superseded draft로 보존한다. (depends on T001-T031)
- [ ] T033 실제 구현·자동 검증 결과와 운영 절차를 기준으로 `README.md`, `specs/001-automated-content-publishing/tasks.md`, `specs/001-automated-content-publishing/quickstart.md`, `specs/001-automated-content-publishing/contracts/`, 운영 runbook을 갱신하고 더 이상 부분 구현을 완료로 표시하지 않도록 문서 추적성을 정리 (depends on T032)

---

## 의존성 및 실행 순서

### Phase 의존성

1. Phase 1은 즉시 시작할 수 있다.
2. Phase 2는 Phase 1의 관련 기반 작업을 사용하며 production 외부 쓰기를 차단하는 공통 gate다.
3. US1은 안전한 근거와 초안을 만든다.
4. US2는 US1의 revision·claim·evidence 계약에 의존한다.
5. US3는 US1 수집 identity와 US2 publication state machine에 의존한다.
6. Phase 6은 T001~T031 구현 완료 뒤 수행하는 최종 release gate다.

### 병렬 실행 가능 묶음

- Setup: T001, T002, T003
- Foundational: T004, T005, T007
- US1: T010 또는 T011과, 별도 추출 파일 경계의 T014
- US2와 US3는 공통 service 파일과 명시적 의존성이 있으므로 현재 main 작업에서는 순차 실행한다.

### 출시 차단선

- T001~T008 미완료: production 외부 채널 연결 금지
- T009~T018 미완료: 출처 검증 완료 초안으로 판정 금지
- T019~T026 미완료: 자동발행 활성화 금지
- T027~T031 미완료: 예약 production 실행 금지
- T032 미완료: 운영 출시 완료 선언 금지

---

## 구현 전략

1. 공통 안전 기반 T001~T008을 먼저 닫는다.
2. US1 T009~T018로 외부 발행 없는 수동 run→근거→초안을 완성한다.
3. US2 T019~T026으로 승인 기반 WordPress→Blogger 발행과 복구를 완성한다.
4. US3 T027~T031로 일정·중지·정정·보존 운영을 연결한다.
5. T032에서 자동화 검증과 성공 기준을 측정한다.
6. 검증 근거가 확보된 뒤에만 T033 문서와 완료 표시를 갱신한다.

---

## AI Execution Metadata (English)

T015 keeps legacy-HWP profile 1.1.0 immutable and inactive while enforcing the exact-capability
root supervisor, zero-capability child/validator, subreaper cleanup, and exact streamed provenance.
T032 owns immutable artifact/signature validation, hermetic signed release material, and approval
of golden profile 1.2.0. It must replace the deliberately hard-false gate only with exact
versioned artifact-byte and external-trust-root signature verification. Version 1.1.0 remains an
immutable superseded draft and is not retired.

T018 resolves the active topic policy from release JSON and persists an append-only snapshot without
an approval workflow or mutable head. Same key/version with changed release or implementation bytes
is a permanent conflict. Canonical revisions use bodyBlocks and freeze sorted verification, eligible
evidence, and excluded/duplicate/conflict snapshots. They distinguish only `fact`, `company_claim`,
`interpretation`, and `outlook`, pass the ten exact editorial gates plus the conditional visual gate,
and recheck current policy/evidence eligibility. Manual edits remain pending behind
`editorial.revalidate_requested` until the entire claim graph and gate report are rebuilt.

T019 owns the immutable approval decision domain and the exact existing approval-POST contract. It
uses both `expectedLatestApprovalId` and `expectedHeadVersion`, persists decision reason/hash and
material/head versions, and returns the current head projection without inferring it from the replayed
row. `decidedBy` is the non-null approval owner; actual admin/worker actor provenance is stored
separately and worker actor IDs are null. Revocation and approved unpublish require purpose-bound reauthentication. T026, not T019, owns
new route registration, approval-history pagination, admin UI, and E2E coverage. T019 remains unchecked
until T018 and its external blockers are complete.

T020 separates intent creation from dispatch. Intent request replay is resolved before mutable CAS by
an article-scoped request identity, versioned request hash, and authoritative head projection. All target
snapshot/command/validation/activation reference sets are bounded to 20 and semantic duplicate target IDs
are 422. Valid UUID input is case-insensitive and canonicalizes to lowercase for hashing, storage, and
responses, including the path article UUID; `revisionNo` is bounded to 1 through 9007199254740991 at API
and direct service boundaries. Every administrator and worker caller supplies a required trimmed,
audit-safe `reason`. Intent
create/replay is 201/200; changed material is 409. Dispatch canonicalizes omitted and null `publishAt`
identically and accepts strict RFC 3339 (`T/t`, seconds, `Z/z|+/-HH:MM`) non-null instants, canonicalizing
the separator to uppercase `T` and the instant to UTC `Z`, then atomically persists one
append-only dispatch ledger, one first attempt per frozen target, outbox work, and audit material. An
unconditional `(publication_intent, publication)` unique constraint preserves one logical row while retries
advance its counters. Dispatch create/replay is 202/200 with a bounded immutable result whose acceptance
`attemptNo` is always 1; it never returns mutable retry state or unbounded history. T026 owns route completion, cursor history,
admin UI, and E2E coverage. T020 remains unchecked until T019 and predecessor blockers are complete.

T021 separates acceptance, delivery, business execution, domain claim, receipt lease, and read-only
reconciliation generations. New execution/reconcile uses the exact v2 payloads. Version 1 may only replay
an exact existing source-event binding or bind a proven virgin generation one; all other v1 deliveries are
audited `legacy-unverifiable-v1` no-op quarantines. Attempt/execution rows freeze authoritative internal
raw capabilities and audit-only hashes. Each reconcile claim/reclaim appends an identity-frozen
`PublicationReconcileDeliveryObservation` whose state/finish time advances monotonically; a late result
references that exact observation.

The pre-I/O fence checks an unexpired current raw capability at statement time immediately before I/O;
settlement accepts the same current raw token/generation after clock expiry when no higher reclaim exists.
Create mutates only after a schema-complete bounded read proves exact target/blog, complete pagination,
and zero lookup/marker matches. A mutation 2xx succeeds only when either that response or an immediate
bounded authenticated exact GET proves exact target/blog, remote ID, state, title, and canonical actual body.
Retry-After supports delay-seconds and IMF-fixdate. A processing receipt
stores an all-or-none set-once terminal reservation including internal raw `terminal_lease_token`; callback
verification is mandatory and reserved receipt deletion/success is forbidden. Audit binds event,
domain/reconcile/receipt generations, consumer, token hash, write authorization/marker, delivery
observation, and terminal reservation hash/error, never raw tokens. Remote URLs are bounded to
1000-character HTTP(S); invalid raw values are discarded and only their SHA-256 is retained. Bounded
reconciliation and immutable Dispatch-ledger rules remain unchanged. T026 owns OpenAPI history/retry
routes, cursor pagination, UI and E2E. T021 stays unchecked until T020 and predecessor blockers complete.

T023 stores only a credential reference identity and secret version in database target material. The
Blogger bundle has exact access/refresh tokens, Bearer type, required Blogger scope, UTC expiry and
version, while signed OAuth state binds administrator, server session, target snapshot/config, redirect
URI and nonce for ten minutes. Expired-token refresh is single-flight through the target lock and
secret-store expected-version CAS, preserving an unrotated refresh token. Disconnect is remote-first:
Google revokes the refresh token and WordPress deletes and verifies absence of the exact introspected
Application Password UUID. Only proven success clears local reference/version; ambiguous outcomes remain
reconciling. Raw credential material is forbidden from APIs, problems, audit and logs. T023 acceptance
is backed by the dedicated credential lifecycle suite plus publishing worker/approval/intent regressions.

T024 freezes a protected Blogger-to-WordPress logical-attempt FK for every action. Its dependency subject
hash binds the immutable same-intent/revision/article/environment lineage, while the Blogger final render
binds the exact verified WordPress URL in canonical URL, body, and content hash. Dispatch and correction
create WordPress first; release, execution, final rendering, and reconciliation never search for another
WordPress Publication. Cross-environment/test material and ambiguous legacy cohorts fail closed. T024
remains unchecked until the T022 predecessor is eligible for completion.

```yaml
schema_version: "1.0"
feature: "001-automated-content-publishing"
task_count: 33
task_id_range: "T001-T033"
source_review_date: "2026-07-28"
execution_policy:
  implementation_status_source: "code plus acceptance evidence, never checkbox alone"
  production_writes_blocked_until: ["T001", "T002", "T003", "T004", "T005", "T006", "T007", "T008"]
  auto_publish_blocked_until: ["T009-T026", "T032"]
  production_release_blocked_until: ["T001-T032"]

phases:
  setup:
    tasks: ["T001", "T002", "T003"]
  foundational:
    tasks: ["T004", "T005", "T006", "T007", "T008"]
  user_story_1:
    priority: "P1"
    tasks: ["T009", "T010", "T011", "T012", "T013", "T014", "T015", "T016", "T017", "T018"]
  user_story_2:
    priority: "P2"
    tasks: ["T019", "T020", "T021", "T022", "T023", "T024", "T025", "T026"]
  user_story_3:
    priority: "P3"
    tasks: ["T027", "T028", "T029", "T030", "T031"]
  release_gate:
    tasks: ["T032", "T033"]

dependencies:
  T001: []
  T002: []
  T003: []
  T004: []
  T005: []
  T006: ["T004", "T005"]
  T007: []
  T008: ["T005"]
  T009: ["T002", "T004", "T006", "T007"]
  T010: ["T001", "T009"]
  T011: ["T001", "T009"]
  T012: ["T001", "T009", "T010", "T011"]
  T013: ["T002", "T009", "T010", "T011", "T012"]
  T014: ["T002", "T003"]
  T015: ["T001", "T003", "T014"]
  T016: ["T005", "T014", "T015"]
  T017: ["T001", "T014", "T015", "T016"]
  T018: ["T002", "T013", "T016", "T017"]
  T019: ["T004", "T006", "T018"]
  T020: ["T005", "T019"]
  T021: ["T020"]
  T022: ["T002", "T018", "T020", "T021"]
  T023: ["T001", "T003", "T004"]
  T024: ["T020", "T021", "T022", "T023"]
  T025: ["T004", "T009", "T014", "T018", "T019", "T021", "T022", "T023", "T024"]
  T026: ["T007", "T019", "T020", "T021", "T022", "T023", "T024", "T025"]
  T027: ["T002", "T004", "T005", "T006", "T009"]
  T028: ["T005", "T006", "T020", "T021", "T027"]
  T029: ["T005", "T006", "T013", "T018", "T021", "T022", "T024", "T028"]
  T030: ["T005", "T006", "T009", "T022", "T029"]
  T031: ["T007", "T027", "T028", "T029", "T030"]
  T032: ["T001-T031"]
  T033: ["T032"]

parallel_groups:
  - ["T001", "T002", "T003"]
  - ["T004", "T005", "T007"]
  - ["T010", "T014"]
  - ["T011", "T014"]

traceability:
  admin_security_and_credentials: ["FR-001", "FR-013", "FR-017", "CR-004", "T003", "T004", "T019", "T023", "T025"]
  source_collection_and_evidence: ["FR-003", "FR-004", "FR-005", "FR-006", "FR-007", "FR-024", "CR-002", "CR-005", "CR-006", "T009-T018"]
  article_identity_and_quality: ["FR-008", "FR-009", "FR-010", "FR-022", "CR-001", "CR-003", "CR-007", "T013", "T018"]
  publication_safety: ["FR-011", "FR-012", "FR-013", "FR-015", "FR-016", "FR-023", "T019-T026"]
  scheduling_recovery_corrections_retention: ["FR-014", "FR-017", "FR-018", "FR-019", "FR-020", "FR-021", "T027-T031"]
  measurable_success: ["SC-001-SC-012", "T032"]
```
