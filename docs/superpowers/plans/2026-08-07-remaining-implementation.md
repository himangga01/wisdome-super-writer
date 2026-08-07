# Wisdome Super Writer 남은 구현 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 승인된 출처 수집부터 근거 기반 초안, WordPress·Blogger 발행, 일정·정정·보존 운영까지 T012~T033의 남은 계약을 현재 코드 위에서 완성한다.

**Architecture:** Django 모듈형 단일 서비스와 PostgreSQL 정본 상태를 유지한다. 각 단계는 승인 시점의 registry·policy·profile·revision·target material을 ID·version·hash로 고정하고, Celery Outbox 이벤트는 DB 계보를 다시 검증한 뒤에만 다음 단계를 실행한다. 기존 모델·서비스를 보존하고 명세와 충돌하거나 빠진 경계만 보강한다.

**Tech Stack:** Python 3.12, Django 5.2 LTS, Celery 5.6, PostgreSQL 17+, Redis, S3/MinIO, httpx, PaddleOCR 3.7.0 PPStructureV3, PyMuPDF/pypdf/pdfplumber, Playwright, pytest/pytest-django.

## Global Constraints

- 기본 언어와 관리자 문서는 한국어이며, AI 실행 메타데이터는 문서 후반 영문 섹션에 둔다.
- 기본 시간대는 `Asia/Seoul`이다.
- 사용자는 Django staff 단일 관리자 역할만 지원한다.
- 공개되고 승인된 출처만 접근하며 로그인·유료벽·CAPTCHA·기술적 제한을 우회하지 않는다.
- 모든 사실 주장은 독자가 도달할 수 있는 원출처 또는 승인된 교차 검증 증거와 연결한다.
- 권리 상태가 확인되지 않은 비텍스트 자료는 게시하지 않는다.
- OCR은 `paddleocr[doc-parser]==3.7.0`의 `PPStructureV3`만 사용하고 다른 OCR 엔진으로 자동 우회하지 않는다.
- WordPress가 대표 원문과 canonical URL을 먼저 확정한 뒤 Blogger를 발행·수정·철회한다.
- PostgreSQL이 실행·추출·승인·발행 상태의 유일한 정본이며 Redis task result로 성공을 판정하지 않는다.
- 외부 create 응답 유실 시 create를 반복하지 않고 결정적 remote lookup key로 reconcile한다.
- 원문·초안은 90일, 발행·감사 기록은 365일 보존한다.
- AGENTS.md에 따라 테스트, 빌드, Django check, migration check·실행과 외부 연동 검증은 사용자 승인 전 실행하지 않는다.
- task 완료는 체크박스가 아니라 코드, 계약 정합성, 리뷰 결과와 사용자 승인 범위의 검증 근거로 판단한다.

## 실행 규칙

1. 현재 작업 트리에는 T012 미커밋 변경이 있으므로 다른 task 구현 전에 T012를 마감한다.
2. 각 task 시작 시 `tasks.md` 요구사항과 실제 대상 파일의 현재 구현을 읽고 만족된 부분을 다시 작성하지 않는다.
3. migration은 기존 번호 다음 번호를 사용하고 과거 migration을 수정하지 않는다.
4. task별 변경은 별도 커밋으로 유지한다. 서로 다른 task의 모델 변경을 한 커밋에 섞지 않는다.
5. 검증 명령은 계획에 기록하지만 사용자 승인 전 실행하지 않는다.
6. T012는 3개 관점의 병렬 비판 리뷰를 2회 수행하고, 모든 지적은 메인 세션에서 재검증한 뒤 수용한다.
7. 후속 task는 구현 완료 후 fresh reviewer의 spec 적합성 검토와 코드 품질 검토를 거친다.

## 파일 책임 지도

| 경계 | 책임 파일 |
|---|---|
| 출처 정책·HTTP | `src/apps/topics/services.py`, `src/adapters/sources/http.py`, `src/adapters/sources/errors.py` |
| 수집 상태·계보 | `src/apps/collection/models.py`, `services.py`, `tasks.py` |
| 추출 프로필·증거 | `src/apps/evidence/models.py`, `profiles.py`, `services.py`, `tasks.py`, `src/adapters/extractors/` |
| 사건·초안·편집 | `src/apps/editorial/models.py`, `services.py`, `tasks.py`, `corrections.py` |
| 발행·원격 복구 | `src/apps/publishing/models.py`, `services.py`, `tasks.py`, `corrections.py` |
| 일정·중지 | `src/apps/scheduling/models.py`, `services.py`, `tasks.py` |
| 감사·보존 | `src/apps/audit/models.py`, `services.py`, `retention.py` |
| API·관리자 UI | 각 앱의 `api.py`, `urls.py`, `src/templates/admin_console/`, `src/static/admin_console/` |
| 계약 문서 | `specs/001-automated-content-publishing/contracts/`, `data-model.md`, `quickstart.md` |
| 출시 검증 | `tests/unit/`, `tests/contract/`, `tests/integration/`, `tests/e2e/` |

---

## Wave 0 — T012 출처 접근·권리 런타임 마감

### Task 1: T012 병렬 비판 리뷰 2회와 정책 런타임 보완

**Files:**
- Modify: `config/source-registry/housing_subscription.json`
- Modify: `config/source-registry/semiconductor_news.json`
- Modify: `src/adapters/sources/http.py`
- Modify: `src/adapters/sources/errors.py`
- Modify: `src/apps/topics/services.py`
- Modify: `src/apps/topics/tasks.py`
- Modify: `src/apps/collection/models.py`
- Modify: `src/apps/collection/services.py`
- Modify: `src/apps/collection/tasks.py`
- Modify: `src/apps/collection/api.py`
- Modify: `src/apps/evidence/tasks.py`
- Modify: `src/wisdome_writer/infrastructure/event_routes.py`
- Modify: `src/wisdome_writer/infrastructure/http_safety.py`
- Create/Finalize: `src/apps/collection/migrations/0008_source_access_policy_runtime.py`
- Modify: `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml`
- Modify: `specs/001-automated-content-publishing/contracts/job-events.md`
- Modify: `specs/001-automated-content-publishing/data-model.md`
- Modify: `specs/001-automated-content-publishing/quickstart.md`
- Deferred test specification: `tests/unit/test_source_access_policy.py`
- Deferred integration specification: `tests/integration/test_collection_pipeline.py`

**Interfaces:**
- Consumes: frozen `SourceDefinitionSnapshot`, `CollectionRun.topic_policy`, registry membership, `SourceAccessError`.
- Produces: `dispatch_collection_run(run)`, `collect_source_attempt(attempt_id, delivery_attempt_no)`, `finalize_collection_run_state(run)`, append-only `SourceCollectionObservation`.
- Failure contract: `SourceAccessError(code, category, detail, remediation, retryable, retry_after_seconds, http_status)`.

- [ ] **Step 1: 현재 T012 변경을 리뷰 기준선으로 고정한다.**

  `git diff --name-status 7d04126`과 `git diff --stat 7d04126`으로 대상 파일만 기록한다. 이 단계에서는 코드를 수정하지 않는다.

- [ ] **Step 2: 1차 리뷰를 세 관점으로 병렬 수행한다.**

  - Reviewer A: HTTP·SSRF·robots·redirect·MIME·rate limit·Retry-After·응답 크기
  - Reviewer B: source fanout·retry·terminal aggregation·migration·append-only·동시성
  - Reviewer C: access/rights/freshness/authority·API/OpenAPI·이벤트·문서

  각 reviewer는 심각도, 파일/행, 재현 가능한 코드 경로, 명세 근거를 반환하고 수정은 하지 않는다.

- [ ] **Step 3: 메인 세션에서 1차 지적을 재검증한다.**

  다음 판정 형식을 사용한다.

  ```text
  finding_id: R1-A-01
  verdict: accept | reject | narrow
  evidence: exact function/model/contract
  required_change: exact invariant or none
  ```

- [ ] **Step 4: 승인된 접근 오류를 evidence 실패 계약으로 변환한다.**

  `src/apps/evidence/tasks.py`의 첨부 다운로드 경계에서 `SourceAccessError`가 분류 없이 worker 밖으로 빠지지 않게 한다.

  ```python
  try:
      data, mime_type, filename = _download_attachment(run_source_item, attachment)
  except SourceAccessError as exc:
      raise ExtractorError(
          exc.code,
          exc.detail,
          retryable=exc.retryable,
      ) from exc
  ```

  영구 policy/schema/security 오류는 attachment 단위 실패로 기록하고, transient/infrastructure 오류만 승인된 delivery budget 안에서 재시도한다.

- [ ] **Step 5: append-only와 HTTP 계약의 확인된 차이를 수정한다.**

  `SourceCollectionObservation`에 instance 경로뿐 아니라 bulk 변경 차단 queryset/manager를 적용한다.

  ```python
  class SourceCollectionObservationQuerySet(models.QuerySet):
      def update(self, **kwargs):
          raise TypeError("SourceCollectionObservation is append-only")

      def delete(self):
          raise TypeError("SourceCollectionObservation is append-only")
  ```

  동시에 3xx without `Location`, OpenAPI nullable/enum, HTTP status 100~599 제약, freshness 제외 관측과 retry exhaustion의 durable 분류를 1차 판정 결과대로 맞춘다.

- [ ] **Step 6: 2차 리뷰를 새 컨텍스트의 세 reviewer로 반복한다.**

  1차 reviewer 결과를 정답으로 전달하지 않는다. 현재 코드와 spec/contracts만 제공해 독립 검토하게 한다.

- [ ] **Step 7: 2차 지적을 재검증하고 차단 항목을 닫는다.**

  CRITICAL/HIGH 항목은 모두 코드 또는 계약으로 해소한다. MEDIUM/LOW 항목을 보류할 경우 `tasks.md`의 기존 task가 추적하는지 명시하고, 추적되지 않을 때만 convergence task를 추가한다.

