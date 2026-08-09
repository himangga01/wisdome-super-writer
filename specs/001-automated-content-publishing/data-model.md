# 데이터 모델: 주제 기반 자동 블로그 발행

## 모델링 원칙

- PostgreSQL이 실행, 승인, 발행과 감사 상태의 유일한 기준이다.
- 원문과 추출 자산은 수정하지 않고 새 버전을 추가한다. DB에는 객체 저장소의 키,
  버전, MIME, 크기와 SHA-256을 저장한다.
- 실제 발행된 모든 사실 주장과 시각 자료는 당시의 증거·권리·정책 버전까지 재현할 수
  있어야 한다.
- 외부 API 토큰과 비밀 값은 모델에 직접 저장하지 않고 비밀 저장소 참조만 둔다.
- 업무 식별자는 UUIDv7을 사용하고, 사람이 보는 실행 번호는 별도 짧은 표시값으로 둔다.
- 모든 시각은 UTC로 저장하고 화면과 일정 계산에서 `Asia/Seoul`을 기본 적용한다.
- `canonical JSON` 지문은 문자열 Unicode NFC 정규화 후 RFC 8785 JCS로 직렬화하고
  SHA-256을 계산한다.

## 열거형

| 이름 | 값 |
|---|---|
| `TopicCode` | `housing_subscription`, `semiconductor_news` |
| `AuthorityTier` | `primary_official`, `primary_regulatory`, `primary_corporate`, `trusted_industry`, `trusted_secondary`, `discovery_only` |
| `AccessMethod` | `public_api`, `open_data_api`, `rss_atom`, `public_html`, `public_file` |
| `RightsStatus` | `allowed`, `attribution_required`, `internal_analysis_only`, `unknown`, `prohibited` |
| `EvidenceKind` | `text`, `table`, `pdf`, `image`, `chart`, `spreadsheet`, `screenshot`, `attachment` |
| `EvidenceDerivationType` | `raw`, `document_derived`, `other_derived`, `visualization_derived` |
| `LocatorType` | `document_block`, `spreadsheet_cell`, `html_dom`, `structured_path`, `media_time`, `image_region`, `hwpx_path`, `hwp_conversion`, `visualization`, `manual` |
| `DocumentInputKind` | `pdf`, `standalone_image` |
| `ExtractionEngine` | `native_pdf`, `paddleocr_ppstructurev3`, `html_parser`, `structured_parser`, `spreadsheet_parser`, `hwpx_parser`, `legacy_hwp_converter`, `browser_capture`, `media_parser`, `manual_entry` |
| `ExtractionState` | `queued`, `running`, `succeeded`, `low_confidence`, `failed` |
| `GenericValidationMode` | `deterministic`, `calibrated`, `manual` |
| `ArticleType` | `housing_notice`, `housing_correction`, `semiconductor_daily_digest`, `semiconductor_breaking`, `correction` |
| `RunTrigger` | `manual`, `schedule`, `source_change`, `retry` |
| `SourceDiscoveryKind` | `new_version`, `unchanged`, `corrected`, `retracted`, `unavailable`, `restored` |
| `ApprovalMode` | `manual`, `validated_auto` |
| `OverlapPolicy` | `skip`, `queue_one` |
| `ChannelCode` | `wordpress`, `blogger` |
| `ChannelRole` | `primary_canonical`, `secondary_distribution` |
| `RenderStage` | `preview`, `final` |
| `CanonicalLinkState` | `not_applicable`, `pending`, `resolved` |
| `PublicationAction` | `create`, `update`, `unpublish`, `mark_withdrawn` |
| `RemotePublicationState` | `draft`, `scheduled`, `published`, `withdrawn`, `deleted`, `unknown` |
| `TargetValidationState` | `not_run`, `passed`, `failed`, `stale` |
| `TargetEnvironment` | `test`, `production` |
| `CorrectionKind` | `correction`, `retraction`, `source_unavailable` |

## 엔터티

### AdminAccount

Django의 사용자 모델을 확장한다. MVP에서는 활성 staff 계정 하나만 허용한다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 관리자 식별자 |
| `email` | Email, unique | 로그인 ID |
| `password_hash` | string | Django 해시; 평문 저장 금지 |
| `is_active`, `is_staff` | boolean | 접근 조건 |
| `last_reauthenticated_at` | datetime nullable | 고위험 작업 재인증 시각 |
| `created_at`, `updated_at` | datetime | 감사 시각 |

### ReauthenticationProof

고위험 mutation용 세션 결합 단기 증명이다. `id`, `admin_id`, `session_binding_hash`, 정렬된
`action_scopes`, `issued_at`, `expires_at`(최대 5분), `consumed_at`, `consumed_entity_type/id`,
`consumed_action`, `state: active/consumed/expired/revoked`를 가진다. 현재 비밀번호와 구성된 MFA를
다시 확인한 뒤 발급하며 비밀번호/MFA 값은 저장·로그하지 않는다. proof ID는 bearer credential이
아니고 동일 관리자 session+CSRF에만 유효하다. mutation은 scope/entity/expiry를 행 잠금으로
검증하고 결정/AuditEvent와 같은 트랜잭션에서 1회 소비한다. idempotent replay는 저장된 decision의
request key로 반환하며 proof를 다시 소비하지 않는다.

### OperationalControl / KillSwitchDecision

`OperationalControl`은 `key=global_kill_switch` singleton, `enabled`, `version`,
`latest_decision_id`, `updated_at`을 가진 PostgreSQL current projection이다.
`KillSwitchDecision`은 `id`, `expected_control_version`, `decision: enabled/disabled`,
`request_key`, `request_hash`, `reauth_proof_id` nullable, `reason`, `decided_by`, `decided_at`을
가지며 request key가 고유하다. enable(외부 쓰기 차단)은 즉시 가능하고 disable은
`kill_switch_disable` proof가 필수다. control 행 잠금/CAS 아래 decision, projection,
AuditEvent를 원자 저장하며 동일 key/payload는 기존 결과, stale/different payload는 409다.
scheduler와 모든 publisher worker는 dispatch 직전과 각 외부 API 호출 직전에 current enabled/
version을 DB에서 재조회한다. enable 경합 중 이미 시작된 호출은 unknown-outcome reconcile로
귀결하고 새 호출은 0건이다.

### TopicPolicy

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 정책 식별자 |
| `code` | TopicCode | 주제 코드 |
| `version` | positive int | 주제별 단조 증가 버전 |
| `status` | `draft/approved/retired` | 승인 상태 |
| `freshness_rules` | JSONB | 자료 유형별 최신성 기준 |
| `verification_rules` | JSONB | 1차/교차 검증 조건 |
| `article_rules` | JSONB | 건별, 일일 요약, 속보 규칙 |
| `breaking_rules` | JSONB nullable | 반도체 4개 범주와 출처 기준 |
| `approved_by`, `approved_at` | FK/datetime nullable | 승인 기록 |

고유 제약: `(code, version)`. 한 주제당 `approved` 정책은 하나만 활성화한다.

### SourceDefinition

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 출처 식별자 |
| `topic_code` | TopicCode, indexed | 소속 주제 |
| `name`, `publisher` | string | 표시명과 발행 주체 |
| `authority_tier` | AuthorityTier | 검증 가중치 |
| `independence_group_id` | string | 동일 owner/editorial control을 한 출처 그룹 |
| `owner_name`, `editorial_control_name` | string | 독립성 판정 snapshot material |
| `base_url` | URL | 공식 기준 URL |
| `access_method` | AccessMethod | 허용 접근 방식 |
| `adapter_key` | string | 호출할 어댑터 |
| `external_config` | JSONB | 비밀을 제외한 엔드포인트/매개변수 |
| `secret_ref` | string nullable | 비밀 저장소 참조 |
| `allowed_mime_types` | string array | 수집 허용 형식 |
| `default_rights_status` | RightsStatus | 자산 기본 권리 상태 |
| `terms_url`, `robots_url`, `license_url` | URL nullable | 접근·권리 근거 |
| `poll_interval_seconds` | positive int | 최소 점검 간격 |
| `rate_limit_policy` | JSONB | 동시성/분당 호출 제한 |
| `external_config.accessPolicy` | policy v1 JSONB | 승인 시각·User-Agent·요청/재시도/redirect 예산과 origin별 목적·method·path·robots 판정 |
| `external_config.rightsPolicy` | policy v1 JSONB | record·document attachment·media attachment별 권리 근거·귀속 문구·게시 가능 판정 |
| `enabled` | boolean | 운영 여부 |
| `latest_approved_snapshot_version` | positive int nullable | draft는 null인 read-only projection |
| `latest_draft_snapshot_id`, `latest_draft_snapshot_version`, `latest_draft_config_hash` | FK/int/SHA-256 nullable | create/PATCH가 만든 current draft material |
| `last_health` | JSONB | 마지막 점검 결과 |

인증 헤더, 쿠키와 토큰은 `external_config`에 넣을 수 없다. `topic_code`는 생성 후
불변이며 다른 주제로 옮기려면 새 SourceDefinition을 만든다. 정의 변경 승인 시
`SourceDefinitionSnapshot`을 생성한다.

### SourceDefinitionSnapshot

실행 재현을 위한 개별 출처의 불변 JSON 스냅샷이다. `id`, `source_definition_id`,
`topic_code`, `snapshot_version`, `config_json`, `config_hash`, `frozen_config_json`,
`frozen_config_hash`, `accessPolicy`, `accessPolicyHash`, `rightsPolicy`, `rightsPolicyHash`,
`independence_group_id`, `owner_name`, `editorial_control_name`,
`status: draft/approved/retired`, `request_key`, `request_hash`, `created_at`, `approved_at`을
가진다. `config_json/config_hash`는 기존 registry와 run이 참조한 역사적 identity material을
보존한다. adapter는 mutable SourceDefinition이나 이 역사 필드를 다시 조합하지 않고,
별도로 정규화·해시한 `frozen_config_json/frozen_config_hash`만 실행 입력으로 사용한다.
신규 v2 snapshot은 두 material/hash가 같고, legacy snapshot은 역사 hash를 바꾸지 않은 채
당시 실행에 필요한 legacy wrapper만 frozen material로 분리한다.
`topic_code`는 SourceDefinition과 같고 `(source_definition_id, snapshot_version)`이 고유하다.
SourceDefinition create/PATCH는 definition projection과 immutable draft snapshot을 한
트랜잭션으로 만들며 `(source_definition_id, request_key)`가 멱등이다. 응답은 draft snapshot
ID/version/config hash를 노출한다. Registry approval은 모든 enabled membership snapshot이
approved이거나 해당 registry가 선택한 draft인지 검증하고, 선택 draft의 approve 결정,
SourceDefinition latest-approved projection, registry decision과 AuditEvent를 한 트랜잭션으로
확정한다. retired snapshot은 선택할 수 없고 registry approval 실패 시 일부 source만 승인되지
않는다.

### SourceRegistrySnapshot

주제별 실행이 사용하는 출처 집합 전체의 불변 승인 버전이다. `id`, `topic_code`, `version`,
`status: draft/approved/retired`, `manifest_hash`, `approved_by`, `approved_at`, `created_at`을
가지며 `row_version`, `latest_decision_id`, `draft_request_key`, `draft_request_hash`도 보존한다.
draft는 `base_approved_registry_id`, `base_approved_version`, `base_approved_manifest_hash`를
추가로 고정한다.
`(topic_code, version)`과 `(topic_code, draft_request_key)`가 고유하고 주제별
approved 최신 버전은 하나다.
`SourceRegistryMembership`은 `source_registry_snapshot_id`,
`source_definition_snapshot_id`, `enabled`, `display_order`를 가지며 registry 안에서 같은
SourceDefinition은 한 번만 포함된다. manifest hash는 membership을 SourceDefinition ID와
snapshot ID, enabled, order 순으로 정규화한 전체 집합의 NFC+RFC 8785 JCS SHA-256이다.
한 source만 변경해 새 registry version을 승인해도 변경되지 않은 모든 활성 source의 직전
snapshot membership을 원자적으로 carry-forward한다. disabled/retired source도 해당 과거
registry manifest에는 그대로 남아 과거 실행을 재현한다.
저장소 seed는 같은 draft를 재실행하면 기존 draft를, 현재 approved head와 source/policy
material이 모두 같으면 그 approved registry를 반환하며 새 snapshot이나 registry를 만들지
않는다. head retire 뒤 같은 seed를 실행하면 retired registry를 재생하지 않고 새 draft를
만들되 재사용 가능한 승인 source snapshot은 carry-forward한다.

draft membership 변경은 registry 행의 `expected_row_version`과 manifest hash를 CAS로 검사하고
변경·전체 carry-forward manifest 재계산·AuditEvent를 원자 처리한다. approved/retired registry는
불변이다. `TopicRegistryHead`는 `topic_code`, `current_approved_registry_id/version/manifest_hash`,
`row_version`을 가지는 topic별 단일 CAS projection이다. `SourceRegistryDecision`은 `id`,
`source_registry_snapshot_id`, `version`,
`decision: approved/retired`, `expected_row_version`, `expected_manifest_hash`,
`expected_current_head_id/version/manifest_hash`, `supersedes_decision_id`, `request_key`,
`request_hash`, `decision_hash`, `decided_by`, `decided_at`, `reason`을
가진다. 최근 재인증이 필수이며 같은 request key/payload는 기존 결정을, stale CAS는 409를
반환한다. `(source_registry_snapshot_id, version)`과
`(source_registry_snapshot_id, request_key)`가 고유하다. approve는 topic head를 잠그고
draft base와 current head 및 요청 expected head가 모두 같은지 확인한다. 오래된 분기 draft는
409로 rebase/new draft를 요구한다. 선택 source snapshot 승인, 이전 head retire/supersede, 새
head, decision/projection/AuditEvent를 한 트랜잭션으로 저장한다.
전이는 draft→approved→retired이며 retired는 terminal이다. replacement approve는 이전 head를
retired로 만들고 새 head를 설정한다. current head 직접 retire는 head ID/version/hash를 null로
만들고 그 topic의 Schedule/새 CollectionRun을 즉시 차단한다. 과거 head로 자동 rollback하지
않으며 재개하려면 새 draft를 승인해야 한다. non-current/retired 대상 전이는 409다.
각 membership PUT은 `SourceRegistryMutation(id, registry_id, source_definition_id, request_key,
request_hash, before_manifest_hash, after_manifest_hash, resulting_row_version, created_at)`을 함께
추가하며 `(registry_id, request_key)`가 고유하다. 동일 key/payload는 기존 결과를, 다른 payload는
409를 반환한다.

### Schedule

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 일정 식별자 |
| `version` | positive int | 변경마다 증가하는 schedule 버전 |
| `topic_code` | TopicCode | 실행 주제 |
| `cron_expression` | string | 5필드 cron; 초 단위 미지원 |
| `timezone` | IANA timezone | 기본 `Asia/Seoul` |
| `window_minutes` | positive int | 수집 대상 시간창 |
| `target_ids` | UUID array | 발행 대상 |
| `approval_mode` | ApprovalMode | 승인 방식 |
| `overlap_policy` | OverlapPolicy | 중복 실행 처리 |
| `enabled` | boolean | 일정 활성 상태 |
| `next_run_at`, `last_dispatched_at` | datetime nullable | 디스패치 상태 |
| `validated_policy_version` | int nullable | 자동발행 검증 버전 |
| `auto_publish_validation_refs`, `auto_validation_manifest_hash` | sorted JSONB array/SHA-256 | validated_auto일 때 target별 현재 검증 ID/material hash와 정렬 manifest |
| `auto_publish_activation_refs`, `auto_activation_manifest_hash` | sorted JSONB array/SHA-256 | validated_auto일 때 target별 current enable 결정 ID/version/hash와 정렬 manifest |
| `updated_by` | FK AdminAccount | 마지막 변경자 |

`validated_auto`는 대상별 검증 기록과 activation이 현재 출처·추출·생성·편집·품질·채널
정책 및 target operational snapshot과 모두 일치할 때만 저장할 수 있다. manual 일정에서는
두 ref 배열이 모두 비어 있어야 한다.

`target_ids`에 `secondary_distribution` Blogger가 있으면 같은 일정에 활성
`primary_canonical` WordPress target도 반드시 포함한다. 초기 발행은 WordPress를 먼저
처리하고, 재시도만 이미 성공한 WordPress Publication을 근거로 Blogger 단독 실행할 수 있다.

### ScheduleDispatch

다중 scheduler의 cron tick·overlap 결정을 보존한다. `id`, `schedule_id`, `schedule_version`,
`scheduled_for`, `tick_key`, `state: skipped/queued/dispatched/coalesced`, `reason_code`,
`collection_run_id` nullable, `coalesced_into_dispatch_id` nullable, `created_at`, `dispatched_at`
nullable, queued root에는 `coalesced_window_start/end`, 정렬 `coalesced_tick_manifest_hash`,
`tick_set_version`을 가지며 `(schedule_id, scheduled_for)`와 `tick_key`가 고유하다. scheduler는 schedule
행을 잠그고 해당 schedule의 active run과 queued dispatch를 한 트랜잭션에서 확인한다.
`skip`은 반드시 skipped 행을 남긴다. `queue_one`은 active run이 있으면 partial unique
`UNIQUE(schedule_id) WHERE state=queued`인 pending 하나만 만들고 후속 tick은 그 ID를 가리키는
coalesced 행만 만든다. active run terminal 전이와 pending dispatch→CollectionRun/outbox 생성은
같은 lock에서 처리한다. coalesced tick마다 pending root의 bounded union window와 tick manifest/
version을 원자 갱신하고, 실제 dispatch lock 시 그 최종 union을 한 번만 CollectionRun snapshot과
request fingerprint에 고정한다. 동일 tick 100회 또는 scheduler 두 노드·3 ticks 경합도
run/outbox 하나며 전체 tick window를 빠짐없이 포함한다.

### AutoPublishValidation

자동발행 자격을 임의 hash로 만들지 못하게 하는 append-only 검증 기록이다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 검증 식별자 |
| `topic_code` | TopicCode | 검증 주제 |
| `target_snapshot_id`, `target_config_hash` | FK/SHA-256 | 검증한 정확한 발행 대상 |
| `source_registry_snapshot_id`, `registry_manifest_hash` | FK/SHA-256 | 검증한 출처 집합 |
| `source_adapter_manifest_hash` | SHA-256 | 활성 source adapter 구현·설정 묶음 |
| `extraction_profile_manifest_hash` | SHA-256 | native/PaddleOCR/generic 승인 profile material 묶음 |
| `generation_pipeline_manifest_hash` | SHA-256 | 초안 생성기·prompt/schema/후처리 묶음 |
| `topic_policy_version`, `editorial_policy_hash` | int/SHA-256 | 사실·편집 기준 |
| `quality_gate_manifest_hash` | SHA-256 | blocking 품질 검사 코드·버전·threshold 묶음 |
| `render_contract_version`, `channel_contract_version` | string | 채널 렌더·공식 API 계약 |
| `publisher_adapter_manifest_hash` | SHA-256 | 실제 request/action mapping 구현 묶음 |
| `test_report_object_key`, `test_report_object_version`, `test_report_hash` | scalar | immutable canary/회귀 보고서 |
| `material_hash` | SHA-256 unique | 위 nullable 포함 전체 검증 material 지문 |
| `status` | `draft/passed/revoked/stale` | decision 전 draft와 현재 자격 projection |
| `latest_decision_id`, `decision_version` | FK nullable/int | current append-only validation decision projection |
| `invalidated_at`, `invalidation_reason` | datetime/text nullable | 만료·철회 근거 |

