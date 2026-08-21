# 자동 콘텐츠 발행 기능 남은 작업 인수인계

갱신일: 2026-08-21  
브랜치 정본: `origin/main`

## 한국어 — 정본과 현재 단계

이 파일은 기능 단위 인수인계 진입점이다. 세부 상태는 다음 순서로 확인한다.

1. [`tasks.md`](tasks.md): task 체크 상태와 dependency 정본
2. [`T031_WORK_IN_PROGRESS.md`](T031_WORK_IN_PROGRESS.md): 현재 활성 목표, 구현 diff, 검증 증거, 다른 PC 재개 명령
3. [`../../docs/superpowers/plans/2026-08-07-remaining-implementation.md`](../../docs/superpowers/plans/2026-08-07-remaining-implementation.md): 구현 순서와 task별 완료 조건
4. [`quickstart.md`](quickstart.md): 승인된 운영·검증 절차

## 현재 상태

| 범위 | 체크 상태 | 인수인계 판단 |
|---|---:|---|
| T001~T014 | 완료 | 체크된 구현·계약 기준 유지 |
| T015~T018 | 미완료 | 구현 material은 있으나 HWP 외부 release trust와 선행 extraction acceptance를 포함한 완료 gate가 열려 있음 |
| T019~T022 | 미완료 | 승인·dispatch·execution·media 구현 material은 있으나 선행 dependency와 최종 acceptance 때문에 체크 금지 |
| T023 | 완료 | publisher credential lifecycle 완료 표시 |
| T024~T028 | 미완료 | canonical dependency·auto publish·운영 제어 구현 커밋은 있으나 선행 task 완료/검증 gate가 열려 있음 |
| T029~T030 | 완료 | 정정·철회와 dependency-aware retention 완료 표시 |
| T031 | 진행 중 | 운영 API/관리자 콘솔 WIP가 `main`에 병합됨. 상세는 T031 문서 참조 |
| T032 | 대기 | 자동 단위·계약·통합·E2E 및 release evidence |
| T033 | 대기 | 실제 검증 결과 기반 최종 문서·runbook 정합화 |

## 다음 실행 순서

1. T031 문서에 기록된 중단 focused test를 실행한다.
2. run control, retention, correction, schedule 직접 회귀를 닫는다.
3. runtime/OpenAPI/감사 replay/재인증/CAS/UI 문자열을 대조한다.
4. `quickstart.md`와 `data-model.md`에 한국어 절을 먼저, English/AI-readable 절을 뒤에 동기화한다.
5. 승인된 정적 검사까지 통과한 뒤에만 T031을 `[X]`로 바꾼다.
6. 이후 T032 검증 증거를 만들고 마지막으로 T033 문서를 마감한다.

## 새 PC 주의 사항

- `origin/main`을 fast-forward로 받고 clean status에서 새 continuation branch를 만든다.
- Python 3.12를 사용한다.
- `.env`, OAuth token, cookie, WordPress Application Password, Blogger token은 Git에 없다. `.env.example`에서 새 로컬 설정을 만든다.
- PaddleOCR model bytes, HWP converter attestation, publisher credential, 운영 데이터는 별도 안전 경로로 준비한다.
- 현재 T031은 WIP이므로 운영 배포하거나 외부 쓰기 가능 상태로 취급하지 않는다.

---

# Automated Publishing Remaining Work (English/AI-readable)

This is the feature-level handoff index. `tasks.md` is authoritative for task/dependency status. `T031_WORK_IN_PROGRESS.md` is authoritative for the active goal, current implementation, evidence, gaps, and cross-machine commands. The dated implementation plan defines ordering and completion gates.

T001-T014, T023, T029, and T030 are checked. T015-T022 and T024-T028 retain substantial code and commits but remain unchecked because their dependency, external acceptance, or verification gates are not fully satisfied. T031 is active and merged as WIP into `main`. T032 automated verification/release evidence and T033 final documentation remain afterward.

Resume T031 from a clean `origin/main`, create a continuation branch, run the interrupted focused test, close direct regressions and contract gaps, synchronize Korean-first then English documentation, and check T031 only after its approved evidence is green. Never move secrets or non-Git model/attestation artifacts through the repository.
