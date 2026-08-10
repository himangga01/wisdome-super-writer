# 내부 작업 이벤트 계약

## 전달 의미

- Redis/Celery 전달은 **at-least-once**로 간주한다. 소비자는 중복을 정상 상태로 처리한다.
- 메시지는 엔터티 ID와 정책 버전만 전달하고 본문·원문·토큰은 PostgreSQL/객체 저장소에서
  권한을 확인한 뒤 읽는다.
- DB 상태가 기준이며 브로커 결과 backend는 업무 성공의 기준이 아니다.
- 생산자는 업무 상태와 outbox 행을 같은 DB 트랜잭션에 만든다. 커밋 뒤 broker로 직접
  보내지 않으며, lease/fencing을 가진 별도 dispatcher만 outbox를 전송한다.
- 새 버전은 하위 호환 필드 추가만 허용한다. 제거·의미 변경은 새 `event_version`을 만든다.

## 공통 Envelope

```json
{
  "event_id": "uuid-v7",
  "event_type": "publication.requested",
  "event_version": 1,
  "occurred_at": "2026-07-17T00:00:00Z",
  "correlation_id": "uuid-v7",
  "causation_id": "uuid-v7-or-null",
  "job_id": "uuid-v7",
  "entity_type": "publication_attempt",
  "entity_id": "uuid-v7",
  "operation": "create",
  "attempt": 1,
  "dedupe_key": "sha256-or-domain-key",
  "policy_versions": {
    "registry": 3,
    "topic": 2,
    "editorial": "2026-07-17.1",
    "publisher": "wordpress-v1"
  },
  "not_before": null,
  "payload": {}
}
```

필수 필드: `event_id`, `event_type`, `event_version`, `occurred_at`, `correlation_id`,
`job_id`, `entity_type`, `entity_id`, `operation`, `attempt`, `dedupe_key`, `payload`.

## 이벤트와 최소 Payload

| 이벤트 | Payload | 소비자 동작 |
|---|---|---|
| `source.check_requested` | `source_id`, `source_snapshot_id`, `source_config_hash`, `check_id` | 정확한 frozen snapshot과 접근·권리 정책을 재조회해 비게시 source check 수행 |
| `run.requested` | `run_id` | 실행이 `queued`일 때 수집 시작 |
| `run.stop_requested` | `run_id`, `reason_code` | 새 하위 작업을 만들지 않고 안전 지점에서 중지 |
| `source.collect_requested` | `source_collection_attempt_id` | DB의 run/TopicPolicy/source snapshot/window와 adapter·access-policy manifest를 재조회해 source 하나를 독립 수집 |
| `run.collection_finalize_requested` | `run_id` | 모든 source attempt가 terminal인지 재평가하고 0성공 실패 또는 부분/전체 성공의 extraction 전이를 원자 집계 |
| `source.item_changed` | `source_collection_attempt_id`, `run_id`, `run_source_item_id`, `source_item_id`, `change_kind=new_version/corrected/retracted/unavailable/restored` | succeeded attempt의 adapter/response provenance와 불변 RunSourceItem 계보·활성 registry membership·kind/status 매핑을 검증; new/corrected/restored는 추출·검증, retracted/unavailable/restored는 전체 계보 기반 정정·철회·복원 영향 평가; unchanged에는 이벤트 없음 |
| `evidence.document_extract_requested` | `run_id`, `run_source_item_id`, `source_item_id`, `input_asset_id?`, `input_kind`, `document_extraction_id`, `extraction_run_id`, `retry_of_run_id?`, `profile_snapshot_id`, `profile_material_hash`, `profile_key`, `profile_version`, `page_set_hash`, `fingerprint_schema_version` | PDF 또는 가상 1페이지 독립 이미지의 결정적 child run; run/source 계보와 snapshot ID/material hash로 DB의 승인 profile 재조회 |
| `evidence.document_ready` | `run_id`, `run_source_item_id`, `source_item_id`, `document_extraction_id`, `input_page_count`, `coverage_manifest_hash`, `selected_evidence_manifest_hash`, `document_complete=true` | run/source 계보와 상위 ID로 routing manifest와 선택 child/EvidenceAsset 전체를 DB에서 조회해 page coverage·권리·신뢰·중복을 재확인; 단일 child ID를 완료 대표로 사용하지 않음 |
| `evidence.other_extract_requested` | `run_id`, `run_source_item_id`, `source_item_id`, `input_asset_id?`, `generic_extraction_attempt_id`, `profile_snapshot_id`, `profile_material_hash`, `engine`, `extractor_version`, `config_hash`, `validation_mode`, `calibration_profile_key?`, `calibration_profile_version?`, `calibration_profile_hash?`, `fingerprint_schema_version`, `extraction_fingerprint` | run/source 계보, queued attempt와 승인 profile snapshot/material hash를 재조회한 뒤 비문서 파생 실행 |
| `evidence.other_ready` | `run_id`, `run_source_item_id`, `source_item_id`, `evidence_asset_id`, `generic_extraction_attempt_id`, `engine`, `locator_type`, `validation_mode`, `result_checksum`, `low_confidence_reasons_hash?`, `calibration_profile_key?`, `calibration_profile_version?`, `calibration_profile_hash?`, `extraction_fingerprint` | RunSourceItem 계보, attempt/result checksum과 engine-locator-validation 매트릭스를 DB에서 재검증한 뒤 권리·신뢰·중복 검증 |
| `evidence.review_decided` | `evidence_audit_snapshot_id`, `evidence_asset_id?`, `review_decision_id`, `review_subject_schema_version`, `review_subject_hash`, `request_key`, `decision` | durable audit subject의 current decision CAS를 통과한 append-only 관리자 판정; 추출 상태는 변경하지 않음 |
| `visualization.render_requested` | `visualization_render_id` | DB의 ArticleRevision·stable-order input claim/evidence join과 renderer/transform manifest를 재조회해 복수 출처 시각화 생성 |
| `visualization.render_ready` | `visualization_render_id`, `evidence_asset_id`, `input_manifest_hash`, `transform_spec_hash`, `output_checksum` | visualization-derived provenance와 전체 input join/hash·권리·alt를 재검증 |
| `cluster.validation_requested` | `event_cluster_id` | 출처 수와 충돌 검증 |
| `article.draft_requested` | `generation_attempt_id` | DB의 승인 generation pipeline과 exact input evidence manifest를 재조회해 구조화 초안 생성 |
| `article.generation_ready` | `generation_attempt_id`, `article_revision_id`, `input_evidence_manifest_hash`, `generation_pipeline_manifest_hash`, `output_checksum` | current revision provenance를 검증하고 주장 재구성 요청 |
| `article.claims_requested` | `article_revision_id`, `revision_content_hash`, `input_evidence_manifest_hash` | 새 revision의 모든 factual text에서 Claim을 다시 추출하고 Evidence를 재연결; unsupported fact면 blocked |
| `article.quality_requested` | `article_revision_id`, `revision_content_hash`, `quality_gate_manifest_hash` | current claim graph에 전체 차단형 품질 검사 재실행 |
| `editorial.revalidate_requested` | `article_id`, `article_revision_id`, `editorial_policy_snapshot_id`, `editorial_policy_material_hash`, `verification_manifest_hash`, `input_evidence_manifest_hash`, `excluded_material_manifest_hash` | 수동 개정의 exact bodyBlocks와 frozen policy/verification/evidence/exclusion material을 DB에서 재조회하고 current policy/evidence publish eligibility 확인 뒤 claim graph와 전체 gate 재생성 |
| `media.upload_requested` | `remote_media_id`, `publication_attempt_id`, `publication_intent_id`, `target_snapshot_id`, `target_config_hash` | exact intent/attempt의 WordPress checksum 매핑 확인 후 업로드 또는 재사용 |
| `media.available` | `remote_media_id`, `publication_attempt_id`, `publication_intent_id`, `target_snapshot_id`, `target_config_hash`, `remote_source_url` | 같은 current attempt의 final render 자리만 결합; stale intent면 dispatch 금지 |
| `media.reconcile_requested` | `remote_media_id`, `publication_attempt_id`, `publication_intent_id` | media slug/marker로 exact attempt의 응답 유실 조정; 업로드 반복 금지 |
| `delivery.prepare_requested` | `public_delivery_asset_id`, `publication_attempt_id`, `publication_intent_id`, `expected_lease_generation` | Blogger final용 장기 공개 객체를 행 잠금으로 생성/재사용하고 pending delete를 취소 |
| `delivery.available` | `public_delivery_asset_id`, `publication_attempt_id`, `publication_intent_id`, `lease_generation`, `public_url` | 같은 current attempt의 승인 template 자산 자리만 immutable URL로 결합 |
| `delivery.reconcile_requested` | `public_delivery_asset_id`, `publication_attempt_id`, `publication_intent_id` | 객체 checksum·익명 GET과 PublicationMedia binding/원격 본문 참조 조정 |
| `delivery.delete_requested` | `public_delivery_asset_id`, `expected_lease_generation` | ref=0 grace 뒤 행 잠금에서 binding/in-flight/hold/원격 본문을 재검산해 bytes 삭제 또는 취소 |
| `delivery.deleted` | `public_delivery_asset_id`, `lease_generation`, `delete_reason`, `last_reconcile_hash` | mapping tombstone을 감사 수명 동안 유지하고 physical object 삭제 결과 기록 |
| `publication.requested@1` | `publication_attempt_id` | exact bound replay 또는 proven virgin generation 1 전용; 나머지는 `legacy-unverifiable-v1` no-op quarantine |
| `publication.requested@2` | `publication_attempt_id`, `execution_attempt_no` | exact 1~5 business write 세대를 결속하고 승인·kill switch·execution lease를 외부 쓰기 직전에 재검증 |
| `publication.primary_public` | `primary_publication_attempt_id`, `publication_intent_id`, `article_revision_id`, `publication_id`, `target_snapshot_id`, `target_config_hash`, `secondary_publication_attempt_ids`, `remote_url`, `public_checked_at` | 같은 current intent에 미리 생성된 Blogger attempt에만 공개 WordPress URL을 결합하고 보조 발행 요청 |
| `publication.reconcile_requested@1` | `publication_attempt_id` | exact bound replay 또는 proven virgin generation 1 전용; 나머지는 no-op quarantine |
| `publication.reconcile_requested@2` | `publication_attempt_id`, `reconcile_attempt_no` | exact 1~5 read-only 원격 결과 조정; create 반복 금지 |
| `correction.detected` | `correction_case_id` | 변경 근거 검증과 영향 글 검색 |
| `correction.apply_primary_requested` | `publication_attempt_id` | attempt가 고정한 current intent/revision/target snapshot/resolved action으로 WordPress 기존 글 수정·철회 |
| `correction.primary_completed` | `publication_attempt_id`, `publication_intent_id`, `correction_case_id`, `article_revision_id`, `target_snapshot_id`, `target_config_hash`, `resolved_action`, `terminal_state`, `secondary_publication_attempt_ids`, `remote_url?` | 같은 intent의 pre-created secondary attempt만 해제; update/mark는 URL 결합, unpublish는 URL 없이 철회 요청 |
| `correction.apply_secondary_requested` | `publication_attempt_id` | attempt가 고정한 Blogger resolved action과 final binding으로 기존 post update/revert/delete |
| `retention.expire_requested` | `retention_batch_id` | 보류 조건 확인 후 데이터·객체 만료 |