한 validation은 target 하나만 검증한다. Schedule/CollectionRun은 target별 validation ID와
material hash의 정렬 manifest를 고정한다. registry, target snapshot, topic/editorial/render/
source adapter, extraction profile(PaddleOCR package/runtime/model/config 포함), generation,
quality gate, channel contract나 test report 중 하나라도 바뀌면
기존 validation은 stale다. candidate 생성은 `(target_id, request_key)` 멱등이며 동일 material
hash는 기존 candidate를 반환한다. 새 validation은 immutable 테스트 보고서와 재인증 관리자
결정 없이는 `passed`가 될 수 없고, import/worker가 임의 승인할 수 없다.

### AutoPublishValidationDecision

`id`, `validation_id`, `version`, `decision: passed/revoked`, `supersedes_decision_id`,
`request_key`, `request_hash`, `decision_hash`, `decided_by`, `decided_at`, `reason`을 가진 append-only 관리자
결정이다. `(validation_id, version)`과 `(validation_id, request_key)`가 고유하다. 생성은
validation latest projection을 잠그고 `expected_latest_decision_id` CAS를 확인하며 동일 key/payload는
기존 결정을, stale CAS나 다른 payload는 409를 반환한다. `passed`는 보고서 object version/hash와
모든 material hash가 로컬/DB 승인 snapshot에 일치하고 최근 재인증된 관리자만 허용한다.
결정·projection·AuditEvent를 한 트랜잭션에 저장한다. material이 이후 바뀌면 결정 행은 보존하고
validation status projection만 stale가 된다.

### AutoPublishActivation

검증과 운영 config를 먼저 확정한 뒤 자동발행을 켜는 append-only 결정이다. `id`,
`target_id`, `target_snapshot_id`, `target_operational_config_hash`, 정렬된
`auto_publish_validation_ids`, `validation_manifest_hash`, `version`,
`decision: enabled/revoked`, `supersedes_activation_id`, `request_key`, `activation_hash`,
`request_hash`, `reauth_proof_id`, `decided_by`, `decided_at`, `reason`을 가진다. activation hash는 target/snapshot/operational
hash, 정렬 validation ID/material hash, version, decision, supersedes ID를 NFC+RFC 8785 JCS로
정규화해 계산한다. `(target_id, version)`과 `(target_id, request_key)`가 고유하다.

결정 트랜잭션은 target latest-activation projection 행을 잠그고 요청의
`expected_latest_activation_id`를 비교한다. 같은 request key·같은 payload는 기존 결정을
반환하고, 다른 payload 또는 stale CAS는 409다. enable은 모든 validation이 현재 `passed`이고
같은 operational hash일 때만 insert/projection/AuditEvent를 한 트랜잭션으로 만든다. off는
새 revoked activation을 추가해 즉시 projection을 끄며 PublicationTargetSnapshot을 다시 만들지
않는다. `auto_publish_enabled`는 latest activation이 enabled이고 그 operational hash가 current
target snapshot과 같으며 모든 validation이 여전히 current/passed일 때만 true다. target config나
검증 material 변경은 즉시 false/stale로 재계산한다.

Schedule, CollectionRun과 PublicationIntent는 target별 `AutoPublishActivationRef`의 activation
ID, target/snapshot ID, version, activation hash를 고정한다. revoke 시 그 activation을 참조한
미실행 validated-auto intent는 모두 `stale/cancelled`되고 예약 outbox도 외부 호출 전에
차단된다. 이후 re-enable은 새 activation과 이를 참조하는 새 superseding intent를 요구하며
과거 intent를 되살리지 않는다.

세 엔터티의 `validated_auto` 저장 불변조건은 target ID 집합 = target snapshot ref의 target
집합 = validation ref의 target 집합 = activation ref의 target 집합이다. 각 배열에는 target별
정확히 한 행만 있고 ID가 중복될 수 없다. 같은 target의 모든 ref는 동일 target snapshot
ID/config hash를 가리키며 activation이 고정한 validation ID/material 집합과 요청 validation
refs도 정확히 같다. 배열 정렬/manifest hash와 DB FK를 한 트랜잭션에서 검증한다. manual은
validation/activation ref와 두 manifest가 모두 빈 배열/null이어야 한다.

### CollectionRun

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUIDv7 PK | 상관관계 ID 겸 실행 ID |
| `display_id` | string unique | 관리자 표시용 ID |
| `topic_code` | TopicCode | 실행 주제 |
| `trigger` | RunTrigger | 시작 원인 |
| `schedule_id` | FK nullable | 일정 실행인 경우 |
| `window_start`, `window_end` | datetime | 자료 시간 범위 |
| `source_registry_snapshot_id`, `registry_version`, `policy_version` | FK/int/int | 실행 시 고정된 주제별 출처 집합과 정책 버전 |
| `registry_manifest_hash` | SHA-256 | 실행 시 고정된 registry manifest |
| `topic_policy_id`, `policy_hash` | FK/SHA-256 | 실행 시 고정한 활성 TopicPolicy와 전체 정책 지문 |
| `freshness_minutes`, `freshness_cutoff` | positive int/datetime | `window_end - freshness_minutes`로 고정한 최신성 경계 |
| `allowed_authority_tiers` | ordered string array | TopicPolicy에서 고정한 수집 허용 authority tier |
| `requested_target_ids` | sorted UUID array | 실행 생성 시 고정한 발행 대상; 빈 배열은 초안 전용 |
| `requested_target_snapshot_refs` | sorted JSONB array | target ID, immutable target snapshot ID/config hash 묶음 |
| `target_snapshot_manifest_hash` | SHA-256 | 위 정렬 배열의 NFC+RFC 8785 JCS 지문; 빈 배열도 고정 hash |
| `approval_mode` | ApprovalMode | 실행 생성 시 고정한 승인 방식 |
| `schedule_snapshot`, `schedule_snapshot_hash` | JSONB/SHA-256 nullable | 일정 ID/version, cron/timezone/window/targets/mode의 불변 요청 snapshot |
| `auto_validation_snapshot`, `auto_validation_snapshot_hash` | JSONB/SHA-256 nullable | validated_auto 자격의 출처·편집·채널 정책/검증 지문 |
| `auto_publish_validation_refs` | sorted JSONB array | target별 AutoPublishValidation ID/material hash |
| `auto_publish_activation_refs` | sorted JSONB array | target별 AutoPublishActivation ID/version/hash |
| `auto_activation_manifest_hash` | SHA-256 nullable | 위 activation ref 정렬 배열의 지문 |
| `request_fingerprint` | SHA-256 unique | 아래 전체 불변 실행 의도의 정규 지문 |
| `dedupe_key` | string unique | request fingerprint와 중복 정책에서 만든 키 |
| `state` | RunState | 현재 단계 |
| `stop_requested_at` | datetime nullable | 즉시 중지 요청 |
| `counters` | JSONB | 수집/제외/검증/초안/발행 수 |
| `error_summary` | JSONB nullable | 최종 오류 요약 |
| `started_at`, `completed_at` | datetime nullable | 실행 시각 |

`RunState`: `queued → collecting → extracting → validating → drafting → awaiting_approval → publishing → completed`.
어느 작업 단계에서도 `stopping → stopped`, 재시도 한도 초과 시 `failed`로 전이할 수 있다.
완료·중지·실패 상태에서 직접 이전 단계로 돌아가지 않고 `trigger=retry`인 새 실행 또는
명시적 하위 작업 재개를 생성한다.

request fingerprint는 topic, window, trigger, source registry snapshot ID/manifest hash,
TopicPolicy ID/version/hash, freshness 기준, authority tier, 정렬된 target snapshot
refs/manifest hash, approval mode, schedule snapshot hash,
auto-validation snapshot hash와 auto-activation manifest hash의 null 표현까지 포함한다.
CollectionRun target/mode snapshot은 자동 파이프라인의
최초 의도와 감사 기준이며 외부 쓰기는 아래 append-only PublicationIntent를 사용한다.
이후 Schedule·target·정책 수정은 진행/과거 run을 변형하지 않는다. `validated_auto`이면 auto-validation snapshot/hash가 필수이고 현재 실행의
고정 registry/editorial/channel 정책과 일치하고 activation refs도 생성 시점 current enabled
결정과 일치해야 한다.
`requested_target_ids` 집합과 `requested_target_snapshot_refs[].target_id` 집합은 정확히
같아야 하고 각 ref의 target ID, snapshot ID와 config hash도 같은 PublicationTargetSnapshot
행과 일치해야 한다.

### RunStep

단계별 시도와 관측 정보를 보존한다. `run_id`, `step_name`, `attempt_no`, `state`,
`worker_task_id`, `started_at`, `finished_at`, `input_count`, `output_count`, `error_code`,
`error_detail_redacted`, `retry_at`, `fanout_completed_at`을 가진다.
`(run_id, step_name, attempt_no)`가 고유하다.

`fanout_completed_at`은 extract parent가 모든 child attempt와 outbox를 영속 생성한 뒤에만
설정하는 완료 projection이다. evidence finalizer는 이 값이 null인 동안 run을 다음 단계로
전진시키지 않는다. marker 도입 시점의 복구는 `CollectionRun=extracting`,
`RunStep(name=extract, attempt_no=1, state=running)`, 시작 시각 있음, marker 없음인 행만
대상으로 한다. 기존 run counters에 `evidence`와 `extractionFailures`가 모두 있으면 과거
parent 완료 증거로 marker를 복원하고 finalizer를 재발행한다. 증거가 없으면 marker를 만들지
않고 같은 run의 fan-out을 안정 dedupe key로 다시 요청한다. 이미 전진했거나 terminal인
run/step은 변경하지 않는다. 이 data migration은 historical model과 schema editor의 DB
alias만 사용하며, immutable outbox material과 hash 공식도 migration 안에 고정한다. 따라서
이후 runtime model이나 enqueue helper가 바뀌어도 과거 upgrade 의미는 변하지 않는다.
parent route 소진 시 아직 extracting인 run과 step만 종결하고, stop 요청이 있으면 두
projection을 모두 `stopped`, 없으면 run=`failed`, step=`failed`로 맞춘다.

`RunRetryRequest`는 `id`, `collection_run_id`, `from_step`, 정렬 target IDs, `request_key`,
`request_hash`, `reauth_proof_id`, `reason`, `requested_by`, `created_at`, `accepted_job_id`를 가진
append-only 관리자 명령이다. `(collection_run_id, request_key)`가 고유하며 proof의
`bulk_retry` scope/entity를 같은 트랜잭션에서 소비한다. 동일 key/payload는 기존 job을,
다른 payload는 409를 반환한다.

### RunSourceItem

한 SourceItem이 여러 CollectionRun에서 재발견될 수 있으므로 실행 계보는 명시적 join으로
보존한다. `id`, `collection_run_id`, `source_collection_attempt_id`, `source_item_id`, `source_snapshot_id`,
`previous_run_source_item_id`(같은 lineage의 직전 관측 self FK),
`discovery_kind: SourceDiscoveryKind`,
`discovered_at`을 가지며 `(collection_run_id, source_item_id)`가 고유하다.
`source_snapshot_id`의 SourceDefinition은 SourceItem의 `source_definition_id`와 같아야
하고 그 snapshot은 CollectionRun의 `source_registry_snapshot_id`에 enabled membership으로
포함돼야 한다. registry snapshot의 topic/version/manifest hash는 CollectionRun의 고정
topic/registry version과 같아야 한다.
실행별 evidence 조회는 이 join에서 시작하고 event의 `run_id`는 클라이언트가 임의 지정한
값이 아니라 이 행의 `collection_run_id`와 대조한다.

`discovery_kind`는 다음처럼 계산한다. 최초 active 버전이나 active 상태의 새 content/의미
메타데이터 버전은 `new_version`, 동일 source version hash 재발견은 `unchanged`, status가
각각 corrected/retracted/unavailable이면 같은 이름의 kind, retracted/unavailable 뒤 active로
돌아오면 `restored`다. 모든 후속 관측은 `previous_run_source_item_id`로 같은 lineage의
직전 RunSourceItem을 가리킨다. 새 source version hash로 SourceItem 행을 만들 때만 그 행의
`supersedes_id`가 직전 SourceItem 버전을 가리킨다. `unchanged`는 새 SourceItem이나
`source.item_changed`를 만들지 않고 RunSourceItem만 추가한다.
`new_version/corrected/restored`는 변경 이벤트 후 추출·검증을
시작하고, `retracted/unavailable`은 신규 추출 대신 정정/철회 영향을 평가한다.

### SourceCollectionAttempt

collector 구현까지 재현하는 source 단위 durable 실행 projection이다. `id`,
`collection_run_id`, `source_definition_snapshot_id`, `adapter_name`, `adapter_version`,
`adapter_implementation_manifest_hash`, `adapter_config_hash`, `request_fingerprint`,
`request_window_start/end`, `response_checksum` nullable, `http_status` nullable,
`state: queued/running/retry_scheduled/succeeded/failed/skipped`, `failure_category`,
`error_code`, `error_detail_redacted`, `retry_count`, `retry_at`, `retry_after_seconds`,
`request_count`, `response_count`, `access_policy_hash`, `rights_policy_hash`, `authority_tier`,
`freshness_cutoff`, `freshness_excluded_count`, `duration_ms`, `started_at`, `finished_at`을 가진다.
`(collection_run_id, source_definition_snapshot_id, request_fingerprint)`가 고유하다. RunSourceItem은
반드시 자신을 만든 succeeded attempt를 참조하고, adapter/response 지문은 SourceItem identity/
version 판단과 PublishedEvidenceSnapshot provenance에 포함한다. 배포 adapter manifest가 승인된
source-adapter manifest와 다르면 수집 전에 실패하며 event hint만 신뢰하지 않는다.

각 enabled membership은 독립 `source.collect_requested` 이벤트를 가진다. 기본적으로
`policy/schema/authentication/security` 접근 오류는 영구 실패이며, 명시적으로 retryable인
`transient/infrastructure` 오류만 `retry_scheduled`와 `retry_at`을 기록하고 outbox 전달 예산
안에서 재시도한다. 영구 오류나
전달 예산 소진은 해당 attempt만 `failed`로 끝낸다. `freshness/authority/rights` 제외는
별도 failure category로 기록한다. finalizer는 모든 source attempt가 terminal일 때만
집계하며 성공 출처가 하나도 없으면 run을 `failed`로 끝내고 evidence fan-out을 만들지 않는다.
하나 이상 성공하면 부분 실패를 counters/error summary에 남기고 `extracting`으로 전진한다.
최신성 판정 시각은 `modified_at`을 우선하고 없을 때 `published_at`을 사용한다. 신규 evidence를
만들 수 있는 `active/corrected` record에 같은 기준을 적용하고, 명시적 `reconciliation_only`와
신규 evidence를 만들지 않는 `retracted/unavailable`만 시간창 판정의 예외로 둔다. 성공한 source에서
시간창 밖 자료를 제외한 경우에도 attempt와 해당 전달 observation에 `freshness` 분류와
`freshness_excluded_count`를 남기고 run counters/error summary에 같은 합계를 투영한다.

### SourceCollectionObservation

source 전달 세대마다 남기는 append-only 관측이다. `id`,
`source_collection_attempt_id`, `delivery_attempt_no`, `outcome`,
`failure_category`, `error_code`, `error_detail_redacted`, `http_status`,
`retry_count`, `retry_at`, `retry_after_seconds`, `request_count`, `freshness_excluded_count`, `duration_ms`,
`access_policy_hash`, `authority_tier`, `freshness_cutoff`, `recorded_at`을 가진다.
`(source_collection_attempt_id, delivery_attempt_no)`가 고유하며 instance/queryset/base manager와
PostgreSQL trigger가 update/delete를 거부한다.
attempt는 최신 운영 projection이고 observation은 재시도와 terminal 결정의 감사 계보다.
worker 종료로 정산되지 않은 앞선 전달 번호는 다음 전달 또는 소진 callback이
`source_delivery_interrupted` infrastructure 관측으로 보충한다.

### SourceItem

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 원문 식별자 |
| `source_definition_id` | FK | snapshot 버전과 무관한 출처 identity |
| `first_seen_source_snapshot_id` | FK | 이 content 버전을 처음 수집한 정확한 출처 설정 |
| `external_id` | string nullable | 출처가 제공한 안정 ID |
| `canonical_url` | URL | 정규화 URL |
| `title`, `publisher` | string | 원문 메타데이터 |
| `published_at`, `modified_at`, `fetched_at` | datetime nullable | 원문/수집 시각 |
| `language` | BCP-47 string | 원문 언어 |
| `status` | `active/corrected/retracted/unavailable` | 원문 상태 |
| `content_hash` | SHA-256 | 정규화 콘텐츠 해시 |
| `source_version_hash` | SHA-256 | 본문·상태·의미 있는 메타데이터를 묶은 append-only 버전 지문 |
| `origin_source_identity_hash` | SHA-256 nullable | 전재/보도자료의 최초 보고 identity; 자체 원보도면 자신의 identity |
| `syndication_kind` | `original/syndicated/unknown` | 독립 출처 수에서 전재 중복을 제거하는 분류 |
| `raw_object_key`, `raw_object_version` | string | 원본 객체 위치 |
| `raw_checksum` | SHA-256 | 원본 검증값 |
| `http_metadata` | JSONB | ETag, Last-Modified, MIME 등 |
| `supersedes_id` | self FK nullable | 정정 관계 |

`source_version_hash`는 external ID/canonical URL, title, publisher, published/modified 시각,
language, status, content hash, raw checksum과 의미 있는 ETag/Last-Modified/MIME을 null까지
명시해 Unicode NFC+RFC 8785 JCS로 직렬화한 SHA-256이다. 매 수집 시각인 `fetched_at`은
제외한다. 안정 ID가 있으면 `(source_definition_id, external_id, source_version_hash)`,
없으면 `(source_definition_id, canonical_url, source_version_hash)`가 버전 고유키다.
`external_id` 또는 canonical URL은 lineage key이지 단독 고유키가 아니다. 같은 lineage의
source version hash가 바뀌면 새 SourceItem을 만들고 `supersedes_id`로 직전 버전을
가리키며, 같은 hash가 다시 수집되면 기존 버전을 재사용한다. 따라서 본문이 같아도
정정·철회 상태나 의미 있는 메타데이터가 바뀌면 append-only 버전이 생기고,
SourceDefinitionSnapshot이 새로 승인된 것만으로는 같은 관측을 새 identity로 만들지
않는다. RunSourceItem은 이 lineage가 아니라 해당 실행에서 실제로 관측한 구체 SourceItem
버전 ID와 그 실행의 정확한 `source_snapshot_id`를 가리킨다. active A → unavailable B →
동일 hash의 active A 복구에서는 최초 A SourceItem을 재사용하고 새 RunSourceItem의
`previous_run_source_item_id`가 B 관측을 가리켜 `restored` 전이를 보존한다.
`supersedes_id`는 같은 SourceDefinition과 lineage의 직전 새 버전만 가리킬 수 있다.

### ExtractionProfileSnapshot

문서·비문서 추출기가 참조하는 승인 설정의 불변 기준 저장소다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | profile snapshot |
| `profile_key`, `profile_version` | string | 사람이 관리하는 안정 식별자와 버전 |
| `engine` | ExtractionEngine | 적용 엔진 |
| `extractor_version` | string | 승인된 추출기 구현 버전 |
| `package_version`, `runtime_version`, `pipeline_name` | string nullable | 설치 패키지, 실행 런타임, 파이프라인 버전/이름 |
| `implementation_manifest_hash` | SHA-256 | 추출기 코드·패키지·런타임 manifest 지문 |
| `approval_state` | `draft/approved/retired` | 신규 실행 허용 상태 |
| `latest_decision_id`, `decision_version` | FK nullable/int | current append-only 관리 결정 projection |
| `config` | JSONB | 정규화된 전체 설정 |
| `config_hash` | SHA-256 | NFC+JCS config 지문 |
| `validation_mode` | GenericValidationMode nullable | 비문서 profile의 검증 의미 |
| `calibration_profile_key`, `calibration_profile_version` | string nullable | 승인된 보정 profile 식별자와 버전 |
| `calibration_manifest_object_key`, `calibration_manifest_object_version` | string nullable | 골든 표본 ID/version, metric, 임계값, 평가 결과 manifest |
| `calibration_profile_hash` | SHA-256 nullable | calibration manifest 지문 |
| `model_manifest`, `model_manifest_hash` | JSONB/SHA-256 nullable | PaddleOCR 모델 파일 manifest |
| `profile_material_hash` | SHA-256 | 아래 전체 승인 material의 NFC+RFC 8785 JCS 지문 |
| `created_at`, `approved_at`, `retired_at` | datetime nullable | 수명 주기 |

