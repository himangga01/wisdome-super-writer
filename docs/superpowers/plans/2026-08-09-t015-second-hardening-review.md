# T015 2차 격리 보강 구현 계획

> **에이전트 작업 지침:** `superpowers:test-driven-development`와
> `superpowers:receiving-code-review`를 사용해 각 단계의 RED→GREEN을 확인한다.

**목표:** legacy HWP sidecar의 supervisor/child/validator 권한, descendant 정리, 저장 보고서,
deadline, recovery, profile activation 및 공급망 계약을 2차 리뷰 기준으로 fail-closed 보강한다.

**구조:** 전용 UID0 supervisor만 CHOWN/KILL/SETUID/SETGID capability를 가지며 parser와 validator는
exec 전에 모든 capability를 지운 별도 UID로 실행한다. supervisor-owned input/trusted output과
parser-owned output을 분리하고, subreaper가 setsid escape descendant까지 정리한다. profile 보고서는
self-reference 없는 core object와 DB envelope로 나누며, HWP 성공/recovery/activation material은 exact
identity와 immutable acceptance artifact로 결속한다.

**기술:** Python 3.12, Django 5.2, Unix-domain socket, Linux `/proc`/`prctl`/`capset`, Docker Compose,
S3 versioned objects, unittest, YAML static contract checks.

## 전역 제약

- focused RED→GREEN 테스트와 정적 검사만 실행한다.
- Docker build, 실제 HWP 변환, deploy, push, full suite를 실행하지 않는다.
- `legacy-hwp-v1@1.1.0`은 immutable golden=false draft로 유지하며 retire transition을 만들지 않는다.
- hermetic vendor/deb/SBOM/signature와 T032 acceptance artifact가 없으므로 production 활성화를 완료로
  주장하지 않는다.

---

### 작업 1: Linux supervisor와 자식 capability/descendant 경계

**파일:** `deploy/containers/hwp-worker/wisdome-hwp-sandbox`, Dockerfile, `compose.yaml`,
`tests/unit/test_legacy_hwp_sandbox.py`

- [ ] UID0 supervisor의 exact capability/NoNewPrivs 검증과 PR_SET_CHILD_SUBREAPER를 요구하는 RED 작성
- [ ] parser 65533/validator 65531 exec identity probe와 zero-capability RED 작성
- [ ] setsid descendant TERM→KILL→reap/zero-descendant RED 작성
- [ ] input/output/trusted directory 권한과 hwp-volume-bootstrap Compose RED 작성
- [ ] 최소 구현 후 해당 focused tests를 GREEN으로 확인

### 작업 2: profile-admin과 verification report core/envelope

**파일:** `deploy/containers/profile-admin/Dockerfile`, `compose.yaml`, profile verification command,
`src/apps/evidence/services.py`, `src/apps/evidence/api.py`, focused test

- [ ] OCR extra와 Paddle/HWP material을 가진 전용 image 계약 RED 작성
- [ ] self-reference 없는 canonical core upload와 실제 VersionId/etag DB envelope RED 작성
- [ ] approval/API가 versioned core bytes와 DB envelope를 검증하는 RED 작성
- [ ] 최소 구현 후 focused tests를 GREEN으로 확인

### 작업 3: absolute deadline과 recovery identity

**파일:** HWP client/wrapper, `src/apps/evidence/tasks.py`, focused test

- [ ] 단계별 timeout 재사용을 거절하는 absolute remaining-budget RED 작성
- [ ] expected attempt/source/run/parent/input/manifest/MIME/locator 결속 recovery RED 작성
- [ ] raw quarantine와 converted/derived zero-output durable counter RED 작성
- [ ] 최소 구현 후 focused tests를 GREEN으로 확인

### 작업 4: activation 및 공급망 material 계약

**파일:** extraction profile JSON/loader, HWP Dockerfile/manifest builder, README/quickstart/plan/contracts/tasks,
focused tests

- [ ] golden=true acceptance key/version/hash·OCI digest·manifest/all-pass 필수 검증 RED 작성
- [ ] absolute Python shebang, fixed fonts config/no active cache, LICENSE/Cargo.lock/qpdf/build-policy material RED 작성
- [ ] compose implementation binding과 profile import 회귀 RED 작성
- [ ] 1.2 approved 후 1.1 superseded draft 유지 및 외부 material blocker 문서화

### 작업 5: 최종 focused 검증과 단일 커밋

- [ ] T015 focused tests, T014 regression, profile-report focused tests 실행
- [ ] Python compile, Compose YAML/contract, `git diff --check` 실행
- [ ] `fix: close legacy HWP release trust gaps` 단일 커밋 생성

## English / AI-readable

Implement the second T015 hardening round with RED→GREEN checkpoints. The trusted supervisor is
container UID 0 with only CHOWN/KILL/SETUID/SETGID, exact `/proc` capability validation, and child
subreaping. Parser UID 65533 and validator UID 65531 must prove zero capabilities after exec.
Separate immutable input, untrusted output, and trusted validator-readable PDF directories. Store
profile verification as self-reference-free canonical core bytes plus a DB key/version/hash envelope.
Use one absolute deadline, bind recovery to the expected attempt and all persisted material, and
require immutable T032 acceptance/OCI/manifest material for any golden profile. External hermetic
source/vendor/deb/SBOM/signature and acceptance objects remain explicit release blockers.
