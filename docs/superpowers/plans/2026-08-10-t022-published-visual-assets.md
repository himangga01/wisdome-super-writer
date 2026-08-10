# T022 발행 시각자료·미디어 불변 정본 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 검증된 revision의 시각자료를 불변 snapshot cohort로 동결하고, WordPress 미디어와 Blogger 공개 객체를 동일한 승인 매니페스트·작업 세대·삭제 안전 조건에 결속한다.

**Architecture:** publishing 도메인이 revision별 `PublishedAssetCohort`와 evidence/visualization snapshot을 소유하고, preview에는 raw evidence가 아닌 정렬된 canonical manifest만 저장한다. 채널별 mapping은 `PublicationMedia`로 snapshot과 연결하며, 모든 prepare/reconcile/delete I/O는 append-only `MediaDeliveryOperation`과 exact outbox receipt capability를 거쳐 투영한다.

**Tech Stack:** Python 3.12, Django ORM/migrations, SQLite/PostgreSQL parity triggers, Celery transactional outbox, boto3 S3-compatible object storage, WordPress REST Media API.

## 전역 제약

- 기존 `editorial.VisualPlacement`를 입력 정본으로 사용하며 editorial에 publishing FK나 신규 0004 migration을 추가하지 않는다.
- 생성하는 문서는 한국어 절을 먼저 두고 동일한 English/AI-readable 절을 뒤에 둔다.
- 원문 전문, secret, credential 값은 snapshot·manifest·event payload에 저장하지 않는다.
- 동일 revision의 동일 material은 같은 cohort로 replay하고, 다른 material은 새 revision 없이 거부한다.
- 승인된 preview/final render와 snapshot/cohort/mapping identity는 ORM·SQLite·PostgreSQL에서 append-only 또는 단조 상태 전이로 보호한다.
- 원격 write 결과가 불명확하면 동일 create/upload를 반복하지 않고 reconcile한다.
- Blogger 전달 객체 삭제는 non-empty exact object version을 필수로 하며 version 없는 delete marker 경로를 금지한다.
- 외부 article write는 승인된 media manifest와 exact binding set이 모두 current·available일 때만 연다.
- 유예기간은 최소 30일이며 active/prepared/in-flight/remote-body/hold 참조가 하나라도 있으면 삭제하지 않는다.
- T023 credential, T024 canonical WordPress dependency, T025 activation, T026 UI/E2E, T028 global stop, T030 retention 실행은 재정의하지 않는다.

---

## 한국어 구현 계획

### Task 1: 불변 발행 자산 cohort와 snapshot 모델

**Files:**
- Modify: `src/apps/publishing/models.py`
- Create: `src/apps/publishing/migrations/0012_published_asset_snapshots.py`
- Create: `tests/unit/test_published_asset_snapshots.py`
- Create: `tests/unit/test_published_asset_snapshots_db.py`

**Interfaces:**
- Consumes: `editorial.ArticleRevision`, `editorial.VisualPlacement`, `editorial.VisualizationRender`, `evidence.EvidenceAsset`.
- Produces: `PublishedAssetCohort`, `PublishedEvidenceSnapshot`, `PublishedVisualizationSnapshot`, `PublishedVisualizationInput`.

- [ ] **Step 1: 모델 계약 RED를 작성한다.**

  ```python
  def test_published_asset_models_expose_append_only_identity():
      assert PublishedAssetCohort._meta.get_field("manifest_hash").max_length == 64
      assert PublishedEvidenceSnapshot._meta.get_field("evidence_content_hash").max_length == 64
      assert PublishedVisualizationSnapshot._meta.get_field("output_checksum").max_length == 64
      assert PublishedVisualizationInput._meta.get_field("display_order").null is False
  ```

- [ ] **Step 2: 모델 RED를 실행해 클래스 부재로 실패하는지 확인한다.**

  Run: `python -m django test tests.unit.test_published_asset_snapshots -v 2`

  Expected: `ImportError` 또는 모델 class 부재 assertion 실패.