- [ ] **Step 8: T012 문서와 상태를 갱신한다.**

  `tasks.md`는 리뷰와 승인된 검증이 끝난 뒤에만 T012를 `[X]`로 바꾼다. `REMAINING_WORK.md`에는 한국어 구현 상태를 먼저, 영문 AI handoff를 뒤에 기록한다.

- [ ] **Step 9: 사용자에게 T012 검증 승인을 요청한다.**

  승인 후 실행할 명령과 기대 결과:

  ```powershell
  .\.venv\Scripts\python.exe src\manage.py check
  .\.venv\Scripts\python.exe src\manage.py makemigrations --check --dry-run
  .\.venv\Scripts\python.exe -m compileall -q src
  ```

  기대 결과는 Django error 0건, 새 migration drift 0건, compile error 0건이다.

- [ ] **Step 10: T012만 커밋한다.**

  ```powershell
  git add config/source-registry specs/001-automated-content-publishing src/adapters/sources src/apps/collection src/apps/evidence/tasks.py src/apps/topics src/wisdome_writer/infrastructure
  git commit -m "feat: enforce frozen source access policy runtime"
  ```

---

## Wave 1 — T013~T018 근거 기반 초안

### Task 2: T013 사건 cluster, 검증 결정과 canonical article identity

**Files:**
- Modify: `src/apps/editorial/models.py`
- Create: `src/apps/editorial/clustering.py`
- Create: `src/apps/editorial/migrations/0002_event_cluster_verification.py`
- Modify: `src/apps/editorial/services.py`
- Modify: `src/apps/editorial/tasks.py`
- Modify: `src/apps/collection/tasks.py`
- Modify: `config/editorial-policies/housing_subscription.json`
- Modify: `config/editorial-policies/semiconductor_news.json`
- Modify: `specs/001-automated-content-publishing/data-model.md`
- Modify: `specs/001-automated-content-publishing/contracts/job-events.md`
- Deferred test: `tests/unit/test_event_clustering.py`

**Interfaces:**
- Consumes: terminal `RunSourceItem` lineage, `SourceItem.external_id`, `SourceItem.metadata["originIdentity"]`, topic policy version/hash.
- Produces: `cluster_run_items(run_id) -> list[EventCluster]`, `verify_event_cluster(cluster_id) -> EventClusterVerification`, `article_identity_for_verification(verification) -> str`.
- Downstream: T018 consumes only the latest immutable verified decision, never mutable cluster fields.

- [ ] **Step 1: cluster membership와 검증 결정을 별도 모델로 추가한다.**

  ```python
  class EventClusterItem(models.Model):
      cluster = models.ForeignKey(EventCluster, on_delete=models.PROTECT, related_name="members")
      run_source_item = models.ForeignKey("collection.RunSourceItem", on_delete=models.PROTECT)
      origin_identity_hash = models.CharField(max_length=64)
      independence_group = models.CharField(max_length=120)
      role = models.CharField(max_length=24)
      selection_state = models.CharField(max_length=24)
      decision_reason = models.CharField(max_length=500)

  class EventClusterVerification(models.Model):
      cluster = models.ForeignKey(EventCluster, on_delete=models.PROTECT, related_name="verifications")
      version = models.PositiveIntegerField()
      decision = models.CharField(max_length=32)
      article_type = models.CharField(max_length=40)
      category = models.CharField(max_length=64)
      primary_source_count = models.PositiveIntegerField()
      independent_origin_count = models.PositiveIntegerField()
      decision_reason = models.CharField(max_length=500)
      policy_version = models.CharField(max_length=40)
      policy_hash = models.CharField(max_length=64)
      evidence_manifest_hash = models.CharField(max_length=64)
      supersedes = models.ForeignKey("self", null=True, on_delete=models.PROTECT)
  ```

  `(cluster, run_source_item)`와 `(cluster, version)` 고유 제약을 추가한다.

- [ ] **Step 2: 주택 canonical key를 공식 공고 identity로 만든다.**

  ```python
  def housing_cluster_key(item) -> str:
      material = {
          "authority": item.source_item.publisher,
          "noticeId": item.source_item.external_id,
          "correctionId": item.source_item.metadata.get("correction_id"),
      }
      return canonical_hash(material)
  ```

  제목만 같은 다른 공고는 병합하지 않고, 정정 ID는 동일 공고 계보 안에서 새 verification을 만든다.

- [ ] **Step 3: 반도체 사건과 independence group을 계산한다.**

  사건 key는 주체·행위·공식 발표 ID·KST 사건일을 사용한다. syndication/origin identity가 같은 자료는 독립 출처 한 곳으로 계산한다.

- [ ] **Step 4: 충돌 선택 순서를 정책으로 고정한다.**

  주택은 `latest correction document > authority detail > structured API > aggregator` 순서로 선택하고, 모든 excluded/conflicting member에 reason code를 남긴다.

- [ ] **Step 5: 반도체 중요 속보 조건을 구현한다.**

  ```python
  BREAKING_CATEGORIES = {
      "regulation_export_control",
      "factory_supply_disruption",
      "merger_or_material_earnings",
      "critical_technology_or_mass_production",
  }

  def breaking_decision(*, category: str, primary_count: int, independent_origin_count: int) -> str:
      if category not in BREAKING_CATEGORIES:
          return "daily_digest_candidate"
      return "verified_breaking" if primary_count >= 1 or independent_origin_count >= 2 else "held"
  ```

- [ ] **Step 6: article identity를 run UUID에서 verification으로 전환한다.**

  housing은 notice/correction identity, breaking은 cluster canonical key, daily digest는 `KST date + policy version` hash를 사용한다.

- [ ] **Step 7: collection 완료 이벤트를 clustering task로 연결한다.**

  `run.evidence_ready` 이후 `editorial.cluster_requested`를 만들고 cluster verification 완료 후에만 `editorial.generate_requested`를 발행한다.

- [ ] **Step 8: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/editorial src/apps/collection/tasks.py config/editorial-policies specs/001-automated-content-publishing
  git commit -m "feat: add verified event clustering and article identity"
  ```

### Task 3: T014 PaddleOCR model bootstrap과 profile material 고정

**Files:**
- Create: `deploy/containers/paddleocr-model-bootstrap/Dockerfile`
- Create: `deploy/containers/paddleocr-model-bootstrap/bootstrap.py`
- Modify: `deploy/containers/paddleocr-worker/Dockerfile`
- Modify: `compose.yaml`
- Modify: `.env.example`
- Modify: `config/extraction-profiles/paddleocr/paddle-ko-v1.json`
- Modify: `config/extraction-profiles/paddleocr/paddle-en-v1.json`
- Modify: `src/apps/evidence/profiles.py`
- Modify: `src/apps/evidence/management/commands/verify_ocr_manifest.py`
- Deferred contract test: `tests/contract/test_evidence_extractor.py`

**Interfaces:**
- Produces: `${PADDLEOCR_MODEL_MANIFEST_ROOT}/manifest.json` schema v1 with file-level SHA-256.
- Consumes: `verify_local_profile(profile)` and `PaddleOCRExtractor` model directory bindings.

- [ ] **Step 1: bootstrap output schema를 고정한다.**

  ```python
  manifest = {
      "schema_version": "v1",
      "paddleocr_version": "3.7.0",
      "pipeline": "PPStructureV3",
      "models": model_entries,
  }
  ```

  각 model entry는 `model_name`, absolute `directory`, 정렬된 `{path, sha256, byte_size}`를 포함한다.

- [ ] **Step 2: bootstrap container가 승인된 모델만 내려받고 manifest를 생성하게 한다.**

  모델 목록은 한국어/영어 recognition, layout, table, formula, chart와 orientation 구성 전체를 명시한다. 임의 최신 버전 해석을 금지한다.

- [ ] **Step 3: Compose에 완료형 bootstrap dependency와 read-only volume을 연결한다.**

  `ocr-model-bootstrap`은 `paddle-models` volume에 기록하고 `ocr-worker`는 `service_completed_successfully` 이후 같은 volume을 `:ro`로 읽는다.

- [ ] **Step 4: profile document가 실제 manifest root를 참조하게 한다.**

  `model_manifest_file`을 `${PADDLEOCR_MODEL_MANIFEST_ROOT}/manifest.json`으로 통일하고 zero hash나 누락 파일을 fail-closed 처리한다.

- [ ] **Step 5: extractor에 전체 model directory를 명시적으로 주입한다.**

  `PaddleOCRExtractor` 생성 시 profile model names를 PPStructureV3 constructor option에 1:1 매핑하고 runtime download option을 비활성화한다.

- [ ] **Step 6: 사용자 승인 후 offline manifest 검증을 수행하고 커밋한다.**

  ```powershell
  git add deploy/containers/paddleocr-model-bootstrap deploy/containers/paddleocr-worker compose.yaml .env.example config/extraction-profiles/paddleocr src/apps/evidence
  git commit -m "feat: pin PaddleOCR deployment model manifests"
  ```

### Task 4: T015 legacy HWP 격리 converter

**Files:**
- Create: `deploy/containers/hwp-worker/Dockerfile`
- Create: `deploy/containers/hwp-worker/wisdome-hwp-sandbox`
- Create: `deploy/containers/hwp-worker/build_manifest.py`
- Modify: `compose.yaml`
- Modify: `config/extraction-profiles/generic/legacy-hwp-v1.json`
- Modify: `src/adapters/extractors/legacy_hwp.py`
- Modify: `src/apps/evidence/profiles.py`
- Deferred contract test: `tests/contract/test_evidence_extractor.py`

**Interfaces:**
- Consumes: `{input}`, `{output}`, `{report}` arguments from `LegacyHwpConverter`.
- Produces: validated PDF and `conversion-report.json` containing input/output checksum, page count, converter manifest hash and sandbox policy.

- [ ] **Step 1: wrapper argument와 exit code 계약을 구현한다.**

  ```text
  wisdome-hwp-sandbox --network=none --input /input/a.hwp --output /output/a.pdf --report /output/report.json
  exit 0: valid PDF and report
  exit 20: invalid input
  exit 21: converter failure
  exit 22: resource limit
  ```

- [ ] **Step 2: container 격리를 구성한다.**

  read-only rootfs/input, tmpfs output, no network, non-root UID, `pids_limit`, memory/CPU/time/file-size 한도를 Compose와 wrapper 양쪽에 적용한다.

- [ ] **Step 3: converter 실제 bytes manifest를 생성한다.**

  manifest schema v1에 converter binary, shared library, wrapper와 config의 SHA-256을 기록하고 `legacy-hwp-v1.json`의 zero hash를 실제 manifest path/hash로 교체한다.

- [ ] **Step 4: 결과 report를 더 엄격히 검증한다.**

  `LegacyHwpConverter.extract()`는 report의 `converter_manifest_hash`, `sandbox_policy.network_allowed == false`, input/output checksum과 PDF MIME을 모두 확인한다.

- [ ] **Step 5: 변환 성공 PDF만 document extraction으로 전달한다.**

  partial text나 converter stdout은 evidence로 만들지 않고 `follow_up_engine_allowlist`의 native PDF/PaddleOCR만 허용한다.

- [ ] **Step 6: 사용자 승인 후 sandbox contract를 검증하고 커밋한다.**

  ```powershell
  git add deploy/containers/hwp-worker compose.yaml config/extraction-profiles/generic/legacy-hwp-v1.json src/adapters/extractors/legacy_hwp.py src/apps/evidence/profiles.py
  git commit -m "feat: isolate legacy HWP conversion sandbox"
  ```

### Task 5: T016 extraction uniqueness, fencing과 terminal finalizer

**Files:**
- Modify: `src/apps/evidence/models.py`
- Modify: `src/apps/evidence/services.py`
- Modify: `src/apps/evidence/tasks.py`
- Create: `src/apps/evidence/migrations/0004_extraction_runtime_fencing.py`
- Modify: `src/wisdome_writer/infrastructure/event_routes.py`
- Deferred integration test: `tests/integration/test_collection_pipeline.py`

**Interfaces:**
- Consumes: `DocumentExtraction.input_fingerprint`, `ExtractionRun`, `GenericExtractionAttempt`, approved profile snapshot.
- Produces: one selected child result per page, fenced delivery observations, exactly one `evidence.document_ready` or `evidence.other_ready` terminal event.

- [ ] **Step 1: generation/lease 필드를 모델에 추가한다.**

  ```python
  lease_generation = models.PositiveIntegerField(default=1)
  delivery_count = models.PositiveIntegerField(default=0)
  next_retry_at = models.DateTimeField(null=True, blank=True)
  terminal_event_key = models.CharField(max_length=255, blank=True)
  ```

  활성 상태의 동일 fingerprint가 하나만 존재하도록 conditional unique constraint를 추가한다.

- [ ] **Step 2: begin 함수가 row lock과 expected generation을 사용하게 한다.**

  ```python
  def begin_document_extraction(document_id, *, expected_generation: int):
      document = DocumentExtraction.objects.select_for_update().get(id=document_id)
      if document.lease_generation != expected_generation:
          raise EvidenceConflict("stale extraction generation")
      return document
  ```

- [ ] **Step 3: 예상 밖 예외도 terminal projection을 남기게 한다.**

  task 외부의 broad exception 경계에서 redacted code를 `failed`로 저장하고 finalizer wake event를 같은 transaction의 Outbox로 만든다.

- [ ] **Step 4: child 결과와 page coverage를 원자적으로 집계한다.**

  `aggregate_document_extraction()`은 selected child의 page index 합집합이 `expected_page_indices`와 정확히 같을 때만 succeeded/low_confidence로 종결한다.

- [ ] **Step 5: stop hook와 duplicate delivery를 연결한다.**

  worker 시작 전과 외부 추출 직전에 `CollectionRun.stop_requested_at`을 다시 확인하고, 같은 source event replay는 기존 terminal 결과를 반환한다.

- [ ] **Step 6: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/evidence src/wisdome_writer/infrastructure/event_routes.py
  git commit -m "feat: fence extraction attempts and terminal aggregation"
  ```