`correction.primary_completed`에서 `resolved_action=update/mark_withdrawn`이면
`terminal_state=published/marked_withdrawn`과 `remote_url`이 필수다. `resolved_action=unpublish`이면
`terminal_state=withdrawn/deleted/draft`이고 `remote_url`을 생략하며 secondary consumer는
payload를 조립하지 않고 지정된 secondary PublicationAttempt를 재조회해 Blogger
revert/delete를 실행한다.

## 소비자 멱등 규칙

1. 소비 시작 시 `(event_id, consumer_name)` 처리 기록을 잠근다.
2. 이미 성공한 기록이면 side effect 없이 ACK한다.
3. 동일 업무를 다른 event가 요청할 수 있으므로 도메인 `dedupe_key`와 DB 고유 제약도
   함께 확인한다.
4. 외부 API 호출 직전 `PublicationAttempt=running`, exact `execution_attempt_no`,
   `execution_generation`, source event와 current consumer receipt lease envelope를 원자적으로
   저장한다. 실제 mutating HTTP 바로 전 짧은 pre-I/O fence에서 statement-time의 같은 current
   ownership·미만료 raw capability와 승인, intent, target snapshot, kill switch를 다시 확인한다.
5. 외부 결과를 저장하기 전 워커가 죽으면 `reconcile_requested`를 만들고 원격 상태를
   확인한다.
6. 동일 profile·config·page set의 진짜 일시 오류만 같은 `event_id`의 attempt를 증가시키고
   도메인 멱등 키를 유지한다.
7. PaddleOCR 저신뢰로 profile/config를 바꾸면 원 run을 terminal `low_confidence`로 끝내고,
   새 ExtractionRun·`event_id`·`dedupe_key`·fingerprint를 만든다. 새 이벤트는 원 run을
   `retry_of_run_id`와 `causation_id`로 연결하며 기존 이벤트 payload를 변경하지 않는다.
   추출 `dedupe_key`와 fingerprint는 `document_extraction_id`를 포함하며 성공 결과 재사용은
   같은 상위 DocumentExtraction 안에서만 허용한다.
8. generic 추출은 GenericExtractionAttempt fingerprint 고유 제약으로 단일화한다. 성공·
   저신뢰 결과와 `evidence.other_ready` outbox를 같은 트랜잭션에 만들고 failed attempt는
   EvidenceAsset과 ready 이벤트를 만들지 않는다.
9. evidence review는 EvidenceAsset이 남아 있으면 그 행과 항상 존재하는 EvidenceAuditSnapshot
   행 잠금 아래 subject version/hash와
   `expected_latest_decision_id`를 CAS로 비교한다. decision, latest projection과 outbox는 같은
   트랜잭션이며 `(evidence_audit_snapshot_id, request_key)`가 고유하다. raw purge 뒤 소비자는
   nullable evidence ID가 아니라 audit snapshot ID/hash로 멱등·감사를 식별한다.
10. 영구 오류는 dead-letter 상태와 분류된 `error_code`를 기록하고 자동 반복을 중단한다.
11. Blogger 발행·정정 이벤트는 대응하는 `publication.primary_public` 또는
   `correction.primary_completed`의 `event_id`를 `causation_id`로 가져야 한다. 완전
   unpublish 분기의 secondary 이벤트에는 `canonical_source_url`이 없어도 된다.
12. 모든 publication/correction/media request와 완료 신호는 pre-created PublicationAttempt와
    그 exact PublicationIntent를 고정한다. 소비자는 attempt→intent→revision→target command/
    snapshot→Approval을 DB에서 재조회하고 intent가 여전히 current인지 확인한 뒤에만 호출·후속
    dispatch한다. superseding intent 뒤 늦게 도착한 primary/media/reconcile 완료 이벤트는
    side effect 없이 stale 처리하며 최신 intent를 추측 조회하지 않는다.
13. generation worker는 GenerationAttempt ID로 exact input/pipeline manifest를 재조회한다.
    succeeded attempt, ArticleRevision과 claims/quality outbox를 원자 저장한다. 관리자 edit는 새
    revision ID/content hash의 claims→quality 이벤트를 새로 만들며 이전 revision의 claim,
    visualization, render, intent 또는 approval을 재사용하지 않는다.
14. PublicDeliveryAsset prepare/delete 소비자는 asset 행을 잠그고 expected lease generation을
    CAS로 검사한다. prepared/active PublicationMedia와 scheduled/in-flight publish/update/
    withdraw/reconcile attempt도 보호 참조다. 재참조는 pending delete를 취소하고 generation을
    올리며, stale delete는 bytes를 지우지 않는다. available 이벤트는 current intent/attempt와
    일치할 때만 Blogger final render를 해제한다.

## 재시도 기준

| 오류 | 기본 처리 |
|---|---|
| connect/read timeout, 429, 502/503/504 | 최대 5회, Retry-After 우선, 지수 백오프+지터 |
| source access policy/robots/path/method/MIME 거부 | 영구 source 실패; 외부 요청 또는 후속 evidence 없음 |
| source poll/rate/concurrency 제한, Redis/DNS/transport 일시 오류 | attempt=`retry_scheduled`; Retry-After 또는 bounded backoff 뒤 동일 source event만 최대 5회 전달 |
| source authentication 실패 | 영구 source 실패; 승인 snapshot·secret 참조를 수정하고 새 source check/registry 승인을 요구 |
| source freshness/authority/rights 제외 | 분류된 제외 관측을 남기고 해당 record/evidence 생성 금지 |
| parser schema mismatch | GenericExtractionAttempt/RunStep을 1회 재확인 후 `failed`; EvidenceAsset·other_ready 없음, 출처 health 저하 |
| calibrated generic 저신뢰 | GenericExtractionAttempt=`low_confidence`; EvidenceAsset=`manual_required/publishable=false`, 교차 근거로 자동 승인 금지 |
| PaddleOCR/표 저신뢰 | 원 run=`low_confidence`; 승인된 대체 profile run 1회 후에도 저신뢰이면 DocumentExtraction=`low_confidence`, EvidenceAsset=`manual_required/publishable=false` |
| PaddleOCR model/config checksum 불일치 | ExtractionRun=`failed`, DocumentExtraction=`failed`; EvidenceAsset·document_ready 없음, 배포 manifest 복구 필요 |
| PDF 페이지 누락 | `evidence.document_ready` 금지; 누락 chunk run 1회 후에도 불완전하면 DocumentExtraction=`failed/page_incomplete`, partial EvidenceAsset 게시 금지 |
| PDF 암호화·손상·안전 한도 초과 | DocumentExtraction=`failed`, RunStep=`failed`; EvidenceAsset·document_ready 없음, 분류된 사유 기록 |
| 정적 이미지 다중 frame/애니메이션 | DocumentExtraction=`failed/unsupported_multiframe_image`; 첫 frame 부분 성공·document_ready 금지 |
| 400/422 콘텐츠 제한 | 영구 실패; 채널 preview 위반으로 반환 |
| 401 만료 | 토큰 갱신 1회 후 재시도; 실패 시 target expired |
| 403 권한/정책 | 영구 실패; target blocked 또는 revoked |
| 외부 결과 불명 | create 재시도 금지; reconcile 큐로 이동 |