- [ ] **Step 3: 네 모델과 canonical 필드를 최소 구현한다.**

  ```python
  class PublishedAssetCohort(models.Model):
      revision = models.OneToOneField("editorial.ArticleRevision", on_delete=models.PROTECT)
      schema_version = models.CharField(max_length=40, default="published-assets-v1")
      item_count = models.PositiveIntegerField()
      manifest = models.JSONField(default=list)
      manifest_hash = models.CharField(max_length=64, validators=[sha256_validator])

  class PublishedEvidenceSnapshot(models.Model):
      cohort = models.ForeignKey(PublishedAssetCohort, on_delete=models.PROTECT, related_name="evidence_snapshots")
      visual_placement = models.OneToOneField("editorial.VisualPlacement", on_delete=models.PROTECT)
      evidence = models.ForeignKey("evidence.EvidenceAsset", on_delete=models.PROTECT)
      evidence_content_hash = models.CharField(max_length=64, validators=[sha256_validator])
      asset_checksum = models.CharField(max_length=64, validators=[sha256_validator])

  class PublishedVisualizationSnapshot(models.Model):
      cohort = models.ForeignKey(PublishedAssetCohort, on_delete=models.PROTECT, related_name="visualization_snapshots")
      visual_placement = models.OneToOneField("editorial.VisualPlacement", on_delete=models.PROTECT)
      visualization = models.ForeignKey("editorial.VisualizationRender", on_delete=models.PROTECT)
      output_checksum = models.CharField(max_length=64, validators=[sha256_validator])

  class PublishedVisualizationInput(models.Model):
      visualization_snapshot = models.ForeignKey(PublishedVisualizationSnapshot, on_delete=models.PROTECT)
      evidence_snapshot = models.ForeignKey(PublishedEvidenceSnapshot, on_delete=models.PROTECT)
      display_order = models.PositiveIntegerField()
  ```

  실제 모델에는 block/order, object key/version, MIME/size, locator, source/evidence/version hash, rights, attribution, alt/caption, transform/input/renderer/presentation hash를 명시 필드로 저장하고 raw text/secret은 저장하지 않는다.

- [ ] **Step 4: append-only ORM과 DB invariant RED를 작성한다.**

  ```python
  def test_snapshot_update_and_delete_are_rejected(self):
      with self.assertRaises(TypeError):
          PublishedAssetCohort.objects.filter(pk=self.cohort.pk).update(item_count=99)

  def test_sqlite_rejects_cross_revision_or_mutated_snapshot(self):
      with self.assertRaises(IntegrityError):
          self.raw_update_snapshot_to_other_cohort()
  ```

- [ ] **Step 5: 0012 migration에 SQLite/PG 동형 guard를 구현한다.**

  cohort/snapshot/input UPDATE·DELETE를 금지하고, placement revision과 cohort revision 일치, evidence/visualization XOR, stable order unique, manifest item count/hash shape를 강제한다. legacy preview가 빈 manifest면 empty cohort로 backfill하고 non-empty material을 정확히 재구성할 수 없으면 `legacy-unverifiable-v1`로 격리한다. corrupt mapping은 migration을 중단하고 populated reverse는 `IrreversibleError`로 닫는다.

- [ ] **Step 6: 모델·MigrationExecutor focused GREEN을 확인한다.**

  Run: `python -m django test tests.unit.test_published_asset_snapshots tests.unit.test_published_asset_snapshots_db -v 2`

  Expected: 신규 모델/actual SQLite forward/reverse/trigger 테스트 모두 PASS.

- [ ] **Step 7: Task 1을 커밋한다.**

  ```powershell
  git add src/apps/publishing/models.py src/apps/publishing/migrations/0012_published_asset_snapshots.py tests/unit/test_published_asset_snapshots.py tests/unit/test_published_asset_snapshots_db.py
  git commit -m "feat: freeze publication asset snapshots"
  ```

### Task 2: canonical cohort builder와 preview media manifest

**Files:**
- Modify: `src/apps/publishing/services.py`
- Modify: `tests/unit/test_published_asset_snapshots.py`
- Modify: `tests/unit/test_publication_approval_service.py`

**Interfaces:**
- Consumes: Task 1 모델과 existing `sha256_hex` canonical hashing.
- Produces: `freeze_revision_asset_cohort(*, revision, using="default") -> PublishedAssetCohort`, `build_channel_media_manifest(*, cohort, channel) -> list[dict[str, object]]`.

