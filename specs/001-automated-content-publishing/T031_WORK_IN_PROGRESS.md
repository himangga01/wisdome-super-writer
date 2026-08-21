# T031 작업 현황·목표·남은 작업

기록 시각: 2026-08-21 (Asia/Seoul)

## 활성 목표

현재 활성 목표는 기존 작업을 이어서 T031 `운영 API와 관리자 콘솔`을 실제 계약과 서비스 정본에 맞게 완료하는 것이다.

완료 기준은 다음과 같다.

1. schedule CRUD와 expected-version CAS를 제공한다.
2. global kill switch와 개별 run stop을 분리하고 각각 재인증·CAS·감사 이력을 보장한다.
3. run detail에서 source/document/publication 복구 단위와 허용된 selective retry 범위를 표시한다.
4. correction 검증·결정·진행 상태를 관리자 화면에서 처리한다.
5. retention preview→상세/items→재인증 승인→실행→실패 item 재개 전체 여정을 제공한다.
6. audit cursor 조회에서 actor/action/correlation/entity/reason/result를 필터링한다.
7. 관리자 화면은 한국어 우선이며 안전한 DOM/HTTP(S) 링크 정책을 사용한다.
8. OpenAPI, runtime API, 서비스, 테스트, 한국어/영문 문서가 같은 계약을 표현한다.
9. 승인된 T031 focused 회귀와 정적 검사가 모두 통과한 뒤 T031을 완료 처리한다.

T031 이후의 상위 목표는 T032 자동 검증/release evidence와 T033 최종 문서·운영 runbook을 순서대로 완료하는 것이다.

## 이번 체크포인트 성격

- 기준 커밋은 `e5e8cf1 feat: enforce dependency-aware retention deletion`이다.
- T023~T030 결과는 이미 기준 브랜치에 포함되어 있다.
- 현재 변경은 T031 구현 중간 상태다.
- `tasks.md`의 T031 체크박스는 의도적으로 `[ ]`로 유지한다.
- 이번 커밋·푸시는 작업 유실을 막기 위한 WIP 체크포인트이며 T031 완료 또는 배포 가능 선언이 아니다.
- 전체 suite와 E2E는 T032 범위이므로 이번 체크포인트에서 실행하지 않았다.

## 지금까지 작업한 내용

### 1. 일정과 운영 제어

- schedule 목록·상세·생성·수정·비활성화 API를 OpenAPI operation과 연결했다.
- schedule 수정·비활성화 요청에 `expectedVersion` CAS를 결속했다.
- global kill switch 조회·변경 응답을 실제 제어 row와 결정 row에 맞췄다.
- run stop 요청에 `expectedState`, `requestKey`, `reauthProofId`, `reason`을 결속했다.
- `run_stop`을 재인증 허용 scope와 OpenAPI enum에 추가했다.
- run stop view가 T028 서비스와 관리자 `AuditContext`를 사용하도록 연결했다.

### 2. 실행 조회와 선택 복구

- run 목록·상세·stop·selective retry API를 OpenAPI operation과 연결했다.
- run 상세에 source/document/publication 단위의 복구 가능 항목과 정확한 retry scope를 표시한다.
- stop/retry view는 model을 직접 변경하지 않고 T028 service를 호출한다.
- `RunControlDecision` 생성·재생 시 `collection_run.control_decided` 감사 이벤트를 기록·검증하도록 확장했다.
- 기존 수동 run 화면 POST에 `targetIds`, `approvalMode`, validation/activation refs, `requestKey`를 추가해 `CreateRunRequest`와 호환시켰다.

### 3. 정정 운영

- correction 전역 목록 API를 추가했다.
- 기존 correction 상세·결정 흐름을 운영 화면에 연결했다.
- verification/reject 결정은 기존 dual-CAS 및 재인증 서비스 경계를 그대로 사용한다.

### 4. 보존 삭제 운영