### Task 6: T017 extraction routing과 parser fail-closed 경계

**Files:**
- Modify: `src/apps/evidence/tasks.py`
- Modify: `src/apps/evidence/services.py`
- Create: `src/adapters/extractors/manual.py`
- Modify: `src/adapters/extractors/browser_capture.py`
- Modify: `src/adapters/extractors/media.py`
- Modify: `src/adapters/extractors/hwpx.py`
- Modify: `src/adapters/extractors/native_pdf/adapter.py`
- Modify: `src/adapters/storage/s3.py`
- Modify: `specs/001-automated-content-publishing/contracts/evidence-extractor.md`
- Modify: `specs/001-automated-content-publishing/contracts/generic-extractor.md`
- Deferred contract test: `tests/contract/test_evidence_extractor.py`

**Interfaces:**
- Consumes: approved extraction profile and bounded raw input stream.
- Produces: `EvidenceAsset` with engine-specific locator, confidence semantics and immutable S3 object version.

- [ ] **Step 1: profile-to-engine routing table을 한 곳에 고정한다.**

  ```python
  GENERIC_ENGINE_FACTORIES = {
      "html_parser": HtmlExtractor,
      "structured_parser": StructuredExtractor,
      "spreadsheet_parser": SpreadsheetExtractor,
      "hwpx_parser": HwpxExtractor,
      "browser_capture": BrowserCaptureExtractor,
      "media_parser": MediaExtractor,
      "manual_entry": ManualEntryExtractor,
  }
  ```

  계약에 있으나 실행되지 않는 profile이 없도록 하고, 지원하지 않을 엔진은 manifest와 계약에서 함께 제거한다.

  `ManualEntryExtractor`는 관리자가 생성한 JSON 입력의 text, source URL, locator, rights decision과 actor를 검증하고 `validation_mode=manual` evidence만 생성한다.

  ```python
  class ManualEntryExtractor:
      engine = "manual_entry"

      def extract(self, path: Path) -> GenericExtractionOutput:
          payload = json.loads(path.read_text(encoding="utf-8"))
          record = validate_manual_evidence_payload(payload)
          return GenericExtractionOutput(
              engine=self.engine,
              extractor_version="manual-entry-v1",
              validation_mode="manual",
              records=[record],
              metadata={"actor_id": payload["actorId"]},
          )
  ```

- [ ] **Step 2: HWPX/XML parser를 fail-closed로 만든다.**

  external entity, DTD, path traversal, duplicate ZIP entry, 압축 비율·entry 수·총 크기 초과를 입력 거부로 분류한다.

- [ ] **Step 3: PDF와 image decoder 한도를 강제한다.**

  page count, dimension, pixel count, frame count, decompressed byte와 render time 한도를 검사하며 다중 frame 이미지는 거부한다.

- [ ] **Step 4: S3 read를 object version과 bounded stream으로 제한한다.**

  `S3ObjectStorage.get_bytes()`의 무제한 read 대신 `get_bounded_bytes(key, version_id, max_bytes)`를 만들고 추출 task가 frozen object version만 읽게 한다.

- [ ] **Step 5: locator와 confidence 규칙을 강제한다.**

  모든 evidence는 locator type별 필수 필드를 검증한다. 숫자 confidence는 calibrated profile에만 허용하고 deterministic extractor는 pass/fail validation만 기록한다.

- [ ] **Step 6: 사용자 승인 후 extractor 계약을 검증하고 커밋한다.**

  ```powershell
  git add src/apps/evidence src/adapters/extractors src/adapters/storage/s3.py specs/001-automated-content-publishing/contracts
  git commit -m "feat: close generic extraction routing and safety gaps"
  ```

### Task 7: T018 불변 editorial policy와 차단형 품질 gate

**Files:**
- Modify: `src/apps/editorial/models.py`
- Create: `src/apps/editorial/policies.py`
- Modify: `src/apps/editorial/services.py`
- Modify: `src/apps/editorial/tasks.py`
- Modify: `src/apps/editorial/api.py`
- Create: `src/apps/editorial/migrations/0003_editorial_policy_runtime.py`
- Modify: `src/adapters/generators/base.py`
- Modify: `src/adapters/generators/template.py`
- Modify: `src/wisdome_writer/settings/__init__.py`
- Modify: `config/editorial-policies/housing_subscription.json`
- Modify: `config/editorial-policies/semiconductor_news.json`
- Modify: `src/templates/admin_console/`
- Modify: `src/static/admin_console/`
- Deferred unit test: `tests/unit/test_editorial_policy.py`

**Interfaces:**
- Consumes: latest `EventClusterVerification`, publishable `EvidenceAsset`, locator/rights/freshness/authority material.
- Produces: `ArticleRevision` with frozen editorial policy version/hash, typed `Claim`, `ClaimEvidence`, `QualityCheck`, `VisualizationRender`/placement material.

- [ ] **Step 1: generation과 revision에 policy material을 고정한다.**

  ```python
  editorial_policy_version = models.CharField(max_length=40)
  editorial_policy_hash = models.CharField(max_length=64)
  event_verification = models.ForeignKey(EventClusterVerification, on_delete=models.PROTECT)
  evidence_manifest_hash = models.CharField(max_length=64)
  ```

- [ ] **Step 2: policy loader를 RFC 8785 canonical hash로 구현한다.**

  ```python
  @dataclass(frozen=True)
  class EditorialPolicy:
      document: Mapping[str, Any]
      material_hash: str

  def load_editorial_policy(topic_code: str) -> EditorialPolicy:
      path = Path(settings.EDITORIAL_POLICY_ROOT) / f"{topic_code}.json"
      document = json.loads(path.read_text("utf-8"))
      return EditorialPolicy(document=document, material_hash=canonical_hash(document))
  ```