`(profile_key, profile_version)`과 `(engine, profile_material_hash)`가 고유하다.
`profile_material_hash`는 `extractor_version`, `package_version`, `runtime_version`,
`pipeline_name`, `implementation_manifest_hash`, `config_hash`, `validation_mode`,
`calibration_profile_key`, `calibration_profile_version`, `calibration_profile_hash`,
`model_manifest_hash`를 필드별 `null`까지 명시해 Unicode NFC 정규화 후 RFC 8785 JCS로
직렬화하고 SHA-256으로 계산한다. 따라서 PostgreSQL의 nullable 복합 `UNIQUE` 의미에
의존하지 않는다. snapshot은 수정하지 않고 새 버전을 만든다. producer는 attempt/run을
만들 때 `approved` snapshot ID를 저장하고 event의 key/version/hash hint도 같은 행에서
복사한다. worker는 snapshot을 ID로 다시 읽어 event hint, 추출기·패키지·런타임·파이프라인
버전, 로컬 구현/model manifest, config, calibration key/version/hash를 모두 비교한 뒤에만
실행한다. 실행 후 retired돼도 기존 감사·재현은 유지하지만 신규 실행에는 사용할 수 없다.
profile 전이는 `draft→approved→retired`만 허용하며 retired→approved는 409다. 같은 material을
다시 쓰려면 새 profile version/snapshot과 새 관리자 결정을 만든다.

`ExtractionProfileDecision`은 `id`, `profile_snapshot_id`, `version`,
`decision: approved/retired`, `expected_material_hash`, `supersedes_decision_id`, `request_key`,
`request_hash`, `decision_hash`, `verification_report_object_key`,
`verification_report_object_version`, `verification_report_hash`, `decided_by`, `decided_at`,
`reason`을 가진다. migration 호환을 위해 세 report 필드는 DB에서 nullable이지만 새 approved
결정에는 모두 필수이며 profile projection과 정확히 일치해야 한다. retired 결정은 직전 approved
결정의 frozen report envelope를 그대로 복사한다. envelope는 decision hash와 AuditEvent metadata에도
포함되어 이후 profile projection 변경으로 감사 근거가 바뀌지 않는다.
0004 migration은 기존 decision chain에 현재 profile projection의 key/version/hash를 backfill한다.
기존 행에 당시 envelope가 따로 저장되지 않았으므로 이것이 복원 가능한 유일한 호환 경계다.
projection이 없거나 불완전하면 임의 추정하지 않고 migration을 중단한다. 따라서 기존 서비스는
retire/API 검증을 계속할 수 있지만, backfill 값이 결정 당시 값이었다고 새로 주장하지 않는다.
`(profile_snapshot_id, version)`과 `(profile_snapshot_id, request_key)`가 고유하다. profile projection 행을 잠그고
`expected_latest_decision_id`와 material hash를 CAS로 확인하며 최근 재인증이 필수다. 같은
request key/payload는 기존 결정을 반환하고 stale/different payload는 409다. decision,
projection, AuditEvent를 원자 저장하며 import/worker는 결정을 만들 수 없다.

profile과 AutoPublishValidation의 보고서 API는 별도 mutable 승인 데이터가 아니라 immutable
object를 hash 검증해 만든 `VerificationReport` read model이다. subject type/ID/material hash,
object key/version/report hash, 비민감 sample ID/hash와 expected/observed outcome hash, metric,
threshold, stage 결과, overall result와 생성·검증 시각만 노출한다. 원문·토큰·개인정보는
포함하지 않는다. object version/hash 또는 subject material hash가 DB 기준과 다르거나 보고서
일부를 로드하지 못하면 응답과 approve/passed 결정을 모두 거절한다.
approved/retired profile API는 latest decision에 동결된 envelope를 source of truth로 사용하며,
mutable profile projection과 하나라도 다르면 409를 반환한다. 승인 후 report command는 기존
versioned core bytes와 DB/decision envelope를 재검증만 하고 새 object/version으로 덮어쓰지 않는다.

### DocumentExtraction

한 PDF 또는 독립 정적 이미지 원본의 전체 페이지 라우팅과 완전성을 판정하는 상위
집계다. 독립 이미지는 `input_page_count=1`, `expected_page_indices=[0]`인 가상 1페이지
문서로 취급해 PDF와 같은 PaddleOCR provenance와 감사 규칙을 사용한다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 문서 추출 식별자 |
| `run_source_item_id` | RunSourceItem FK | 실행과 원문을 함께 고정하는 계보 |
| `source_item_id` | FK | 입력 원문 |
| `input_asset_id` | EvidenceAsset FK nullable | SourceItem에 딸린 특정 원본 첨부/자산 |
| `input_object_key`, `input_object_version` | string | 실제 처리한 불변 객체 위치 |
| `input_kind` | DocumentInputKind | PDF 또는 독립 정적 이미지 |
| `input_fingerprint` | unique SHA-256 nullable | `document-input-v1`, run-source-item ID, 입력 종류, 입력 checksum의 canonical hash; null은 과거 중복 격리 전용 |
| `input_mime_type` | string | sniff 후 allowlist와 일치한 실제 MIME |
| `input_frame_count` | integer nullable | 독립 이미지의 디코더 확인 frame 수; PDF는 null |
| `input_checksum` | SHA-256 | 입력 PDF/이미지 지문 |
| `input_page_count` | integer >= 1 | 안전 파서가 확인한 원본 전체 페이지 수 |
| `expected_page_indices` | integer array | 반드시 `0..input_page_count-1`인 전체 집합 |
| `covered_page_indices` | integer array | 선택된 child run 결과의 합집합 |
| `routing_manifest` | JSONB | 페이지별 native/PaddleOCR 선택, child run과 선택 이유 |
| `coverage_manifest_hash` | SHA-256 nullable | 전체 페이지→선택 결과 manifest 지문 |
| `selected_evidence_manifest_hash` | SHA-256 nullable | 선택된 전체 EvidenceAsset ID/checksum manifest 지문 |
| `document_complete` | boolean | DB에 저장하되 아래 불변조건으로 재계산 가능한 완료 projection |
| `state` | ExtractionState | 전체 문서 결과 |
| `started_at`, `finished_at` | datetime nullable | 실행 시각 |
| `error_code`, `error_detail_redacted` | string nullable | 원문·비밀을 제외한 오류 |

생성 시 `expected_page_indices`는 DB 또는 도메인 계층이 전체 범위로 만들며 임의 요청값을
받지 않는다. 독립 이미지는 실제 MIME이 승인된 정적 이미지이고 디코더의 frame 수가
정확히 1일 때만 허용하며 전체 범위를 `[0]`으로 고정한다. 다중 프레임 TIFF/APNG와
애니메이션 입력은 첫 프레임만 처리하지 않고 `failed/unsupported_multiframe_image`로
종결한다. `state=succeeded`와
`document_complete=true`, `coverage_manifest_hash`와 `selected_evidence_manifest_hash`는
선택된 child ExtractionRun의
처리 페이지 합집합이 expected 집합과 정확히 같고 누락·중복 충돌이 없으며 모든 선택
결과가 품질 게이트를 통과한 경우에만 허용한다. `low_confidence` child가 하나라도 있으면
현재 review subject에 대한 모든 필수 관리자 결정이 approved일 때만 true가 된다. 승인
대기·거절·stale 결정이면 false이고 두 manifest hash는 null이다. child run은 전체 성공을 선언하거나
`evidence.document_ready`를 발행할 수 없다.
`run_source_item_id`가 가리키는 SourceItem은 같은 행의 `source_item_id`와 같아야 하며,
그 join의 `collection_run_id`만 document event의 `run_id`로 허용한다. 이 계보 불일치는
트랜잭션에서 거절한다.

`input_fingerprint`는 같은 run/source 계보와 같은 실제 입력이 재전달될 때 하나의
DocumentExtraction만 선택하는 전역 고유 identity다. 기존 행을 재사용할 때 계보, 입력 종류,
checksum이 모두 다시 일치해야 하며 다르면 fail-closed 처리한다. 정상 PDF/이미지 경로와 이미
성공 또는 저신뢰로 끝난 legacy HWP 변환 결과의 PDF 승격 경로가 이 identity와 동일한 route
outbox dedupe 규칙을 공유한다. 신규 행에는 fingerprint가 필수다. upgrade 전 동일 identity의
과거 행이 여러 개면 `document_complete+succeeded`, `succeeded`, `low_confidence`,
실패/종료, 그 밖의 상태 순으로 canonical 행 하나를 고르고 그 안에서는 가장 이른 행을
선택한다. 나머지 행은 삭제·병합하거나 합성 fingerprint를 부여하지 않고 null로 보존해
감사 이력과 singleton identity를 동시에 유지한다.

성공 또는 저신뢰로 이미 종결된 legacy HWP 변환은 PDF 승격 전에 연결된 EvidenceAsset을
재사용한다. 연결이 누락된 과거 행은 같은 attempt의 유효한 객체 위치와 SHA-256 checksum을
가진 파생 자산이 정확히 하나일 때만 결합을 복구한다. 후보가 없거나 여러 개이거나 object
material이 불완전하면 임의 자산을 선택하지 않고 attempt를 `failed`로 종결하며 terminal
finalizer를 같은 transaction에 기록한다.

### ExtractionRun

DocumentExtraction 아래의 결정적 페이지 묶음을 어떤 추출기·모델·설정으로 처리했는지
재현한다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | child 추출 실행 식별자 |
| `document_extraction_id` | FK | 전체 PDF 집계 |
| `retry_of_run_id` | self FK nullable | 저신뢰 대체 profile 또는 운영 재실행의 원 실행 |
| `extraction_profile_snapshot_id` | FK | 승인된 불변 profile 기준 |
| `engine` | ExtractionEngine | 네이티브 추출 또는 PaddleOCR 등 승인 엔진 |
| `page_set_hash` | SHA-256 | 결정적 요청 페이지 집합 지문 |
| `fingerprint_schema_version` | string | 정규 fingerprint 공식 버전 |
| `extraction_fingerprint` | SHA-256 | 상위 문서·입력·엔진·profile·버전·model/config의 정규 직렬화 hash |
| `requested_page_indices`, `processed_page_indices` | integer array | 0-based 요청·완료 페이지 집합 |
| `profile_key`, `profile_version`, `config_hash` | string/SHA-256 | 승인 추출 프로필과 설정 지문 |
| `profile_material_hash` | SHA-256 | 실행에 고정된 ExtractionProfileSnapshot 전체 material 지문 |
| `package_version`, `runtime_version` | string | PaddleOCR와 PaddlePaddle 또는 파서 버전 |
| `pipeline_name` | string nullable | PaddleOCR이면 `PPStructureV3` |
| `model_manifest`, `model_manifest_hash` | JSONB/SHA-256 nullable | 모델명·로컬 경로·파일 SHA-256 목록 |
| `language_profile`, `device_type` | string nullable | 한국어/영어 라우팅과 CPU/GPU 실행 종류 |
| `state` | ExtractionState | child 실행 결과 |
| `low_confidence_reasons` | JSONB nullable | block/필드별 차단 근거 |
| `low_confidence_reasons_hash` | SHA-256 nullable | 정규화된 저신뢰 사유 지문 |
| `low_confidence_reasons_object_key`, `low_confidence_reasons_object_version` | string nullable | 큰 reason manifest 객체 참조 |
| `result_object_key`, `result_checksum` | string/SHA-256 nullable | 불변 추출 결과 manifest와 지문 |
| `started_at`, `finished_at` | datetime nullable | 실행 시각 |
| `duration_ms`, `peak_memory_bytes` | integer nullable | 용량·성능 회귀 지표 |
| `error_code`, `error_detail_redacted` | string nullable | 원문·비밀을 제외한 오류 |

fingerprint v1은 `fingerprint_schema_version`, `document_extraction_id`, DocumentExtraction의
input kind/MIME/frame count/checksum, page set, `extraction_profile_snapshot_id`,
`profile_material_hash`, engine,
`profile_key`, `profile_version`, package/runtime, model
manifest와 config hash의 null 표현까지 포함한다. `(document_extraction_id,
extraction_fingerprint)`가 고유하며, 성공 결과 재사용과 동시 실행 단일화도 같은 상위
DocumentExtraction 안에서만 수행한다. 동일한 PDF가 다른 SourceItem으로 다시 수집되면 새
DocumentExtraction과 child run을 만들고 서로의 수명 주기·권리 판정을 공유하지 않는다.
PaddleOCR 실행은 `package_version=3.7.0`,
`pipeline_name=PPStructureV3`, 비어 있지 않은 model manifest와 config hash를 요구한다.
child `state=succeeded`는 요청·처리 페이지 집합이 정확히 같을 때만 허용한다. 대체 profile은
원 run을 `low_confidence`로 종결한 뒤 새 fingerprint·run ID로 만들고 `retry_of_run_id`로
연결한다.

### GenericExtractionAttempt

HTML, 구조화 데이터, 스프레드시트, 브라우저 캡처, 미디어와 수동 입력처럼
DocumentExtraction을 사용하지 않는 비문서 파생 작업의 성공·저신뢰·실패를 재현한다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 비문서 추출 시도 |
| `run_source_item_id` | RunSourceItem FK | 실행과 원문을 함께 고정하는 계보 |
| `source_item_id`, `input_asset_id` | FK/FK nullable | 원문과 선택 입력 자산 |
| `evidence_asset_id` | FK nullable | 성공·저신뢰 결과; 실패면 null |
| `extraction_profile_snapshot_id` | FK | 승인된 불변 profile 기준 |
| `profile_material_hash` | SHA-256 | 실행에 고정된 snapshot 전체 material 지문 |
| `engine`, `extractor_version`, `config_hash` | scalar | 승인된 추출 구현 snapshot |
| `validation_mode` | GenericValidationMode | 검증 의미 |
| `calibration_profile_key`, `calibration_profile_version` | string nullable | 교정 프로필 식별자 |
| `calibration_profile_hash` | SHA-256 nullable | 골든 표본·metric·임계값 manifest 지문 |
| `fingerprint_schema_version`, `extraction_fingerprint` | string/SHA-256 | 현재 `v1`과 정규 작업 지문 |
| `state` | ExtractionState | `queued/running/succeeded/low_confidence/failed` |
| `result_checksum`, `low_confidence_reasons_hash` | SHA-256 nullable | 결과와 저신뢰 사유 지문 |
| `low_confidence_reasons_object_key`, `low_confidence_reasons_object_version` | string nullable | 검토 가능한 정규 reason manifest |
| `started_at`, `finished_at`, `error_code`, `error_detail_redacted` | scalar nullable | 관측·오류 |

fingerprint v1은 run source item ID, source/input asset ID와 checksum, extraction profile snapshot ID,
profile material hash,
engine/extractor version/config hash,
validation mode와 calibration profile hash를 null까지 포함해 정규화한다. 같은 입력 범위의
fingerprint는 하나만 실행한다. `succeeded/low_confidence`만 EvidenceAsset을 만들 수 있고
result checksum이 필수다. `failed`는 EvidenceAsset을 만들지 않으며 RunStep과 이 attempt에
오류를 기록한다. calibrated 저신뢰 결과가 게시되려면 현재 review subject에 대한 관리자
승인이 필요하다.
`run_source_item_id`의 SourceItem은 attempt의 `source_item_id`와 같아야 하며 join의
`collection_run_id`만 generic event의 `run_id`로 허용한다.

### EvidenceAsset

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 증거 자산 식별자 |
| `source_item_id` | FK nullable | 원문; multi-source visualization이면 null |
| `origin_run_source_item_id` | RunSourceItem FK nullable | 단일 출처 근거의 최초 실행·출처 snapshot; multi-source visualization이면 null |
| `derivation_type` | EvidenceDerivationType | 원본, PDF/문서 파생, 단일 출처 기타 파생 또는 복수 근거 시각화 |
| `document_extraction_id` | FK nullable | 전체 PDF 완전성 집계 |
| `extraction_run_id` | FK nullable | 이 근거를 만든 재현 가능한 추출 실행 |
| `generic_extraction_attempt_id` | FK nullable | 비문서 파생 근거를 만든 추출 시도 |
| `visualization_render_id` | FK VisualizationRender nullable | 복수 Claim/Evidence에서 만든 deterministic 시각화 provenance |
| `parent_asset_id` | self FK nullable | 원문→페이지→캡처 등의 파생 관계 |
| `kind` | EvidenceKind | 자산 유형 |
| `locator_type` | LocatorType nullable | locator discriminated union 종류 |
| `locator` | JSONB | page index, polygon/bbox, block/cell, 읽기 순서, 시트, CSS 경로, 시간 구간 |
| `object_key`, `object_version` | string nullable | 바이너리/파생 자산 위치 |
| `mime_type`, `byte_size`, `checksum` | scalar nullable | 파일 무결성 |
| `extracted_text` | text nullable | 추출 본문 |
| `structured_data` | JSONB nullable | 표 셀·HTML, 차트, 수식, 레이아웃과 메타데이터 |
| `extraction_method`, `extractor_version` | string nullable | 파생 자산의 engine/package snapshot |
| `extraction_config_hash` | SHA-256 nullable | generic 파서 또는 capture 설정 지문 |
| `validation_mode` | GenericValidationMode nullable | 비문서 파생 결과의 검증 의미 |
| `extraction_result_checksum` | SHA-256 nullable | 비문서 추출 결과 manifest 지문 |
| `calibration_profile_key`, `calibration_profile_version` | string nullable | 승인된 교정 기준 식별자 |
| `calibration_profile_hash` | SHA-256 nullable | 골든 표본·임계값·metric manifest 지문 |
| `confidence` | decimal 0..1 nullable | OCR/구조 추출 신뢰도 |
| `confidence_detail` | JSONB nullable | text/table/chart/formula 및 block별 원시·교정 신뢰도 |
| `low_confidence_reasons` | JSONB | high-impact 우선 결정 순서의 전체 비민감 immutable reason manifest |
| `rights_status` | RightsStatus | 사용 가능성 |
| `rights_basis_url`, `attribution_text` | text nullable | 권리·표시 근거 |
| `alt_text` | text nullable | 게시 시 대체 텍스트 |
| `review_state` | `pending/passed/rejected/manual_required` | 검토 상태 |
| `manual_review_required` | boolean | 저신뢰·충돌 핵심 값의 자동발행 차단 |
| `manual_reviewed_at`, `manual_reviewed_by_admin_id` | datetime/FK nullable | 명시적 관리자 판정 |
| `evidence_content_hash` | SHA-256 | 정규화된 extracted text/structured data/object 내용 지문 |
| `review_subject_schema_version` | string | 현재 `v1` |
| `review_subject_hash` | SHA-256 | 아래 불변 입력만 포함한 관리자 검토 지문 |
| `latest_review_decision_id` | EvidenceReviewDecision FK nullable | 현재 지문에 대한 최신 관리자 판정 |
| `publishable` | boolean | 계산·확정된 게시 허용 여부 |