- retention preview, batch detail, signed-cursor item 목록, 승인·실행 API를 실제 T030 모델에 맞췄다.
- retention item의 원본 object key를 반환하지 않고 `sha256:<16hex>`로 redaction한다.
- failed batch의 실패 item만 재검증해 `candidate`로 되돌리고 `lease_generation`을 증가시키는 재개 service를 추가했다.
- 재개 시 성공/held sibling은 다시 실행하지 않으며 failed item의 오류·완료 material만 초기화한다.
- retention preview와 승인·재개에 관리자 감사 컨텍스트를 연결했다.
- `retention_batch.previewed`와 `retention_batch.authorized` 감사 이벤트 및 redaction policy를 추가했다.

### 5. 감사 조회

- audit cursor 목록에 actor type/actor ID 필터를 추가했다.
- run control과 retention 상태 변경의 request hash, decision/result, actor provenance를 감사 화면에서 조회할 수 있도록 연결 중이다.

### 6. 관리자 콘솔

- `/console/operations/`를 한국어 우선 화면으로 다시 작성했다.
- schedule, kill switch, run 복구, correction 검증, retention, audit 영역을 한 화면에 연결했다.
- 동적 렌더링은 `textContent`, `createElement`, `replaceChildren`을 사용하며 `innerHTML`을 사용하지 않는다.
- 외부 링크는 기존 HTTP(S) URL 안전성 helper가 허용한 경우에만 생성한다.

### 7. 계약과 테스트

- `admin-api.openapi.yaml`에 schedule CRUD/CAS, kill switch, run stop/retry, correction 목록, retention detail/items/execute, audit actor filter 및 request/response schema를 반영했다.
- 신규 `tests/unit/test_operations_admin_contract.py`에 operation/route, closed request, 서비스 위임, redaction, 복구 projection, safe DOM 계약을 추가했다.
- 기존 run-control 및 retention 테스트에 재인증 감사 이벤트와 failed-batch 재개 동작을 추가했다.

## 현재 변경 파일

- `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml`
- `specs/001-automated-content-publishing/T031_WORK_IN_PROGRESS.md`
- `src/apps/accounts/services.py`
- `src/apps/audit/api.py`
- `src/apps/audit/redaction.py`
- `src/apps/audit/retention.py`
- `src/apps/audit/urls.py`
- `src/apps/collection/api.py`
- `src/apps/collection/services.py`
- `src/apps/editorial/api.py`
- `src/apps/editorial/urls.py`
- `src/apps/scheduling/api.py`
- `src/apps/scheduling/services.py`
- `src/static/admin_console/app.css`
- `src/static/admin_console/operations.js`
- `src/static/admin_console/runs.js`
- `src/templates/admin_console/operations/index.html`
- `tests/unit/test_operations_admin_contract.py`
- `tests/unit/test_retention_dependency_graph.py`
- `tests/unit/test_run_control.py`

## 검증 증거

현재 확인된 통과 결과는 다음과 같다.

1. 초기 operations focused test: 8/8 통과.
2. stop CAS·재인증, failed retention resume, 기존 run 화면 호환 구현 후 focused test: 10/10 통과.
3. 실제 run stop 감사 이벤트 단일 test: 1/1 통과.
4. 누락된 `AuditEvent` test import를 수정한 뒤 retention preview 감사 test + operations contract: 10/10 통과.

가장 최신 변경에는 failed retention resume 감사 이벤트 assertion이 추가되었다. 해당 단일 test 실행은 시작 직후 사용자 중단으로 종료되어 결과가 없다. 따라서 현재 diff 전체가 GREEN이라는 증거는 아직 없다.

## 앞으로 작업해야 하는 내용

### 우선순위 1 — 중단된 focused 검증

아래 명령으로 가장 최신 failed-resume 감사 변경을 먼저 검증한다.

```powershell
$env:WISDOME_ENVIRONMENT='development'
$env:PYTHONPATH='src'
$env:DJANGO_SETTINGS_MODULE='wisdome_writer.settings'
.\.venv\Scripts\python.exe -m django test tests.unit.test_retention_dependency_graph.RetentionServiceTests.test_failed_batch_resume_revalidates_only_failed_items -v 1
```

### 우선순위 2 — T031 직접 관련 회귀

- `tests.unit.test_operations_admin_contract`
- `tests.unit.test_run_control`
- `tests.unit.test_retention_dependency_graph`
- correction orchestration 관련 focused module
- schedule CRUD/dispatch material 관련 focused module