- [ ] **Step 3: generator output을 typed claim으로 바꾼다.**

  generator 계약은 `fact`, `company_claim`, `interpretation`, `outlook` 중 하나와 evidence IDs를 필수로 반환한다. 회사 발표만 근거인 문장을 fact로 승격하지 않는다.

- [ ] **Step 4: 차단형 품질 검사를 구현한다.**

  최소 code 집합은 `source_coverage`, `claim_type_separation`, `high_impact_locator`, `independence`, `freshness`, `rights`, `citation_length`, `duplicate_conflict`, `readability`, `exaggeration`이다. 하나라도 failed면 `quality_state=failed`다.

- [ ] **Step 5: visual placement와 provenance를 revision에 연결한다.**

  각 visual은 block ID, 본문 위치, source evidence, rights status, alt text, caption, presentation hash를 가진다. 게시 가능한 rights와 locator가 없으면 quality gate가 실패한다.

- [ ] **Step 6: 수동 개정 재검증 task/API를 연결한다.**

  `create_manual_revision()`은 pending revision만 만들고 `editorial.revalidate_requested` Outbox를 생성한다. 재검증 성공 전 preview/approval/publish가 revision을 선택하지 못하게 한다.

- [ ] **Step 7: 관리자 화면에 claim·evidence·제외 사유를 표시한다.**

  article detail에서 claim type, linked evidence locator, source URL, rights/freshness, excluded item reason과 blocking quality result를 조회한다.

- [ ] **Step 8: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/editorial src/adapters/generators config/editorial-policies src/templates/admin_console src/static/admin_console
  git commit -m "feat: enforce immutable editorial quality policy"
  ```

---

## Wave 2 — T019~T026 안전 발행

### Task 8: T019 target별 승인 latest-decision projection

**Files:**
- Modify: `src/apps/publishing/models.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/api.py`
- Modify: `src/apps/publishing/tasks.py`
- Create: `src/apps/publishing/migrations/0008_approval_projection.py`
- Modify: `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml`
- Deferred unit test: `tests/unit/test_publication_approval.py`

**Interfaces:**
- Consumes: `PublicationIntent`, target snapshot, revision quality hash, reauthentication proof.
- Produces: immutable `Approval` decisions and one CAS-controlled `PublicationApprovalHead` per intent/target.
- Gate: approve/reject/revoke is rechecked at intent dispatch and attempt execution.

- [ ] **Step 1: latest approval projection 모델을 추가한다.**

  ```python
  class PublicationApprovalHead(models.Model):
      publication_intent = models.ForeignKey(PublicationIntent, on_delete=models.PROTECT)
      target = models.ForeignKey(PublicationTarget, on_delete=models.PROTECT)
      latest_approval = models.ForeignKey(Approval, on_delete=models.PROTECT)
      version = models.PositiveIntegerField()
      subject_hash = models.CharField(max_length=64)

      class Meta:
          constraints = [models.UniqueConstraint(
              fields=["publication_intent", "target"], name="uq_intent_target_approval_head"
          )]
  ```

- [ ] **Step 2: approve/reject/revoke를 append-only decision + head CAS transaction으로 통합한다.**

  `decide_approval()`은 `expectedLatestApprovalId`와 `expectedVersion`을 확인하고, decision·head·AuditEvent를 한 transaction에 저장한다. 같은 request key와 payload replay는 같은 `Approval`을 반환한다.

- [ ] **Step 3: revoke/reject가 모든 쓰기 gate를 즉시 차단하게 한다.**

  ```python
  def current_approval(intent, target):
      head = PublicationApprovalHead.objects.select_related("latest_approval").get(
          publication_intent=intent, target=target
      )
      return head.latest_approval
  ```

  `dispatch_publication()`과 `validate_attempt_gate()`는 attempt에 저장된 과거 approval만 보지 않고 현재 head가 같은 approved subject인지 확인한다.

- [ ] **Step 4: OpenAPI 응답에 head version과 supersedes를 반영한다.**

  nullable, UUID, enum과 409 stale CAS problem response를 실제 API와 일치시킨다.

- [ ] **Step 5: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/publishing specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml
  git commit -m "feat: add publication approval decision projection"
  ```

### Task 9: T020 publication intent와 target attempt 멱등성

**Files:**
- Modify: `src/apps/publishing/models.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/api.py`
- Create: `src/apps/publishing/migrations/0009_intent_request_identity.py`
- Modify: `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml`
- Deferred unit test: `tests/unit/test_publication_intent_idempotency.py`

**Interfaces:**
- Consumes: approved `ArticleRevision`, exact target snapshot refs, commands, request key and payload hash.
- Produces: one `PublicationIntent` replay identity and exactly one first-generation `PublicationAttempt` per target.

- [ ] **Step 1: idempotency lookup을 mutable CAS 검사보다 먼저 이동한다.**

  ```python
  def find_intent_replay(*, article_revision, request_key, request_hash):
      existing = PublicationIntent.objects.filter(
          article_revision=article_revision,
          request_key=request_key,
      ).first()
      if existing and existing.request_hash != request_hash:
          raise Conflict("request key reused with different publication payload")
      return existing
  ```

  `PublicationIntent.request_hash`를 non-null로 추가하고 migration에서 기존 `intent_hash`로 backfill한다.

- [ ] **Step 2: target ID와 command 집합을 canonicalize한다.**

  target ID 중복, 존재하지 않는 target, snapshot/command 집합 불일치를 payload hash 계산 전에 거부한다. target 정렬은 UUID 문자열 오름차순으로 고정한다.

- [ ] **Step 3: intent 생성과 attempt 생성을 한 transaction에 묶는다.**

  `(publication_intent, target_snapshot, attempt_no=1)` 고유 제약을 추가하고 `bulk_create(ignore_conflicts=True)`를 사용하지 않는다.

- [ ] **Step 4: API replay status를 명확히 한다.**

  최초 생성은 201, 동일 replay는 200과 같은 intent ID, 다른 payload는 409를 반환한다.

- [ ] **Step 5: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/publishing specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml
  git commit -m "fix: enforce publication intent request idempotency"
  ```

### Task 10: T021 publication attempt lease, generation과 terminal aggregation

**Files:**
- Modify: `src/apps/publishing/models.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/tasks.py`
- Modify: `src/adapters/publishers/wordpress/client.py`
- Modify: `src/adapters/publishers/blogger/client.py`
- Create: `src/apps/publishing/migrations/0010_publication_attempt_fencing.py`
- Modify: `specs/001-automated-content-publishing/contracts/publisher-adapter.md`
- Deferred contract test: `tests/contract/test_publishers.py`

**Interfaces:**
- Consumes: current approval head, target snapshot/config hash, publisher adapter manifest, kill switch.
- Produces: fenced `PublicationExecutionObservation`, bounded `PublicationReconcileGeneration`, terminal `Publication` projection.

- [ ] **Step 1: attempt lease generation을 추가한다.**

  ```python
  lease_generation = models.PositiveIntegerField(default=1)
  lease_token_hash = models.CharField(max_length=64, blank=True)
  lease_expires_at = models.DateTimeField(null=True, blank=True)
  ```

  `begin_attempt()`은 row lock 안에서 generation을 증가시키고 worker event의 lease token hash를 고정한다.

- [ ] **Step 2: 외부 쓰기 직전 gate를 한 번 더 확인한다.**

  `validate_attempt_gate()`는 kill switch, current approval head, revision quality, target snapshot, activation, WordPress dependency와 lease generation을 모두 확인한다.

- [ ] **Step 3: late response가 최신 상태를 덮지 못하게 한다.**

  ```python
  if attempt.lease_generation != expected_generation:
      return attempt  # persist only an append-only stale observation
  ```

  stale worker 결과는 `PublicationExecutionObservation.result_state=stale`로 남기고 `Publication`을 변경하지 않는다.

- [ ] **Step 4: retry와 reconcile generation 한도를 단일 규칙으로 통합한다.**

  retryable failure는 새 attempt number, unknown outcome은 reconcile generation을 사용한다. 최대 5회 후 `manual_required`로 종결하며 create를 다시 실행하지 않는다.

- [ ] **Step 5: adapter remote lookup contract를 확인한다.**

  WordPress는 결정적 slug/marker, Blogger는 label/HTML marker로 정확히 1건을 조회한다. 0건 또는 복수건은 자동 create가 아니라 manual required다.

- [ ] **Step 6: publication terminal projection과 dependent release를 원자적으로 처리한다.**

  succeeded/permanent_failed/manual_required/stale 모든 경로에서 duration, terminal impact, recovery state와 dependent Blogger 상태를 갱신한다.

- [ ] **Step 7: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/publishing src/adapters/publishers specs/001-automated-content-publishing/contracts/publisher-adapter.md
  git commit -m "fix: fence publication attempts and remote reconciliation"
  ```

### Task 11: T022 visual placement와 published evidence snapshot

**Files:**
- Modify: `src/apps/editorial/models.py`
- Create: `src/apps/editorial/migrations/0004_visual_placement.py`
- Modify: `src/apps/publishing/models.py`
- Create: `src/apps/publishing/migrations/0011_published_asset_snapshots.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/tasks.py`
- Modify: `src/adapters/storage/s3.py`
- Modify: `src/adapters/publishers/wordpress/client.py`
- Deferred integration test: `tests/integration/test_published_asset_snapshots.py`

**Interfaces:**
- Consumes: approved revision visual placement, `EvidenceAsset`, `VisualizationRender`, rights/alt/caption material.
- Produces: immutable `PublishedEvidenceSnapshot`, `PublishedVisualizationSnapshot`, channel delivery binding and media manifest.

- [ ] **Step 1: revision visual placement 모델을 추가한다.**

  ```python
  class VisualPlacement(models.Model):
      revision = models.ForeignKey(ArticleRevision, on_delete=models.PROTECT, related_name="visual_placements")
      block_id = models.CharField(max_length=255)
      source_evidence = models.ForeignKey("evidence.EvidenceAsset", null=True, on_delete=models.PROTECT)
      visualization = models.ForeignKey(VisualizationRender, null=True, on_delete=models.PROTECT)
      usage = models.CharField(max_length=16)
      display_order = models.PositiveIntegerField()
      presentation_hash = models.CharField(max_length=64)
  ```

  evidence/visualization XOR와 `(revision, block_id)` 고유 제약을 둔다.