실제 시간은 출처/채널별 `Retry-After`, 약관과 운영 측정값으로 조정한다.

## 큐 분리

```text
collect.housing
collect.semiconductor
extract.document
extract.ocr.paddle
extract.generic
editorial
publish.media.wordpress
publish.wordpress
publish.blogger
reconcile
maintenance
```

한 출처·주제·채널의 실패가 다른 큐를 고갈시키지 않도록 동시성, rate limit와 worker
pool을 분리한다. PaddleOCR worker는 발행·수집 자격 증명에 접근할 수 없고 검증된 로컬
객체와 사전 탑재된 checksum 일치 모델만 읽는다. 런타임 외부 네트워크와 모델 다운로드를
허용하지 않는다. child ExtractionRun은 `evidence.document_ready`를 직접 발행하지 않으며 상위
DocumentExtraction이 expected=`0..input_page_count-1`와 선택 child 결과의 coverage를
검증하고 `document_complete=true`가 된 뒤에만 document ready 이벤트를 만든다. 독립
이미지는 디코더 frame 수가 정확히 1일 때만 `input_page_count=1`, expected=`[0]`을
허용한다. generic worker는 별도 `extract.generic` 큐에서 GenericExtractionAttempt만
처리하며 PaddleOCR 모델이나 발행 자격 증명에 접근하지 않는다.

## 금지 Payload

- OAuth access/refresh token, 쿠키, 비밀번호, API key
- 원문 전체, 첨부 바이너리, 생성 본문 전체
- 관리자 세션/CSRF 값
- 외부 응답의 개인정보 또는 비밀 헤더

이벤트와 로그에는 객체/DB ID, 체크섬, 건수, 상태와 분류된 오류만 포함한다.

## T005 구현 보충: 버전·전송·내부 오케스트레이션

### 엄격한 라우팅과 검증

- 소비자 route와 payload schema의 식별자는 `(event_type, event_version)`이다.
- 등록되지 않은 버전, 선언되지 않은 payload 필드, 잘못된 UUID/SHA-256/enum/version,
  자격 증명·query·fragment가 포함된 URL, 비밀처럼 보이는 값은 fail-closed 처리한다.
- 공통 envelope는 위에 선언한 최상위 필드만 허용한다. immutable 필드는 저장 행과
  정확히 같아야 하고 dispatcher와 consumer가 SHA-256을 다시 계산해 상수 시간 비교한다.
  `attempt`는 immutable identity가 아니라 양의 dispatcher 전달 세대다. 수신 값이 저장된
  현재 dispatch attempts 이하이면 at-least-once 과거 전달로 허용하고, 현재 값보다 큰
  미래 세대만 fail-closed 처리한다.
- `succeeded` 또는 `dead_letter`인 terminal consumer receipt는 늦은 과거/future/malformed
  전달로 강등하거나 event 상태를 덮어쓰지 않는다.
- `event_id`를 읽을 수 있는 잘못된 이벤트는 영속 consumer receipt와 outbox DLQ에
  기록한다. `event_id` 자체가 없거나 UUID가 아니어서 영속 행을 특정할 수 없는 transport
  메시지는 3회로 제한해 broker 재전달한 뒤 오류 로그와 함께 종료한다.
- consumer의 DB/receipt 인프라 오류는 1/2/4초의 독립 bounded retry 뒤에도 DB가
  불능이면 ACK하지 않고 broker에 requeue한다. hard timeout과 worker loss도 requeue하며,
  receipt lease는 hard limit보다 10분 길다. 정산은 만료 시각만으로 거부하지 않고
  token+generation의 현재 소유권으로 fence한다. claim 이후 handler 실행, payload 누락
  종결, domain 실패 종결, 성공 정산 중 어느 지점에서든 `DatabaseError`가 발생하면 같은
  token+generation으로 receipt claim을 해제하고 domain attempt를 소비하지 않는다. claim
  해제 자체도 DB 오류로 실패하면 인프라 재시도가 소유권을 전달받아 살아 있는 40분
  lease를 즉시 재점유한다.
- receipt 결과 정산은 처리 상태와 current raw token+`lease_generation`이 같고 더 높은
  generation reclaim이 없으면 wall-clock `claimed_until`이 막 지났다는 이유만으로 거부하지
  않는다. 반면 mutating HTTP를 시작하는 pre-I/O fence는 실제 I/O 직전 statement-time에
  소유권이 current이고 lease가 아직 유효하거나 같은 transaction에서 안전하게 갱신됐음을
  확인한 경우에만 통과한다. 이전 transaction에서 읽은 시각이나 hash만으로 쓰기를 승인하지
  않는다. reclaim으로 generation이 바뀐 worker는 외부 쓰기와 current projection 변경을 모두
  수행할 수 없다.
- raw receipt `lease_token` UUID와 terminal 예약의 raw `terminal_lease_token` UUID는 DB와
  worker event context 내부에서만 exact
  capability로 사용한다. domain row는 exact 비교를 위한 raw capability를 내부 필드에
  보존하되, AuditEvent·로그·event payload·API는 SHA-256 token hash만 노출하고 raw token은
  절대 직렬화하지 않는다. hash는 감사·노출용이며 권한 판정의 근거가 아니다. terminal
  예약 5필드(`terminal_reserved_at`, raw token, generation, token hash, error code)는
  all-or-none·set-once이고 예약된 receipt 삭제와 성공 전이를 DB가 금지한다.
- `run.requested`, publication/canary/disconnect 이벤트에는 queue 선택용 `topic_code` 또는
  `channel`을 넣지 않는다. dispatcher가 payload의 엔터티 ID로 DB 기준 topic/channel을
  다시 조회하며, 엔터티가 없거나 채널을 확정할 수 없으면 안전하게 DLQ 처리한다.

### 구현 전용 내부 이벤트

아래 이벤트는 상위 aggregate를 분할하거나 기존 coarse-grained 단계를 연결하기 위한
내부 오케스트레이션 계약이다. 모두 `event_version=1`이며 payload 추가·삭제·의미 변경은
새 버전으로만 수행한다.

| 내부 이벤트 | v1 최소 Payload | 용도 |
|---|---|---|
| `run.collection_finalize_requested` | `run_id` | source fan-out 및 각 terminal 결과 뒤 collection 종료 조건 재평가 |
| `run.evidence_requested` | `run_id` | 수집 종료 후 run 단위 evidence fan-out 시작 |
| `evidence.document_route_requested` | `run_id`, `run_source_item_id`, `source_item_id`, `input_asset_id?`, `input_kind`, `document_extraction_id`, `input_checksum` | 상위 DocumentExtraction이 page route와 child run을 결정하도록 요청 |
| `evidence.finalize_requested` | `run_id` | ready 또는 terminal 결과 뒤 run evidence 종료 조건 재평가 |
| `evidence.profile_decided` | `profile_snapshot_id`, `decision_id`, `decision`, `profile_material_hash` | profile 관리자 판정 durable audit 신호 |
| `run.evidence_ready` | `run_id` | evidence fan-out이 성공적으로 종결된 run의 clustering 연결 |
| `editorial.cluster_requested` | `run_id` | terminal RunSourceItem을 cluster에 결합하고 불변 검증 결정 생성 |
| `editorial.generate_requested` | `verification_id`, `run_id`, 정렬 `verification_ids`, `generation_manifest_hash` | 최신 frozen verification set에서 canonical article identity와 초안 생성 |
| `editorial.revalidate_requested` | `article_id`, `article_revision_id`, `editorial_policy_snapshot_id`, `editorial_policy_material_hash`, `verification_manifest_hash`, `input_evidence_manifest_hash`, `excluded_material_manifest_hash` | 새 수동 revision의 current policy/evidence 재검증과 claim/quality 재생성 |
| `run.draft_requested` | `run_id` | 기존 영속 이벤트 소비 호환용 legacy 연결; 신규 생산 금지 |
| `publication.scheduled_run_requested` | `run_id` | validated schedule run의 publication intent/attempt 생성 |
| `publication.preflight_requested` | `target_id`, `target_snapshot_id`, `target_config_hash` | 고정된 target fence로 preflight 시작 |
| `publishing.target_preflight.completed` | `target_id`, `target_snapshot_id`, `target_config_hash`, `result_hash`, `passed` | fence가 유지된 preflight 결과의 durable 완료 신호 |
| `publishing.target_canary.requested` | `canary_run_id`, `target_id` | target canary 실행 |
| `publishing.target_disconnect.requested` | `decision_id`, `target_id` | 승인된 credential revoke 실행 |

`evidence.document_extract_requested`는 child `ExtractionRun` 단위의 공개 내부 계약이고,
`article.draft_requested`는 미리 생성된 `GenerationAttempt` 단위 계약이다. 현재 상위
orchestrator는 이 두 이름을 축약 payload로 재사용하지 않는다. 상위 문서 routing은
`evidence.document_route_requested`를 사용한다. 신규 생성 경로는
`run.evidence_ready` → `editorial.cluster_requested` → `editorial.generate_requested`다.
`held` verification은 독립 breaking 이벤트를 만들지 않고 daily digest frozen set에만 포함한다.
`run.draft_requested` route/schema는
이미 저장된 이벤트의 호환 소비를 위해서만 유지한다.

