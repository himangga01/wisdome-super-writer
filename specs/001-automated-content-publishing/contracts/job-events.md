# 내부 작업 이벤트 계약

## 전달 의미

- Redis/Celery 전달은 **at-least-once**로 간주한다. 소비자는 중복을 정상 상태로 처리한다.
- 메시지는 엔터티 ID와 정책 버전만 전달하고 본문·원문·토큰은 PostgreSQL/객체 저장소에서
  권한을 확인한 뒤 읽는다.
- DB 상태가 기준이며 브로커 결과 backend는 업무 성공의 기준이 아니다.
- 생산자는 DB 트랜잭션 커밋 후 메시지를 보낸다. 필요한 경우 outbox 행을 같은
  트랜잭션에 만들고 별도 디스패처가 전송한다.
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
| `run.requested` | `run_id` | 실행이 `queued`일 때 수집 시작 |
| `run.stop_requested` | `run_id`, `reason_code` | 새 하위 작업을 만들지 않고 안전 지점에서 중지 |
| `source.collect_requested` | `source_collection_attempt_id` | DB의 run/source snapshot/window와 adapter name/version/implementation/config manifest를 재조회해 조건부 수집 |
| `source.item_changed` | `source_collection_attempt_id`, `run_id`, `run_source_item_id`, `source_item_id`, `change_kind=new_version/corrected/retracted/unavailable/restored` | succeeded attempt의 adapter/response provenance와 RunSourceItem 계보·kind/status 매핑을 검증; new/corrected/restored는 추출·검증, retracted/unavailable은 정정·철회 영향 평가; unchanged에는 이벤트 없음 |
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
| `media.upload_requested` | `remote_media_id`, `publication_attempt_id`, `publication_intent_id`, `target_snapshot_id`, `target_config_hash` | exact intent/attempt의 WordPress checksum 매핑 확인 후 업로드 또는 재사용 |
| `media.available` | `remote_media_id`, `publication_attempt_id`, `publication_intent_id`, `target_snapshot_id`, `target_config_hash`, `remote_source_url` | 같은 current attempt의 final render 자리만 결합; stale intent면 dispatch 금지 |
| `media.reconcile_requested` | `remote_media_id`, `publication_attempt_id`, `publication_intent_id` | media slug/marker로 exact attempt의 응답 유실 조정; 업로드 반복 금지 |
| `delivery.prepare_requested` | `public_delivery_asset_id`, `publication_attempt_id`, `publication_intent_id`, `expected_lease_generation` | Blogger final용 장기 공개 객체를 행 잠금으로 생성/재사용하고 pending delete를 취소 |
| `delivery.available` | `public_delivery_asset_id`, `publication_attempt_id`, `publication_intent_id`, `lease_generation`, `public_url` | 같은 current attempt의 승인 template 자산 자리만 immutable URL로 결합 |
| `delivery.reconcile_requested` | `public_delivery_asset_id`, `publication_attempt_id`, `publication_intent_id` | 객체 checksum·익명 GET과 PublicationMedia binding/원격 본문 참조 조정 |
| `delivery.delete_requested` | `public_delivery_asset_id`, `expected_lease_generation` | ref=0 grace 뒤 행 잠금에서 binding/in-flight/hold/원격 본문을 재검산해 bytes 삭제 또는 취소 |
| `delivery.deleted` | `public_delivery_asset_id`, `lease_generation`, `delete_reason`, `last_reconcile_hash` | mapping tombstone을 감사 수명 동안 유지하고 physical object 삭제 결과 기록 |
| `publication.requested` | `publication_attempt_id` | 승인·kill switch 재검증 후 채널 호출 |
| `publication.primary_public` | `primary_publication_attempt_id`, `publication_intent_id`, `article_revision_id`, `publication_id`, `target_snapshot_id`, `target_config_hash`, `secondary_publication_attempt_ids`, `remote_url`, `public_checked_at` | 같은 current intent에 미리 생성된 Blogger attempt에만 공개 WordPress URL을 결합하고 보조 발행 요청 |
| `publication.reconcile_requested` | `publication_attempt_id` | 원격 성공 여부 조정; create 반복 금지 |
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
4. 외부 API 호출 직전 `PublicationAttempt=in_progress`와 요청 지문을 원자적으로 저장한다.
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