- [ ] **Step 2: 발행 snapshot 모델을 추가한다.**

  `PublishedEvidenceSnapshot`은 evidence content hash, locator, source URL, rights, alt/caption/attribution, object key/version을 저장한다. `PublishedVisualizationSnapshot`은 render checksum, transform/input manifest, rights/alt/caption과 object version을 저장한다.

- [ ] **Step 3: intent 생성 시 revision의 media manifest를 동결한다.**

  `_create_preview_render()`가 mutable EvidenceAsset을 직접 읽지 않고 snapshot IDs와 presentation hash를 `media_manifest`에 기록한다.

- [ ] **Step 4: WordPress media와 Blogger public delivery를 각각 준비한다.**

  WordPress는 `(target, asset_checksum, presentation_hash)` RemoteMedia를 사용한다. Blogger는 immutable PublicDeliveryAsset URL을 사용하고 active reference count를 publication binding과 같은 transaction에서 관리한다.

- [ ] **Step 5: lease generation CAS와 orphan cleanup을 연결한다.**

  media upload/deletion callback이 expected lease generation과 일치할 때만 상태를 변경한다. 활성 `PublicationMedia`가 0인 자산만 grace period 이후 삭제한다.

- [ ] **Step 6: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/editorial src/apps/publishing src/adapters/storage/s3.py src/adapters/publishers/wordpress/client.py
  git commit -m "feat: freeze published evidence and visual assets"
  ```

### Task 12: T023 Blogger OAuth refresh와 credential disconnect

**Files:**
- Modify: `src/wisdome_writer/infrastructure/secrets.py`
- Modify: `src/adapters/publishers/blogger/oauth.py`
- Modify: `src/adapters/publishers/blogger/client.py`
- Modify: `src/adapters/publishers/wordpress/client.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/tasks.py`
- Modify: `src/apps/publishing/api.py`
- Modify: `.env.example`
- Deferred contract test: `tests/contract/test_publishers.py`

**Interfaces:**
- Consumes: credential reference only; raw token values remain in the configured secret provider.
- Produces: scope-verified Blogger token bundle, refresh rotation, revoke result and disconnected target snapshot.

- [ ] **Step 1: secret provider token bundle contract를 고정한다.**

  ```python
  class OAuthTokenBundle(TypedDict):
      access_token: str
      refresh_token: str
      token_type: str
      scope: list[str]
      expires_at: str
      version: str
  ```

  DB와 API에는 bundle reference와 version만 저장한다.

- [ ] **Step 2: authorization code exchange에서 scope와 session binding을 검증한다.**

  OAuth state의 admin ID, session hash, target snapshot ID, nonce와 expiry를 검증하고 Blogger publish scope가 없으면 연결하지 않는다.

- [ ] **Step 3: refresh를 single-flight로 구현한다.**

  secret version CAS를 사용해 동시에 만료를 감지한 worker 중 하나만 refresh하고, 나머지는 새 version을 다시 읽는다. refresh token rotation이 있으면 같은 operation에서 교체한다.

- [ ] **Step 4: disconnect를 remote revoke 후 local projection으로 처리한다.**

  remote revoke unknown outcome은 target을 disconnected로 확정하지 않고 manual required로 남긴다. 성공 후 credential ref를 새 immutable target snapshot에서 제거한다.

- [ ] **Step 5: WordPress disconnect도 remote capability 확인을 기록한다.**

  Application Password 폐기 결과를 확인할 수 없으면 credential ref를 조용히 삭제하지 않고 reconciliation 상태를 남긴다.

- [ ] **Step 6: 로그·problem response·AuditEvent redaction을 확인하고 커밋한다.**

  ```powershell
  git add src/wisdome_writer/infrastructure/secrets.py src/adapters/publishers src/apps/publishing .env.example
  git commit -m "feat: complete publisher credential lifecycle"
  ```

### Task 13: T024 frozen WordPress canonical URL과 Blogger dependency

**Files:**
- Modify: `src/apps/publishing/models.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/automation.py`
- Modify: `src/apps/publishing/corrections.py`
- Modify: `src/apps/publishing/tasks.py`
- Create: `src/apps/publishing/migrations/0012_publication_dependency.py`
- Deferred contract test: `tests/contract/test_publishers.py`

**Interfaces:**
- Consumes: target refs frozen on `PublicationIntent`, WordPress `Publication.remote_url` and public verification timestamp.
- Produces: explicit Blogger attempt dependency and final render with the exact WordPress URL.

- [ ] **Step 1: attempt dependency를 모델에 명시한다.**

  ```python
  depends_on_attempt = models.ForeignKey(
      "self", null=True, blank=True, on_delete=models.PROTECT, related_name="dependent_attempts"
  )
  dependency_subject_hash = models.CharField(max_length=64, blank=True)
  ```

- [ ] **Step 2: WordPress attempt가 성공·공개 확인된 뒤 Blogger를 release한다.**

  `_wordpress_dependency_ready()`는 `remote_state=published`, `canonical_ready_at`, intent의 exact primary target snapshot과 public URL hash를 확인한다.

- [ ] **Step 3: Blogger final render를 frozen URL로 생성한다.**

  mutable target 또는 다른 WordPress publication을 검색하지 않고 dependency attempt의 publication ID와 URL만 사용한다. test target URL은 production intent에 결합하지 않는다.

- [ ] **Step 4: correction dependency도 같은 순서를 사용한다.**

  update/mark-withdrawn/unpublish 모두 WordPress terminal result 후 Blogger command를 release한다.

- [ ] **Step 5: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/publishing
  git commit -m "fix: freeze WordPress canonical publication dependency"
  ```

### Task 14: T025 서버 유도형 auto-publish validation과 activation

**Files:**
- Modify: `src/apps/publishing/models.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/tasks.py`
- Modify: `src/apps/publishing/api.py`
- Modify: `src/apps/evidence/profiles.py`
- Modify: `src/apps/topics/services.py`
- Modify: `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml`
- Deferred integration test: `tests/integration/test_auto_publish_activation.py`

**Interfaces:**
- Consumes: approved registry/profile/editorial policy, generator manifest, publisher adapter manifest, credential version, preflight/canary/pilot evidence.
- Produces: immutable `AutoPublishValidation` and `AutoPublishActivation` hashes derived only from server state.

- [ ] **Step 1: validation material builder를 서버 조회형으로 만든다.**

  ```python
  def build_auto_publish_validation_material(target: PublicationTarget, topic_code: str) -> dict:
      return {
          "targetSnapshot": snapshot_ref(target.current_snapshot),
          "registry": approved_registry_ref(topic_code),
          "profiles": approved_profile_refs(),
          "editorialPolicy": approved_editorial_policy_ref(topic_code),
          "publisher": publisher_manifest_ref(target.channel_code),
          "credentialVersion": resolved_credential_version(target),
      }
  ```

  API client가 hash나 implementation version을 직접 공급하지 못하게 한다.

- [ ] **Step 2: canary와 pilot evidence를 validation에 결합한다.**

  test target canary의 create→update→media→public verify→withdraw/delete와 cleanup object IDs를 저장한다. production pilot는 관리자 승인 게시와 공개 URL 확인 결과를 참조한다.

- [ ] **Step 3: activation freshness와 revocation을 강제한다.**

  registry/profile/policy/target/credential material 중 하나라도 바뀌면 기존 validation과 activation을 stale로 만들고 실행 직전 gate가 거부한다.

- [ ] **Step 4: validation/activation decision을 재인증·CAS·감사 transaction으로 유지한다.**

  existing service의 expected latest decision/activation, request replay와 AuditEvent를 서버 유도 material hash 기준으로 맞춘다.