### T013 검증 cluster 이벤트 불변조건

- 세 이벤트는 모두 DB business transaction 안에서 outbox에 기록하며 run 또는 verification ID를
  dedupe key에 포함한다.
- `editorial.generate_requested`는 primary verification과 정렬된 전체 verification ID 목록,
  각 evidence/rule/result hash·정책·KST 날짜의 canonical `generation_manifest_hash`를 고정한다.
  consumer는 목록과 hash 및 모든 cluster head를 DB에서 다시 확인한다. 하나라도 superseded면
  실패 재시도 대신 `{state: superseded}` 정상 no-op이다.
- 중요 후보 `held`는 독립 breaking work unit이 0건이다. daily digest는 같은 run에서 대표
  verification 하나를 사용하되 `daily_digest_candidate`와 `held` 전체 frozen set을 payload에
  포함한다. 생성 입력은 이 목록의 selected/included publishable evidence만 허용한다.
- 모든 cluster가 `rejected`이거나 과거 run의 늦은 전달이라 해당 run 소유 generation work unit이
  0건이면 `validating`에 남겨두지 않고 editorial 단계에서 성공적으로 `completed` 종결한다.
  이미 생성 이벤트가 만들어진 뒤 전부 superseded된 경우에도 run 전체 frozen event 집합을 확인한
  마지막 no-op worker가 같은 종결을 수행하며, 일부만 superseded이면 다른 생성 단위를 막지 않는다.
- 기사 identity는 청약 기관+공고/정정 identity, breaking cluster canonical key, 또는 run에서
  고정한 KST local date+policy version hash다. 전달·처리 지연 시각은 identity에 사용하지 않는다.
- `run.evidence_ready`, `editorial.cluster_requested` terminal callback은 payload/aggregate의 run ID를,
  generation terminal callback은 payload의 `run_id`와 `verification_id`를 명시적으로 전달한다.
  callback은 active worker event/lease를 재검증하고 immutable audit을 남긴다. generation 단위가
  이미 superseded면 부분 stale은 정상 no-op, run 전체 stale은 `completed`이며, current 단위의
  전달 소진만 run/editorial step을 `failed/manual_required`로 멱등 종결한다.
- 같은 canonical article을 다른 run이 재사용한 경우 run 전체 generation event가 기존 article 또는
  superseded 결과로 해소되고 run-owned article이 0건이면 성공 no-op `completed`로 종결한다. 일부
  단위가 새 run-owned article을 만들면 기존 drafting/approval 상태를 유지한다.
- validated-auto publication은 같은 run의 모든 generation work unit이 run-owned current revision으로
  끝난 마지막 worker만 검토한다. run-owned article이 정확히 1개일 때만 publication event를 만들고,
  reused digest 0개 또는 다중 article 2개 이상은 fail-closed 0건이다. 기존 dedupe event가 있으면
  새 causation으로 enqueue하지 않는다.

### T018 편집 정책·수동 재검증 이벤트 불변조건

- `editorial.generate_requested@1`의 첫 current 처리 transaction은 배포 release JSON에서 주제의
  current editorial policy를 해석하고 append-only `EditorialPolicySnapshot`을 생성하거나 exact
  material만 재사용한다. editorial policy에는 approval/head가 없다. 같은 key/version의 release,
  config 또는 implementation bytes가 다르면 generator 입력을 읽기 전에 영구 실패한다.
- generated/manual `ArticleRevision`은 canonical `bodyBlocks`, policy snapshot ID/material hash,
  정렬 multi-verification snapshot, 실제 input-evidence snapshot, excluded/duplicate/conflict snapshot과
  각각의 manifest hash를 고정한다. event hint와 DB material이 다르면 fail-closed다.
- `editorial.revalidate_requested@1`의 exact payload는 `article_id`, `article_revision_id`,
  `editorial_policy_snapshot_id`, `editorial_policy_material_hash`, `verification_manifest_hash`,
  `input_evidence_manifest_hash`, `excluded_material_manifest_hash`이며 추가 필드를 허용하지 않는다.
  base-revision CAS로 pending revision을 만드는 transaction에서 같은 event를 한 번만 기록한다.
- consumer는 current article/revision, exact policy snapshot, 세 manifest를 잠근 뒤 current release
  policy와 사용 evidence의 current publish eligibility를 다시 계산한다. policy 교체, freshness 만료,
  권리 철회, source lineage 변경은 과거 snapshot을 수정하지 않고 revision을 blocked/manual-required로
  종결한다.
- claim type은 정확히 `fact/company_claim/interpretation/outlook`이다. title, summary, bodyBlocks,
  caption의 모든 publishable assertion을 atomic Claim으로 재구성하며 이전 revision의 Claim,
  ClaimEvidence, QualityCheck, Approval 또는 PublicationIntent를 복사하지 않는다.
- review-ready는 `all_publishable_claims_grounded`, `high_risk_verification_satisfied`,
  `claim_independence_satisfied`, `source_freshness_satisfied`,
  `evidence_publish_eligibility_current`, `quotation_limits_satisfied`,
  `claim_types_separated_and_attributed`, `duplicate_or_conflict_resolved`,
  `korean_readability_and_repetition`, `no_exaggeration_or_false_experience`의 exact 10개
  content/evidence gate가 모두 passed이고 visual이 있으면 `visual_rights_and_alt_text`도 passed일
  때만 가능하다. terminal 성공은 claim graph,
  `quality_gate_manifest_hash`, `quality_report_hash`, article/revision state와 audit를 한 transaction에
  기록한다. 전달 소진은 current pending revision만 failed/manual-required로 닫고 이전 revision을
  current로 되돌리지 않는다.

### 발행·추출 재전달 규칙 보충

- T020 `PublicationDispatchResult.attemptNo=1`은 immutable acceptance 값이다. 이벤트 envelope
  `attempt`는 dispatcher delivery attempt, `execution_attempt_no`는 business write 1~5,
  `execution_generation`은 domain claim fence, consumer receipt `lease_generation`은 transport
  ownership, `reconcile_attempt_no`는 read-only reconcile 1~5다. 서로 대입하거나 API에서
  같은 `attemptNo`로 직렬화하지 않는다.
- 신규 `publication.requested@2` exact payload는 `publication_attempt_id`,
  `execution_attempt_no`다. 같은 event는 pre-write receipt reclaim 뒤 같은 business 번호를
  유지한 채 새 consumer lease와 domain `execution_generation`으로 다시 claim될 수 있다. 각
  execution observation은 source event, consumer name, receipt lease generation/token hash와
  write marker를 동결하며 generation만 고유하다. v1은 이미 exact source event에 결속된 행의
  replay이거나, counter·observation·terminal lineage가 모두 비어 있는 virgin generation 1임이
  증명될 때만 한 번 결속한다. 그 밖의 v1은 현재 counter를 추측하지 않고
  `legacy-unverifiable-v1` 감사 sentinel을 남기는 no-op quarantine이며 현재 projection을
  변경하지 않는다.
- `PublicationAttempt=running`인 event에 write marker/start가 있으면 재전달은 외부 write를
  반복하지 않는다. 같은 트랜잭션에서 `unknown_outcome`으로 전환하고
  `publication.reconcile_requested`를 영속 생성한다. marker 전 receipt reclaim은 이전
  observation을 fenced/no-result로 닫고 같은 `execution_attempt_no`의 새 domain generation만
  claim한다.
- reconcile의 `retryable_failed`와 반복 `unknown_outcome`은 새 dedupe key의 후속
  reconcile 이벤트로 최대 5회 이어지며, 한도를 넘으면 `manual_required`가 된다.
  `publication.reconcile_requested@2`의 exact payload는 `publication_attempt_id`,
  `reconcile_attempt_no`이고, 이 번호는 execute/admin `attempt_no`와 분리된 영속
  counter다. 각 1~5 세대는 `delivery_identity_version=publication-reconcile-v1`, exact source
  event와 consumer receipt lease envelope를 가진 append-only `PublicationReconcileGeneration`
  행에 결합된다. state는 `queued|running|completed|delivery_failed`다. 완료/전달 실패한 동일
  이벤트는 adapter를 다시 호출하지 않고, queued/running 동일 이벤트는 같은 세대를 재개한다.
  v1은 이미 exact source event에 결속된 세대의 replay이거나 아무 reconcile lineage도 없는
  virgin generation 1일 때만 한 번 결합한다. 나머지는 `legacy-unverifiable-v1` no-op
  quarantine이며 전달 순서나 현재 counter로 generation을 추측하지 않는다.
- running reconcile의 receipt lease가 만료된 뒤 같은 source event를 current로 클레임한
  더 높은 `lease_generation`이 도착하면 새 reconcile 세대를 만들지 않고 같은
  `PublicationReconcileGeneration` row의 current projection만 재결속하여 `running`을 유지한다.
  각 claim/reclaim은 `(reconcile_generation, consumer_lease_generation)`이 고유하고 identity/raw
  capability가 불변인 `PublicationReconcileDeliveryObservation`을 추가하고 이전
  active observation을 `superseded`로 닫는다. 이전 worker의 늦은 result는 current projection을
  바꾸지 않고 그 exact delivery observation FK 및 같은 generation/capability denormalization을
  가진 factual `PublicationLateExecutionResult`로만 append한다. `delivery_failed`는 exact
  terminal reservation/DLQ가 증명된 경우에만 허용한다.
