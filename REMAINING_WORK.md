# Wisdome Super Writer 현재 인수인계

기준일: 2026-08-21  
기준 브랜치: `main`  
저장소: `https://github.com/himangga01/wisdome-super-writer.git`

## 한국어 — 먼저 읽을 내용

현재 작업은 T031 `운영 API와 관리자 콘솔` 구현 중간 상태다. 가장 상세한 활성 목표, 구현 내역, 검증 증거, 남은 작업과 다른 PC 재개 명령은 아래 문서가 정본이다.

- [T031 작업 현황·목표·남은 작업](specs/001-automated-content-publishing/T031_WORK_IN_PROGRESS.md)
- [정본 작업 체크리스트](specs/001-automated-content-publishing/tasks.md)
- [상세 구현 계획](docs/superpowers/plans/2026-08-07-remaining-implementation.md)

### 현재 체크 상태

- 완료 표시: T001~T014, T023, T029, T030
- 구현 커밋은 존재하지만 선행 dependency·외부 acceptance·검증 gate 때문에 미완료 표시 유지: T015~T022, T024~T028
- 현재 진행: T031
- 이후: T032 자동 검증/release evidence, T033 최종 문서·운영 runbook

커밋이 존재한다는 사실만으로 task를 완료 처리하지 않는다. `tasks.md`의 현재 체크 상태와 각 task의 명시적 dependency/acceptance 조건을 우선한다.

### 새 PC에서 시작

```powershell
git clone https://github.com/himangga01/wisdome-super-writer.git
Set-Location wisdome-super-writer
git switch main
git pull --ff-only origin main
git status --short
Get-Content -Encoding UTF8 specs\001-automated-content-publishing\T031_WORK_IN_PROGRESS.md
git switch -c codex/t031-operations-continue
```

Python 3.12와 Docker Desktop/Compose를 사용한다. `.env.example`에서 새 PC 전용 `.env`를 만들고 이전 PC의 비밀, token, cookie, Application Password를 복사하거나 커밋하지 않는다. 자세한 환경 준비와 첫 focused test는 T031 문서의 ‘다른 PC에서 이어서 작업하는 절차’를 따른다.

### 안전 상태

- T031은 WIP이며 배포 가능 선언이 아니다.
- 최신 failed-retention-resume 감사 assertion은 실행 직후 중단되어 아직 결과가 없다.
- 전체 suite/E2E는 T032 범위다.
- 외부 게시를 포함한 전체 환경을 처음 올릴 때는 global kill switch 차단 상태를 유지한다.

---

# English — AI handoff

The active task is the in-progress T031 Operations APIs and administration console. Use `specs/001-automated-content-publishing/T031_WORK_IN_PROGRESS.md` as the detailed continuation authority, `tasks.md` as the checkbox/dependency authority, and the dated implementation plan for sequencing.

Checked tasks are T001-T014, T023, T029, and T030. T015-T022 and T024-T028 have substantial implementation commits but intentionally remain unchecked because dependency, external acceptance, or verification gates remain open. T031 is active; T032 and T033 follow.

On another machine, clone `origin/main`, confirm a clean tree, read the T031 handoff, and create a new continuation branch. Use Python 3.12. Recreate `.env` from `.env.example`; do not transfer secrets through Git. The current T031 material is WIP and not a deployability claim.