- [ ] **Step 5: 사용자 승인 후 check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/publishing src/apps/evidence/profiles.py src/apps/topics/services.py specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml
  git commit -m "fix: derive auto-publish gates from approved server material"
  ```

### Task 15: T026 발행 API·관리자 UI 계약 완성

**Files:**
- Modify: `src/apps/publishing/api.py`
- Modify: `src/apps/publishing/urls.py`
- Modify: `src/apps/publishing/console_urls.py`
- Modify: `src/templates/admin_console/publishing/`
- Modify: `src/static/admin_console/`
- Modify: `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml`
- Deferred E2E test: `tests/e2e/test_admin_journey.py`

**Interfaces:**
- Consumes: T019~T025 service functions only; views do not mutate models directly.
- Produces: OpenAPI-aligned target/OAuth/preflight/canary/validation/activation/preview/approval/dispatch/retry/reconcile/disconnect operations.

- [ ] **Step 1: OpenAPI operation과 URL/view 매핑 표를 만든다.**

  각 operation ID에 정확히 하나의 URL과 view가 있는지 확인하고 누락 path를 추가한다. 문서에 있으나 지원하지 않는 operation은 삭제하지 말고 T019~T025 service로 연결한다.

- [ ] **Step 2: request/response schema를 실제 serializer payload와 맞춘다.**

  UUID, enum, nullable, `additionalProperties: false`, cursor pagination, 201/200 replay, 409 CAS와 problem response를 공통 API 검증 계층으로 통과시킨다.

- [ ] **Step 3: 관리자 target 화면을 완성한다.**

  connection state, credential version, preflight, canary, pilot, validation, activation, disconnect/revoke 상태와 안전한 다음 action을 표시한다.

- [ ] **Step 4: article publish 화면을 완성한다.**

  WordPress/Blogger preview, pending canonical link, claim/evidence/visual manifest, approval head, attempt/reconcile 상태와 target별 retry만 제공한다.

- [ ] **Step 5: 고위험 action에 재인증 proof를 연결한다.**

  auto-publish enable/disable, approval revoke, manual retry, disconnect는 중앙 reauthentication service를 사용한다.

- [ ] **Step 6: 사용자 승인 후 API schema/E2E 검증을 수행하고 커밋한다.**

  ```powershell
  git add src/apps/publishing src/templates/admin_console/publishing src/static/admin_console specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml
  git commit -m "feat: complete publication administration contracts"
  ```

---

## Wave 3 — T027~T031 운영 수명주기

### Task 16: T027 immutable ScheduleDispatch와 queue-one locking

**Files:**
- Modify: `src/apps/scheduling/models.py`
- Modify: `src/apps/scheduling/services.py`
- Modify: `src/apps/scheduling/tasks.py`
- Modify: `src/apps/publishing/automation.py`
- Create: `src/apps/scheduling/migrations/0003_schedule_dispatch_material.py`
- Modify: `specs/001-automated-content-publishing/data-model.md`
- Deferred integration test: `tests/integration/test_scheduling_operations.py`

**Interfaces:**
- Consumes: exact schedule version, topic registry/policy, target snapshots, approval mode, validation/activation refs at tick time.
- Produces: immutable `ScheduleDispatch` material and one `CollectionRun` request fingerprint per scheduled tick.

- [ ] **Step 1: dispatch에 실행 material을 추가한다.**

  ```python
  schedule_material = models.JSONField(default=dict)
  schedule_material_hash = models.CharField(max_length=64)
  registry_snapshot_id = models.UUIDField()
  registry_manifest_hash = models.CharField(max_length=64)
  target_snapshot_refs = models.JSONField(default=list)
  approval_mode_snapshot = models.CharField(max_length=20)
  validation_refs = models.JSONField(default=list)
  activation_refs = models.JSONField(default=list)
  ```

- [ ] **Step 2: tick transaction에서 material을 서버 조회해 고정한다.**

  `_dispatch_material()`은 mutable Schedule JSON만 복사하지 않고 현재 approved registry와 exact target snapshots/activation을 조회한 뒤 RFC 8785 hash를 만든다.

- [ ] **Step 3: duplicate tick와 queue-one을 row lock으로 결정한다.**

  schedule row와 active run/queued dispatch를 `select_for_update()`로 잠그고 `(schedule, scheduled_for)` replay는 기존 dispatch를 반환한다. database integrity error를 정상 제어 흐름으로 사용하지 않는다.

- [ ] **Step 4: automation의 mutable schedule reread를 제거한다.**

  `dispatch_scheduled_run_publication()`은 `ScheduleDispatch.schedule_material`과 run에 동결된 refs만 사용한다.

- [ ] **Step 5: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/scheduling src/apps/publishing/automation.py specs/001-automated-content-publishing/data-model.md
  git commit -m "feat: freeze scheduled execution material"
  ```

### Task 17: T028 stop, selective retry와 run terminal aggregation

**Files:**
- Modify: `src/apps/collection/models.py`
- Modify: `src/apps/collection/services.py`
- Modify: `src/apps/collection/tasks.py`
- Modify: `src/apps/evidence/tasks.py`
- Modify: `src/apps/editorial/tasks.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/tasks.py`
- Modify: `src/apps/scheduling/services.py`
- Create: `src/apps/collection/migrations/0009_run_control_decision.py`
- Modify: `specs/001-automated-content-publishing/contracts/job-events.md`
- Deferred integration test: `tests/integration/test_scheduling_operations.py`

**Interfaces:**
- Consumes: run state, kill switch version, terminal steps/attempts/channels, retry request key.
- Produces: append-only `RunControlDecision`, cancelled/stopped projections, selective retry Outbox and final run state.

- [ ] **Step 1: run 제어 결정을 append-only로 추가한다.**

  ```python
  class RunControlDecision(models.Model):
      run = models.ForeignKey(CollectionRun, on_delete=models.PROTECT, related_name="control_decisions")
      action = models.CharField(max_length=24)
      scope = models.JSONField(default=dict)
      request_key = models.CharField(max_length=200)
      request_hash = models.CharField(max_length=64)
      reauth_proof_id = models.UUIDField(null=True)
      decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
  ```

  `(run, request_key)` 고유 제약으로 stop/retry replay를 멱등 처리한다.

- [ ] **Step 2: 모든 worker가 외부 작업 전에 stop을 다시 확인한다.**

  collection, evidence, editorial, publishing의 task entry와 외부 HTTP/CPU 작업 직전에 `stop_requested_at`과 global kill switch를 확인한다.

- [ ] **Step 3: queued Outbox와 attempt를 안전하게 취소한다.**

  아직 lease되지 않은 메시지는 cancelled terminal observation을 남기고, 실행 중 worker는 결과를 보존하되 다음 fanout을 만들지 않는다.

- [ ] **Step 4: run/step/channel terminal 집계를 하나의 service로 통합한다.**

  ```python
  @dataclass(frozen=True)
  class RunTerminalProjection:
      state: str
      completed_at: datetime
      error_summary: dict | None
      terminal_impact: dict

  TERMINAL_STEP_STATES = {"succeeded", "failed", "stopped"}
  FAILED_REQUIRED_STEP_STATES = {"failed"}
  TERMINAL_SOURCE_STATES = {
      SourceCollectionAttemptState.SUCCEEDED,
      SourceCollectionAttemptState.FAILED,
      SourceCollectionAttemptState.SKIPPED,
  }
  TERMINAL_PUBLICATION_STATES = {
      PublicationAttempt.State.SUCCEEDED,
      PublicationAttempt.State.PERMANENT_FAILED,
      PublicationAttempt.State.MANUAL_REQUIRED,
      PublicationAttempt.State.STALE,
  }
  FAILED_PUBLICATION_STATES = {
      PublicationAttempt.State.PERMANENT_FAILED,
      PublicationAttempt.State.MANUAL_REQUIRED,
  }

  def build_run_error_summary(steps, sources, publications) -> dict | None:
      failures = [
          {"scope": "step", "id": f"{row.name}:{row.attempt_no}", "code": row.error_code}
          for row in steps if row.state == "failed"
      ]
      failures.extend(
          {"scope": "source", "id": str(row.id), "code": row.error_code}
          for row in sources if row.state == SourceCollectionAttemptState.FAILED
      )
      failures.extend(
          {"scope": "publication", "id": str(row.id), "code": row.error_code}
          for row in publications if row.state in FAILED_PUBLICATION_STATES
      )
      return {"failures": failures} if failures else None

  def build_run_terminal_impact(steps, sources, publications) -> dict:
      return {
          "failedStepCount": sum(row.state == "failed" for row in steps),
          "failedSourceCount": sum(row.state == SourceCollectionAttemptState.FAILED for row in sources),
          "failedPublicationCount": sum(row.state in FAILED_PUBLICATION_STATES for row in publications),
      }

  def calculate_run_terminal_projection(run: CollectionRun) -> RunTerminalProjection | None:
      steps = list(run.steps.all())
      sources = list(run.collection_attempts.all())
      publications = list(PublicationAttempt.objects.filter(
          publication_intent__origin_collection_run_id=run.id,
      ))
      if any(row.state not in TERMINAL_STEP_STATES for row in steps):
          return None
      if any(row.state not in TERMINAL_SOURCE_STATES for row in sources):
          return None
      if any(row.state not in TERMINAL_PUBLICATION_STATES for row in publications):
          return None
      if run.stop_requested_at:
          state = RunState.STOPPED
      elif not any(row.state == SourceCollectionAttemptState.SUCCEEDED for row in sources):
          state = RunState.FAILED
      elif any(row.state in FAILED_REQUIRED_STEP_STATES for row in steps):
          state = RunState.FAILED
      elif any(row.state in FAILED_PUBLICATION_STATES for row in publications):
          state = RunState.FAILED
      else:
          state = RunState.COMPLETED
      return RunTerminalProjection(
          state=state,
          completed_at=timezone.now(),
          error_summary=build_run_error_summary(steps, sources, publications),
          terminal_impact=build_run_terminal_impact(steps, sources, publications),
      )

  def project_collection_run_terminal(run_id, *, audit_context: AuditContext) -> CollectionRun:
      with transaction.atomic():
          run = CollectionRun.objects.select_for_update().get(id=run_id)
          projection = calculate_run_terminal_projection(run)
          if projection is None:
              return run
          run.state = projection.state
          run.completed_at = projection.completed_at
          run.error_summary = projection.error_summary
          run.terminal_impact = projection.terminal_impact
          run.save(update_fields=["state", "completed_at", "error_summary", "terminal_impact"])
          transaction.on_commit(lambda: release_waiting_for_topic(
              run.topic_code,
              audit_context=audit_context,
          ))
          return run
  ```

  `calculate_run_terminal_projection()`은 required step/source/document/publication 상태를 모두 읽고 all-source failure를 `failed`로 유지한다.

- [ ] **Step 5: selective retry 범위를 검증한다.**

  `sourceAttemptId`, `documentExtractionId`, `publicationAttemptId`, `targetId` 중 정확히 하나의 승인된 terminal 단위만 재시도한다. 이미 성공한 다른 단위를 새로 만들지 않는다.

- [ ] **Step 6: 모든 terminal 경로에서 queue-one을 해제한다.**

  `release_waiting_for_topic()`을 transaction commit 이후 정확히 한 번 호출하고, WordPress 성공 후 Blogger 대기 상태는 terminal로 보지 않는다.