- 새 reconcile 세대와 event를 할당하는 transaction은 attempt와 publication projection도
  함께 `reconciling`으로 고정한다. 따라서 execute 재전달이나 관리자 retry가 열린 세대와
  경합해 외부 write 경로를 다시 열 수 없다.
- reconcile 결과 저장은 source event와 generation을 함께 검증한다. 현재 세대보다 늦게
  도착한 과거 결과는 publication/attempt 결과를 덮어쓰지 못한다. 결과와 세대 완료,
  필요 시 다음 v2 event와 다음 세대 시작은 각각 하나의 DB transaction에서 원자적으로
  기록하며 6번째 세대는 생성하지 않는다.
- v1/v2 reconcile 전달이 영구 실패하거나 route 예산을 소진하면 terminal callback이 그
  source event의 세대를 `delivery_failed`로 종결하고 attempt/publication을
  `manual_required`로 종결한다. callback 전 receipt에
  `terminal_reserved_at`, internal raw `terminal_lease_token`, `terminal_lease_generation`,
  `terminal_lease_token_hash`, `terminal_error_code`를 all-or-none·set-once로 예약하고 receipt는 callback 동안
  `processing`을 유지한다. callback이 exact reservation을 재검증해 domain projection과
  audit를 남긴 뒤에만 receipt/event를 `dead_letter`로 전이한다. 예약부터
  DLQ 전이까지 한 transaction이며 callback 실패는 전체를 rollback한다.
  후속 생성 및 upgrade backfill도 source outbox 또는 해당 consumer receipt의
  `dead_letter`를 감지해 같은 종결을 수행하며, 이후 replay는 no-op이다.
- mutating HTTP의 timeout, 연결 종료와 적용 여부를 증명하지 못하는 408/429/5xx는
  `unknown_outcome`이다. 같은 create event나 다음 execution event로 write를 반복하지 않고
  reconcile로만 이동한다. 명시적인 pre-write failure처럼 원격 미적용이 증명된 경우만 새
  `publication.requested@2`와 다음 execution 세대를 같은 transaction에서 만들 수 있다.
- create preflight는 bounded read가 성공하고 응답 schema·pagination·target/blog가 정확하며
  exact marker/lookup match가 0건임을 증명한 경우에만 mutating create를 허용한다. timeout,
  408/429/5xx, JSON/schema 오류, 불완전 pagination, target/blog 불일치와 복수 match는 write
  0건으로 retry/manual 처리한다.
- reconcile과 mutating 2xx의 성공은 2xx 응답 자체 또는 그 직후의 bounded authenticated exact GET이
  action/content/state proof를 모두 제공해야 한다. marker 내 expected
  content hash를 믿지 않고, provider가 반환한 actual raw body에서 marker를 분리한 뒤
  versioned canonical remote-body material을 재계산해 rendered content와 비교한다.
  create/update/mark는 exact target/blog, remote identity, title, versioned
  lookup/command/content marker, canonical body와 기대 공개 상태가 일치해야 한다. unpublish는
  exact target/blog·remote identity와 WordPress `draft|trash` 또는 Blogger `draft` 같은 실제
  비공개 상태가 확인돼야 하며 adapter가 `withdrawn`을 합성하지 않는다. 증명 필드가 없거나
  다르면 `unknown_outcome`/manual reconcile이고 성공 projection은 없다. 0건은 create를
  반복하지 않고 bounded backoff로 최대 5회 read-only
  reconcile한 뒤 `manual_required`, 복수건은 즉시 `manual_required`다. Blogger list는
  `nextPageToken`을 따르되 live/draft/scheduled 합산 최대 15 page·750 item·wall 30초
  (`maxResults=50/page`)를 넘으면 fail-closed한다.
- preflight/read가 mutating HTTP 전에 실패하면 `external_write_authorized=false`로 정산하고
  write marker를 만들지 않는다. 408/429/5xx·timeout의 retryable 분류는
  `Retry-After`의 delay-seconds 또는 IMF-fixdate를 모두 UTC 기준 bounded seconds로 보존해
  우선 사용하며, 실제 mutating call을 시작한 뒤에는
  `external_write_authorized=true`로 정산하고 불명확한 결과를 direct retry로 보내지 않는다.
- execution/reconcile terminal callback은 원 consumed source event, execution/reconcile 세대와
  receipt ownership을 exact하게 결속한다. 과거 callback과 늦은 result는 현재 state를 바꾸지
  않고 별도 factual late-result observation만 append한다. terminal reservation, projection,
  AuditEvent와 dependent wake-up은 한 transaction이며 exact replay는 no-op이다.
- execution start/result AuditEvent identity는 business attempt number만이 아니라 exact source event,
  `execution_generation`, consumer name·`lease_generation`·token hash, write authorization/marker를
  결속한다. reconcile audit도 source event·reconcile generation·delivery observation·consumer
  name·lease generation·token hash와 terminal reservation hash/error를 결속한다. raw token은
  AuditEvent에 들어가지 않는다. 유효 remote URL은 `http(s)`이고 최대 1000자만 저장하며,
  invalid scheme/초과 URL은 raw를 버리고 SHA-256만 감사한다. T020이 만든
  `PublicationDispatch` ledger의 frozen target/Publication/Attempt cohort는 T021 retry·reclaim·reconcile로
  추가되거나 교체되지 않고, 각 logical attempt의 counter/generation만 전진한다.
- generic attempt 생성과 `evidence.other_extract_requested` 생성은 같은 트랜잭션이다.
  일시 추출 오류는 `queued`로 되돌리고 finalizer를 깨우지 않는다. 영구 오류 또는
  consumer 재시도 소진 시에만 `failed`와 단 하나의 terminal finalizer 이벤트를 같은
  트랜잭션에 기록한다.
- 문서 입력은 `document-input-v1` schema, `run_source_item_id`, `input_kind`,
  `input_checksum`의 안정 SHA-256인 `input_fingerprint`로 식별한다. 동일 입력의
  활성 canonical DocumentExtraction과 route outbox는 각각 하나만 존재하며, 이미 성공한
  legacy HWP 변환 결과의 PDF 승격 경로도 같은 identity를 사용한다. upgrade 시 과거 중복
  행은 삭제하거나 합성 hash를 부여하지 않고 audit용 null fingerprint로 보존하며,
  `document_complete+succeeded`, `succeeded`, `low_confidence`, 실패/종료 순으로 canonical
  행을 선택한다. 성공/저신뢰 legacy HWP 행에 연결된 EvidenceAsset이 없으면 checksum과
  객체 위치가 유효한 단 하나의 파생 자산만 복구한다. 없거나 여러 개이거나 불변 객체
  material이 불완전하면 attempt를 `failed`로 바꾸고 terminal finalizer를 기록한다.
- target preflight는 짧은 DB fence snapshot, transaction 밖 외부 호출, fence를 다시
  확인하는 결과·audit·완료 이벤트 transaction의 세 단계로 수행한다.
- run evidence parent는 외부 download/storage I/O 밖의 짧은 row-lock transaction에서
  `RunStep.fanout_completed_at`, counters, `evidence.finalize_requested`를 함께 기록한다.
  finalizer는 marker 전에는 run을 전진시키지 않으므로 0 child, 빠른 child, 중간 실패도
  fan-out 완료 wake-up 뒤에만 재평가된다.
- marker 도입 migration은 `extract/running`, 시작 시각 있음, marker 없음,
  `CollectionRun=extracting`인 행만 검사한다. 기존 counters에 evidence와
  extractionFailures가 모두 있으면 과거 parent 완료 증거로 인정해 marker와 stable
  finalizer를 기록하고, 증거가 없으면 marker를 추정하지 않고 stable re-fanout event를
  기록한다. 이미 전진했거나 terminal인 run은 변경하지 않는다. 이 복구는 migration의
  historical model과 대상 DB alias만 사용하고, immutable outbox material/hash 생성도
  migration 안에 고정해 미래 runtime 코드 변경에 영향받지 않는다.
- parent evidence 처리에서 `ExtractorError`, `httpx.HTTPError`, `OSError`만 분류된
  partial failure로 집계한다. `DatabaseError`, soft time limit과 programming exception은
  marker를 기록하지 않고 다시 던져 route receipt 재시도 대상이 된다. parent route의
  세 번 전달이 모두 소진되면 terminal callback이 아직 extracting인 run/step만
  `failed` 또는 stop 요청 시 둘 다 `stopped`로 일관되게 종결한다.
- 영구 오류 또는 retry 소진 terminal handler는 원 consumed envelope의 event context
  안에서 실행한다. 따라서 terminal finalizer outbox는 원 correlation ID와
  `causation_id=current event_id`를 유지하며 receipt terminal 전이와 원자적이다.
- routed leaf task는 Celery `self.retry`/`autoretry` 예산을 갖지 않는다. receipt route가
  유일한 전달 재시도 권한이며 `run.requested=4`, `run.evidence_requested=3`,
  `publication.preflight_requested=4`, target canary=2, 그 밖의 route=5로 모두 5회
  이하이다.