단일 출처 근거는 `origin_run_source_item_id`에서 origin CollectionRun과
SourceDefinitionSnapshot을 유도한다. 문서/비문서 파생 근거는 각각
DocumentExtraction/GenericExtractionAttempt의
`run_source_item_id`와 반드시 같고, raw 근거는 최초 생성 RunSourceItem을 가리킨다. 이후
실행에서 같은 SourceItem 근거를 재사용해도 origin 계보는 바꾸지 않으며 실행별 조회는
요청 run의 RunSourceItem→SourceItem 관계로 필터한다. `visualization_derived`는 source/origin,
document/generic FK가 모두 null이고 visualization_render_id만 non-null이며, 아래 input join의
모든 출처가 정본이다.

`derivation_type=raw`는 추출 참조가 없어도 되며, `other_derived`는 HTML DOM, 구조화 경로,
스프레드시트 셀, HWPX XML 문단/표 셀, legacy HWP 변환 page/block, 미디어 시간 또는 이미지
영역 locator와 generic extractor version/config를
요구한다. `validation_mode=deterministic/manual`이면 `confidence`와 calibration profile은
null이고, `validation_mode=calibrated`인 경우에만 교정 기준이 명시된 0..1 confidence와
비어 있지 않은 calibration profile key/version/hash를 요구한다. `manual_entry`는 반드시
`validation_mode=manual`이다. engine, validation mode와 locator는 승인 매트릭스와
일치해야 한다. `document_derived`인 PDF 또는 독립 이미지
PaddleOCR EvidenceAsset은 `document_extraction_id`와
`extraction_run_id`, engine/package snapshot이 필수이고 `locator_type=document_block` 및
locator에
`page_index`, block type, polygon 또는 bbox, 읽기 순서를 보존한다. 표는 셀의 행·열·좌표·
텍스트·신뢰도와 정규화 HTML을, 차트·수식은 원본 영역과 구조 결과를 함께 보존한다.
`publishable=true`은 권리 상태가 `allowed` 또는 `attribution_required`이고 비어 있지 않은
`rights_basis_url`이 있어야 한다. `attribution_required`이면 비어 있지 않은
`attribution_text`도 필수이고, `image/chart/screenshot` 시각 자산은 비어 있지 않은
`alt_text`가 있어야 한다. 문서 파생 자산은 상위 DocumentExtraction 전체 페이지 완전성과
profile별 추출 신뢰도 기준을 통과한 경우에만 허용한다. 저신뢰 청약 가격·날짜·자격 등
고위험 값은 다른 근거의 존재와 무관하게 `manual_review_required=true`,
`review_state=manual_required`, `publishable=false`다. 교차 근거는 관리자 판단 자료일 뿐
`validated_auto`의 우회 조건이 아니다. child ExtractionRun은 이때 불변
`state=low_confidence`로 끝나며 사람의 판정으로 상태를 바꾸지 않는다. 관리자가 원문과
근거를 확인하고 현재 `review_subject_hash`에 대한 append-only EvidenceReviewDecision을
남긴 뒤에만 `manual_review_required=false`로 투영하고 게시 가능성을 다시 계산한다.
저신뢰 run에서 나온 자산이 `publishable=true`이면 현재 지문과 일치하는 명시적
`approved` 결정이 반드시 있어야 한다.
저신뢰 reason은 `impact`, code, field/block ref, 관측 confidence, threshold, calibration hash와
비민감 설명을 가진다. 전체 배열은 impact(high 우선), scope, field/block ref(null 우선),
code, 관측값, threshold, 설명 순으로 안정 정렬하고 Unicode NFC+RFC 8785 JCS hash를 child
run/attempt의 reason hash와 일치시킨다. API와 관리자 승인 화면은 이 배열 전체를 검증해
표시하며 한 항목이라도 로드·hash 검증에 실패하면 승인할 수 없다.

HWP 계열 locator는 DB enum/check와 API discriminator에서 같은 값과 정규형을 사용한다.
`hwpx_path`는 NFC 정규화한 archive-internal `section_path`(상대 POSIX 경로), paragraph/table/
row/column/embedded-object 식별자의 명시적 null을 저장하고 ZIP 밖 경로·`..`·역슬래시를
허용하지 않는다. paragraph locator는 `paragraph_id`가 non-null이고 table cell locator는
`table_id`, `row_index`, `column_index`가 모두 non-null이어야 한다. `hwp_conversion`은 원본 HWP와
출력 PDF checksum, 승인 converter manifest hash, sandbox report hash를 모두 요구한다.
`hwpx_parser↔hwpx_path`, `legacy_hwp_converter↔hwp_conversion` 조합만 허용하며 둘 다
`derivation_type=other_derived`, `validation_mode=deterministic`이다. 구현 migration은 HWP profile을
승인·활성화하기 전에 PostgreSQL `LocatorType` enum 또는 동등 CHECK에 두 값을 먼저 추가하고,
기존 행 검증·API contract test 뒤 worker를 배포한다.

`review_subject_hash` v1은 Unicode NFC 후 RFC 8785 JCS canonical JSON을 SHA-256으로
계산하며 선택 필드의 null도 명시한다. 입력은 정확히 다음과 같다.

- `review_subject_schema_version`, EvidenceAsset ID, SourceItem ID, origin RunSourceItem ID와
  source snapshot ID, 실제 입력 원문/첨부의 checksum
- `derivation_type`, `kind`, `locator_type`, 정규화 locator, `evidence_content_hash`
- confidence 값과 정규화 `confidence_detail`의 SHA-256
- 문서 파생이면 child `extraction_run_id`, `extraction_fingerprint`, `result_checksum`,
  `low_confidence_reasons_hash`
- 비문서 파생이면 GenericExtractionAttempt ID와 fingerprint, engine, extractor version,
  config hash, validation mode, result checksum, low-confidence reason hash와 calibrated 모드의
  calibration profile key/version/hash

상위 DocumentExtraction의 `state`, expected/covered page, `document_complete`,
`coverage_manifest_hash`, `selected_evidence_manifest_hash`, EvidenceAsset의 권리·검토·
게시 projection, 관리자 결정과 시각은
지문에서 반드시 제외한다. 따라서 승인으로 상위 집계가 완료돼도 검토 지문은 바뀌지
않는다. 위 입력 중 하나가 바뀔 때만 새 v1 지문을 계산하고 기존 결정은 stale 처리한다.

### EvidenceAuditSnapshot

검토 결정 또는 발행 승인 전에 만드는 비민감 불변 tombstone이다. `id`,
`original_evidence_asset_id`, `source_identity_hash`, `evidence_content_hash`, `locator_hash`,
`provenance_type`, `provenance_manifest_hash`, `low_confidence_reasons_hash` nullable,
`review_subject_schema_version`, `review_subject_hash`, `snapshot_hash`, `created_at`,
`raw_purged_at` nullable를 가진다. 원문 text/bytes, 상세 locator 내용과 비밀은 포함하지 않는다.
90일 raw purge 뒤에도 결정·감사·PublishedEvidenceSnapshot이 참조하는 동안 최대 1년 또는
관련 hold 종료까지 유지하며 원 EvidenceAsset/provenance FK가 null이 되어도 동일 subject를
식별한다.

### EvidenceReviewDecision

저신뢰·충돌 증거에 대한 관리자 판단을 추출 실행과 분리해 append-only로 보존한다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 판정 식별자 |
| `evidence_asset_id` | FK nullable | 90일 purge 전 검토 증거 |
| `evidence_audit_snapshot_id` | FK EvidenceAuditSnapshot | 장기 불변 검토 subject tombstone |
| `decision_provenance_type` | `raw/document/generic` | 아래 두 provenance FK의 XOR discriminator |
| `review_subject_schema_version` | string | 판정 공식 버전 `v1` |
| `review_subject_hash` | SHA-256 | 판정 당시 원문·locator·내용·confidence·provenance 지문 |
| `extraction_run_id` | FK nullable | purge 전 판정 대상 child run; 이후 audit snapshot manifest로 대체 |
| `generic_extraction_attempt_id` | FK nullable | purge 전 비문서 attempt; 이후 audit snapshot manifest로 대체 |
| `supersedes_decision_id` | self FK nullable | 판정 변경 시 직전 유효 결정 |
| `request_key` | string | 관리자 요청 멱등 키 |
| `decision` | `approved/rejected` | 관리자 판정 |
| `reason` | text | 판정 근거 |
| `reviewer_admin_id` | FK | 세션에서 결정한 관리자 |
| `decided_at` | datetime | 판정 시각 |

판정은 수정·삭제하지 않는다. `(evidence_audit_snapshot_id, request_key)`가 고유하며 같은 키·같은
payload는 기존 결정을 반환하고 같은 키·다른 payload는 충돌이다. 생성 트랜잭션은
EvidenceAsset 행과 대응 EvidenceAuditSnapshot을 잠그고 요청의 subject version/hash와
`expected_latest_decision_id`가 현재
projection과 모두 같은지 확인한 뒤 decision insert, latest projection 갱신과 outbox event를
원자적으로 수행한다. 불일치는 409다. 판정을 바꿀 때는 현재 latest ID를 CAS 값과
`supersedes_decision_id`로 사용한다. 새 추출, locator, 내용, confidence 또는 원문 checksum으로
`review_subject_hash`가 바뀌면 과거 승인은 자동 무효이며 다시 검토해야 한다. 승인 결정은
신뢰도를 바꾸거나 child run을 `succeeded`로 위조하지 않고, 현재 EvidenceAsset의 검토
projection과 상위 DocumentExtraction 완료 가능성만 갱신한다.

현재 decision은 EvidenceAsset ID와 subject version/hash가 projection과 같아야 한다.
`document_derived`이면 `extraction_run_id`가 현재 child run과 같고
`generic_extraction_attempt_id=null`, `other_derived`이면 그 반대이며 attempt ID가 현재
provenance와 같아야 한다. `raw`이면 두 provenance ID가 모두 null이다. 이 XOR와 ID equality는
행 잠금 트랜잭션의 DB/domain 불변조건이며 응답 직렬화 전에도 재검증한다.

### EventCluster

여러 출처의 같은 사건을 묶는 목표 계약은 `topic_code`, `canonical_event_key`, `headline`,
`event_time`, `category`, `breaking_candidate`, `verification_state`, `conflict_summary`,
`first_seen_at`, `last_seen_at`을 가진다. T013 현재 projection은 `topic_code`, `canonical_key`,
`title`, `verification_state`, `source_item_ids`, `created_at`까지 구현했으며, event semantic을
upstream adapter가 아직 공급하지 않는 필드는 후속 구현 대상으로 남긴다. `EventClusterItem`은 cluster와
정확한 `RunSourceItem`을 연결하고 `origin_identity_hash`, frozen snapshot의
`independence_group`, `role`, `selection_state`, `decision_reason`을 보존한다.
`(topic_code, canonical_key)`, `(event_cluster_id, run_source_item_id)`가 각각 고유하며 cluster
worker는 모든 canonical key를 전역 정렬한 뒤 upsert/lock해 동시 run을 하나로 합치고 교차 run의
반대 발견 순서로 인한 lock cycle을 피한다. 청약 cluster key는
공식 기관+공고 ID이며 정정 ID는 이 key에서 제외한다. 따라서 같은 공고의 정정은 동일 cluster에
합류하고 새 verification을 만든다. 정정 ID는 아래 article identity에는 포함한다. 반도체 key는
주체+행위+공식 발표 ID+KST 사건일의 canonical hash다.
`verification_state`는 최신 결정의 `candidate/verified_notice/daily_digest_candidate/
verified_breaking/held/rejected` projection이다. immutable history의 기준은 이 mutable 필드가 아니라
EventClusterVerification이다.

공식 source가 `corrected`를 반환했지만 안정적인 공식 correction ID를 제공하지 않으면 content/version
hash를 correction identity로 대체하지 않는다. 해당 공식 계보 head는
`official_correction_identity_missing`으로 fail-closed `rejected`하고, adapter가 correction document
또는 sequence identity를 공급한 새 run에서만 정정 article identity를 만든다.

`EventClusterVerification`은 `(cluster_id, version)`과 `(cluster_id, origin_run_id)`가 고유한
append-only 결정이다. `origin_run_id`가 cluster head보다 과거인 늦은 작업은 head를 덮어쓰지
않고, 정상 새 run의 결정은 그 run의 `(created_at, id)` causal key 이하 멤버만 사용한다.
이미 고정된 최신 head 뒤에 도착한 과거 run membership은 immutable 과거 결정을 소급 재작성하지
않으며 다음 정상 신규 run의 causal 범위에서 합류한다. 이는 처리 지연 때문에 이미 생성·검토된
기사의 근거 집합을 묵시적으로 바꾸지 않기 위한 의도된 observed-head 정책이다.
`decision`, `article_type`, `category`, `primary_source_count`, `independent_origin_count`,
`decision_reason`, run에 고정된 `policy_version/policy_hash`, `local_event_date`, 정렬된 전체
`evidence_manifest`와 그 SHA-256, `conflict_manifest`, `excluded_source_manifest`,
`rule_manifest_hash`, `result_manifest_hash`, `supersedes_id`, `verified_at`을 가진다. 목표 계약의
정렬 source snapshot ID, independence/origin ID, direct-primary 및 excluded company-claim 목록은
현재 evidence/excluded manifest 안에 명시적으로 포함한다. manifest는 선택·포함·
제외·충돌 멤버 모두의 RunSourceItem, SourceItem, SourceDefinitionSnapshot/frozen config hash,
origin/independence, reason code와 근거 ID/content hash를 보존한다. 같은 owner 그룹이나 같은
origin 보도 전재는 union된 한 독립 출처로 센다. 기업이 자기 제품의 세계 최초·수율·고객 채택을
발표한 자료는 ‘회사가 그렇게 발표했다’는 `company_claim`만 support하고 사건 사실의
direct-primary count에서는 독립 확인 전 제외한다.

반도체 속보는 `category`가 `regulation_export_control`, `factory_supply_disruption`,
`merger_or_material_earnings`, `critical_technology_or_mass_production` 중 하나이고, 공식·규제
1차 출처가 하나
이상이거나 서로 독립적인 group과 original-report identity가 둘 이상이어야
`verified_breaking`이 된다. 이 조건은 EventClusterVerification DB/domain gate로 강제한다.
조건 미달 중요 후보는 `held`이며 독립 breaking 생성 이벤트를 만들지 않는다. 대신 같은 run의
daily digest frozen verification set에는 `held` role로 포함한다. 비중요 사건은
`daily_digest_candidate`다. `eventSubject/eventAction/officialAnnouncementId/breakingCategory`가
모두 명시되지 않은 자료는 primary fallback으로 속보를 검증하지 않고 fail-closed
`daily_digest_candidate`로 내린다. 현재 semiconductor adapter가 이 semantic 묶음을 항상
공급하지 않는 것은 upstream blocker다. `retracted/unavailable`은 명시 reason으로 제외하고,
청약 공식 계보 head가 terminal이면 과거 active 자료를 다시 선택하지 않고 `rejected`한다.

### EditorialPolicySnapshot

편집 정책은 관리자 승인 상태나 mutable head를 갖지 않는다. 현재 정책은 배포에 포함된
`config/editorial-policies/{topic_code}.json`의 exact release document를 서버가 직접 해석해
결정한다.
해석한 material은 다음 append-only snapshot으로 보존한다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 정책 snapshot 식별자 |
| `policy_key`, `policy_version` | string unique pair | release JSON의 안정 key/version |
| `topic_code`, `language` | enum/BCP-47 | 적용 주제와 기본 `ko-KR` |
| `schema_version` | string | 정책 문서 schema |
| `release_document_hash` | SHA-256 | exact topic release JSON bytes |
| `config`, `config_hash` | JSONB/SHA-256 | NFC+RFC 8785 JCS canonical policy와 hash |
| `implementation_manifest`, `implementation_manifest_hash` | JSONB/SHA-256 | gate/generator/parser의 정렬 파일 경로와 exact hash |
| `material_hash` | SHA-256 unique | 위 immutable material 전체의 canonical hash |
| `created_at` | datetime | 최초 해석 시각; material identity에는 미포함 |

`(policy_key, policy_version)`은 유일하다. 같은 key/version의 snapshot이 이미 있으면 release document,
config, implementation과 material hash가 모두 같을 때만 재사용한다. 한 byte라도 다르면 새 행이나
암묵적 version을 만들지 않고 영구 충돌로 거절한다. queryset/model과 PostgreSQL·SQLite trigger는
insert 뒤 update/delete를 거부한다. event 재전달은 저장 snapshot을 사용하지만 새 generation 또는
manual revision은 해당 시점 release JSON을 다시 resolve한다.

### DraftArticle

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 글 식별자 |
| `run_id` | FK nullable | 최초 생성 실행 |
| `source_verification_id` | EventClusterVerification FK nullable | 생성을 허용한 최신 불변 검증 결정 |
| `topic_code`, `article_type` | enum | 글 분류 |
| `article_identity_key` | string | 공식 공고/cluster/digest 날짜 기반 주제별 안정 identity |
| `local_digest_date`, `digest_policy_version` | date/int nullable | 반도체 daily digest dedupe 기준 |
| `state` | ArticleState | 현재 상태 |
| `language` | BCP-47 | 기본 `ko-KR` |
| `current_revision_no` | positive int | 현재 개정 |
| `scheduled_publish_at` | datetime nullable | 예약 발행 시각 |
| `withdrawal_state` | `none/pending/withdrawn/marked` | 철회 상태 |
| `created_at`, `updated_at` | datetime | 시각 |

`(topic_code, article_type, article_identity_key)`가 고유하다. 청약은 공식 기관+notice/correction ID,
반도체 breaking은 canonical cluster key, daily digest는 `Asia/Seoul` local date+policy version을
identity로 사용한다. content/title hash만으로 서로 다른 공고를 합치지 않는다. 동일 identity의
다른 run/window는 기존 DraftArticle/GenerationAttempt subject를 멱등 반환한다.

`ArticleEventCluster`는 `article_id`, `event_cluster_id`, `verification_id`,
`role: lead/supporting/held`, `display_order`, `inclusion_reason`, `cluster_snapshot_hash`를 가지는
M:N join이다. daily digest는
선정·보류된 모든 cluster의 stable order와 이유를 이 join에 고정하고 GenerationAttempt의 input
manifest에 포함한다. 이후 한 cluster 정정은 이 join으로 영향 digest를 찾는다.
`(article_id, event_cluster_id)`와 `(article_id, display_order)`가 각각 고유하다. article 최초
생성 transaction에서만 고정하며 기존 revision/article 재사용 시 membership을 변경하지 않는다.
모델/queryset과 PostgreSQL·SQLite trigger는 insert 이후 update/delete를 거절한다. insert도
`verification.cluster_id=event_cluster_id`이고 `cluster_snapshot_hash`가 해당 verification의
`evidence_manifest_hash`와 같을 때만 허용한다.

`ArticleState`: `drafting → review_ready → approved → publishing → published`.
품질 실패는 `blocked`, 정정 감지는 `correction_pending`, 관리자 중지는 `stopped`, 복구
불가능 오류는 `failed`로 전이한다. 발행된 글은 수정하지 않고 새 ArticleRevision을 만든다.

