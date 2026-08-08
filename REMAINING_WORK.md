# Wisdome Super Writer 남은 작업 현황

## 한국어 — 현재 상태

- 기준일: 2026-08-08
- 작업 브랜치: `main`
- T012 리뷰 기준선: `7d04126`
- 현재 단계: T012 구현, 3개 관점 병렬 비판 리뷰 2회, 승인 검증 완료
- 검증 상태: Django check issue 0건, migration drift 0건, compileall exit 0
- 완료 판정: T012 `[X]`; T012 구현·계약·검증 문서를 하나의 changeset으로 커밋

### T012에 반영한 핵심 내용

1. frozen access/rights policy와 hash, traffic scope, origin purpose, robots, redirect, MIME,
   HTTP status, rate/retry/Retry-After 경계를 실제 source 및 attachment 요청에 연결했다.
2. source별 durable attempt와 실제 outbox consumer 전달 번호 기반 append-only observation,
   재시도·소진 callback·부분 성공 finalizer를 연결했다.
3. attachment 접근 오류를 evidence 실패 계약으로 변환하면서 retry 가능 여부와
   `retry_after_seconds`를 보존했다.
4. schema/policy/authentication/security 오류의 영구 실패 분류와
   transient/infrastructure 오류의 outbox 재시도 예산을 분리했다.
5. 마지막 retryable 전달은 worker 성공으로 종료하지 않고 outbox 소진 callback이
   receipt dead-letter와 source attempt terminal 관측을 함께 남기도록 수정했다.
6. 예상 밖 source worker 예외도 실제 전달 번호의 retry 관측으로 남긴다. worker 종료로
   정산되지 않은 앞선 전달 번호는 다음 전달 또는 소진 callback이
   `source_delivery_interrupted` 관측으로 보충하고, 마지막 전달은 terminal callback이 종결한다.
7. `SourceCollectionObservation`의 instance/queryset/base manager 변경을 차단하고
   PostgreSQL UPDATE/DELETE 거부 trigger를 추가했다.
8. freshness는 `modified_at` 우선, `published_at` fallback으로 판정하며 신규 evidence 대상인
   corrected record에도 적용한다. 제외 수는 attempt/observation/run summary에 보존한다.

### 다음 작업

1. 승인된 순서대로 T013 사건 clustering, T014~T017 extraction, T018 editorial,
   T019~T026 publishing, T027~T031 operations, T032 검증, T033 문서 정합성을 진행한다.

## English — AI Handoff

```yaml
as_of: 2026-08-08
branch: main
review_baseline: 7d04126
current_task: T013
T012_status: complete_committed
implementation_status: implementation_two_review_rounds_and_approved_validation_complete
validation_status:
  compileall: passed_exit_0
  django_check: passed_zero_issues
  migration_drift_check: passed_no_changes_detected
resolved_validation_root_cause:
  contract_format: hostname
  contract_path: components.schemas.SourceAccessOriginPolicy.properties.host
  resolution: added_hostname_to_validator_allowlist
  dependency_support: jsonschema_format_nongpl_with_fqdn
  evidence:
    - specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml:1460
    - src/wisdome_writer/api/openapi.py:43
    - pyproject.toml:22
task_checkbox: T012_checked
review_rounds:
  count: 2
  parallel_perspectives:
    - http_security_runtime
    - state_concurrency_database
    - policy_rights_contracts
next_actions:
  - continue_T013_through_T033_in_plan_order
```