## T012 구현 보충: 출처 단위 수집과 정책 실패

- `run.requested`는 활성 registry membership마다 SourceCollectionAttempt와
  `source.collect_requested`를 한 트랜잭션에서 만든다. 이벤트 payload는 attempt ID만 전달하며
  worker는 run의 TopicPolicy/registry hash, source frozen config, access/rights policy hash,
  adapter 구현 hash를 DB에서 다시 대조한다.
- 각 전달은 outbox consumer receipt의 실제 attempt 번호로 append-only
  SourceCollectionObservation을 남긴다. retryable 오류는
  `retry_scheduled`와 `retry_at`을 기록한 뒤 원 오류를 다시 던져 outbox receipt route의 동일
  최대 5회 예산만 사용한다. 영구 오류와 전달 소진 callback은 attempt를 terminal `failed`로
  만든다. worker 종료로 결과 정산이 실행되지 않은 앞선 receipt 번호는 다음 전달 시작 또는
  소진 callback에서 `source_delivery_interrupted` infrastructure 관측으로 보충한다.
- source 하나의 실패는 다른 source attempt를 취소하지 않는다. dispatch 직후와 각 source
  terminal 결과 뒤 서로 다른 dedupe cause의 finalizer를 깨운다. 아직 non-terminal attempt가
  있으면 no-op이며 모두 끝난 시점에만 집계한다.
- 성공 source가 0개이면 run/collect step을 `failed`로 끝내고
  `run.evidence_requested`를 만들지 않는다. 하나 이상 성공하면 실패·freshness 제외 수를
  counters/error summary에 보존하고 `extracting`으로 전진한다.
- source check는 같은 snapshot/config/access-policy/rights-policy hash의 최근 `passed`
  taxonomy 결과만 registry 승인 근거로 인정한다. 새 draft나 정책 변경은 기존 health를
  재사용할 수 없다.

## T015 구현 보충: legacy HWP sidecar identity와 실패 이벤트

- legacy HWP도 기존 `evidence.other_extract_requested@1`과 `extract.generic` queue를 그대로
  사용한다. 별도 HWP Celery queue/event를 만들지 않으며 event payload에는 converter bytes,
  filesystem path, nonce 또는 report를 넣지 않는다.
- consumer가 DB의 GenericExtractionAttempt와 승인 profile snapshot을 다시 읽은 뒤 attempt ID를
  no-network sidecar UDS request identity로 사용한다. T015 호환 `generation=1`과 random nonce는
  sidecar request/response replay 혼동을 막는 echo material일 뿐 outbox delivery generation이나
  DB fence가 아니다. persisted generation/lease/stale-result 차단은 T016에서 추가한다.
- UDS 응답의 attempt/generation/nonce, manifest, policy, input/output checksum·size, qpdf page
  count가 모두 일치한 경우에만 GenericExtractionAttempt, converted EvidenceAsset과
  `evidence.other_ready`를 기존 트랜잭션으로 저장한다. report의 verified page count 전체가
  후속 DocumentExtraction identity와 expected page indices가 된다. canonical exact report 전체와
  report hash를 EvidenceAsset structured data에 보존하고 object upload 직전 실제 PDF checksum/size를
  report/locator와 다시 대조한다. recovery와 기존 DocumentExtraction 재사용도 같은 결속과 object/page
  identity 전체를 재검증한다.
- socket/daemon 단절만 기존 receipt route의 retryable infrastructure 오류다. unsupported,
  warning/missing-font/fallback, wrapper exit 20/21/22, protocol/report/PDF tamper는 permanent
  failure로 terminal callback에 전달한다. 실패 attempt는 EvidenceAsset,
  `evidence.other_ready`, DocumentExtraction을 0건 만들고 evidence finalizer가 run을
  `manual_required` recovery로 투영한다.
- profile 선택 전에 실패해 attempt가 없더라도 required `.hwp` attachment의 typed failure count/code를
  run counters에 보존하고 finalizer가 `legacy_hwp_required_failure/manual_required`로 끝낸다. 다른 optional
  attachment의 부분 성공 의미는 바꾸지 않는다.
- `legacy-hwp-v1@1.1.0`은 immutable `golden_corpus_approved=false` draft다. T032 acceptance artifact와
  새 1.2.0 golden profile이 승인된 뒤에도 1.1.0은 immutable superseded draft로 남고 운영 event
  producer가 선택하면 안 된다.

## English — T005 Versioned Internal Event Addendum

### Strict routing and validation

- Route and payload-schema identity is the tuple `(event_type, event_version)`.
- Unsupported versions, undeclared fields, malformed UUID/SHA-256/enum/version values,
  credentialed or query-bearing URLs, and secret-looking values fail closed.
- The transport envelope permits only the declared top-level fields. Immutable fields
  must exactly match the persisted event, and dispatcher and consumer recompute and
  constant-time compare the complete immutable-material SHA-256. `attempt` is a positive
  dispatcher delivery generation, not immutable identity: a received generation at or
  below the persisted dispatch-attempt count is valid at-least-once delivery, while a
  future generation fails closed.
- A terminal `succeeded` or `dead_letter` consumer receipt cannot be downgraded, nor can
  its event be overwritten, by a late stale, future, or malformed delivery.
- A malformed message with an extractable `event_id` is recorded in the durable receipt
  and outbox DLQ. If no valid `event_id` exists, broker retries are bounded to three
  deliveries because no durable row can be addressed.
- Consumer database/receipt infrastructure failures use independent bounded 1/2/4-second
  retries and broker requeue instead of ACK when the database remains unavailable. Hard
  timeout and worker loss requeue as well. The receipt lease exceeds the hard limit by
  ten minutes, and settlement is fenced by current token+generation ownership rather
  than lease-clock expiry alone.
- Settlement by the still-current token and generation is not rejected only because the
  clock passed `claimed_until`. Starting a mutating HTTP call is stricter: the pre-write
  fence must prove an unexpired current lease or renew it atomically. A reclaimed worker
  appends only a factual late-result observation and cannot write or mutate the current
  projection.
- The raw receipt lease-token UUID is an internal database/event-context capability only.
  Domain claim rows retain the raw capability solely for exact constant-time ownership
  checks, while events, audit, logs, and APIs expose only SHA-256 token hashes.
- Queue selection metadata is not added to domain payloads. The dispatcher resolves the
  topic or publication channel from the referenced database entity and dead-letters an
  event when that identity cannot be resolved safely.

### Implementation-only orchestration events

Entries are version 1 unless the event key shows another version. Any field removal or
semantic change requires a new event version.

| Event key | Exact payload |
|---|---|
| `run.collection_finalize_requested@1` | `run_id` |
| `run.evidence_requested@1` | `run_id` |
| `evidence.document_route_requested@1` | `run_id`, `run_source_item_id`, `source_item_id`, optional `input_asset_id`, `input_kind`, `document_extraction_id`, `input_checksum` |
| `evidence.finalize_requested@1` | `run_id` |
| `evidence.profile_decided@1` | `profile_snapshot_id`, `decision_id`, `decision`, `profile_material_hash` |
| `run.evidence_ready@1` | `run_id` |
| `editorial.cluster_requested@1` | `run_id` |
| `editorial.generate_requested@1` | `verification_id`, `run_id`, sorted `verification_ids`, `generation_manifest_hash` |
| `editorial.revalidate_requested@1` | `article_id`, `article_revision_id`, `editorial_policy_snapshot_id`, `editorial_policy_material_hash`, `verification_manifest_hash`, `input_evidence_manifest_hash`, `excluded_material_manifest_hash` |
| `run.draft_requested@1` | `run_id` |
| `publication.scheduled_run_requested@1` | `run_id` |
| `publication.preflight_requested@1` | `target_id`, `target_snapshot_id`, `target_config_hash` |
| `publishing.target_preflight.completed@1` | `target_id`, `target_snapshot_id`, `target_config_hash`, `result_hash`, `passed` |
| `publishing.target_canary.requested@1` | `canary_run_id`, `target_id` |
| `publishing.target_disconnect.requested@1` | `decision_id`, `target_id` |
| `publication.requested@1` | `publication_attempt_id`; exact bound replay or proven virgin generation one only |
| `publication.requested@2` | `publication_attempt_id`, `execution_attempt_no` |
| `publication.reconcile_requested@1` | `publication_attempt_id`; exact bound replay or proven virgin generation one only |
| `publication.reconcile_requested@2` | `publication_attempt_id`, `reconcile_attempt_no` |

The implementation does not overload `evidence.document_extract_requested` with a
parent-document payload, and it does not overload `article.draft_requested` with a
run payload. The former remains the child `ExtractionRun` contract; the latter remains
the pre-created `GenerationAttempt` contract.

### T010 source-change lineage

`source.item_changed@1` validates the succeeded attempt, immutable RunSourceItem chain,
enabled run-registry membership, and exact change-kind/status mapping. New, corrected,
and restored observations enter extraction. Retracted, unavailable, and restored
observations also walk the full prior lineage to converge article correction,
withdrawal, unavailability, or restoration impact. Unchanged observations emit no event.

### Redelivery state machine