### ArticleRevision

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 개정 식별자 |
| `article_id`, `revision_no` | FK/int unique pair | 개정 번호 |
| `title`, `summary` | text | 제목과 요약 |
| `body_blocks` | JSONB | 사실/기업주장/배경/해석/전망/주의/출처/정정의 구조화 정본 블록 |
| `canonical_markdown` | text | `body_blocks`에서 결정적으로 파생한 관리자 검토용 표현 |
| `content_hash` | SHA-256 | 개정 내용 지문 |
| `provenance_kind` | `automated/admin_edit` | 개정 생성 경로 |
| `generation_attempt_id` | FK GenerationAttempt nullable | automated 개정의 성공 생성 시도 |
| `editorial_policy_snapshot_id`, `editorial_policy_material_hash` | FK/SHA-256 | release JSON에서 해석해 고정한 exact append-only policy |
| `verification_snapshot`, `verification_manifest_hash` | JSONB/SHA-256 | 정렬 verification ID와 각 evidence/rule/result/policy/KST identity |
| `input_evidence_snapshot` | JSONB | 실제 사용한 evidence와 source/version/locator/origin/freshness/rights/current eligibility |
| `excluded_material_snapshot`, `excluded_material_manifest_hash` | JSONB/SHA-256 | excluded/duplicate/conflict material과 안정 reason |
| `input_evidence_manifest_hash` | SHA-256 | 생성·재검증에 사용한 exact evidence set |
| `generation_pipeline_manifest_hash` | SHA-256 nullable | model/prompt/schema/postprocess 묶음; admin edit는 null |
| `claim_graph_state` | `queued/running/passed/blocked` | 현재 개정의 주장 추출·근거 재연결 상태 |
| `quality_gate_manifest_hash`, `quality_report_hash` | SHA-256 nullable | 실행한 blocking gate 정의와 결과 |
| `generator_name`, `generator_version` | string nullable | 생성기 정보 |
| `prompt_policy_version`, `editorial_policy_version` | string | 정책 재현 정보 |
| `correction_note` | text nullable | 독자 표시 정정 이력 |
| `created_by_type` | `system/admin` | 생성 주체 유형 |
| `created_by_admin_id` | FK nullable | 관리자 수정인 경우 |
| `created_at` | datetime | 생성 시각 |

`body_blocks`가 정본이며 block은 stable `id`, `type`, `content`를 가진다. block type은
`fact`, `company_claim`, `background`, `interpretation`, `outlook`, `caution`, `sources`,
`correction`이다. canonical Markdown과 채널 HTML은 이 구조에서만 파생하며 모델 출력 원문을 바로
발행하지 않는다. 제목·요약·caption도 Claim coverage 대상이다.

ArticleDetail의 `runtime_eligibility`는 revision을 수정하는 상태가 아니라 read-time projection이다.
평가한 policy snapshot/material, 현재 release policy snapshot/material, `policy_current`,
`evidence_current`, `publish_eligible`, 정렬 blocking code와 `evaluated_at`을 반환한다. current policy가
바뀌거나 사용 evidence의 freshness·rights·lineage·publishability가 달라지면 false이며 새 revision
재검증 없이는 preview/approval/publish 대상이 될 수 없다.

ArticleDetail은 revision의 verification/evidence/exclusion 값을 live SourceItem이나 현재 EvidenceAsset
값으로 보충하지 않는다. source version, modified/retrieved 시각, rights, attribution, alt text와 제외
사유는 revision에 고정된 값만 반환한다. 정확한 release-document/config/implementation hash가 없는
격리 legacy policy snapshot은 해당 세 필드를 `null`로 반환하고 runtime eligibility를 false로 한다.
API는 누락 hash를 `material_hash`로 대체하여 존재하지 않는 release identity를 만들어 내지 않는다.
정확한 역사 evidence material을 복원할 수 없는 이관 revision은 `evidenceId`와
`legacyQuarantine=true`만 가진 별도 최소 projection으로 반환한다. source/version/rights/freshness를
현재 행이나 빈 문자열로 합성하지 않으며 해당 revision은 발행 불가다.

### GenerationAttempt

자동 초안 생성의 append-only 실행이다. `id`, `article_id`, `origin_collection_run_id`,
`editorial_policy_snapshot_id`, `editorial_policy_material_hash`, `verification_manifest_hash`,
`input_evidence_manifest_hash`, `excluded_material_manifest_hash`, `generation_manifest_hash`,
`generation_pipeline_manifest_hash`, `provider`, `model_name`,
`model_version`, `prompt_template_hash`, `output_schema_hash`, `postprocessor_manifest_hash`,
`request_fingerprint`, `state: queued/running/succeeded/failed`, `output_checksum`,
`article_revision_id` nullable, `started_at`, `finished_at`, `error_code`를 가진다. 동일 fingerprint의
성공 시도는 하나고, worker는 event hint가 아니라 승인된 generation pipeline snapshot을 DB에서
재조회한다. succeeded 결과와 immutable ArticleRevision, claim/quality outbox를 한 트랜잭션으로
만든다.

`generation_manifest_hash`는 정렬 verification ID와 각 verification의 evidence/rule/result hash,
정책 version/hash, KST 기준일을 고정한다. `input_evidence_manifest_hash`는 이 generation material과
`selection_state=selected/included`인 publishable evidence만 함께 hash하며 excluded, conflicting,
duplicate evidence는 생성 입력에 포함하지 않는다.

관리자 `CreateRevisionRequest`는 base revision CAS와 request key로 멱등 처리하며 새 revision을
`provenance_kind=admin_edit`, `claim_graph_state=queued`, Article=`drafting`으로 만든다. 이전
Claim/ClaimEvidence/Visualization/QualityCheck/Render/Approval/Intent를 새 revision에 복사하지 않고
새 본문에서 주장 추출→근거 재연결→전체 blocking quality gate를 다시 실행한다. 미지원 fact가
하나라도 있거나 claim graph/quality가 passed가 아니면 preview, intent, approval과 publish를
차단한다. admin-edit revision은 generation attempt가 없으므로 manual approval만 허용한다.
요청 transaction은 current release `EditorialPolicySnapshot`과 정렬 verification/input-evidence/
excluded-material snapshot을 먼저 고정하고 정확한 `editorial.revalidate_requested@1`을 함께 만든다.
event는 `article_id`, `article_revision_id`, `editorial_policy_snapshot_id`,
`editorial_policy_material_hash`, `verification_manifest_hash`, `input_evidence_manifest_hash`,
`excluded_material_manifest_hash` 일곱 필드만 허용한다.
요청은 `bodyBlocks`와 exact `claimBindings`를 받는다. binding은 요청 안에서 고유한 비어 있지 않은
`claimRef`, `blockId`, `statement`, `claimType`, 정렬 `evidenceIds`, `citationMarker`, evidence ID별
`sourceSpans`, `semanticKey`, `actor`, `attribution`, `horizon`, `uncertaintyNote`, 정렬
`derivedFromClaimRefs`만 허용한다. interpretation의 파생 참조는 같은 요청의
`fact/company_claim` claimRef만 가리킨다. 서버는 이를 revision-scoped deterministic Claim UUID로
해석해 저장·응답의 `claimId/derivedFromClaimIds`로 고정한다. 성공 응답은
article/revision ID와 revision number, revalidation/quality state, content/evidence/policy/
verification/exclusion hash를 반환한다. 같은 request key의 동일 material은 200, 새 revision은 201,
다른 material replay 또는 stale base CAS는 409, 구조·의미 검증 실패는 422다.
worker는 snapshot hash뿐 아니라 current release policy와 모든 사용 evidence의 current publish
eligibility를 다시 검사한다. policy 교체, freshness 만료, 권리 철회, source lineage 변경이 있으면
과거 revision을 수정하지 않고 blocked/manual-required로 남겨 새 revision 재검증을 요구한다.
`validated_auto` intent는 revision의 succeeded GenerationAttempt와 input/generation/quality
manifest가 모든 target AutoPublishValidation의 exact material과 같고 최신 자동 quality report가
전부 passed인 경우에만 생성할 수 있다.

### PublicationIntent

외부 쓰기 시점의 대상을 고정하되 과거 CollectionRun을 변형하지 않는 append-only dispatch
의도다. `id`, 모든 동작에 필수인 `article_revision_id`, 선택적 원인
`correction_case_id`, `origin_collection_run_id` nullable, `target_snapshot_refs`,
`target_commands`, `target_snapshot_manifest_hash`, `approval_mode`,
`auto_publish_validation_refs`, `auto_validation_manifest_hash`,
`auto_publish_activation_refs`, `auto_activation_manifest_hash`,
`generation_attempt_id`, `input_evidence_manifest_hash`, `generation_pipeline_manifest_hash`,
`quality_gate_manifest_hash`, `quality_report_hash`,
`supersedes_intent_id`, `intent_hash`, `request_key`, `state`, `created_by`, `created_at`을 가진다.
state는 `draft/awaiting_approval/approved/stale/dispatched/cancelled`이다.

`target_commands`는 target snapshot ref마다 정확히 하나인 정렬 불변 명령이며 `target_id`,
`target_snapshot_id`, `target_config_hash`, `resolved_action: create/update/unpublish/mark_withdrawn`,
`canonical_dependency_target_id` nullable를 가진다. capability에 따라 WordPress는 `unpublish`,
Blogger는 `mark_withdrawn`처럼 같은 정정 사건도 target별 action이 다를 수 있으므로 전역 action을
두지 않는다. Blogger create/update/mark command는 primary WordPress target을 canonical dependency로
고정하고, unpublish에는 dependency가 없다.

초기 자동 경로는 CollectionRun snapshot을 복사해 intent를 만들지만 target 없는 초안도
나중에 관리자가 target을 골라 manual intent를 만들 수 있다. target credential/capability/
validation snapshot 변경, 재승인 또는 CorrectionCase update는 기존 intent를 수정하지 않고
새 target snapshot refs로 superseding intent를 만든다. current intent의 refs/hash만
ArticleChannelRender, Approval, PublicationAttempt와 비교하며 origin CollectionRun은 감사용
최초 의도로 남는다. `(article_revision_id, request_key)`가 멱등이고 intent hash는 exact
revision ID/content hash, correction case, 정렬 target refs/commands, mode, validation refs와
activation refs, generation/input/quality manifest를 모두 포함한다. unpublish도 마지막 승인
revision과 target별 action subject를
고정한다. current
revision이 달라지면 409로 새 intent/렌더/승인을 요구한다.
intent 생성은 DraftArticle/CorrectionCase subject 행을 잠그고
`expected_latest_intent_id`를 비교해 insert, latest projection과 outbox를 한 트랜잭션으로
갱신한다. 같은 request key·payload는 기존 intent를 반환하고 다른 payload 또는 stale CAS는
409다. superseded/stale intent는 승인·publish·worker에서 거절한다. multi-target intent는
모든 target에 current-subject latest approved Approval이 있을 때만 `approved`가 된다.

### ArticleChannelRender

채널별 파생본을 재현하고 WordPress 대표 URL과 Blogger 맞춤본의 관계를 검증한다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 렌더 식별자 |
| `publication_intent_id` | FK PublicationIntent | 이 렌더를 요청한 현재 발행 의도 |
| `article_revision_id` | FK | 원본 개정 |
| `target_id` | FK PublicationTarget | 대상 채널 |
| `target_snapshot_id`, `target_config_hash` | FK/SHA-256 | 승인한 정확한 연결·capability snapshot |
| `channel_role` | ChannelRole | 주 발행 또는 보조 배포 역할 |
| `render_stage` | RenderStage | 승인 전 미리보기 또는 발행용 최종 렌더 |
| `title`, `body_html` | text | 정화·제한 검증을 마친 채널 결과 |
| `source_links` | URL array | 독자 표시 출처 |
| `included_claim_ids` | UUID array | 해당 채널 본문에 포함한 검증 주장 |
| `canonical_source_url` | URL nullable | Blogger가 참조하는 WordPress 대표 URL |
| `canonical_link_state` | CanonicalLinkState | 원문 링크 바인딩 상태 |
| `template_hash` | SHA-256 | 대표 URL·원격 미디어 자리표시자를 포함한 승인 템플릿 지문 |
| `content_hash`, `source_manifest_hash` | SHA-256 | 렌더와 사실·출처 목록 지문 |
| `created_at` | datetime | 생성 시각 |

`(publication_intent_id, target_snapshot_id, render_stage)`가 고유하다. target snapshot이
바뀌면 기존 렌더를 수정하지 않고 같은 revision의 새 immutable 렌더를 만든다. WordPress preview와 final은
`canonical_link_state=not_applicable`, `canonical_source_url=null`이다. WordPress final은
승인 자산의 원격 media URL만 같은 템플릿에 결합해 외부 게시 전에 만든다. Blogger
preview는 `canonical_link_state=pending`, `canonical_source_url=null`로 만들고 관리자에게
원문 링크 자리와 미확정 상태를 보여준다. Blogger final은 WordPress Publication이 실제
공개 상태이고 공개 URL이 검증된 뒤 같은 `template_hash`의 대표 URL·media 자리표시자만
결합해 새로 만들며 `canonical_link_state=resolved`와 `canonical_source_url`이 필수다.
`source_manifest_hash`는
두 렌더가 같은 승인 개정과 근거 집합에서 파생됐음을 나타낸다. Blogger는 WordPress의
검증 주장 일부를 요약에서 생략할 수 있지만 새로운 사실을 추가하거나 포함한 주장의
의미·출처를 변경할 수 없다.

### Claim / ClaimEvidence

`Claim`은 `article_revision_id`, `block_id`, `statement`, `claim_type`, `risk_level`,
`verification_state`, `actor`, `attribution`, `horizon`, `uncertainty_note`,
`derived_from_claim_ids`, `review_note`를 가진다. canonical `claim_type`은 정확히 `fact`,
`company_claim`, `interpretation`, `outlook`이다. `company_claim`은 주장 주체와 발표 출처를
독자에게 명시하며 독립 검증 전 fact로 승격하거나 breaking direct-primary 근거로 사용할 수 없다.
`interpretation`은 검증된 입력 fact Claim과 추론 설명을, `outlook`은 주체·기간과 불확실성
표현을 필수로 가진다. 한 문장에 역사 사실과 기업 전망이 섞여 있으면 atomic Claim 둘 이상으로
분리한다.

`ClaimEvidence`는 `claim_id`, 90일 purge 뒤 nullable인 `evidence_asset_id`, 발행본에서 필수인
`published_evidence_snapshot_id`, `relation`, 90일까지만 유지하는 `source_span` nullable,
장기 `source_span_hash`, `verification_strength`, `independence_group`, `origin_identity_hash`,
`checked_at`을 가진다. `relation`은 `supports`, `contradicts`,
`context`이다. 게시 전 `fact` 주장은 `supports` 연결이 하나 이상이어야 하고, 청약 핵심
사실과 반도체 속보는 TopicPolicy의 강화된 검증 수를 충족해야 한다. raw purge는 source_span
본문을 null로 만들고 PublishedEvidenceSnapshot의 hash만 남긴다.

### VisualPlacement

`article_revision_id`, 90일 purge 뒤 nullable인 `evidence_asset_id`,
`published_evidence_snapshot_id` nullable, `published_visualization_snapshot_id` nullable,
`block_id`, `display_order`, `caption`,
`alt_text_snapshot`, `attribution_snapshot`, `rights_status_snapshot`, `render_variant_key`를
가진다. 발행 VisualPlacement는 일반 근거 snapshot 또는 visualization snapshot 중 정확히
하나를 참조한다. 권리와 표시 정보를 배치 시점 스냅샷으로 보존한다. 승인 트랜잭션은 발행에
쓰인 ClaimEvidence/VisualPlacement마다 해당 published snapshot을 생성·hash 검증하고 연결한
뒤에만 Approval을 추가한다.

### VisualizationRender

허용된 수치 근거로 자체 표·차트를 만드는 GenericExtractionAttempt와 분리된 deterministic
provenance다. `id`, `article_revision_id`,
`input_manifest_hash`, 단위·기준일·결측/반올림·변환식을 담은 `transform_spec`와
`transform_spec_hash`, `renderer_name`, `renderer_version`, `config_hash`, output kind,
`output_object_key/version/checksum`, `rights_manifest_hash`, `alt_text`, `evidence_asset_id`,
`state`, `created_at`을 가진다. 원 수치를 새로 추론하지 않고 모든 input은 publishable
EvidenceAsset/검증 Claim이어야 한다. `VisualizationInputClaim`과
`VisualizationInputEvidence` join은 render ID, input ID, stable order를 가지며 복수 출처 전체의
정본 계보다. 생성 EvidenceAsset은 `derivation_type=visualization_derived`,
`visualization_render_id`와 locator=`visualization`만 사용하고 source/origin/document/generic FK는
null, validation mode=`deterministic`, confidence=null이다. input/transform/renderer/output hash를
review·source manifest에 포함한다. 차트/표의 출처·단위·기준일을
캡션에 표시하고 input 권리보다 넓은 사용을 허용하지 않는다.

### QualityCheck

`article_revision_id`, `check_code`, `check_version`, `result`, `score`, `blocking`,
`details`, `executed_at`을 가진다. revision별 정렬 check code/version/config를
`quality_gate_manifest_hash`로, 결과/details hash를 `quality_report_hash`로 집계하며 둘은
ArticleRevision과 PublicationIntent에 고정한다. 결과는 `passed/failed/manual_required`이며 모든 행은
append-only다. 필수 content/evidence 차단 검사는 정확히 다음 10개다.

1. `all_publishable_claims_grounded`
2. `high_risk_verification_satisfied`
3. `claim_independence_satisfied`
4. `source_freshness_satisfied`
5. `evidence_publish_eligibility_current`
6. `quotation_limits_satisfied`
7. `claim_types_separated_and_attributed`
8. `duplicate_or_conflict_resolved`
9. `korean_readability_and_repetition`
10. `no_exaggeration_or_false_experience`

visual이 하나라도 있으면 별도 `visual_rights_and_alt_text`도 필수다. direct quote 길이는 법적
safe-harbor 숫자가 아니라 source rights snapshot이 허용한 연속/총 Unicode 길이와 원문 비율의
보수적 product cap이다. unknown/internal-only source의 허용 길이는 0이다. failed 또는
manual_required인 blocking check가 하나라도 있으면 revision은 review-ready가 아니다.

### Approval

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 승인 식별자 |
| `article_revision_id` | FK | 승인한 정확한 개정 |
| `publication_intent_id` | FK PublicationIntent | 승인한 current dispatch intent |
| `target_id` | FK PublicationTarget | 대상 채널 |
| `target_action` | enum | intent의 해당 target resolved action |
| `article_channel_render_id` | FK ArticleChannelRender nullable | create/update/mark_withdrawn에서 승인한 정확한 preview 렌더; unpublish는 null |
| `action_subject` | JSONB | action-discriminated 불변 승인 대상 |
| `target_snapshot_id`, `target_config_hash` | FK/SHA-256 | 렌더·current intent와 일치한 정확한 대상 snapshot |
| `mode` | ApprovalMode | 수동/검증 자동 |
| `decision` | `approved/rejected/revoked` | 결과 |
| `approval_subject_hash` | SHA-256 | intent/revision/target action+subject/target/policy/quality/source manifest 지문 |
| `approval_material_version` | string | 현재 신규 결정은 `approval-subject-v3`; subject hash 정규화 버전 |
| `head_version` | positive integer | 이 결정을 insert한 뒤 intent/target head version |
| `supersedes_approval_id` | self FK nullable | 같은 target/current subject의 직전 결정 |
| `request_key` | string | target별 관리자 또는 validated-auto worker 결정 요청 멱등 키 |
| `request_hash` | SHA-256 | exact request replay 판별 지문 |
| `decision_hash` | unique SHA-256 | 결정·head·요청·actor·사유를 결속한 `approval-decision-v1` 지문 |
| `decision_reason` | string | 관리자가 제출했거나 validated-auto worker가 정책에서 생성한 불변 결정 사유 |
| `decision_actor_type` | `admin/worker` | 결정 hash에 결속한 actor 종류 |
| `decision_actor_id` | UUID nullable | admin이면 실제 actor ID, worker이면 null |
| `decision_event_key` | UUID string nullable | worker이면 소비한 outbox event key, admin이면 null; API 응답에는 노출하지 않음 |
| `reauth_proof_id` | FK nullable | revoke 또는 approved-unpublish에서 소비한 용도 결속 재인증 증명 |
| `policy_snapshot_hash` | SHA-256 | 승인 당시 정책 묶음 |
| `quality_report_hash` | SHA-256 | 검사 결과 지문 |
| `render_template_hash` | SHA-256 nullable | content action 미리보기 템플릿 지문; unpublish는 null |
| `source_manifest_hash` | SHA-256 | 승인한 근거 또는 철회 근거 지문 |
| `admin_id` | FK non-null | API `decidedBy`; 수동 결정 관리자 또는 자동 모드 activation/schedule 승인 관리자 |
| `decided_at` | datetime | 시각 |