- [ ] **Step 7: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/collection src/apps/evidence/tasks.py src/apps/editorial/tasks.py src/apps/publishing src/apps/scheduling specs/001-automated-content-publishing/contracts/job-events.md
  git commit -m "feat: complete run stop retry and terminal aggregation"
  ```

### Task 18: T029 정정·철회 end-to-end orchestration

**Files:**
- Modify: `src/apps/editorial/models.py`
- Modify: `src/apps/editorial/corrections.py`
- Modify: `src/apps/editorial/services.py`
- Modify: `src/apps/editorial/tasks.py`
- Modify: `src/apps/editorial/api.py`
- Modify: `src/apps/publishing/corrections.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/tasks.py`
- Create: `src/apps/editorial/migrations/0005_correction_decision.py`
- Modify: `src/wisdome_writer/celery.py`
- Modify: `specs/001-automated-content-publishing/contracts/job-events.md`
- Deferred integration test: `tests/integration/test_corrections.py`

**Interfaces:**
- Consumes: `SourceCollectionObservation` change lineage and affected article identity.
- Produces: idempotent `CorrectionCase`, append-only `CorrectionDecision`, corrected revision, WordPress-first correction intent.

- [ ] **Step 1: correction decision과 revision binding을 추가한다.**

  ```python
  class CorrectionDecision(models.Model):
      correction_case = models.ForeignKey(CorrectionCase, on_delete=models.PROTECT, related_name="decisions")
      decision = models.CharField(max_length=16)
      subject_hash = models.CharField(max_length=64)
      diff_manifest_hash = models.CharField(max_length=64)
      supersedes = models.ForeignKey("self", null=True, on_delete=models.PROTECT)
      request_key = models.CharField(max_length=200)
      decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
  ```

  `CorrectionCase.corrected_revision`과 latest decision projection/version을 추가한다.

- [ ] **Step 2: source change beat/task를 연결한다.**

  terminal/restored/corrected/retracted 관측을 주기적으로 읽어 `detect_correction_cases_for_observation()`을 호출한다. 같은 article/subject hash는 같은 case를 반환한다.

- [ ] **Step 3: 관리자 verify/reject API를 구현한다.**

  source diff, prior/current evidence locator, 영향 claim과 publication을 표시하고 재인증 proof·reason·expected latest decision을 요구한다.

- [ ] **Step 4: verified case에서 새 revision과 T018 gate를 실행한다.**

  외부 글은 감지나 verify 직후 바꾸지 않는다. corrected revision의 claim/evidence와 quality gate가 passed가 된 뒤 correction publication intent를 생성한다.

- [ ] **Step 5: channel render에 공개 정정 이력을 넣는다.**

  correction type, 검증 시각, 변경 요약, source links를 `ArticleChannelRender.correction_history`와 본문 상단에 포함한다.

- [ ] **Step 6: WordPress-first dependency와 remote ID 보존을 강제한다.**

  update/mark-withdrawn/unpublish는 기존 `remote_post_id`를 유지한다. WordPress terminal 후에만 Blogger를 release한다.

- [ ] **Step 7: SLA와 실패 복구 상태를 기록한다.**

  detected/verified/dispatched/completed timestamps와 30분 초과 여부, target별 실패·retry/reconcile 상태를 case detail에 노출한다.

- [ ] **Step 8: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/editorial src/apps/publishing src/wisdome_writer/celery.py specs/001-automated-content-publishing/contracts/job-events.md
  git commit -m "feat: orchestrate verified corrections and retractions"
  ```

### Task 19: T030 retention dependency graph와 실제 객체 삭제

**Files:**
- Modify: `src/apps/audit/models.py`
- Modify: `src/apps/audit/retention.py`
- Modify: `src/apps/audit/services.py`
- Modify: `src/apps/publishing/models.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/adapters/storage/s3.py`
- Modify: `config/retention/default.json`
- Create: `src/apps/audit/migrations/0003_retention_object_tombstone.py`
- Deferred integration test: `tests/integration/test_scheduling_operations.py`

**Interfaces:**
- Consumes: retention policy, legal hold, active publication/media/correction references, object key/version/checksum.
- Produces: immutable preview items, approved deletion lease, version-aware S3 deletion and DB tombstone.

- [ ] **Step 1: retention item을 immutable object candidate로 확장한다.**

  ```python
  policy_code = models.CharField(max_length=64)
  object_version = models.CharField(max_length=255, blank=True)
  object_checksum = models.CharField(max_length=64, blank=True)
  candidate_hash = models.CharField(max_length=64)
  dependency_manifest = models.JSONField(default=list)
  lease_generation = models.PositiveIntegerField(default=1)
  tombstone_at = models.DateTimeField(null=True)
  ```

- [ ] **Step 2: category handler를 명시적으로 분리한다.**

  raw source/evidence, draft/revision, published snapshot, audit record, public delivery, WordPress media별 handler가 cutoff와 dependency를 계산한다.

- [ ] **Step 3: hold와 active reference graph를 적용한다.**

  legal hold, active publication/media binding, open correction, referenced claim evidence, not-yet-expired audit chain이 하나라도 있으면 item state를 held로 만든다.

- [ ] **Step 4: 승인 전 candidate checksum/object version을 재확인한다.**

  preview 이후 객체나 참조가 바뀌면 stale batch로 실패하고 새 preview를 요구한다.

- [ ] **Step 5: S3 object version을 실제 삭제한 뒤 tombstone을 저장한다.**

  ```python
  storage.delete_version(key=item.object_key, version_id=item.object_version)
  item.state = RetentionBatchItem.State.PURGED
  item.tombstone_at = timezone.now()
  ```

  응답 유실 시 `head_version`으로 삭제 여부를 reconcile하고 동일 version delete를 재실행해도 안전해야 한다.

- [ ] **Step 6: remote public delivery/media cleanup을 별도 lease로 실행한다.**

  active reference count 0과 grace cutoff를 다시 확인하고 remote result를 AuditEvent에 남긴다.

- [ ] **Step 7: 사용자 승인 후 migration/check를 검증하고 커밋한다.**

  ```powershell
  git add src/apps/audit src/apps/publishing src/adapters/storage/s3.py config/retention/default.json
  git commit -m "feat: enforce dependency-aware retention deletion"
  ```

### Task 20: T031 운영 API와 관리자 콘솔

**Files:**
- Modify: `src/apps/scheduling/api.py`
- Modify: `src/apps/scheduling/urls.py`
- Modify: `src/apps/audit/api.py`
- Modify: `src/apps/audit/urls.py`
- Modify: `src/apps/editorial/api.py`
- Modify: `src/apps/editorial/urls.py`
- Modify: `src/apps/collection/api.py`
- Modify: `src/templates/admin_console/operations/`
- Modify: `src/static/admin_console/operations.js`
- Modify: `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml`
- Deferred E2E test: `tests/e2e/test_admin_journey.py`

**Interfaces:**
- Consumes: T027~T030 service functions; views never write state directly.
- Produces: schedule CRUD/CAS, kill switch, run stop/selective retry, correction decision, retention preview/approve/execute and audit cursor endpoints.

- [ ] **Step 1: schedule CRUD와 expected-version CAS를 OpenAPI에 맞춘다.**

  create/update/disable/list/detail에서 timezone, cron, target refs, overlap policy, approval mode와 validation/activation refs를 검증한다.

- [ ] **Step 2: run detail에 복구 정보를 노출한다.**

  source/document/editorial/publication 단계, terminal impact, affected targets, retryability와 허용된 selective retry action을 반환한다.

- [ ] **Step 3: kill switch와 run stop을 분리해 표시한다.**

  global kill switch version/reason과 개별 run stop decision을 혼동하지 않고 각각 재인증·CAS·감사 이벤트를 요구한다.

- [ ] **Step 4: correction 검증 화면을 구현한다.**

  source diff, evidence locator, affected claim/channel, verify/reject reason과 correction progress를 표시한다.

- [ ] **Step 5: retention 전체 여정을 구현한다.**

  preview summary→candidate detail/items→재인증 승인→execute→failed item resume를 제공하고 object key의 민감 부분은 redaction한다.

- [ ] **Step 6: 감사 cursor 조회와 상태 변경을 연결한다.**

  actor, action, correlation ID, entity, before/after hash, reason/result를 관리자 화면에서 필터링한다.

- [ ] **Step 7: 사용자 승인 후 API/E2E 검증을 수행하고 커밋한다.**

  ```powershell
  git add src/apps/scheduling src/apps/audit src/apps/editorial src/apps/collection/api.py src/templates/admin_console/operations src/static/admin_console/operations.js specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml
  git commit -m "feat: complete operations administration console"
  ```

---

## Wave 4 — T032~T033 출시 게이트