1. T020 response `attemptNo=1` is the immutable acceptance value. Envelope `attempt` is
   the dispatcher delivery attempt; `execution_attempt_no` is business write generation
   1..5; `execution_generation` is the domain claim fence; receipt `lease_generation` is
   transport ownership; `reconcile_attempt_no` is read-only reconciliation generation
   1..5. They are not interchangeable.
2. New execution uses exact `publication.requested@2` payload
   `publication_attempt_id` plus `execution_attempt_no`. Version 1 may only replay an
   existing exact source-event binding or bind once to a proven virgin generation one.
   Every other v1 delivery records a `legacy-unverifiable-v1` audit sentinel and is a
   no-op quarantine; it never guesses from the current counter.
3. Redelivery of a `running` publication attempt with a write marker/start never repeats
   the external write. It atomically records `unknown_outcome` and enqueues reconciliation.
   A pre-write receipt reclaim instead fences the prior observation as no-result and
   claims a new domain generation with the same business attempt number.
4. Retryable or repeatedly unknown reconciliation creates a new durable follow-up event
   and becomes `manual_required` after five bounded attempts. Version 2 carries the exact
   payload `publication_attempt_id` plus `reconcile_attempt_no`; this persisted counter is
   independent of the execution and delivery counters. Version 1 may only replay an
   exact existing binding or bind a proven virgin generation one; every other delivery is
   a `legacy-unverifiable-v1` no-op quarantine. Expired
   `running` delivery reclaimed by a higher current receipt lease rebinds the same
   reconciliation generation and remains `running`; it never becomes `delivery_failed`
   and never consumes another reconciliation attempt. A result from the prior capability
   appends through the exact `PublicationReconcileDeliveryObservation` foreign key to the
   reconcile-parented factual late-result ledger only. Each claim/reclaim has one identity-frozen
   observation keyed by reconciliation generation plus receipt lease generation; only its state
   and finish time may advance monotonically.
5. A create preflight may mutate only after a bounded, schema-complete read proves exact
   target/blog pagination and zero marker/lookup matches. Timeout, 408/429/5xx, malformed
   JSON/schema, incomplete pagination, target mismatch, or multiple matches authorize no
   write. Ambiguous mutating HTTP outcomes enter read-only reconciliation rather than
   repeating create. A mutation 2xx succeeds only when either that response or an immediate
   bounded authenticated exact GET, and every reconciliation success, proves exact target/blog,
   remote identity, action-aware state, title, and a canonical hash recomputed from the actual
   provider-returned body after separating its marker; the marker's embedded expected content
   hash is not proof by itself.
   Zero matches receive at most five bounded reads, multiple matches manualize immediately,
   and Blogger follows `nextPageToken` for at most 15 pages, 750 items and 30 wall-clock
   seconds across live/draft/scheduled (`maxResults=50` per page).
6. A read/preflight failure before mutating HTTP settles with
   `external_write_authorized=false`, creates no write marker, and preserves both
   delay-seconds and IMF-fixdate forms of Retry-After as bounded seconds.
   After mutating HTTP authorization it settles with `external_write_authorized=true`,
   so ambiguous application is reconciled rather than retried directly.
7. The pre-I/O authorization evaluates current ownership and lease validity at statement
   time immediately before external I/O. Settlement accepts the same raw token/generation
   after clock expiry when no higher generation reclaimed it. Raw tokens are authoritative
   internally; hashes are audit-only. Execution/reconcile audit identity binds source event,
   domain/reconcile generation, consumer name, receipt lease generation, token hash,
   write authorization/marker, delivery observation, and terminal reservation hash/error.
   Valid remote URLs are bounded to 1000-character HTTP(S); invalid values are discarded
   and only their SHA-256 is audited. Retry/reclaim/reconcile never adds to or
   replaces the immutable PublicationDispatch target/publication/attempt cohort.
8. Generic attempt creation and its request event are atomic. Retryable extraction
   failures return to `queued`; only permanent failure or exhausted consumer retries
   records `failed` and one terminal finalizer wake-up.
9. Target preflight is split into a short database fence snapshot, external I/O outside
   a database transaction, and a fenced result/audit/completion-event transaction.
10. The evidence parent atomically persists `RunStep.fanout_completed_at`, counters, and a
   finalizer wake-up after all child attempt/outbox fan-out. Finalizers cannot advance the
   run before that marker, including zero-child, fast-child, and partial-failure paths.
11. Permanent/exhausted terminal handlers run inside the original consumed event context,
   preserving correlation and setting the terminal wake-up causation to the consumed
   event ID in the same receipt-terminal transaction.

### Third re-review durability addendum

1. Any post-claim `DatabaseError` from handler execution, missing-payload termination,
   domain-failure settlement, or success settlement releases the matching receipt
   token/generation and restores the consumed domain-attempt count. If that release
   cannot reach the database, the infrastructure retry carries ownership and may
   immediately reclaim the still-live lease.
2. Every reconciliation generation from one through five uses
   `delivery_identity_version=publication-reconcile-v1`, exact source/consumer receipt
   lease identity, and state `queued|running|completed|delivery_failed`. It is append-only
   and bound one-to-one to its source outbox event. Completed/delivery-failed replay is a
   no-op. A higher current receipt lease for the same source event rebinds the existing
   running generation; it does not create a new row or mark delivery failed. A legacy v1
   event is assigned exactly once only when its contiguous historical identity is proven.
3. One requested event may have multiple fenced observations after pre-write receipt
   reclaim: `execution_attempt_no` remains unchanged, while receipt lease generation and
   domain `execution_generation` advance. Only `(publication_attempt_id,
   execution_generation)` is unique; the business attempt number is indexed but not
   unique. Result persistence fences on current source event and generation. A late older result
   cannot overwrite current publication state. Result completion and any next v2
   generation/event allocation are atomic; generation six is forbidden. Allocating a
   generation also projects attempt and publication to reconciling in that transaction,
   so execute redelivery or admin retry cannot reopen the external-write path. Terminal
   v1/v2 delivery callbacks first set the receipt's all-or-none, set-once
   `terminal_reserved_at`, internal raw `terminal_lease_token`, `terminal_lease_generation`,
   `terminal_lease_token_hash`, and `terminal_error_code` while it remains processing.
   A reserved receipt cannot be deleted or become succeeded. The callback verifies that exact
   reservation, projects the bound generation to `delivery_failed`, and manualizes the
   attempt before the receipt/event become dead-letter in the same transaction. Failure
   rolls back the reservation, projection, audit, and DLQ transition; replay is a no-op.
4. `DocumentExtraction.input_fingerprint` is the stable SHA-256 of schema
   `document-input-v1`, run-source-item identity, input kind, and input checksum. It
   provides one active canonical document and one route event for normal and legacy-HWP
   promotion paths. Upgrade canonicalization prioritizes complete success, success,
   low-confidence, and then failed/finished rows; historical duplicates remain
   audit-only with a null fingerprint instead of receiving synthetic hashes. A terminal
   legacy-HWP success recovers exactly one valid derived asset when the link is missing,
   and fails closed with a terminal finalizer for missing, ambiguous, or incomplete
   immutable material.
5. The marker upgrade only inspects in-flight extracting step-one rows. Existing evidence
   and extraction-failure counters prove parent completion and cause marker plus finalizer
   recovery; absence of proof causes stable re-fan-out without synthesizing the marker.
   The migration uses historical models, its target database alias, and migration-local
   immutable outbox material/hash construction rather than current runtime helpers.
6. Only extractor, HTTP-client, and operating-system I/O errors become partial evidence
   failures. Database, soft-time-limit, and programming errors escape without a marker.
   Exhausting the parent route terminates only a still-extracting run/step, projecting
   both to stopped when stop was requested and both to their failure states otherwise.
7. Routed leaf tasks have no Celery retry loop. Receipt routes are the sole delivery
   retry authority, with budgets of four for collection run, three for evidence parent,
   four for preflight, two for canary, and five for all remaining routes.

### T012 source-level collection state machine

`run.requested@1` atomically creates one SourceCollectionAttempt and
`source.collect_requested@1` event per enabled registry membership. The payload carries
only the attempt ID; the consumer reloads and verifies the pinned TopicPolicy, registry,
source snapshot, access/rights-policy hashes, adapter implementation, and time window.

Every delivery appends a SourceCollectionObservation keyed by the actual outbox consumer
receipt attempt. Retryable access failures persist
`retry_scheduled` plus `retry_at` and re-enter only the receipt route's five-delivery
budget. Permanent failures and exhausted-delivery callbacks terminalize that source
without cancelling sibling sources. A later delivery or exhaustion callback backfills any
missing prior receipt generation as an interrupted infrastructure observation. Finalizer
wake-ups are safe no-ops until every source
attempt is terminal. Zero successes fail collection without evidence fan-out; partial
success advances to extraction with durable failure and freshness-exclusion summaries.

### T013 verified-clustering chain