- [ ] **Step 1: exact replay·변조·정렬 RED를 작성한다.**

  ```python
  def test_freeze_revision_asset_cohort_is_canonical_and_idempotent():
      first = freeze_revision_asset_cohort(revision=self.revision)
      second = freeze_revision_asset_cohort(revision=self.revision)
      assert first.pk == second.pk
      assert [row["displayOrder"] for row in first.manifest] == [0, 1]

  def test_freeze_rejects_live_placement_material_drift():
      freeze_revision_asset_cohort(revision=self.revision)
      self.mutate_source_rights()
      with pytest.raises(Conflict):
          freeze_revision_asset_cohort(revision=self.revision)
  ```

- [ ] **Step 2: RED를 실행해 helper 부재/빈 manifest로 실패하는지 확인한다.**

  Run: `python -m django test tests.unit.test_published_asset_snapshots -v 2`

- [ ] **Step 3: snapshot material resolver와 cohort builder를 구현한다.**

  ```python
  def freeze_revision_asset_cohort(*, revision, using="default") -> PublishedAssetCohort:
      placements = list(
          revision.visual_placements.using(using)
          .select_for_update()
          .select_related("source_evidence", "visualization")
          .order_by("block_id", "display_order", "id")
      )
      material = [_published_asset_item(placement) for placement in placements]
      return _create_or_replay_asset_cohort(revision, material, using=using)
  ```

  `_published_asset_item`은 live evidence review/publishable/rights/object identity와 placement frozen material을 exact 비교하고 incomplete material을 fail-close한다. visualization input은 stable ordered evidence snapshot join으로 동결한다.

- [ ] **Step 4: `_create_preview_render()` manifest RED를 작성한다.**

  ```python
  def test_preview_contains_only_frozen_media_manifest(self):
      render = _create_preview_render(self.intent, self.revision, self.target)
      assert render.media_manifest[0]["cohortId"] == str(self.cohort.id)
      assert "sourceText" not in json.dumps(render.media_manifest)
      assert render.media_manifest == sorted(render.media_manifest, key=manifest_sort_key)
  ```

- [ ] **Step 5: preview 생성과 approval subject를 cohort hash에 결속한다.**

  `_create_preview_render()`는 먼저 `freeze_revision_asset_cohort`를 호출하고 channel manifest를 `media_manifest`에 저장한다. 기존 approval v3 media hash는 이 exact JSON을 사용하며 승인 직전 동일 cohort와 current rights를 재검증한다.

- [ ] **Step 6: snapshot·approval focused GREEN을 확인한다.**

  Run: `python -m django test tests.unit.test_published_asset_snapshots tests.unit.test_publication_approval_service -v 2`

- [ ] **Step 7: Task 2를 커밋한다.**

  ```powershell
  git add src/apps/publishing/services.py tests/unit/test_published_asset_snapshots.py tests/unit/test_publication_approval_service.py
  git commit -m "feat: bind previews to immutable media manifests"
  ```

### Task 3: 채널 mapping·publication binding·external-write gate

**Files:**
- Modify: `src/apps/publishing/models.py`
- Modify: `src/apps/publishing/migrations/0012_published_asset_snapshots.py`
- Modify: `src/apps/publishing/services.py`
- Create: `tests/unit/test_publication_media_bindings.py`
- Modify: `tests/unit/test_publication_attempt_fencing.py`

**Interfaces:**
- Consumes: cohort manifest, `PublicationDispatch`, `Publication`, `PublicationAttempt`.
- Produces: `prepare_publication_media_bindings_locked(*, attempt, using="default") -> tuple[PublicationMedia, ...]`, `require_publication_media_ready_locked(*, attempt, using="default") -> None`.

- [ ] **Step 1: content-addressed identity와 XOR RED를 작성한다.**

  같은 bytes·같은 presentation은 channel mapping을 재사용하고, presentation이 다르면 다른 mapping을 만들며, `PublicationMedia`가 cohort item과 remote/public mapping을 각각 exact XOR로 연결하는 테스트를 작성한다.

- [ ] **Step 2: RED를 실행한다.**

  Run: `python -m django test tests.unit.test_publication_media_bindings -v 2`

- [ ] **Step 3: 기존 mapping 모델을 authoritative snapshot FK로 보강한다.**

  ```python
  published_evidence_snapshot = models.ForeignKey(
      PublishedEvidenceSnapshot, null=True, blank=True, on_delete=models.PROTECT
  )
  published_visualization_snapshot = models.ForeignKey(
      PublishedVisualizationSnapshot, null=True, blank=True, on_delete=models.PROTECT
  )
  asset_cohort = models.ForeignKey(PublishedAssetCohort, on_delete=models.PROTECT)
  ```

  raw UUID snapshot 필드는 정확히 backfill한 뒤 제거하고, mapping identity·binding identity UPDATE/DELETE 금지 trigger를 SQLite/PG에 둔다.

