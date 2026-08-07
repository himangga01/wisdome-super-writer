# Wisdome Super Writer 남은 작업 인수인계

**갱신일**: 2026-08-07  
**브랜치**: `main`  
**작성 시점 HEAD**: `7ef036a`  
**정본 작업 목록**: [`tasks.md`](tasks.md)  
**상세 실행 계획**: [`../../docs/superpowers/plans/2026-08-07-remaining-implementation.md`](../../docs/superpowers/plans/2026-08-07-remaining-implementation.md)

## 1. 서비스 목표

승인된 청약·반도체 출처에서 자료를 수집하고, 권리·locator·신뢰도가 보존된 증거를 기반으로
한국어 초안을 생성한다. 관리자가 승인하거나 검증된 자동발행을 활성화하면 WordPress 대표
원문을 먼저 발행하고 Blogger 보조 글을 뒤이어 발행한다. 일정, 중지, 재시도, 정정·철회와
보존 작업은 관리자 화면과 감사 계보로 운영한다.

## 2. 현재 구현 상태

### 완료

- T001~T008: HTTP 보안, canonical hash, production 경계, 재인증, Outbox, 감사, API 공통 계층, 관측성
- T009: immutable source registry와 승인·변경 계보
- T010: 청약홈·LH·공공데이터 수집과 상태 계보
- T011: 정부·규제기관·거래소·기업 IR·뉴스 수집과 상태 계보

### 진행 중

- T012 접근·권리·robots·MIME·rate limit·Retry-After·freshness·authority 런타임 구현이
  현재 작업 트리에 존재한다.
- T012 변경은 아직 커밋되지 않았다.
- 요청된 병렬 비판 리뷰 2회, 메인 세션 재검증, 타당한 수정, 문서·계약 정합성 마감이 남았다.
- T012 검증 명령은 AGENTS.md 지침에 따라 사용자 승인 전 실행하지 않는다.

### 남음

- T013~T018: 사건 검증, OCR/HWP 추출, 편집 정책과 근거 기반 초안
- T019~T026: 승인, 멱등 발행, remote reconcile, 미디어, OAuth, 자동발행, 관리자 UI
- T027~T031: 일정, 중지·재시도, 정정·철회, 보존과 운영 UI
- T032: 단위·계약·통합·E2E 검증과 SC-001~SC-012 증거
- T033: README, 계약, quickstart와 운영 runbook 최종 정합성

## 3. 재검토 판정

| 범위 | 판정 | 다음 처리 |
|---|---|---|
| T012 | 핵심 구현 존재, 미완료 | 현재 코드를 기준으로 비판 리뷰 2회 후 마감 |
| T013 | 대부분 미구현 | cluster item·verification·canonical identity 추가 |
| T014~T017 | 부분 구현 | 모델 bootstrap·HWP sandbox·fencing·routing 보강 |
| T018 | 부분 구현 | 불변 editorial policy와 차단형 quality gate 연결 |
| T019~T026 | 상당한 골격 존재 | 기존 상태 머신 보존, projection·snapshot·dependency·API/UI 보강 |
| T027~T031 | 부분 구현 | 기존 일정·정정·보존 골격을 end-to-end로 연결 |
| T032 | 검증 파일 0개 | 기능 완료 후 사용자 승인 범위로 구현·실행 |
| T033 | 미완료 | 실제 코드·검증 근거 기준으로 문서 갱신 |

체크되지 않은 task가 모두 신규 구현이라는 의미는 아니다. 발행·일정·정정·보존 영역에는 이미
모델과 서비스 골격이 있으므로 상세 계획의 `preserve-valid-code-and-close-verified-gaps` 원칙을
따른다.

## 4. 실행 순서

1. Wave 0: T012 리뷰 1차 → 메인 재검증·수정 → 리뷰 2차 → 메인 재검증·수정 → 승인된 검증 → 커밋
2. Wave 1: T013 사건 검증과 T014 추출 bootstrap 시작 → T015 → T016 → T017 → T018
3. Wave 2: T019과 T023을 독립 경계에서 진행 → T020 → T021 → T022 → T024 → T025 → T026
4. Wave 3: T027 → T028 → T029 → T030 → T031
5. Wave 4: 사용자 승인 후 T032 검증 → T033 문서 마감

## 5. T012 즉시 검토 항목

- 첨부 다운로드의 `SourceAccessError`가 evidence 실패 분류로 변환되는지
- `SourceCollectionObservation` bulk update/delete도 append-only인지
- freshness 제외가 영구 관측과 run counters에 일관되게 남는지
- OpenAPI nullable·enum·HTTP status 범위가 실제 모델/API와 일치하는지
- `Location` 없는 3xx가 성공으로 처리되지 않는지
- in-process retry 소진 후 durable retry 정책이 의도대로 동작하는지
- 출처와 첨부 record host가 승인 access policy에 모두 포함됐는지
- migration backfill과 reverse 동작이 기존 run의 정책 계보를 훼손하지 않는지

## 6. 검증과 완료 정책

- 테스트, 빌드, Django check, migration check·실행과 외부 연동은 사용자 승인 후에만 수행한다.
- 각 task는 model, migration, service, event, API, UI와 문서 계약이 일치해야 한다.
- replay, 동시 실행, 부분 실패, 영구 실패와 관리자 복구 경로가 정의돼야 한다.
- reviewer 지적은 메인 세션에서 코드와 명세로 재검증한 뒤 타당한 항목만 수용한다.
- 구현·리뷰·승인된 검증 근거가 모두 있는 task만 `tasks.md`에서 `[X]`로 표시한다.
- 새로운 격차가 기존 T012~T033에 포함되지 않을 때만 다음 번호의 convergence task를 추가한다.

---

# English — AI Handoff

```yaml
schema_version: "1.0"
feature: "001-automated-content-publishing"
branch: "main"
head_at_update: "7ef036a"
source_of_truth: "specs/001-automated-content-publishing/tasks.md"
execution_plan: "docs/superpowers/plans/2026-08-07-remaining-implementation.md"
completed_tasks: "T001-T011"
current_task: "T012"
remaining_tasks: "T012-T033"
current_worktree:
  t012_changes: "uncommitted"
  automated_tests: "not implemented"
  validation_run: false
execution_strategy: "preserve-valid-code-and-close-verified-gaps"
next_actions:
  - run T012 critical review round 1 with three independent perspectives
  - adjudicate findings in the main session and apply verified fixes
  - run T012 critical review round 2 with fresh context
  - adjudicate and close remaining blockers
  - request approval for validation commands
  - commit T012 and continue with T013/T014 dependency wave
validation_policy: "explicit user approval required"
documentation_policy: "Korean first, English machine-readable handoff second"
```