Successful evidence finalization persists `run.evidence_ready@1`. Its consumer persists
`editorial.cluster_requested@1`, whose worker upserts canonical clusters and append-only
verifications before emitting `editorial.generate_requested@1`. The final payload binds
the primary verification, originating run, sorted frozen verification IDs, and their
canonical generation-manifest hash. Consumers recompute the exact list/hash and verify
every cluster head. A superseded set completes as a normal no-op. Held breaking candidates
emit no standalone breaking unit but remain in the daily-digest frozen set. Generation
uses only selected/included publishable evidence. Routed terminal callbacks receive the
payload run ID explicitly for multi-argument generation events and fail the run with
manual-required recovery after delivery exhaustion only while that generation unit remains
current. They revalidate the active worker event/lease and persist immutable terminal audit;
superseded units are normal no-ops and an all-superseded run completes. When every current
unit reuses an existing canonical article and the run owns no article, the run also completes
as a successful no-op. Auto-publication is emitted only by
the last completed work unit when the run owns exactly one current-revision article; zero
reused or multiple articles fail closed. The legacy
`run.draft_requested@1` route remains consumption-only for already persisted events.
Runs with no run-owned generation unit because every cluster is rejected or a delivery is
causally stale complete successfully at the editorial stage instead of remaining in
`validating`. If events already exist, completion requires every frozen generation event
for the run to be superseded; a partially superseded run leaves its remaining current
work units eligible.

### T018 editorial policy binding and manual revalidation

The first current handling of `editorial.generate_requested@1` resolves the topic's current policy
from the deployed release JSON and persists or reuses an exact append-only
`EditorialPolicySnapshot`. There is no approval record and no mutable policy head. Reusing a
key/version with changed release, config, implementation, or material bytes is a permanent failure
before generator input is read.

Every generated or manual revision freezes canonical bodyBlocks, the policy snapshot, sorted
multi-verification material, used publish-eligible evidence, and excluded/duplicate/conflicting
material under separate hashes. `editorial.revalidate_requested@1` allows exactly the seven fields
declared in the table. The pending manual revision and event are created atomically after base
revision CAS.

The consumer locks and verifies the article, revision, policy, and all three manifests, then checks
the current release policy and current evidence publish eligibility. It rebuilds atomic claims of
exactly `fact`, `company_claim`, `interpretation`, or `outlook`; prior claims, checks, approvals, and
intents are never copied. Review-ready requires these ten exact gates:
`all_publishable_claims_grounded`, `high_risk_verification_satisfied`,
`claim_independence_satisfied`, `source_freshness_satisfied`,
`evidence_publish_eligibility_current`, `quotation_limits_satisfied`,
`claim_types_separated_and_attributed`, `duplicate_or_conflict_resolved`,
`korean_readability_and_repetition`, and `no_exaggeration_or_false_experience`. Revisions containing
visuals also require `visual_rights_and_alt_text`. Policy drift, freshness expiry, rights revocation,
or source-lineage drift blocks the new revision without mutating the old one.

### T015 legacy-HWP sidecar event boundary

Legacy HWP keeps `evidence.other_extract_requested@1` and the existing generic extraction
queue. No converter bytes, local paths, nonce, report, new queue, or new domain event is added
to the broker contract. The consumer reloads the GenericExtractionAttempt and approved profile,
then uses the attempt UUID plus a compatibility `generation=1` and random nonce only inside the
bounded UDS request. That generation is not outbox delivery identity or database fencing; T016
owns persisted generation, lease, and stale-result protection.

Only an exact matching response may persist the attempt result, converted EvidenceAsset, and
existing `evidence.other_ready` event. Its verified qpdf page count becomes the complete page
range of the follow-up DocumentExtraction. A UDS daemon disconnect is the only retryable
infrastructure outcome. Unsupported input, warning/missing-font/fallback, exact wrapper exits
20/21/22, or protocol/report/PDF tamper is permanent: the terminal callback creates zero
EvidenceAsset, ready event, or DocumentExtraction and the evidence finalizer projects manual
recovery. Exact canonical report/locator/object/page bindings are rechecked before upload and
during recovery. A pre-attempt required-HWP failure is retained as a typed run counter marker.
Version 1.1.0 remains an immutable inactive superseded draft; T032 creates the acceptance
artifact and approves golden 1.2.0 without adding a retirement transition for 1.1.0.

## 한국어 — T022 불변 발행 자산 전달 이벤트

신규 미디어 쓰기·조정·삭제는 아래 여섯 v2 이벤트만 사용한다. payload는 표에 적힌 필드만
허용하고 `operation_generation`은 1~5이다. raw receipt token, token hash, write marker,
로컬 경로, 원본 bytes는 public event payload에 넣지 않는다.

| 이벤트 | exact payload |
|---|---|
| `media.upload_requested@2` | `remote_media_id`, `publication_attempt_id`, `publication_intent_id`, `operation_generation`, `target_snapshot_id`, `target_config_hash` |
| `media.reconcile_requested@2` | `remote_media_id`, `publication_attempt_id`, `publication_intent_id`, `operation_generation` |
| `delivery.prepare_requested@2` | `public_delivery_asset_id`, `publication_attempt_id`, `publication_intent_id`, `operation_generation` |
| `delivery.reconcile_requested@2` | `public_delivery_asset_id`, `publication_attempt_id`, `publication_intent_id`, `operation_generation` |
| `media.delete_requested@2` | `remote_media_id`, `operation_generation` |
| `delivery.delete_requested@2` | `public_delivery_asset_id`, `operation_generation` |

각 이벤트는 정확히 하나의 append-only `MediaDeliveryOperation`과 결속한다. worker는 source
event와 현재 processing receipt의 consumer·lease generation·raw token을 확인하고 operation을
claim한다. 외부 mutation 직전 같은 capability, current intent/approval/target, run stop과 kill
switch를 다시 확인해 write marker를 한 번만 기록한다. write 이후 소유권을 잃으면 동일 mutation을
반복하지 않고 새 reconcile generation을 만든다. 늦은 결과는 mapping을 바꾸지 않는다. terminal
callback은 exact terminal reservation이 있을 때만 `delivery_failed`를 투영한다. v1 미디어 이벤트는
이미 증명된 replay/terminal 처리만 허용하고 신규 외부 쓰기에는 사용하지 않는다.

## English / AI-readable — T022 immutable asset delivery events

All new media prepare, reconcile, and delete work uses the six exact v2 payloads listed above.
`operation_generation` is bounded to 1..5, and public payloads never contain raw receipt tokens,
token hashes, write markers, local paths, or asset bytes. Each event owns exactly one append-only
`MediaDeliveryOperation`. Claim verifies the persisted source event and current processing receipt;
the pre-I/O fence rechecks the same capability plus current intent, approval, target, run-stop, and
kill-switch state, then sets one write marker. Capability loss after that marker creates a new
reconcile generation instead of repeating the mutation. Late results are factual only, and terminal
projection requires the exact terminal reservation. Version 1 is consumption-only for a proven
historical replay or terminal outcome and never authorizes a new external write.

## 한국어 — T016 추출 이벤트 fencing 보강

- leaf/fanout handler는 outbox `event_context`의 source event, consumer, token,
  `lease_generation`이 없으면 실행하지 않는다. production synthetic identity는 없다.
- `evidence.document_ready@1`과 `evidence.other_ready@1`의 기존 필드는 그대로 유지한다. 새
  producer는 input/profile/config/content/locator hash를 optional provenance 필드로 함께 보내며,
  router는 과거 event에서 빠진 optional 필드를 `null`로 전달한다. consumer는 행 잠금 뒤 DB의
  전체 lineage와 child expected manifest를 항상 재검증한다.
- permanent extractor/domain 오류는 worker 본문에서 aggregate terminal을 미리 저장하지 않는다.
  `PermanentEventError`가 receipt dead-letter transaction의 terminal callback으로 전달되고,
  callback이 current event/generation을 확인해 child revoke, redacted failure, 단일 finalizer
  outbox를 원자 기록한다. DB/soft-time-limit/합의된 infrastructure 오류는 그대로 raise한다.
- stop fence 상실과 stale fence 상실은 다르다. STOPPING/stop-requested만 terminal stop projection을
  만들며, 더 높은 generation 또는 이미 terminal run의 delivery는 no-op이다.
- migration 중 in-flight requested event와 receipt는 exact immutable identity가 하나일 때만
  pending/retry로 재무장한다. 과거 pending ready는 v1 selected-manifest hash를 바꾸지 않으며,
  aggregate 재계산 후에도 같은 message key로 수용되어야 한다.

## English / AI-readable — T016 extraction event fencing addendum

- Leaf and fanout handlers require the outbox event context: source event, consumer, token, and
  `lease_generation`. Production has no synthetic direct-call identity.
- Existing required fields of `evidence.document_ready@1` and `evidence.other_ready@1` remain
  compatible. New producers add optional input/profile/config/content/locator hashes; routing
  passes omitted legacy fields as null. Consumers always revalidate full locked DB lineage and
  the child expected manifest.
- Permanent extractor/domain failures do not precommit terminal state in the task body. They flow
  as `PermanentEventError` to the receipt dead-letter transaction, whose terminal callback fences
  the current event/generation and atomically revokes children, stores redacted failure, and emits
  one finalizer event. Database, soft-time-limit, and agreed infrastructure failures are re-raised.
- Stop fence loss is distinct from stale fence loss. Only STOPPING/stop-requested creates a stop
  projection; a higher generation or an already terminal run makes delayed delivery a no-op.
- Migration rearms an in-flight requested event/receipt to pending/retry only when one exact
  immutable identity exists. Historical pending-ready keeps the v1 selected-manifest hash and must
  remain acceptable after aggregate recomputation.