승인 트랜잭션은 current PublicationIntent의 해당 target command, target snapshot ref와 current
target snapshot ID/hash가 모두 같은지 확인한다. create/update/mark_withdrawn의 action subject는
render ID/template/source manifest를 포함하고 ArticleChannelRender가 필수다. unpublish의 action
subject는 current remote post ID/state, 사유, 영향 target과 correction evidence manifest hash를
포함하며 render ID/template hash는 null이다. target command, snapshot, 개정 내용, 정책/검사
지문 또는 action subject가 바뀌면 기존 승인은 사용할 수 없다. content action의 final 렌더는
승인된 `render_template_hash`에서 WordPress URL과 승인된 asset ID의 원격 media URL 자리만
결합한 경우에만 같은 승인을 사용할 수 있다.
Approval은 수정·삭제하지 않는다. `(publication_intent_id, target_id, request_key)`가
고유하고 intent/target별 `PublicationApprovalHead` 한 행을 잠가
`expected_latest_approval_id`와 `expected_head_version`을 함께 CAS 검사한다. 최초 head 기대값은
`(null, 0)`이다. 허용 전이는 `none→approved|rejected`, `rejected→approved`,
`approved→revoked`뿐이다. `approved→approved|rejected`, `rejected→rejected|revoked`,
`none→revoked`와 revoked 이후 전이는 409이며, 같은 의미의 중복 결정도 exact request-key replay가
아니면 새 행을 만들지 않는다.

`decision_hash`는 `schemaVersion=approval-decision-v1`, `subjectHash`, `decision`,
`headVersion`, `supersedesApprovalId`, `requestHash`, `actorType`, `actorId`, `eventKey`,
`reason`을 정규화해 계산한다. API의 `decisionReason` 값이 이 hash material의 `reason`으로
들어간다. revoked는 action 종류와 무관하게
`approval_revoke` scope 재인증이 필수이고, approved-unpublish는 `unpublish` scope 재인증이
필수다. 나머지 조합은 `reauth_proof_id=null`이어야 한다. 필수 proof의 누락/null/UUID 형식 오류는
요청 schema 위반 422이고, 형식상 유효하지만 만료됐거나 scope/entity가 다른 proof는 403이다.

insert, latest projection, intent aggregate와 동기 `AuditEvent` 기록을 한 트랜잭션으로 갱신하며 stale
subject/intent/render/snapshot이나 두 CAS 중 하나의 불일치는 409다. publisher는 current intent의
target별 current-subject latest `approved` 한 건만 사용할 수 있다. 과거 결정 replay 응답의
`isCurrent`와 `dispatchEligible`는 그 과거 행으로 추론하지 않고 현재 head를 read-only로 다시
조회해 계산한다. approval decision 전용 outbox event는 발행하지 않는다.

### PublicationApprovalHead

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | projection 식별자 |
| `publication_intent_id` | FK PublicationIntent | 결정 집합 |
| `target_id` | FK PublicationTarget | 대상 채널 |
| `latest_approval_id` | FK Approval PROTECT | 현재 append-only 결정 |
| `version` | positive integer | 현재 head version, 1부터 단조 증가 |
| `subject_hash` | SHA-256 | latest Approval의 `approval_subject_hash` 복제 검증값 |
| `updated_at` | datetime | head가 마지막으로 전진한 시각 |

`(publication_intent_id, target_id)`는 고유하다. head의 latest Approval은 같은 intent/target이고
`Approval.head_version == PublicationApprovalHead.version`이어야 한다. API는 현재 head의 approval
ID/version/decision/subject hash/decision hash/updatedAt과 요청 행의 `isCurrent`,
`dispatchEligible`를 함께 반환한다.

### PublicationTarget

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 발행 대상 |
| `channel` | ChannelCode | 채널 |
| `role` | ChannelRole | 주 발행·대표 원문 또는 보조 배포 |
| `environment` | TargetEnvironment | 격리 검증 또는 운영 대상 |
| `display_name` | string | 관리자 표시명 |
| `remote_blog_id` | string | 외부 블로그 ID |
| `base_url` | URL | 검증된 자체 도메인 또는 블로그 기준 URL |
| `secret_ref` | string | OAuth/자격 증명 참조 |
| `capabilities` | JSONB | 아래 7개 공통 capability boolean |
| `connection_state` | `pending/verified/expired/revoked/blocked` | 연결 상태 |
| `preflight_state`, `canary_state` | TargetValidationState | 읽기 전용 점검과 쓰기 검증 상태 |
| `canary_target_id` | self FK nullable | 운영 target이 참조하는 동일 채널 test target |
| `pilot_state` | TargetValidationState | 운영 target의 관리자 승인 첫 게시 검증 |
| `last_preflight_at`, `last_canary_at` | datetime nullable | 마지막 검증 시각 |
| `last_pilot_at` | datetime nullable | 운영 파일럿 확인 시각 |
| `canary_policy_version` | string nullable | 성공한 쓰기 계약 버전 |
| `auto_publish_enabled` | boolean | latest AutoPublishActivation에서 계산한 projection |
| `latest_auto_publish_activation_id`, `auto_publish_activation_version` | FK nullable/int nullable | current enable/revoke 결정과 버전 |

활성 target 중 `role=primary_canonical`은 환경별로 하나이며 `channel=wordpress`여야 한다.
`role=secondary_distribution`은 `channel=blogger`에만 허용한다. WordPress target은 HTTPS,
posts/media route, 사용자 권한과 permalink를 검증하고, Blogger target은 OAuth 범위와
blog 소유권을 검증한다. 쓰기 canary는 `environment=test` target에서만 수행한다. 운영
target은 동일 채널·역할의 `canary_state=passed` test target을 `canary_target_id`로 참조하고,
자체 `preflight_state=passed`와 관리자 승인 방식의 첫 실제 게시·공개 확인으로
`pilot_state=passed`가 되어야 `verified`와 `auto_publish_enabled=true`가 가능하다.
WordPress Application Password에는 자체 scope가 없으므로 연결 사용자는 필요한 게시·
삭제·업로드 capability만 가진 전용 역할이어야 한다.
`channel`, role, environment, `base_url`, `remote_blog_id`는 생성 후 불변인 원격 destination
identity다. 다른 사이트/블로그로 바꾸려면 새 PublicationTarget을 만들고 별도 migration/
철회 결정을 해야 한다. credential ref, capability와 검증 상태 변경만 같은 target의 새
PublicationTargetSnapshot으로 허용한다.

`TargetDisconnectDecision`은 `id`, `target_id`, `expected_target_snapshot_id/config_hash`,
`request_key`, `request_hash`, `reauth_proof_id`, `reason`, `state: accepted/revoking/reconciling/
completed/failed`, `remote_result_hash`, `decided_by`, `decided_at`을 가진다.
`(target_id, request_key)`가 고유하고 target projection 행 잠금/CAS 아래 자동발행 revoke,
credential local-disable, decision/AuditEvent/outbox를 원자 처리한다. remote OAuth revoke 결과가
불명확하면 reconcile하며 요청을 새로 만들지 않는다.

### PublicationTargetSnapshot

외부 쓰기 대상을 TOCTOU 없이 고정하는 불변 snapshot이다. `id`, `target_id`, `version`,
`channel`, `role`, `environment`, `remote_blog_id`, `base_url`, `secret_ref_identity_hash`,
`capabilities`, `connection_state`, preflight/canary/pilot 상태와 검증 policy/version,
`canary_target_id`, `publisher_contract_version`, `publisher_adapter_manifest_hash`,
`config_hash`(auto toggle을 제외하고 adapter manifest를 포함한 operational config hash),
`created_at`을 가진다. `(target_id, version)`과
`(target_id, config_hash)`가 고유하다. secret 값은 포함하지 않고 어떤 비밀 참조 identity를
사용하는지만 hash에 묶는다. base URL, remote blog ID, credential ref, role/environment,
capability나 검증 상태가 바뀌면 새 snapshot을 만들고 기존 run/Approval은 stale로
표시한다. 발행 직전 current target snapshot hash가 current PublicationIntent와 Approval에 고정된 값과
같아야 하며, 다르면 외부 호출 없이 명시적 재검증·재승인을 요구한다.
배포된 publisher adapter manifest가 달라져도 새 snapshot을 만들고 예약/manual intent를 포함한
기존 render/승인을 stale 처리한다.

공통 capability 키는 `create`, `update`, `unpublish`, `mark_withdrawn`, `draft`, `schedule`,
`media_upload`이다. WordPress는 `unpublish`를 published→draft 또는 trash, Blogger는
`posts.revert/delete`로 구현한다. `mark_withdrawn`은 두 채널 모두 기존 글 update로
구현한다. `schedule`은 공식 기능 canary 결과를 나타내지만 두 채널 운영 예약은 내부
스케줄러가 담당한다. `media_upload`은 WordPress만 true이고 Blogger는 공개 HTTPS 자산
URL 삽입으로 처리한다.

### Publication / PublicationAttempt

`Publication`은 글과 채널의 장기 관계다. `article_id`, `target_id`,
`origin_target_snapshot_id`, `remote_post_id`,
`remote_lookup_key`, `remote_url`, `canonical_source_url`, `published_revision_no`, `state`,
`remote_state`, `scheduled_for`, `canonical_ready_at`, `published_at`, `last_success_at`을
가진다. WordPress Publication의 `remote_url`은 `remote_state=published`이고 비인증 GET
200 검증 후에만 대표 원문 URL이 되며 Blogger Publication의 `canonical_source_url`은
그 URL과 같아야 한다.
`(article_id, target_id)`가 고유하다.

초기 Blogger Publication은 같은 개정의 WordPress Publication이 실제 `published`이고
`canonical_ready_at`이 설정된 경우에만 외부 호출할 수 있다. 정정 시에도 WordPress
update, 공개 확인과 최신 `remote_url` 저장이 먼저 성공한 뒤 Blogger 렌더와 기존 post를
갱신한다. 예약 요청은 내부 `state=scheduled`로 두고 예정 시각에 WordPress부터 처리한다.

`PublicationAttempt`은 한 외부 동작이다. `publication_id`, `article_revision_id`,
`publication_intent_id`,
`target_snapshot_id`, `target_config_hash`, `resolved_action`, `target_command_hash`,
`publisher_contract_version`, `publisher_adapter_manifest_hash`,
`approval_id`, `approval_subject_hash`, `auto_publish_activation_id/hash` nullable,
`idempotency_key`, `remote_lookup_key`, `request_fingerprint`, `state`, `attempt_no`, `remote_request_id`,
`http_status`, `error_code`, `error_detail_redacted`, `started_at`, `finished_at`,
`next_retry_at`, `reconcile_attempt_no`를 가진다. `idempotency_key`가 전역 고유하고
`reconcile_attempt_no`는 `0..5`다.

dispatch 트랜잭션은 current intent의 target command마다 PublicationAttempt를 먼저 생성하고 exact
revision/action/snapshot/approval/activation을 고정한 뒤 attempt ID만 request outbox의 routing
key로 사용한다. WordPress 공개·media·reconcile과 correction 완료 신호도 attempt/intent ID를
반환한다. worker는 payload에서 최신 intent를 추측하지 않고 attempt가 아직 current intent에
속하는지 외부 호출·후속 target 해제 직전에 재검증한다. superseding intent 뒤 늦게 도착한
완료 신호는 stale 처리한다.

`PublicationState`: `pending → scheduled → in_progress → published`; 수정 시 `updating → published`,
철회 시 `withdrawing → withdrawn` 또는 `marking_withdrawn → marked_withdrawn`.
실패는 `retryable_failed` 또는 `permanent_failed`이며, 원격 성공 여부가 불명확하면
`reconciling`에서 원격 ID/콘텐츠를 조회하기 전 생성 호출을 반복하지 않는다.

WordPress `remote_lookup_key`는 Publication UUID 기반 결정적 post slug다. Blogger는 같은
UUID 기반 전용 label과 HTML comment marker 쌍이다. 조회 결과가 정확히 한 건이고 marker와
대상 blog가 일치할 때만 기존 생성 결과로 채택하며 0건 또는 복수건은 `manual_required`로
격리한다.

### PublicationReconcileGeneration

한 PublicationAttempt의 원격 결과 조정 시도를 source outbox event와 함께 append-only로
보존한다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 조정 세대 식별자 |
| `publication_attempt_id` | PublicationAttempt FK, PROTECT | 조정 대상 외부 동작 |
| `generation` | integer `1..5` | execute/admin `attempt_no`와 분리된 조정 세대 |
| `source_event_id` | OutboxMessage one-to-one, PROTECT | 이 세대를 시작한 정확한 reconcile event |
| `state` | `started`, `completed` | 동일 이벤트 재개의 durable 상태 |
| `result_identity` | SHA-256 blank 허용 | 완료 결과의 canonical material hash |
| `result_state` | string blank 허용 | 완료 시 적용된 PublicationAttempt 상태 |
| `not_before` | datetime | source event의 불변 예약 시각 |
| `started_at`, `completed_at` | datetime | 시작과 완료 시각 |

`(publication_attempt_id, generation)`과 `source_event_id`가 각각 고유하다. 같은 완료 event의
재전달은 adapter를 호출하지 않고, 같은 started event의 재전달은 같은 generation을 재개한다.
legacy v1 event는 최초 소비 시 해당 attempt의 다음 연속 세대에 한 번만 결합한다. v2 event의
payload 세대, 행 세대, `PublicationAttempt.reconcile_attempt_no`는 정확히 일치해야 한다.

결과 저장은 source event와 generation을 함께 fence한다. 더 새 세대가 시작된 뒤 도착한 과거
결과는 현재 attempt/publication 결과를 덮어쓰지 못한다. 결과 적용과 generation 완료는 하나의
transaction이며, retryable/unknown 결과의 다음 v2 outbox event와 다음 started generation
할당도 하나의 transaction이다. 다섯 번째 실패는 `manual_required`로 종결하고 여섯 번째 행이나
event를 만들지 않는다. 새 세대를 할당하는 같은 transaction에서 attempt와 publication도
`reconciling`으로 projection하므로 execute 재전달이나 관리자 retry가 외부 write 경로를
다시 열 수 없다. v1/v2 전달의 terminal callback은 정확한 source event에 결합된 세대를
`completed`로 만들고 attempt/publication을 `manual_required`로 종결한다. 후속 생성과
upgrade backfill도 source outbox 또는 해당 consumer receipt의 `dead_letter`를 감지해 같은
종결을 적용하며 완료 세대 replay는 no-op이다.

### RemoteMedia

WordPress 미디어 업로드의 중복과 응답 유실을 조정한다.

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 원격 자산 매핑 |
| `target_id`, `evidence_asset_id` | FK/FK nullable | WordPress target과 90일 purge 전 내부 자산 |
| `asset_checksum` | SHA-256 | 업로드 전 내부 원본 지문 |
| `presentation_hash` | SHA-256 | alt/caption/attribution 표시 지문 |
| `remote_lookup_key` | string | checksum+표시 지문 기반 결정적 media slug/marker |
| `remote_media_id`, `remote_source_url` | string/URL nullable | WordPress 결과 |
| `state` | `pending/uploading/available/reconciling/orphaned/deleted/failed` | 매핑 상태 |
| `request_fingerprint` | SHA-256 | 업로드 요청 지문 |
| `uploaded_at`, `last_reconciled_at`, `orphaned_at`, `deleted_at` | datetime nullable | 운영 시각 |
| `delete_reason`, `last_reconcile_hash` | scalar nullable | 외부 bytes 삭제와 최종 조정 tombstone |

`(target_id, asset_checksum, presentation_hash)`과 `(target_id, remote_lookup_key)`가 각각
고유하다. RemoteMedia는 content-addressed 전달 mapping일 뿐 단일 근거 provenance를 소유하지
않고, article/revision별 PublishedEvidence/VisualizationSnapshot은 PublicationMedia binding이
권위 있게 보존한다. 같은 바이너리라도 alt/caption/attribution이 다르면 별도 원격 media를 만들어
기존 공개 글의 미디어 메타데이터를 덮어쓰지 않는다. 업로드
응답이 유실되면 media slug와 내부 description marker로 정확히 한 건을 찾고 기존 media
ID를 저장한다. WordPress 응답의 checksum을 신뢰하거나 기대하지 않는다. 최종 렌더는
`remote_source_url`로 본문 자산 URL을 치환하고 필요 시 `remote_media_id`를
`featured_media`로 지정한다.

### PublicationMedia

`publication_id`, `remote_media_id` nullable, `public_delivery_asset_id` nullable,
`article_revision_id`, `evidence_asset_id` nullable, `published_evidence_snapshot_id` nullable,
`published_visualization_snapshot_id` nullable, `usage`
(`inline/featured`), `block_id`, `display_order`, `alt_text_snapshot`, `caption_snapshot`,
`attribution_snapshot`, `binding_state: prepared/active/removal_pending/removed`,
`lease_generation`, `remote_body_hash`, `remote_verified_at`, `removed_at`, `created_at`을 가진다.
media 종류에 따라 remote/public delivery ID 중 정확히 하나가 non-null이고 published
evidence/visualization snapshot도 정확히 하나가 non-null이다. PostgreSQL NULL-distinct 우회를 막기 위해
`UNIQUE(publication_id, article_revision_id, remote_media_id, block_id) WHERE remote_media_id IS NOT NULL`과
`UNIQUE(publication_id, article_revision_id, public_delivery_asset_id, block_id) WHERE public_delivery_asset_id IS NOT NULL`
partial index를 각각 둔다.
`published`와 공개 상태를 유지하는 `marked_withdrawn` Publication의 참조는 모두
활성으로 계산한다. 완전 `withdrawn/deleted`에 도달했거나 원격 본문에서 해당 자산 참조가
제거됐음이 조정으로 확인된 Publication만 활성 참조 계산에서 제외한다. 이 기준의 활성
참조가 0개이고 조정 중 시도가 없을 때만 RemoteMedia를 `orphaned`로 바꿀 수 있으며,
유예 기간 후 같은 기준으로 참조 수를 다시 확인한 뒤 원격 bytes를 삭제한다. mapping 행과
URL/ID/checksum/presentation/deleted 시각·사유·reconcile hash tombstone은 PublicationMedia 감사
만료까지 유지한다.

`prepared` binding과 scheduled/in-progress publish/update/withdraw/reconcile attempt도 보호 참조로
계산한다. 자산 재사용은 asset과 binding projection을 행 잠금하고 `orphaned/pending_delete`를
취소한 뒤 lease generation을 증가시킨다. 삭제 worker도 같은 행 잠금과 expected generation
CAS 아래 active/prepared refs, in-flight attempts, 열린 정정/권리 hold, 최근 원격 본문 hash와
확인 시각을 다시 계산한다. 하나라도 바뀌면 삭제를 취소한다. active revision projection은
immutable revision별 binding과 별도로 두며 이전 binding row를 update해 계보를 덮지 않는다.

### PublishedEvidenceSnapshot

발행 승인 시점에 근거·출처·주장 관계의 비민감 최소본을 불변으로 동결한다. `id`,
`article_id`, `article_revision_id`, `claim_id` nullable, `claim_statement_snapshot`,
`claim_relation`, `evidence_asset_id` nullable, `source_item_id` nullable,
`source_definition_id`, `stable_external_id`, `canonical_url`, `source_identity_hash`,
`source_version_hash`, `previous_source_version_hash` nullable, `change_kind`, `source_status`,
`source_title`, `publisher`, `published_at`, `retrieved_at`, `evidence_checksum`,
`locator_snapshot`, `source_span_hash`, `rights_status`, `attribution`, `alt_text`,
`snapshot_hash`, `created_at`을 가진다. 원문 전문·비밀·개인정보는 복사하지 않는다.