- [ ] **Step 4: dispatch binding 원자성 RED를 작성한다.**

  dispatch 중간 fault injection 시 binding/event/outbox/attempt가 모두 rollback되고, exact replay는 동일 binding ID set을 반환하는지 검증한다.

- [ ] **Step 5: binding 준비와 readiness gate를 구현한다.**

  dispatch transaction에서 manifest exact set만 binding하고 prepare event를 함께 enqueue한다. T021 pre-I/O gate는 final render manifest hash, exact binding count/set, mapping current state/URL, current rights/hold를 확인한다. 하나라도 불일치하면 external adapter command를 반환하지 않는다.

- [ ] **Step 6: binding·attempt fence GREEN을 확인한다.**

  Run: `python -m django test tests.unit.test_publication_media_bindings tests.unit.test_publication_attempt_fencing -v 2`

- [ ] **Step 7: Task 3을 커밋한다.**

  ```powershell
  git add src/apps/publishing/models.py src/apps/publishing/migrations/0012_published_asset_snapshots.py src/apps/publishing/services.py tests/unit/test_publication_media_bindings.py tests/unit/test_publication_attempt_fencing.py
  git commit -m "feat: gate publication on exact media bindings"
  ```

### Task 4: append-only media operation ledger와 v2 event ABI

**Files:**
- Modify: `src/apps/publishing/models.py`
- Modify: `src/apps/publishing/migrations/0012_published_asset_snapshots.py`
- Modify: `src/wisdome_writer/infrastructure/event_routes.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/tasks.py`
- Create: `tests/unit/test_media_delivery_operations.py`
- Create: `tests/unit/test_media_delivery_operations_db.py`
- Modify: `tests/unit/test_event_routes.py`

**Interfaces:**
- Consumes: Task 3 mapping/binding, outbox event/receipt lease identity.
- Produces: `MediaDeliveryOperation`, exact claim/persist/finalize services, six v2 routes.

- [ ] **Step 1: operation state machine·payload RED를 작성한다.**

  ```python
  def test_media_upload_v2_payload_is_exact():
      validate_event_payload(
          "media.upload_requested", 2,
          {
              "remote_media_id": str(remote.id),
              "publication_attempt_id": str(attempt.id),
              "publication_intent_id": str(intent.id),
              "operation_generation": 1,
              "target_snapshot_id": str(target.current_snapshot_id),
              "target_config_hash": target.current_config_hash,
          },
      )
  ```

  upload/reconcile/prepare/delivery-reconcile/media-delete/delivery-delete 여섯 payload의 required/extra/range를 각각 검증한다.

- [ ] **Step 2: event/model RED를 실행한다.**

  Run: `python -m django test tests.unit.test_event_routes tests.unit.test_media_delivery_operations -v 2`

- [ ] **Step 3: `MediaDeliveryOperation`과 exact generation 서비스를 구현한다.**

  ```python
  class MediaDeliveryOperation(models.Model):
      mapping_kind = models.CharField(max_length=32)
      remote_media = models.ForeignKey(RemoteMedia, null=True, blank=True, on_delete=models.PROTECT)
      public_delivery_asset = models.ForeignKey(PublicDeliveryAsset, null=True, blank=True, on_delete=models.PROTECT)
      action = models.CharField(max_length=16)
      generation = models.PositiveIntegerField()
      source_event = models.ForeignKey("infrastructure.OutboxMessage", on_delete=models.PROTECT)
      state = models.CharField(max_length=20)
      result_hash = models.CharField(max_length=64, blank=True)
  ```

  mapping XOR, generation contiguous, exact one active, receipt capability, immutable terminal result를 ORM과 양 DB trigger로 강제한다.

- [ ] **Step 4: task claim/replay/late result RED를 작성한다.**

  두 worker가 같은 operation을 claim하면 한 worker만 command를 받고, write 후 응답 유실은 reconcile operation으로 전환되며, 늦은 result는 factual audit만 남기고 mapping을 바꾸지 않는지 검증한다.

