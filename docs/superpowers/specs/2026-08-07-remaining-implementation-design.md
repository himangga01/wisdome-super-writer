# Wisdome Super Writer 남은 구현 설계

**작성일**: 2026-08-07  
**대상 기능**: `001-automated-content-publishing`  
**기준 브랜치**: `main`  
**정본 문서**: `spec.md`, `plan.md`, `tasks.md`, Constitution 1.0.1

## 1. 목적

이 설계의 목적은 T012부터 T033까지 남은 작업을 현재 코드 상태에 맞게 다시 분해하고,
이미 존재하는 모델·서비스·상태 머신을 보존하면서 누락된 계약만 단계적으로 완성하는 것이다.
완료 체크박스만 기준으로 코드를 다시 작성하지 않으며, 각 작업은 명세 요구사항, 실제 코드,
데이터베이스 계보, API 계약과 운영 복구 절차를 함께 대조한 뒤 수정한다.

최종 결과는 다음 사용자 흐름을 하나의 상관관계 ID로 추적할 수 있는 서비스다.

1. 승인된 주제·출처·접근 정책으로 자료를 수집한다.
2. 원문과 첨부파일에서 권리·locator·신뢰도가 보존된 증거를 만든다.
3. 중복·충돌·정정·속보 조건을 검증해 한국어 초안을 생성한다.
4. 관리자가 초안과 근거를 승인하거나 검증된 자동발행 정책을 활성화한다.
5. WordPress 대표 원문을 먼저 확정한 뒤 Blogger 보조 글을 발행한다.
6. 일정 실행, 중지, 재시도, 정정·철회와 보존을 관리자 화면에서 운영한다.

## 2. 범위와 제외 범위

### 구현 범위

- T012 출처 접근·권리·최신성·권위 정책의 실제 요청 경로 강제
- T013~T018 사건 검증, 문서 추출, 편집 정책과 초안 품질 게이트
- T019~T026 승인, 발행, 원격 조정, 미디어와 자동발행 관리자 기능
- T027~T031 일정, 중지, 정정·철회, 보존과 운영 관리자 기능
- T032 자동 검증과 T033 최종 문서 정합성
- 현재 미커밋 T012 코드에 대한 서로 다른 관점의 병렬 비판 리뷰 2회

### 제외 범위

- 두 MVP 주제 외 임의 주제 온보딩
- WordPress와 Blogger 외 발행 채널
- 다중 조직·다중 역할·공개 회원가입
- 로그인, CAPTCHA, 유료벽 또는 기술적 접근 제한 우회
- 권리 상태가 불명확한 원문·비텍스트 자산의 게시
- 기존 요구사항과 무관한 마이크로서비스 분리나 프런트엔드 전면 재작성

## 3. 재검토 결과

| 작업 범위 | 현재 판정 | 설계상 처리 방식 |
|---|---|---|
| T012 | 핵심 구현 존재, 미완료 | 현재 변경을 동결하고 2회 비판 리뷰·수정·계약 정합성 마감 |
| T013 | 대부분 미구현 | 사건 구성원·검증 결정·canonical identity를 새 경계로 추가 |
| T014~T017 | 부분 구현 | 기존 추출 모델과 라우팅을 유지하고 모델 bootstrap·sandbox·fencing을 보강 |
| T018 | 부분 구현 | 기존 초안·주장 모델에 불변 편집 정책과 차단형 검증을 연결 |
| T019~T026 | 상당한 골격 존재 | 승인·attempt·OAuth·validation 구현을 유지하고 누락 snapshot·상태 전이를 보강 |
| T027~T031 | 부분 구현 | 일정·kill switch·정정·보존 골격을 end-to-end orchestration으로 연결 |
| T032 | 검증 파일 없음 | 기능 완료 뒤 승인된 검증 범위만 자동화 |
| T033 | 문서 불일치 | 실제 구현과 검증 근거를 기준으로 최종 갱신 |

`REMAINING_WORK.md`는 과거 커밋과 T039~T057 번호를 사용하므로 현재 정본으로 사용하지 않는다.
정본 작업 ID는 `tasks.md`의 T001~T033이며, 새로운 격차가 기존 T012~T033으로 추적되지 않을
때만 다음 번호의 convergence task를 추가한다.

## 4. 설계 원칙

### 4.1 기존 구현 보존

부분 구현 작업은 새로 작성하지 않는다. 먼저 데이터 모델, 고유 제약, 트랜잭션 경계,
Outbox event, API schema와 운영 복구 경로를 확인한다. 요구사항을 이미 만족하는 코드는 유지하고,
미충족·부분 충족·상충 항목만 수정한다.

### 4.2 불변 입력과 append-only 결정

실행·추출·편집·발행은 승인 시점의 registry, source, access policy, rights policy,
extraction profile, editorial policy, target validation과 activation material을 ID·version·hash로
고정한다. 승인·검증·수집 관측·발행 시도·정정 결정은 append-only 기록으로 남기고 최신 상태는
명시적 projection과 CAS로 관리한다.

### 4.3 부분 성공과 복구