검증할 핵심은 exact replay, CAS conflict, reauthentication scope, append-only audit, failed-item-only resume, raw object-key 비노출, safe DOM이다.

### 우선순위 3 — 구현·계약 최종 대조

- retention 승인/재개 replay가 기존 `AuditEvent`와 exact request hash로 수렴하는지 확인한다.
- run stop audit replay가 `expectedState`와 reauth proof 변경을 충돌로 처리하는지 확인한다.
- selective retry가 단순 결정 기록을 넘어 실제 선택 단위만 재큐잉하는 T028 정본과 연결됐는지 재대조한다.
- OpenAPI response가 runtime serializer와 모든 required/additionalProperties 계약에서 일치하는지 확인한다.
- 운영 화면의 한국어 문자열 인코딩과 브라우저 렌더링을 확인한다.

### 우선순위 4 — 문서와 완료 상태

- `quickstart.md`에 T031 운영 여정을 한국어 먼저, English/AI-readable 절을 뒤에 추가한다.
- `data-model.md`에 운영 projection·감사/재인증 결속을 같은 순서로 기록한다.
- `tasks.md`와 구현 계획의 T031 체크 상태는 위 검증이 모두 끝난 뒤에만 `[x]`로 바꾼다.
- 승인된 정적 검사와 diff 검사를 완료한다.
- 최종 완료 커밋은 계획된 `feat: complete operations administration console`을 사용한다.

## 게시 정보

- 대상 저장소: `https://github.com/himangga01/wisdome-super-writer.git`
- 원격 이름: `origin`
- 현재 기본 브랜치에서 직접 게시하지 않고 WIP feature branch를 생성해 푸시한다.
- 이 기록을 포함한 게시 커밋은 완료 커밋이 아니라 진행 보존용이다.

---

# T031 Status, Goal, and Remaining Work (English/AI-readable)

Recorded: 2026-08-21 Asia/Seoul.

## Active goal

Continue and complete T031 Operations APIs and the administration console against the authoritative T027-T030 services and OpenAPI contract. Completion requires schedule CRUD/CAS, separated kill-switch and run-stop controls, exact recovery projections and selective retry, correction operations, the full retention preview/authorization/execution/failed-item-resume journey, cursor-based audit lookup, safe Korean-first UI rendering, synchronized contracts/docs, and green approved T031 verification.

After T031, continue with T032 automated release evidence and T033 final documentation/runbooks.

## Checkpoint status

Baseline is `e5e8cf1`. The current tree is a T031 work in progress. T031 remains unchecked. This publication is a preservation checkpoint, not a completion or deployability claim.

## Implemented material

- Schedule list/detail/create/update/disable operations with expected-version CAS.
- Kill-switch projection and reauthenticated, state-CAS-bound run stop.
- Run list/detail, exact recovery scopes, stop, and selective-retry service delegation.
- Append-only run-control audit recording/replay.
- Correction listing and existing dual-CAS decision workflow in the operations UI.
- Retention preview/detail/signed-cursor items/authorize/execute and failed-item-only resume.
- Object-key redaction and retention preview/authorization audit policies.
- Actor-filtered audit lookup.
- Korean-first operations console rendered with safe DOM APIs and HTTP(S)-only links.
- OpenAPI and focused contract/service tests for the above boundaries.

## Evidence

Known green evidence: operations 8/8, expanded operations and retention resume 10/10, run-stop audit 1/1, and retention-preview audit plus operations contract 10/10. The newest failed-resume audit assertion was not verified because its test process was interrupted immediately after launch.

## Required next work

Run the interrupted focused test first. Then run the direct T031 regression modules, close any exact replay/CAS/reauth/audit/runtime-schema gaps, verify selective retry performs only the chosen recovery unit, synchronize Korean-first and English/AI-readable documentation, run only the already approved static checks, and mark T031 complete only after all evidence is green.

## Publication

Push this checkpoint to `origin` (`https://github.com/himangga01/wisdome-super-writer.git`) on a feature branch. Do not represent this WIP commit as the final T031 completion commit.