### Task 21: T032 자동 검증 구현과 성공 기준 기록

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/unit/test_source_access_policy.py`
- Create: `tests/unit/test_event_clustering.py`
- Create: `tests/unit/test_editorial_policy.py`
- Create: `tests/contract/test_evidence_extractor.py`
- Create: `tests/contract/test_publishers.py`
- Create: `tests/integration/test_collection_pipeline.py`
- Create: `tests/integration/test_scheduling_operations.py`
- Create: `tests/integration/test_security_audit.py`
- Create: `tests/integration/test_corrections.py`
- Create: `tests/e2e/test_admin_journey.py`
- Create: `tests/fixtures/`
- Create: `tests/fault_injection/`
- Modify: `pyproject.toml`
- Modify: `specs/001-automated-content-publishing/quickstart.md`

**Interfaces:**
- Consumes: all T001~T031 public service/API/event contracts.
- Produces: reproducible evidence for SC-001~SC-012 and the release decision.

- [ ] **Step 1: 사용자에게 전체 검증 구현·실행 승인을 요청한다.**

  승인 범위를 unit, contract, integration, E2E, Docker/외부 sandbox로 분리해 확인한다. 승인되지 않은 범위는 생성·실행하지 않는다.

- [ ] **Step 2: 공통 fixture와 외부 adapter fake를 작성한다.**

  ```python
  @pytest.fixture
  def approved_topic_runtime(db):
      return build_approved_topic_runtime(
          topic_code="housing_subscription",
          registry_version=1,
          policy_version=1,
      )
  ```

  fixture는 실제 secret/token을 포함하지 않고 frozen hashes와 deterministic clock을 사용한다.

- [ ] **Step 3: 수집·권리·cluster 단위 검증을 작성한다.**

  ```python
  def test_robots_denied_request_never_reaches_transport(approved_topic_runtime, fake_transport):
      adapter = approved_topic_runtime.build_adapter(
          transport=fake_transport,
          robots_decision="denied",
      )
      with pytest.raises(SourceAccessError) as exc:
          adapter._get(adapter.entrypoints[0], purpose="collection")
      assert exc.value.category == "policy"
      assert fake_transport.request_count == 0

  def test_syndicated_items_count_as_one_independent_origin(cluster):
      verification = verify_event_cluster(cluster.id)
      assert verification.independent_origin_count == 1
      assert verification.decision != "verified_breaking"
  ```

- [ ] **Step 4: extraction contract와 PDF golden 30페이지를 작성한다.**

  text/scan/rotation/table/mixed-language fixture에서 page index·bbox/polygon·reading order·confidence를 검증한다. locator 누락과 저신뢰 high-impact value의 publishable 결과는 0건이어야 한다.

- [ ] **Step 5: publisher fault-injection 검증을 작성한다.**

  ```python
  @pytest.mark.parametrize("delivery", range(100))
  def test_replayed_publish_creates_one_remote_post(delivery, publication_scenario):
      publication_scenario.deliver_same_event()
      assert publication_scenario.wordpress.remote_post_count == 1
  ```

  WordPress response loss, public delay, Blogger partial failure, token expiry, remote lookup 0/2건과 late worker response를 포함한다.

- [ ] **Step 6: scheduling·stop·correction·retention integration 검증을 작성한다.**

  duplicate tick, queue-one, kill switch, selective retry, WordPress-first correction, legal hold, S3 version delete replay를 검증한다.

- [ ] **Step 7: security/audit 검증을 작성한다.**

  staff/session/CSRF/reauth scope, SSRF, secret redaction, append-only bulk mutation, AuditEvent 원자성과 cursor tamper를 검증한다.

- [ ] **Step 8: 관리자 E2E를 작성한다.**

  수동 run→source/evidence review→revision→preview→approval→WordPress 공개 확인→Blogger 원문 링크→correction→kill switch→retention preview 흐름을 한 시나리오로 검증한다.

- [ ] **Step 9: 승인된 명령만 실행하고 결과를 기록한다.**

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/unit -q
  .\.venv\Scripts\python.exe -m pytest tests/contract -q
  .\.venv\Scripts\python.exe -m pytest tests/integration -q
  .\.venv\Scripts\python.exe -m pytest tests/e2e/test_admin_journey.py -q
  .\.venv\Scripts\python.exe src\manage.py check --deploy
  ```

  SC별 numerator/denominator, duration, 실패 fixture와 artifact hash를 `quickstart.md` 한국어 결과 섹션과 영문 machine-readable 섹션에 기록한다.

- [ ] **Step 10: 검증 코드와 결과만 커밋한다.**

  ```powershell
  git add tests pyproject.toml specs/001-automated-content-publishing/quickstart.md
  git commit -m "test: add automated release verification"
  ```

### Task 22: T033 최종 문서·추적성 정리

**Files:**
- Modify: `README.md`
- Modify: `specs/001-automated-content-publishing/tasks.md`
- Modify: `specs/001-automated-content-publishing/REMAINING_WORK.md`
- Modify: `specs/001-automated-content-publishing/quickstart.md`
- Modify: `specs/001-automated-content-publishing/data-model.md`
- Modify: `specs/001-automated-content-publishing/contracts/admin-api.openapi.yaml`
- Modify: `specs/001-automated-content-publishing/contracts/evidence-extractor.md`
- Modify: `specs/001-automated-content-publishing/contracts/generic-extractor.md`
- Modify: `specs/001-automated-content-publishing/contracts/job-events.md`
- Modify: `specs/001-automated-content-publishing/contracts/publisher-adapter.md`
- Create: `docs/runbooks/operations.md`
- Create: `docs/runbooks/recovery.md`

**Interfaces:**
- Consumes: implemented code and user-approved T032 evidence only.
- Produces: a single current-state handoff with no stale task numbering or unsupported completion claim.

- [ ] **Step 1: requirement→task→code→validation 추적 표를 작성한다.**

  FR-001~FR-024, CR-001~CR-007, SC-001~SC-012마다 담당 task, 구현 파일, 검증 test/artifact와 상태를 기록한다.

- [ ] **Step 2: `tasks.md`를 실제 증거 기준으로 갱신한다.**

  구현·리뷰·승인된 검증이 모두 끝난 task만 `[X]`로 표시한다. 기존 ID를 renumber/reorder/delete하지 않는다.

- [ ] **Step 3: `REMAINING_WORK.md`를 현재 T001~T033 구조로 다시 작성한다.**

  과거 T039~T057 설명과 오래된 기준 커밋을 제거한다. 미완료 항목이 있으면 정확한 현재 commit, 파일, blocker와 다음 action을 기록한다.

- [ ] **Step 4: 운영 runbook을 작성한다.**

  registry/profile 승인, worker startup gate, manual run, publication activation, kill switch, selective retry, correction, retention, secret rotation과 incident recovery 절차를 명령·예상 상태와 함께 기록한다.

- [ ] **Step 5: 모든 MD를 한국어 우선·영문 AI 구조로 정리한다.**

  한국어 사용자 설명을 먼저 두고 영문 schema/YAML 또는 execution metadata를 뒤에 둔다. 서로 다른 섹션의 task ID와 enum을 동일하게 유지한다.

- [ ] **Step 6: 문서 placeholder와 계약 불일치를 자체 검토한다.**

  미확정 표시, 오래된 task ID, 존재하지 않는 path/operation/event, nullable/enum 차이를 제거한다.

- [ ] **Step 7: 최종 문서만 커밋한다.**

  ```powershell
  git add README.md specs/001-automated-content-publishing docs/runbooks
  git commit -m "docs: reconcile release contracts and operations"
  ```

## 명세 Coverage Map

| 명세 | 구현 task | 검증 task |
|---|---|---|
| FR-001~FR-002 관리자 접근·주제 선택 | 기존 T004·T007 유지, T026·T031 UI 연결 | T032 security/E2E |
| FR-003~FR-006 출처·수집·증거·권리 | T012, T014~T017 | T032 unit/contract/integration |
| FR-007 중복·충돌·선택·제외 | T013 | T032 clustering/integration |
| FR-008~FR-010 초안·구조·주제별 글 유형 | T013, T018 | T032 editorial/E2E |
| FR-011 미리보기·근거 검토 | T018, T022, T026 | T032 E2E |
| FR-012 WordPress·Blogger 상태 | T021~T024, T026 | T032 publisher/E2E |
| FR-013 승인·자동발행 활성화 | T019, T025, T026 | T032 approval/activation |
| FR-014 일정·중복 정책 | T027, T031 | T032 scheduling |
| FR-015~FR-016 중복·부분 발행 방지 | T020~T024 | T032 publisher fault injection |
| FR-017~FR-019 중지·복구·상태 확인 | T028, T031 | T032 operations/E2E |
| FR-020 보존·삭제 | T030, T031 | T032 retention |
| FR-021 정정·철회 | T029, T031 | T032 correction/E2E |
| FR-022 반도체 속보 조건 | T013, T018 | T032 clustering/editorial |
| FR-023 Blogger의 WordPress 원문 링크 | T024, T026 | T032 publisher/E2E |
| FR-024 PDF 구조·locator·저신뢰 차단 | T014~T018 | T032 30-page golden contract |
| CR-001·CR-003·CR-007 근거·정직성·품질 | T013, T018 | T032 editorial/E2E |
| CR-002·CR-005·CR-006 권리·접근·시각자료 | T012, T017, T018, T022 | T032 source/extractor/publisher |
| CR-004 자격 증명 보호 | T023, T026 | T032 security/audit |
| SC-001~SC-012 측정 기준 | T032 | T032 결과 artifact와 T033 문서 |

---

# English — AI Execution Metadata

```yaml
schema_version: "1.0"
plan: "remaining-implementation"
feature: "001-automated-content-publishing"
branch: "main"
strategy: "preserve-valid-code-and-close-verified-gaps"
validation_requires_explicit_user_approval: true
task_order:
  - T012
  - [T013, T014]
  - T015
  - T016
  - T017
  - T018
  - [T019, T023]
  - T020
  - T021
  - T022
  - T024
  - T025
  - T026
  - T027
  - T028
  - T029
  - T030
  - T031
  - T032
  - T033
dependencies:
  T012: [T001, T009, T010, T011]
  T013: [T002, T009, T010, T011, T012]
  T014: [T002, T003]
  T015: [T001, T003, T014]
  T016: [T005, T014, T015]
  T017: [T001, T014, T015, T016]
  T018: [T002, T013, T016, T017]
  T019: [T004, T006, T018]
  T020: [T005, T019]
  T021: [T020]
  T022: [T002, T018, T020, T021]
  T023: [T001, T003, T004]
  T024: [T020, T021, T022, T023]
  T025: [T004, T009, T014, T018, T019, T021, T022, T023, T024]
  T026: [T007, T019, T020, T021, T022, T023, T024, T025]
  T027: [T002, T004, T005, T006, T009]
  T028: [T005, T006, T020, T021, T027]
  T029: [T005, T006, T013, T018, T021, T022, T024, T028]
  T030: [T005, T006, T009, T022, T029]
  T031: [T007, T027, T028, T029, T030]
  T032: [T001-T031]
  T033: [T032]
review_gates:
  T012:
    rounds: 2
    perspectives:
      - http_security_runtime
      - orchestration_database_migration
      - policy_rights_contracts
    adjudication: main_session_reverification
  subsequent_tasks:
    - spec_compliance_review
    - code_quality_review
release_invariants:
  - all factual claims have reachable approved evidence
  - rights-unknown non-text assets never publish
  - mutable approved material is never reread after execution starts
  - unknown remote outcomes reconcile before another create
  - WordPress public canonical state precedes Blogger writes
  - queue-one releases on every true terminal path
  - task completion requires approved validation evidence
```