한 출처·문서·채널의 실패가 이미 성공한 다른 단위를 되돌리지 않는다. 모든 외부 작업은
멱등 키, generation 또는 lease fencing, 제한된 재시도와 unknown-outcome reconciliation을 가진다.
관리자가 원인과 영향 범위, 안전한 다음 동작을 확인할 수 없는 실패 상태는 완료로 보지 않는다.

### 4.4 WordPress 우선 발행

WordPress가 대표 원문과 canonical URL을 먼저 확정한다. Blogger는 WordPress 공개 상태와
비인증 URL 확인이 성공한 뒤에만 실행한다. 정정·철회도 WordPress terminal 상태가 확정된 뒤
Blogger를 처리한다.

### 4.5 승인 기반 검증

AGENTS.md 지침에 따라 테스트, 빌드, Django check, migration check, migration 실행과 외부 연동
검증은 사용자의 명시적 승인 없이 실행하지 않는다. 구현 계획에는 검증 명령과 기대 결과를
기록하되 실제 실행은 별도 승인 게이트로 둔다.

## 5. 구현 Wave

### Wave 0 — T012 출처 정책 런타임 마감

현재 미커밋 변경을 기준선으로 고정하고 다음 세 관점의 읽기 전용 리뷰를 병렬로 수행한다.

- HTTP·SSRF·robots·redirect·MIME·rate limit·재시도 관점
- collection orchestration·DB 제약·마이그레이션·append-only 관점
- 접근·권리·freshness·authority·API·OpenAPI·문서 계약 관점

각 리뷰 결과는 메인 세션에서 실제 코드와 계약으로 재검증한다. 타당한 항목만 수정한 뒤 같은
관점으로 2차 리뷰를 반복한다. 두 차례 모두 남은 차단 항목이 없고 문서와 migration이 일치해야
T012를 완료 처리한다.

### Wave 1 — T013~T018 근거 기반 초안 완성

두 작업 흐름을 인터페이스 경계가 겹치지 않는 범위에서 진행한다.

- 사건 흐름: T013의 `EventClusterItem`, `EventClusterVerification`, canonical identity,
  duplicate/conflict/exclusion 결정과 반도체 속보 조건
- 추출 흐름: T014~T017의 OCR model manifest, legacy HWP sandbox, extraction fencing,
  browser/media/manual routing과 안전 parser 경계

두 흐름이 완료되면 T018에서 불변 editorial policy를 고정하고 `fact`, `company_claim`,
`interpretation`, `outlook`을 분리한다. 고위험 값, 독립 출처, freshness, 권리, locator, 인용 길이,
가독성과 과장 검사를 차단형 gate로 연결한다. 수동 개정도 같은 gate를 다시 통과해야 한다.

### Wave 2 — T019~T026 안전 발행 완성

기존 publication 모델과 서비스는 유지한다. 먼저 승인 projection과 intent idempotency를 확정하고,
attempt generation·lease·unknown outcome·reconcile terminal 상태를 닫는다. 자격 증명/OAuth 경계는
공유 모델 변경이 없는 범위에서 병행할 수 있다.

그 다음 evidence·visual·channel snapshot을 revision과 불변 연결하고 WordPress media와 Blogger
public delivery를 연결한다. target validation, canary, pilot, activation material은 서버가 직접
조회·검증하며, API와 관리자 UI는 OpenAPI의 path·schema·status·pagination과 일치시킨다.

### Wave 3 — T027~T031 운영 수명주기 완성

`ScheduleDispatch`에 schedule, registry, target, approval과 activation material을 불변 고정한다.
worker 중지 재조회, queued 취소, run/step/channel terminal 집계와 `queue_one` 해제를 모든 종료
경로에 연결한다.

정정·철회는 source change 관측에서 idempotent case를 만들고 관리자 verify/reject 뒤 새 revision과
편집 gate를 실행한다. 보존은 raw/draft 90일, published/audit 365일 정책, legal hold와 활성 참조를
적용하고 S3 object version 실제 삭제와 DB tombstone을 일치시킨다. 운영 UI는 실패 원인, 영향 대상,
안전한 retry/reconcile과 retention 진행 상태를 제공한다.

### Wave 4 — T032~T033 출시 게이트

기능 구현과 리뷰가 끝난 뒤 승인된 범위에서 단위·계약·통합·E2E 검증을 작성·실행한다.
SC-001~SC-012, 중복 발행 100회, PDF 골든 30페이지, 속보 조건, WordPress 우선 정정과 비밀 redaction
결과를 기록한다. 검증 근거가 있는 항목만 완료로 표시하고 README, quickstart, OpenAPI, event 계약,
운영 runbook과 남은 작업 문서를 실제 상태로 갱신한다.

## 6. 컴포넌트 경계와 데이터 흐름