발행에 사용된 ClaimEvidence/VisualPlacement/PublicationMedia는 이 snapshot을 참조한다. 90일
원문 purge 때 원 SourceItem/EvidenceAsset FK는 `SET NULL`하고 raw bytes/text·locator 원본은
삭제하되 snapshot과 발행/주장 관계는 1년 동안 남긴다. 새 수집은
`source_definition_id + stable_external_id/canonical identity`로 이 index를 조회해 과거
`source_version_hash`와 비교하므로 원문 purge 뒤에도 corrected/retracted/unavailable/restored
전이와 영향 article을 찾을 수 있다. 같은 identity가 다시 나타나면 previous version/hash와
change kind를 새 snapshot/correction evidence에 연결하며 과거 snapshot은 수정하지 않는다.

### PublishedVisualizationSnapshot

복수 출처 생성 시각화의 발행 최소본이다. `id`, `article_id`, `article_revision_id`,
`visualization_render_id` nullable, `output_checksum`, `input_manifest_hash`,
`transform_spec_hash`, `renderer_name/version/config_hash`, `rights_manifest_hash`, `alt_text`,
`caption`, `snapshot_hash`, `created_at`을 가진다. `PublishedVisualizationInput`은
`published_visualization_snapshot_id`, `published_evidence_snapshot_id`, `input_order`,
`claim_relation`을 가지며 stable order 전체가 input manifest와 일치해야 한다. 임의 한 출처를
대표 source로 선택하지 않는다. 90일 raw/VisualizationRender purge 뒤에도 output provenance와
각 입력 출처 identity/version snapshot을 1년 또는 공개/정정 hold 동안 보존한다.

### PublicDeliveryAsset

Blogger처럼 외부 공개 HTTPS URL을 본문에 삽입하는 채널용 장기 전달 객체다. `id`,
`source_evidence_asset_id` nullable, `asset_checksum`,
`presentation_hash`, `mime_type`, `byte_size`, `delivery_object_key`, `delivery_object_version`,
`public_url`, `rights_status_snapshot`, `alt_text_snapshot`, `caption_snapshot`,
`attribution_snapshot`, `state: available/withdrawal_pending/pending_delete/deleted`,
`active_reference_count`, `lease_generation`, `last_remote_body_hash`, `last_reconciled_at`,
`zero_reference_at`, `delete_after`, `deleted_at`, `delete_reason`, `created_at`을
가진다. PublicDeliveryAsset도 content-addressed mapping이며 article별 provenance는
PublicationMedia의 snapshot XOR가 정본이다. `(asset_checksum, presentation_hash)`와
`public_url`은 고유하며 URL은 immutable하다.
이는 90일 원 EvidenceAsset 객체와 다른 storage lifecycle/class를 사용한다.

공개 또는 공개 상태의 marked-withdrawn PublicationMedia가 하나라도 참조하면 객체와 URL을
유지한다. 완전 철회/삭제 또는 본문 자산 제거가 모든 채널의 비인증 GET/원격 본문 조정으로
확인된 뒤에만 참조 수를 0으로 만들고 최소 30일 grace period 후 같은 asset 행 잠금과
lease-generation CAS에서 prepared/active binding, in-flight attempt, 원격 참조·열린 정정·권리
hold를 재확인해 delivery bytes를 삭제한다. 재참조는 pending delete를 원자 취소하고 generation을
증가시킨다. mapping/URL/checksum/표시 hash/deleted 시각·사유 tombstone은 발행 감사 만료까지
유지한다. 권리 철회는 먼저 영향 게시물을 update/unpublish하고 성공을 조정한 뒤 객체를
만료한다. 따라서 90일 원문 purge는 활성 Blogger 본문의 이미지 URL을 끊지 않는다.

### CorrectionCase

| 필드 | 타입/제약 | 설명 |
|---|---|---|
| `id` | UUID PK | 정정 사건 |
| `source_item_id`, `article_id` | FK nullable/FK | purge 전 변경 원문과 영향 글 |
| `published_evidence_snapshot_id`, `source_identity_hash` | FK/SHA-256 | purge 뒤에도 유지하는 출처 version·영향 계보 |
| `observed_source_version_hash`, `change_kind` | SHA-256/enum | 이 사건을 만든 exact source 관측 |
| `correction_subject_hash` | SHA-256 unique | source identity/version/change/article의 정규 dedupe 지문 |
| `supersedes_case_id` | self FK nullable | 같은 identity의 새 source version 사건 |
| `kind` | CorrectionKind | 정정/철회/접근 불가 |
| `diff_summary`, `evidence_snapshot` | JSONB | 검증된 변경 근거 |
| `state` | `detected/verifying/verified/applying/completed/rejected/failed` | 처리 상태 |
| `detected_at`, `verified_at`, `completed_at` | datetime nullable | SLA 측정 시각 |
| `deadline_at` | datetime | 30분 목표 |

정정은 기존 게시물과 독자 표시 이력을 함께 갱신한다. 완전 철회 시 WordPress의
`unpublish=true` 경로가 `withdrawn` terminal state에 도달하면 공개 URL이 없어도 Blogger
`unpublish`를 이어서 수행한다. `unpublish=false`인 채널은 `mark_withdrawn`으로 본문
상단을 수정한다. WordPress가 철회 표시 후 계속 공개되는 경우에만 공개 URL을 다시
검증해 Blogger 표시 갱신에 전달한다.
동일 `(source_identity_hash, observed_source_version_hash, change_kind, article_id)` 재발견은
기존 case를 반환하고 새 revision/intent를 만들지 않는다. 같은 identity의 새로운 source
version만 새 correction subject와 superseding case를 만든다.

### AuditEvent

추가 전용 테이블이다. `id`, `occurred_at`, `correlation_id`, `actor_type`, `actor_id`,
`action`, `entity_type`, `entity_id`, `before_hash`, `after_hash`, `reason_code`,
`metadata_schema_version`, `redaction_policy_version/hash`, `metadata_redacted`를 가진다.
UPDATE/DELETE 권한을 애플리케이션 역할에 주지 않는다. action별 versioned metadata allowlist를
deny-by-default로 적용하고 nested depth 4, 전체 8 KiB, scalar length 제한 뒤 secret-key/value
detector로 authorization/cookie/token/password/API key, 원문/본문/추출 text를 insert 전에
거절한다. 응답 직전에도 같은 policy hash로 재검증하며 실패 event는 metadata 대신 안전한
redaction error code만 노출한다. `actor_type=admin`이면 actor ID가 non-null AdminAccount FK다.

audit 조회는 `(occurred_at DESC, id DESC)` keyset과 DB index를 사용한다. signed opaque cursor에
canonical filter hash, 최초 page의 upper watermark, last tuple과 page limit(기본 50, 최대 200)을
묶는다. from은 inclusive, to는 exclusive다. cursor와 filter가 다르면 400이며 조회 중 append는
watermark 밖이라 기존 page에 중복/누락을 만들지 않는다.

### RetentionBatch / RetentionBatchItem

파괴적 만료를 재현·재개하는 aggregate다. `RetentionBatch`는 `id`, `scope`, `cutoff_at`,
`request_key`, `preview_hash`, `impact_manifest_object_key/version/hash`, `hold_manifest_hash`,
`hold_manifest_object_key/version`,
`expected_item_count`, `expected_byte_count`, `state: previewed/authorized/running/completed/failed/cancelled`,
`phase: not_started/snapshotting/nulling/deleting/tombstoning/done`, `version`, `failure_cursor` nullable,
`processed_count`, `skipped_hold_count`, `failed_count`, `requested_by`, `authorized_by` nullable,
`reauth_proof_id` nullable, `authorization_request_key` nullable, `preview_reason`,
`authorization_reason` nullable, `created_at`,
`authorized_at`, `completed_at`, `error_redacted`를 가진다. `(requested_by, request_key)`와
`(id, authorization_request_key)`가 각각 고유하다.

`RetentionBatchItem`은 batch와 대상 entity/object version, precondition hash, required published/
audit snapshot ID, hold reason, `state: pending/snapshotted/unlinked/deleted/tombstoned/skipped/failed`,
result hash와 오류를 가진다. `(batch_id, entity_type, entity_id, object_version)`이 고유하다.
preview는 영향/hold manifest와 item 집합을 동결한다. execute는 batch 행의 expected version와
preview hash를 CAS로 검사하고 최근 재인증·이유를 요구하며 authorization/AuditEvent/outbox를
원자 저장한다. worker는 item 단계별 멱등 상태를 잠가 snapshot 생성·검증 → 장기 FK null 전환 →
physical bytes 삭제 → tombstone 완료 순서로 재개한다. manifest/hold 변화는 해당 item을 skip하고
새 preview를 요구한다. at-least-once replay나 crash가 이미 완료한 physical delete를 반복하지
않으며 batch count/result hash로 부분 완료를 감사한다.

## 관계 요약

```text
TopicPolicy 1 ── * SourceDefinition ── * SourceDefinitionSnapshot
SourceRegistrySnapshot 1 ── * SourceRegistryMembership * ── 1 SourceDefinitionSnapshot
Schedule 1 ── * CollectionRun ── * RunStep
CollectionRun 1 ── * RunSourceItem * ── 1 SourceItem 1 ── * EvidenceAsset
RunSourceItem 1 ── * originated EvidenceAsset
RunSourceItem 1 ── * DocumentExtraction 1 ── * ExtractionRun 1 ── * derived EvidenceAsset
RunSourceItem 1 ── * GenericExtractionAttempt 0..1 ── 1 other-derived EvidenceAsset
ExtractionProfileSnapshot 1 ── * ExtractionRun/GenericExtractionAttempt
EvidenceAsset 1 ── * EvidenceReviewDecision
EvidenceAuditSnapshot 1 ── * EvidenceReviewDecision
SourceItem * ── * EventCluster
EventCluster 1 ── * DraftArticle 1 ── * ArticleRevision
EditorialPolicySnapshot 1 ── * GenerationAttempt/ArticleRevision
ArticleRevision 1 ── * frozen EventClusterVerification/Evidence/Excluded material snapshots
ArticleRevision 1 ── * Claim * ── * EvidenceAsset
ArticleRevision 1 ── * VisualPlacement * ── 1 EvidenceAsset
ArticleRevision 1 ── * PublishedEvidenceSnapshot
ArticleRevision 1 ── * ArticleChannelRender * ── 1 PublicationTarget
PublicationIntent 1 ── * ArticleChannelRender/Approval/PublicationAttempt
CorrectionCase 0..1 ── * PublicationIntent
ArticleRevision 1 ── * QualityCheck
ArticleRevision 1 ── * Approval * ── 1 PublicationTarget
DraftArticle 1 ── * Publication 1 ── * PublicationAttempt
PublicationAttempt 1 ── 0..5 PublicationReconcileGeneration 1 ── 1 OutboxMessage
Publication 1 ── * PublicationMedia * ── 1 RemoteMedia
Publication 1 ── * PublicationMedia * ── 1 PublicDeliveryAsset
PublishedEvidenceSnapshot 1 ── * PublicationMedia
PublishedVisualizationSnapshot 1 ── * PublishedVisualizationInput * ── 1 PublishedEvidenceSnapshot
PublishedVisualizationSnapshot 1 ── * PublicationMedia
EvidenceAsset 1 ── * RemoteMedia * ── 1 PublicationTarget
SourceItem/DraftArticle 1 ── * CorrectionCase
모든 주요 엔터티 1 ── * AuditEvent
```

## 보존과 삭제

| 데이터 | 기본 보존 | 삭제 방식 |
|---|---:|---|
| 원문·파생 객체, SourceItem, RunSourceItem, DocumentExtraction, ExtractionRun, GenericExtractionAttempt, EvidenceAsset | 마지막 관련 게시/초안 이후 90일 | 권리·분쟁 보존이 없을 때 raw bytes/text와 상세 추출 DB를 만료하고 장기 참조 FK는 SET NULL/tombstone; package/model/config 지문과 PublishedEvidenceSnapshot은 별도 정책으로 보존 |
| 미발행 초안과 개정 | 마지막 수정 후 90일 | 주장/품질/배치와 함께 삭제 |
| 미참조 WordPress 원격 미디어 | prepared/active/in-flight 보호 참조 0개로 고아 표시 후 7일 | 행 잠금·lease CAS로 재조정 후 외부 bytes만 삭제; mapping tombstone은 발행 감사 만료까지 유지 |
| PublicDeliveryAsset | 활성 공개/marked-withdrawn 참조가 존재하는 동안 | 참조 0·원격 조정 완료·hold 없음 이후 최소 30일 grace 뒤 재확인 삭제; 90일 evidence purge와 독립 |
| PublishedEvidenceSnapshot, PublishedVisualizationSnapshot, EvidenceAuditSnapshot, 발행 기록, 시도, 승인, EvidenceReviewDecision, 정정, 감사 이벤트 | 발행/결정/사건 종료 중 가장 늦은 시점부터 1년 | raw FK가 null이어도 출처 identity/version·주장 관계·시각화 입력·권리/표시 snapshot을 유지; 공개 Publication이나 열린 정정/권리 hold가 참조하면 만료를 보류하고 이후 감사 보존 작업으로 삭제 |
| SourceDefinition/Snapshot, SourceRegistrySnapshot/Membership/Mutation/Decision, ExtractionProfileSnapshot/Decision, AutoPublishValidation/Decision/Activation | 이를 참조하는 run/publication/validation/audit 중 가장 늦은 종료부터 최소 1년 | 비민감 immutable config/material/decision 지문과 FK는 hold까지 유지; 큰 calibration report/model manifest bytes는 참조 0·감사 snapshot 생성 뒤 별도 object lifecycle로 만료하되 key/version/hash metadata는 유지 |
| OAuth 토큰/비밀 참조 | 연결 해제 즉시 폐기 | 비밀 저장소에서 폐기 후 참조 상태를 `revoked`로 변경 |
| 운영 로그 | 30일 | 중앙 로그 수명주기; 본문·토큰 기록 금지 |

게시물이 여전히 공개되어 있거나 정정 사건이 열려 있으면 PublishedEvidenceSnapshot과
PublicDeliveryAsset 삭제를 보류한다. raw 원문 purge는 snapshot 생성·해시 검증과 nullable FK
전환을 같은 retention transaction/batch에서 완료한 경우에만 허용한다.
관리자 삭제는 사전 영향 미리보기, 재인증, 이유 입력과 AuditEvent를 요구한다.

## English — T005 Durable Recovery Model Addendum

- `RunStep.fanout_completed_at` is a completion projection set only after the evidence
  parent durably creates all child attempts and outbox events. Upgrade recovery restores
  it only when both existing evidence counters prove completion; otherwise it emits a
  deduplicated re-fan-out request and leaves the marker null. The data migration uses
  historical models, the schema-editor database alias, and migration-local immutable
  outbox material/hash construction. Exhausted parent delivery projects both run and
  step to stopped when requested, or to their failure states otherwise.
- `DocumentExtraction.input_fingerprint` is the unique nullable canonical SHA-256 of
  schema `document-input-v1`, run-source-item identity, input kind, and input checksum.
  New rows require it. Normal document processing and legacy-HWP PDF promotion share
  this identity and one route event. Upgrade canonicalization prioritizes complete
  success, success, low confidence, and then failed/finished rows; historical duplicates
  remain audit-only with null fingerprints. Legacy-HWP convergence recovers exactly one
  valid derived asset and fails closed for missing, ambiguous, or incomplete immutable
  material.
- `PublicationReconcileGeneration` is append-only, ranges from one through five, and is
  bound one-to-one to the exact source outbox event. Source-event plus generation fencing
  makes replay resumable and prevents an older result from overwriting a newer one.
  Allocation atomically projects attempt/publication to reconciling. Terminal route
  callbacks and source-event or receipt DLQ complete the bound generation, manualize the
  attempt/publication, and make replay a no-op.

## T009 출처 레지스트리 계보 보강

`SourceDefinitionSnapshot.config/config_hash`는 레지스트리와 실행이 참조하는 역사적
identity이고, runtime adapter는 별도로 hash를 검증한 `frozen_config/frozen_config_hash`만
사용한다. 과거 v2와 legacy snapshot은 감사·재현을 위해 보존하지만 새 실행에 선택하는
snapshot v3는 adapter version, adapter·보안·secret·canonical hash 구현 파일 checksum과
Python 및 직접 runtime dependency version까지 함께 고정한다. 배포 구현 manifest가 다르면
기존 snapshot을 현재 코드로 재해석하지 않고 새 draft 승인 전 수집을 차단한다. Compose
배포도 migration 뒤 승인된 v3 snapshot 검증이 통과해야 application 서비스를 시작한다.

repository import는 같은 draft와 정확히 같은 approved head를 재사용하고, retired head는
재사용하지 않는다. 변경된 source만 새 snapshot을 만들며 나머지 membership은 직전 승인
snapshot을 carry-forward한다. request key의 동일 payload replay는 같은 결과를 반환하고 다른
payload는 충돌로 처리한다.

## English — T009 Source Registry Lineage Addendum

- `SourceDefinitionSnapshot.config/config_hash` is the immutable historical identity
  used by registry manifests and collection runs. Runtime adapters consume only
  `frozen_config/frozen_config_hash`. Historical v2 and legacy snapshots remain preserved,
  while executable v3 snapshots also bind the adapter version, adapter/security/secret/
  canonical-hash implementation files, Python runtime, and direct dependency versions.
  Migration `0003` restores the original legacy config and hash changed by the v2 wrapper
  migration, then stores that wrapper under its own frozen hash. Deployment gates
  application startup on an approved v3 snapshot matching the new build.
- Repository import reuses an exact draft, returns an exact approved head without
  creating rows, and creates a new generation after head retirement instead of
  replaying a retired registry. A changed import creates snapshots only for changed
  source material and carries reusable approved or current draft memberships forward.
- Request-key replay prevents the operation from being applied twice and returns the
  same stable resource. The OpenAPI resource response is a current projection, while
  registry decisions and mutations remain immutable operation records; the contract
  does not promise a byte-for-byte historical response body.

## T010 주택 출처 관측 계보 보강

`CollectedSourceRecord`는 공고별 stable external ID, canonical URL, 게시·수정 시각,
`active/corrected/retracted/unavailable` 상태, 결정적 raw checksum, 허용된 HTTP metadata와
첨부의 공식 file ID·MIME·크기·checksum·권리 상태를 전달한다. `restored`는 출처가 보내는
상태가 아니라 영속화 시 직전 관측과 비교해 계산하는 `RunSourceItem.discovery_kind`다.

청약홈 external ID는
`applyhome:{category}:{HOUSE_MANAGE_NO}:{PBLANC_NO}`, LH external ID는
`lh:{CCR_CNNT_SYS_DS_CD}:{PAN_ID}:{UPP_AIS_TP_CD}:{AIS_TP_CD}`다. query 전체를 제거하는
로그 redaction URL은 identity로 사용하지 않는다. canonical URL은 userinfo, fragment,
credential, session·tracking parameter를 제거하되 출처별 allowlist에 있는 공식 ID/file
parameter를 정렬해 보존한다.

`source_version_hash`는 NFC+RFC 8785 SHA-256으로 stable ID, canonical URL, title, publisher,
게시·수정 시각, language, status, content hash, 공고별 raw checksum과 ETag/Last-Modified/MIME을
묶는다. 새 hash일 때만 SourceItem과 `supersedes_id`를 만들고 동일 hash는 기존 SourceItem을
재사용한다. 모든 후속 관측은 같은 출처·external ID의 직전 RunSourceItem을
`previous_run_source_item_id`로 가리킨다. 분류 순서는 terminal 상태 뒤 active면 `restored`,
동일 SourceItem이면 `unchanged`, 현재 상태가 corrected/retracted/unavailable이면 같은 이름,
그 밖의 active 새 버전은 `new_version`이다.