- [ ] **Step 5: v2 routes와 worker services를 연결한다.**

  v1은 exact terminal replay만 허용하고 새 write는 즉시 `legacy-unverifiable-v1` manual 상태로 격리한다. task는 event/receipt를 먼저 잠그고 mapping/operation을 뒤에 잠근다.

- [ ] **Step 6: operation DB·event·worker GREEN을 확인한다.**

  Run: `python -m django test tests.unit.test_event_routes tests.unit.test_media_delivery_operations tests.unit.test_media_delivery_operations_db -v 2`

- [ ] **Step 7: Task 4를 커밋한다.**

  ```powershell
  git add src/apps/publishing src/wisdome_writer/infrastructure/event_routes.py tests/unit/test_event_routes.py tests/unit/test_media_delivery_operations.py tests/unit/test_media_delivery_operations_db.py
  git commit -m "feat: add fenced media delivery operations"
  ```

### Task 5: WordPress exact media와 Blogger versioned public delivery

**Files:**
- Modify: `src/adapters/publishers/wordpress/client.py`
- Modify: `src/adapters/storage/s3.py`
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/tasks.py`
- Create: `tests/unit/test_wordpress_media_delivery.py`
- Create: `tests/unit/test_public_delivery_assets.py`

**Interfaces:**
- Consumes: Task 4 operation fence and immutable snapshot bytes/object identity.
- Produces: exact WordPress media verification, exact S3 version prepare/reconcile/delete, mapping projections.

- [ ] **Step 1: WordPress 0/1/many·tamper RED를 작성한다.**

  0건일 때만 POST, 1건 exact match 재사용, 복수건 manual, slug/marker만 남고 alt/caption/source URL이 다르면 manual, mutation 2xx 뒤 authenticated GET proof가 없으면 available 금지를 검증한다.

- [ ] **Step 2: WordPress RED를 실행한다.**

  Run: `python -m django test tests.unit.test_wordpress_media_delivery -v 2`

- [ ] **Step 3: `find_media`와 `upload_media`를 exact material 검증으로 보강한다.**

  ```python
  def find_media(self, *, remote_lookup_key, description_marker, expected_alt_text, expected_caption):
      # 0 -> not_found, 1 exact -> succeeded, >1 or material mismatch -> manual_required
      ...
  ```

  write timeout/5xx/invalid response는 direct upload retry가 아니라 reconcile operation으로 전환한다.

- [ ] **Step 4: Blogger object version·public GET·delete RED를 작성한다.**

  exact checksum/size/MIME/version HEAD와 anonymous HTTPS bounded GET을 모두 요구하고, empty version ID 삭제와 유예 중 재참조 삭제를 거부하는지 검증한다.

- [ ] **Step 5: S3 adapter에 exact-version primitive를 구현한다.**

  ```python
  def delete_exact_version(self, *, key: str, version_id: str) -> None:
      if not version_id.strip():
          raise ValueError("exact object version is required")
      self.client.delete_object(Bucket=self.bucket, Key=_validate_key(key), VersionId=version_id)
  ```

  prepare는 content-addressed key PUT 뒤 returned version/checksum/size/MIME을 HEAD로 재검증하고 public URL을 bounded anonymous GET으로 확인한다.

- [ ] **Step 6: adapter·service GREEN을 확인한다.**

  Run: `python -m django test tests.unit.test_wordpress_media_delivery tests.unit.test_public_delivery_assets -v 2`

- [ ] **Step 7: Task 5를 커밋한다.**

  ```powershell
  git add src/adapters/publishers/wordpress/client.py src/adapters/storage/s3.py src/apps/publishing/services.py src/apps/publishing/tasks.py tests/unit/test_wordpress_media_delivery.py tests/unit/test_public_delivery_assets.py
  git commit -m "feat: verify channel media delivery exactly"
  ```

### Task 6: orphan cleanup, 계약 문서, 집중 회귀

**Files:**
- Modify: `src/apps/publishing/services.py`
- Modify: `src/apps/publishing/tasks.py`
- Modify: `specs/001-automated-content-publishing/contracts/job-events.md`
- Modify: `specs/001-automated-content-publishing/data-model.md`
- Modify: `specs/001-automated-content-publishing/quickstart.md`
- Modify: `specs/001-automated-content-publishing/tasks.md`
- Modify: `docs/superpowers/plans/2026-08-07-remaining-implementation.md`
- Create: `tests/unit/test_publication_media_cleanup.py`

**Interfaces:**
- Consumes: Tasks 1–5 cohort, bindings, operation ledger, exact adapters.
- Produces: `schedule_orphan_media_cleanup_locked`, exact delete generation, synchronized Korean/English contracts.

- [ ] **Step 1: protected-reference·grace·re-reference RED를 작성한다.**

  active/prepared binding, queued/running/reconciling attempt, remote body reference, correction/rights/retention hold 각각이 delete를 차단하고, 30일 경과 뒤 locked recount가 0일 때만 delete operation을 생성하며, 유예 중 재참조가 이전 delete generation을 supersede하는지 검증한다.

- [ ] **Step 2: cleanup RED를 실행한다.**

  Run: `python -m django test tests.unit.test_publication_media_cleanup -v 2`

- [ ] **Step 3: locked reference recount와 exact delete operation을 구현한다.**

  `active_reference_count`를 단독 권위로 쓰지 않고 binding/attempt/remote-body/hold row를 정렬 잠금해 재계산한다. 삭제 성공은 WordPress exact media ID 또는 S3 exact object version 확인 뒤에만 mapping tombstone을 기록한다.

- [ ] **Step 4: 한국어 우선 계약과 English/AI-readable 절을 동기화한다.**

  job event v2 payload, model invariant, preview/approval/dispatch gate, orphan cleanup, legacy quarantine, T023/T026/T030 경계를 한국어 절에 먼저 기록하고 동일 구조의 English 절을 뒤에 둔다. T022 체크박스는 상위 dependency 정책에 따라 그대로 `[ ]` 유지한다.

- [ ] **Step 5: T022 focused 회귀와 승인된 정적 검증을 실행한다.**

  ```powershell
  python -m django test tests.unit.test_published_asset_snapshots tests.unit.test_published_asset_snapshots_db tests.unit.test_publication_media_bindings tests.unit.test_media_delivery_operations tests.unit.test_media_delivery_operations_db tests.unit.test_wordpress_media_delivery tests.unit.test_public_delivery_assets tests.unit.test_publication_media_cleanup -v 2
  python manage.py check
  python manage.py makemigrations --check --dry-run
  git diff --check
  ```

- [ ] **Step 6: T019–T021의 직접 영향 focused 회귀를 실행한다.**

  Run: `python -m django test tests.unit.test_publication_approval_service tests.unit.test_publication_intent_idempotency tests.unit.test_publication_attempt_fencing tests.unit.test_publication_attempt_worker -v 2`

- [ ] **Step 7: self-review 두 라운드에서 발견된 결함은 각각 RED를 먼저 추가해 수정한다.**

  1차는 snapshot/provenance/rights/manifest exactness, 2차는 DB trigger/lock order/operation replay/orphan deletion을 읽기 전용으로 추적한다. 실제 결함을 발견하면 해당 focused test를 실패시키고 최소 구현으로 GREEN을 복구한다.

- [ ] **Step 8: Task 6을 커밋한다.**

  ```powershell
  git add src/apps/publishing src/adapters specs/001-automated-content-publishing docs/superpowers/plans tests/unit
  git commit -m "feat: complete immutable publication media flow"
  ```

---

## English / AI-readable implementation map

1. Add append-only `PublishedAssetCohort`, evidence/visualization snapshots, and stable visualization-input joins in publishing migration `0012`; use the existing editorial `VisualPlacement` and avoid a circular editorial migration.
2. Freeze the ordered cohort during first preview creation, replay exact material, reject live provenance drift, and place only bounded snapshot IDs/hashes in `ArticleChannelRender.media_manifest`.
3. Bind every manifest item to exactly one channel mapping through `PublicationMedia`; dispatch creates the exact binding/event cohort atomically and T021 pre-I/O authorization requires every binding to be current and available.
4. Replace scalar media leases with append-only `MediaDeliveryOperation` generations and exact v2 upload/prepare/reconcile/delete event payloads tied to source event and receipt capability.
5. Verify WordPress media through exact authenticated GET material and verify Blogger delivery through an exact content-addressed object version plus bounded anonymous HTTPS retrieval. Never repeat an ambiguous mutation.
6. Delete only after a 30-day grace and a locked zero-reference recount, using exact WordPress media identity or exact S3 object version. Synchronize Korean-first and English contracts, run only the approved focused/static checks, and remediate review findings through RED→GREEN cycles.