```text
Topic/Registry approval
        ↓ frozen IDs, versions, hashes
CollectionRun → SourceCollectionAttempt → SourceCollectionObservation
        ↓ successful RunSourceItem lineage
DocumentExtraction / GenericExtractionAttempt
        ↓ EvidenceAsset + locator + rights + confidence
EventCluster + immutable verification
        ↓ canonical article identity
DraftArticle / ArticleRevision / ClaimEvidence / quality gates
        ↓ approved revision and frozen channel renders
PublicationIntent → PublicationAttempt → remote reconciliation
        ↓ WordPress canonical URL, then Blogger
Schedule / correction / retention / audit operations
```

각 화살표는 이벤트 payload만 신뢰하지 않고 DB의 상위 객체와 불변 hash를 다시 확인한다.
다음 단계는 이전 단계의 성공 projection과 terminal 상태를 모두 확인한 뒤에만 시작한다.

## 7. 오류 처리와 상태 규칙

- 정책·schema·권리·authority 위반은 영구 실패로 분류하고 자동 재시도하지 않는다.
- 인증 만료는 credential refresh 또는 관리자 재연결이 가능한 별도 상태로 보존한다.
- timeout, 제한 응답과 일시적 DNS 문제는 승인된 최대 횟수와 시간 안에서만 재시도한다.
- 응답 유실처럼 원격 결과를 모르는 경우 create를 반복하지 않고 결정적 원격 식별자로 조정한다.
- 전체 출처 실패는 evidence fanout을 시작하지 않고 run을 실패로 종료한다.
- 부분 출처 성공은 성공 자료만 다음 단계로 보내고 실패 출처와 영향 범위를 보존한다.
- 저신뢰 고위험 값, 충돌 미해결, 권리 불명확 자료는 초안 또는 자동발행을 차단한다.
- WordPress 실패 또는 공개 미확인은 Blogger를 시작하지 않는다.
- append-only 객체는 instance 저장·삭제뿐 아니라 bulk update/delete 우회도 차단한다.

## 8. 작업 완료 게이트

각 task는 다음 조건을 모두 만족해야 완료로 표시한다.

1. 해당 task의 요구사항과 acceptance scenario가 실제 코드 경로에 연결돼 있다.
2. model, migration, service, event, API와 문서가 같은 이름·enum·nullable 규칙을 사용한다.
3. replay, 동시 실행, partial failure, permanent failure와 관리자 복구 경로가 정의돼 있다.
4. 서로 다른 관점의 리뷰 결과를 메인 세션에서 재검증하고 타당한 지적을 반영했다.
5. 사용자 승인 범위의 검증 결과가 기록돼 있다.
6. `tasks.md`와 남은 작업 문서가 실제 구현 상태와 일치한다.

## 9. 계획 문서 구조

실행 계획은 하나의 master plan으로 작성하되 task를 독립 검토 가능한 단위로 분리한다. 각 task에는
정확한 생성·수정 파일, 소비·생산 인터페이스, migration, 실패 상태, 단계별 구현 작업, 승인 후 실행할
검증 명령, 기대 결과와 커밋 경계를 기록한다. 여러 하위 시스템을 한 커밋에 섞지 않는다.

---

# English — AI Execution Design

## Objective

Close T012-T033 by preserving valid existing implementation and remediating only verified gaps across
models, migrations, services, events, APIs, UI, recovery behavior, and documentation.

## Execution Model

```yaml
schema_version: "1.0"
feature: "001-automated-content-publishing"
source_of_intent:
  - specs/001-automated-content-publishing/spec.md
  - specs/001-automated-content-publishing/plan.md
  - specs/001-automated-content-publishing/tasks.md
  - .specify/memory/constitution.md
current_task_range: "T012-T033"
implementation_policy: "preserve-and-close-gaps"
validation_policy: "explicit-user-approval-required"
waves:
  wave_0:
    tasks: [T012]
    gate: "two parallel critical-review rounds, main-session adjudication, fixes, contract closure"
  wave_1:
    tasks: [T013, T014, T015, T016, T017, T018]
    parallel_tracks:
      - [T013]
      - [T014, T015, T016, T017]
    join: T018
  wave_2:
    tasks: [T019, T020, T021, T022, T023, T024, T025, T026]
    invariant: "WordPress public canonical URL precedes Blogger publication"
  wave_3:
    tasks: [T027, T028, T029, T030, T031]
    invariant: "immutable schedule material and recoverable terminal aggregation"
  wave_4:
    tasks: [T032, T033]
    gate: "approved automated verification evidence and documentation reconciliation"
completion_requirements:
  - implementation matches requirement and acceptance scenario
  - model, migration, event, API, UI, and docs contracts agree
  - idempotency, fencing, retry, partial failure, and recovery are explicit
  - review findings are independently revalidated before acceptance
  - validation results exist only for user-approved commands
  - task tracking reflects actual code and evidence
```

## Non-Negotiable Invariants

- No evidence-free factual output.
- No access-control, robots, paywall, login, or CAPTCHA bypass.
- No publication of non-text assets without frozen rights and provenance.
- No mutable reread of approved execution material after a run starts.
- No duplicate remote create after an unknown outcome.
- No Blogger publication or correction before WordPress reaches the required public terminal state.
- No task completion based on a checkbox without code and approved validation evidence.