`source_version_schema`는 배포 전 `legacy-source-item-version-v0`와
`nfc-rfc8785-source-item-version-v1`을 구분한다. 새 수집은 현재 schema hash를 먼저 찾고,
동일한 기존 content·게시 시각·active 상태의 legacy hash가 정확히 일치할 때만 과거 행을
호환 기준으로 인정한다. 이때 legacy 행을 계속 재사용하지 않고 현재 schema의 기준 SourceItem을
append하되 첫 관측만 `unchanged`로 기록한다. 따라서 배포 직후 거짓 변경 이벤트는 만들지
않으면서 이후 raw checksum, HTTP validator, 첨부 공식 ID·checksum만 바뀐 변경도 숨기지 않는다.
SourceItem과 RunSourceItem의 update/delete, 다른 source 또는 external ID로 향하는
`supersedes/previous`는 ORM과 PostgreSQL trigger에서 거부한다. RunSourceItem은 실행 registry에
활성화된 source snapshot, 같은 attempt/run/source, 성공한 과거 관측만 가리킬 수 있다.

출처별 목록 순회는 승인된 page 크기와 page 상한 안에서만 실행하며 반복 page, 중복 identity,
필수 identity/date/detail 구조 누락, total count 미달, page·request·elapsed 상한 소진을
source schema 실패로 처리한다.
timeout, 5xx, parsing 실패 또는 한 번의 목록 누락은 `unavailable`로 바꾸지 않는다.
요청 시간창 밖에서는 성공한 과거 관측의 stable ID만 승인된 reconciliation 기간 안에서
재조회한다. ID 후보 자체도 그 기간 안에 성공적으로 관측된 행으로 제한하므로 누적 identity가
영구 실패를 만들지 않는다. request budget은 redirect와 다중 IP 재시도를 포함한 실제 HTTP
시도마다 차감한다. API의 첨부 필드뿐 아니라 공식 상세 HTML page의 link도 수집하고 공식
file ID를 URL과 분리해 보존한다. 실행형 handler에서 공개 URL을 결정할 수 없으면 실패
폐쇄한다. evidence 다운로드는 frozen `recordHosts`와 `attachmentContentTypes`를 함께
검사하고, 선언 MIME과 sniff MIME의 충돌 또는 확장자 기반 extractor 위장을 거부한다.

공공데이터 서비스 키는 `env://DATA_GO_KR_SERVICE_KEY`로 목적이 고정되고 인증 요청은
adapter별 정확한 HTTPS host/path에서만 가능하다. 실제 값은 source-check와 collection
worker가 요청 직전에 해석한다. adapter version, 구현 파일 manifest, frozen config와 시간창을
묶은 요청 지문, 응답 checksum, SourceItem/RunSourceItem 및 `source.item_changed` outbox는
source 단위 transaction에서 attempt 성공과 함께 커밋된다. 응답 payload의 credential 계열
field/value는 본문·metadata 생성 전에 마스킹한다. 동시 worker는 성공 attempt의 response
checksum을 재검증하고 이미 다음 단계로 간 run을 이전 상태로 되돌리지 않는다.
`unchanged`에는 변경 이벤트와 신규 evidence가 없고 `new_version/corrected/restored`만
추출한다. `retracted/unavailable/restored` 영향 평가는 전체 previous 관측 lineage에서 실제
evidence를 사용한 글을 찾고, 복원은 미적용 terminal case를 닫은 뒤 restoration case를 만든다.

## English — T010 Housing Source Observation Lineage

Each normalized housing record carries a provider-stable identity, identity-preserving
canonical URL, publication and modification timestamps, explicit source status,
deterministic per-notice raw checksum, bounded HTTP metadata, and attachment provenance.
ApplyHome uses `applyhome:{category}:{HOUSE_MANAGE_NO}:{PBLANC_NO}` and LH uses
`lh:{CCR_CNNT_SYS_DS_CD}:{PAN_ID}:{UPP_AIS_TP_CD}:{AIS_TP_CD}`.

The canonical source-version hash binds identity, content, semantic timestamps and status,
raw checksum, and meaningful HTTP validators. A new hash appends a SourceItem and
`supersedes` edge; an exact historical hash reuses the immutable row. Every later
RunSourceItem points to the prior observation. Persistence derives restored only for a
terminal-to-active transition, then unchanged for the exact same item, explicit terminal
or corrected kinds from provider state, and otherwise new_version.

`source_version_schema` distinguishes historical legacy hashes from the NFC/RFC 8785 v1
material. An exact compatible legacy row causes a current-schema baseline SourceItem to
be appended while its first observation is classified unchanged. This avoids a false
cutover event without permanently hiding later raw-validator or attachment-only changes.
SourceItem and RunSourceItem are append-only. ORM and PostgreSQL guards require an enabled
run-registry membership, matching attempt/run/source provenance, and a succeeded prior
observation in the same stable lineage.

Pagination is bounded, cycle-checked, and total-count complete under source-wide request
and elapsed budgets. Only already observed identities are rechecked outside the requested
window within the approved reconciliation horizon, and the candidate identity set is
itself limited to successful observations in that horizon. Physical redirect/IP attempts
consume the frozen request budget. Attachments retain official file IDs; unresolved
script-only downloads fail closed. Downloads enforce both frozen record hosts and the
declared/sniffed attachment MIME contract before extension-specific extraction.

The data.go.kr credential is purpose-bound to its exact adapter HTTPS host/path and is
resolved from `env://DATA_GO_KR_SERVICE_KEY` only at request time inside isolated
source-check and collection workers. Attempt provenance, observations, success, and change
events commit atomically. Credential-like response fields are redacted before persistence.
Concurrent replay must match the succeeded response checksum and cannot regress a run
that entered its next stage. Unchanged and terminal observations create no new evidence.
Terminal and restored impact evaluation walks the immutable lineage to the evidence-bearing
article and converges pending terminal cases. Network and schema failures never imply
unavailable.

## English — T012 Source Access and Collection Runtime

Every version-3 source snapshot freezes canonical `accessPolicy` and `rightsPolicy`
documents plus their SHA-256 hashes. Access policy v1 binds the reviewed decision,
contactable User-Agent, traffic scope, request/retry/redirect budgets, and per-origin
purpose, method, path, and robots rules. Rights policy v1 makes separate fail-closed
decisions for records, document attachments, and media attachments; attachment rights
cannot widen the record decision.

Each CollectionRun pins the exact TopicPolicy identity, version, policy hash, freshness
minutes/cutoff, allowed authority tiers, source-registry identity, and registry manifest.
Each enabled registry membership receives one durable SourceCollectionAttempt and
`source.collect_requested` event. Attempt state is
`queued/running/retry_scheduled/succeeded/failed/skipped`; its projection records stable
failure category, redacted error, HTTP status, retry timing, request/response counts,
access- and rights-policy hashes, authority tier, freshness cutoff, freshness-exclusion
count, and duration. Freshness uses `modified_at` first and falls back to `published_at`
for active and corrected records; explicit reconciliation-only and non-evidence terminal
records are exempt.

SourceCollectionObservation is append-only and unique by attempt plus the actual receipt
delivery generation. It also freezes the delivery's freshness-exclusion count.
It preserves every retry, success, skip, and terminal failure without treating the mutable
attempt projection as audit history. Instance, queryset, base-manager, and PostgreSQL
trigger guards reject updates and deletes. Missing earlier receipt generations are
backfilled as interrupted infrastructure observations by the next delivery or exhaustion
callback. A collection finalizer advances only after every
source attempt is terminal. Zero successful sources fail the run without evidence fan-out;
partial success advances to extraction while retaining failure counters and summaries.

## English — T013 Verified Event Clustering

Housing clusters use authority plus notice ID, while correction ID remains part of the
verification-bound article identity. This keeps corrected versions in one notice lineage
and appends a superseding verification. A corrected official lineage without a stable
correction ID is rejected as `official_correction_identity_missing`; mutable source-version
hashes never substitute for official correction identity. Semiconductor clusters hash subject, action,
official announcement ID, and the KST event date. EventClusterItem is unique by cluster
and exact RunSourceItem and freezes source-snapshot, origin, independence, selection, and
reason provenance in the verification evidence manifest. Workers acquire canonical-key
rows in one global sorted order to avoid cross-run lock cycles.

EventClusterVerification is append-only and unique by cluster plus version and by cluster
plus origin run. Origin-run `(created_at, id)` is the causal fence: a late older run cannot
replace the head, and a normal new run sees only members at or below its causal key. It
does not retroactively rewrite an already frozen newer observed head when an older run's
membership arrives late; that membership joins the next normal newer-run verification.
This preserves immutable generated/reviewed evidence sets.
freezes the run policy version/hash, a run-derived KST local date, every included,
excluded, and conflicting member, evidence identifiers and hashes, conflict and excluded
source manifests, rule/result hashes, counts, result, and the superseded decision. Shared
ownership or shared syndication origin forms one independent component.
Corporate self-claims are not direct official primary evidence. The breaking categories
are `regulation_export_control`, `factory_supply_disruption`,
`merger_or_material_earnings`, and `critical_technology_or_mass_production`. A category
is verified breaking with one official/regulatory primary or two independent origins;
otherwise it is held. A held cluster emits no standalone breaking work unit but remains
in the frozen daily-digest verification set. Non-breaking clusters are daily-digest
candidates. Retracted and unavailable members are explicitly excluded; a terminal housing
lineage head rejects the cluster. Breaking classification requires explicit subject,
action, official announcement ID, and breaking category. Current adapters do not always
supply this semantic bundle, so incomplete records fail closed to daily digest and remain
an upstream blocker. Generation consumes only selected/included publishable evidence from
the exact latest verification set and records that set in GenerationAttempt input material.

ArticleEventCluster freezes the first article creation's stable verification order, role,
reason, and cluster snapshot hash. Reusing an existing article or revision never mutates
those memberships. Model/queryset guards and PostgreSQL/SQLite triggers reject later
updates or deletes, and inserts must bind the verification's own cluster and exact frozen
evidence-manifest hash.

## English — T015 Profile Verification Envelope

ExtractionProfileDecision stores the verification report object key, immutable version, and
SHA-256. The columns remain nullable only for migration compatibility; every newly approved
decision must carry all three and exactly match the profile projection. A retired decision copies
the immediately preceding approved envelope. The envelope is included in the decision hash and
audit metadata. For approved or retired profiles, the report API uses the latest decision's frozen
envelope and returns 409 if the mutable projection differs. Report verification may write a new
core object only while the profile is draft; afterward it can only revalidate the exact existing
core bytes and envelope. Migration 0004 backfills historical decision chains from the current
profile projection and aborts when that projection is incomplete. Because older rows did not store
the contemporaneous envelope, this preserves runtime compatibility without claiming historical
reconstruction beyond the available frozen projection.

## 한국어 — T016 추출 세대·종결·객체 쓰기 계약

`RunStep`은 source event와 consumer receipt의 단조 증가 `lease_generation`, owner/token으로
구성된 lease envelope만 영속화한다. `DocumentExtraction`, `ExtractionRun`,
`GenericExtractionAttempt`는 같은 lease envelope와 terminal event key/state를 영속화한다.
`delivery_count`는 receipt attempt 수가 아니라 같은 64-bit lease generation의
도메인 별칭이다. RUNNING 행은 완전한 lease를 가져야 하고 queued/terminal 행은 lease를
비워야 한다. succeeded/low-confidence는 `terminal_state=ready`, failed는
`terminal_state=failed`와 단 하나의 terminal key를 가져야 한다. legacy HWP UDS protocol의
`generation=1`은 이 DB lease와 분리된 호환 상수다.

추출 claim, retry, completion, dead-letter terminal callback은 `CollectionRun → RunStep →
DocumentExtraction/GenericExtractionAttempt → ExtractionRun → EvidenceAsset` 순서로 잠근다.
동일 source event의 더 높은 receipt lease만 RUNNING을 재점유할 수 있고, 이전 token 결과는
no-op이다. retry가 lease를 해제한 직후 max-attempt terminal callback이 실행되어도 같은
source event/current generation이면 terminalize한다. stop은 모든 active child lease를 같은
transaction에서 폐기하지만 이미 failed/completed/stopped인 run의 지연 delivery는 run 상태를
변경하지 않는다.

`ExtractionObjectWriteReservation`은 raw/result/reason/converted 객체 쓰기를
`reserved → uploaded → bound`로 기록한다. fence 상실, stop, crash recovery는 아직 bound되지
않은 행을 `orphaned`로 수렴시킨다. object key는 content-addressed/shared일 수 있으므로 이
ledger에서 즉시 삭제하지 않는다. 실제 삭제는 참조를 재계산하는 retention/reconcile 작업
(T030 이후 경계)만 수행한다.

evidence migration 0005는 collection 0009와 infrastructure 0002 뒤에 실행한다. historical
model과 schema-editor DB alias만 사용해 exact requested event/receipt를 pending/retry로
재무장하고, 증명 가능한 pending ready 및 full child provenance manifest만 보존한다. 0개 또는
복수 event, 불완전 lineage, 불명확한 성공/실행 중 행은 synthetic ready를 만들지 않고
deterministic failed/manual recovery로 닫는다.

## English / AI-readable — T016 extraction generation, terminal, and object-write contract

`RunStep` persists only the source-event and monotonic consumer-receipt lease envelope:
`lease_generation`, owner, and token. `DocumentExtraction`, `ExtractionRun`, and
`GenericExtractionAttempt` persist that lease envelope plus terminal event key/state.
`delivery_count` is a 64-bit domain alias of the receipt lease generation, not the rollback-prone
attempt counter. RUNNING requires a complete lease; queued and terminal rows clear it. Successful
or low-confidence rows reserve `terminal_state=ready`, failed rows reserve
`terminal_state=failed`, and each aggregate has one terminal key. The legacy-HWP UDS
`generation=1` remains a separate compatibility constant.

Claims, retries, completions, and DLQ terminal callbacks lock in the order CollectionRun, RunStep,
Document/Generic, ExtractionRun, then Evidence. Only a higher receipt lease for the same source
event may reclaim RUNNING work; an older token is a no-op. A max-attempt callback may terminalize
the same event/current generation after retry released the lease. Stop revokes every active child
in one transaction, while delayed work for an already terminal run never rewrites the run state.

`ExtractionObjectWriteReservation` records raw, result, reason, and converted writes as
`reserved → uploaded → bound`; fence loss, stop, and recovery converge unbound rows to
`orphaned`. Shared content-addressed keys are never deleted immediately. Physical deletion belongs
to a later reference-aware retention/reconcile boundary (T030+).

Evidence migration 0005 depends on collection 0009 and infrastructure 0002. It uses historical
models and the schema-editor alias, rearms only exact requested events/receipts to pending/retry,
and preserves only provable pending-ready envelopes and full child-provenance manifests. Missing,
multiple, or ambiguous material fails closed instead of creating synthetic ready state.

## English / AI-readable — T018 editorial policy and revision contract

The current editorial policy is resolved from exact deployed topic release JSON bytes. There is no policy
approval lifecycle and no mutable policy head. `EditorialPolicySnapshot` is append-only and binds
the exact policy key/version, release-document hash, canonical config, implementation manifest, and
material hash. Reusing a key/version with changed bytes is a permanent conflict rather than an
implicit new version.

ArticleDetail never fills a frozen verification, evidence, or exclusion field from the live SourceItem or
current EvidenceAsset. Source version, modified/retrieved times, rights, attribution, alt text, and exclusion
reason come only from the revision snapshot. A quarantined legacy policy snapshot without exact release,
config, or implementation identities exposes those three hashes as null and is runtime-ineligible; the API
never substitutes `material_hash` for a missing identity.
A migrated revision whose exact historical evidence material cannot be recovered exposes only the honest
`evidenceId` plus `legacyQuarantine=true` projection. It never synthesizes source/version/rights/freshness
from live rows or empty strings, and it is not publishable.

`ArticleRevision.body_blocks` is canonical. A revision binds one policy snapshot plus three sorted,
independently hashed materials: all contributing verifications, all used publish-eligible evidence,
and all excluded/duplicate/conflicting inputs. Every assertion in the title, summary, body blocks,
or captions is an atomic `fact`, `company_claim`, `interpretation`, or `outlook`. Company claims
require actor attribution; interpretations derive from verified claims; outlooks require an actor,
horizon, and uncertainty language.

The exact mandatory content/evidence gate set is:
`all_publishable_claims_grounded`, `high_risk_verification_satisfied`,
`claim_independence_satisfied`, `source_freshness_satisfied`,
`evidence_publish_eligibility_current`, `quotation_limits_satisfied`,
`claim_types_separated_and_attributed`, `duplicate_or_conflict_resolved`,
`korean_readability_and_repetition`, and `no_exaggeration_or_false_experience`. A revision with any
visual also requires `visual_rights_and_alt_text`. Any blocking failed or manual-required result
prevents review-ready state.

A manual edit atomically creates a new pending revision and `editorial.revalidate_requested@1`.
That event allows exactly `article_id`, `article_revision_id`, `editorial_policy_snapshot_id`,
`editorial_policy_material_hash`, `verification_manifest_hash`, `input_evidence_manifest_hash`, and
`excluded_material_manifest_hash`.
`CreateRevisionRequest` accepts canonical bodyBlocks and exact claimBindings containing only block/statement/
claim type, a unique non-empty request-local `claimRef`, sorted evidence IDs, citation marker, per-evidence
source spans, semantic key, actor, attribution, horizon, uncertainty note, and sorted
`derivedFromClaimRefs`. Interpretation references may target only fact/company_claim bindings in the same
request. The server resolves them to revision-scoped deterministic Claim UUIDs persisted and returned as
`claimId/derivedFromClaimIds`. The response returns the article/revision identity,
revision number, revalidation/quality states, and content/evidence/policy/verification/exclusion hashes. An
identical request-key replay is 200, creation is 201, changed replay or stale base is 409, and invalid material
is 422.
Revalidation resolves the current release policy, checks current evidence publish eligibility, and
rebuilds every claim, relation, and quality result from the new body blocks. It never copies the
prior revision's claims, checks, approvals, or publication intents.

## English / AI-readable — T019 approval decision and head contract

`Approval` is append-only. Each decision persists `approval-subject-v3` subject material, its head
version, exact request hash, immutable decision reason, and an `approval-decision-v1` hash over the
subject hash, decision, head version, superseded approval ID, request hash, actor type/ID, worker
event key, and reason.
One `PublicationApprovalHead` row per publication intent and target points to the current decision and
monotonically increasing version.

Every non-replay request compares both `expectedLatestApprovalId` and `expectedHeadVersion`; the
initial expected pair is `(null, 0)`. The only legal transitions are no head to approved or rejected,
rejected to approved, and approved to revoked. Revoked is terminal. Same-key exact replay returns the
existing decision; changed replay, stale CAS, or any other transition returns 409.

A revoked decision always consumes a purpose-bound `approval_revoke` reauthentication proof. An
approved unpublish consumes an `unpublish` proof. Every other decision/action combination requires a
null proof. A missing, null, or malformed UUID in a proof-required request is a schema-level 422;
a syntactically valid but expired or scope/entity-mismatched proof is 403. The request key and immutable
reason come from either the administrator request or the validated-auto worker policy decision.
Decision insertion, head advancement, intent aggregate change, and synchronous
`AuditEvent` recording are atomic. No approval-decision outbox event is emitted. Dispatch and execution
recheck the current approved head and frozen subject. API replay of
a historical row derives `currentHead`, `isCurrent`, and `dispatchEligible` from the shared read-only
head projection, never from the replayed row itself. `decidedBy` is always the non-null approval owner
(including the activation/schedule administrator for worker auto mode). `decisionActorType` identifies
admin versus worker execution; `decisionActorId` is the administrator UUID and is null for workers.
