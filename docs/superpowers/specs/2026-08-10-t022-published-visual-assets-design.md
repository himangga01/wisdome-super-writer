# T022 발행 시각자료·미디어 불변 정본 설계

## 한국어 설계

### 1. 목표와 범위

T022는 검증된 `ArticleRevision`의 시각자료를 발행 시점의 비민감 최소본으로 동결하고,
WordPress 미디어와 Blogger 공개 전달 객체가 동일한 snapshot·표시·권리 material을 사용하도록
보장한다. 중복 업로드, 응답 유실, 오래된 worker 결과, 재참조 중 삭제, 원문 보존기간 만료로 인한
공개 이미지 단절을 차단한다.

T022가 소유하는 범위는 다음과 같다.

- `VisualPlacement`의 기존 T018 정본을 입력으로 사용한다. editorial 모델에 publishing FK를
  역방향으로 추가하지 않는다.
- `PublishedEvidenceSnapshot`, `PublishedVisualizationSnapshot`, stable-order visualization input
  join을 publishing 도메인에 추가한다.
- preview/intent 생성 시 revision별 snapshot cohort와 정렬 media manifest를 한 transaction에서
  동결한다. 승인 시 현재 권리·근거와 exact manifest를 다시 검증한다.
- dispatch 뒤 채널별 전달 준비를 시작하고, 모든 binding이 available일 때만 T021 publication
  external-write gate를 연다.
- WordPress는 `RemoteMedia`, Blogger는 `PublicDeliveryAsset`을 content-addressed mapping으로
  재사용하되, 각 비동기 작업은 append-only operation generation과 source event/receipt lease
  provenance를 가진다.
- 활성 참조가 0이고 유예기간·hold·원격 본문 확인을 모두 통과한 자산만 exact generation과
  object version 조건으로 삭제한다.

T022는 OAuth/credential 수명주기(T023), WordPress 대표 URL 의존성(T024), 운영 validation
activation(T025), 관리자 history/UI/E2E(T026), 전체 stop 집계(T028), 최종 retention 실행(T030)을
재정의하지 않는다.

### 2. 선택한 구조

#### 2.1 불변 snapshot cohort

첫 publication intent/preview 생성 transaction에서 revision의 `VisualPlacement`를
`(block_id, display_order, id)` 순으로 잠그고 다음을 만든다.

- `PublishedEvidenceSnapshot`: source/evidence identity와 version hash, checksum, locator, 권리,
  attribution, alt/caption, 필요한 claim 관계만 저장한다. 원문 전문과 비밀은 저장하지 않는다.
- `PublishedVisualizationSnapshot`: output checksum/object version, input/transform/renderer/rights
  manifest, alt/caption을 저장한다.
- `PublishedVisualizationInput`: 각 visualization input을 `PublishedEvidenceSnapshot`에 stable order로
  연결한다.
- `PublishedAssetCohort`: revision, canonical manifest, item count, manifest hash, schema version을 가진
  append-only root다. 같은 revision/material replay는 같은 cohort를 반환하고 다른 material은 새
  revision 없이는 거부한다.

`ArticleChannelRender.media_manifest`에는 raw EvidenceAsset 값이 아니라 cohort ID, snapshot ID,
asset checksum, presentation hash, block/order/usage, MIME/size, 권리·표시 hash만 기록한다.
Approval v3 subject의 기존 media manifest hash가 이 값을 결속한다.

snapshot 이름은 발행 최소본을 뜻하지만 실제 공개 성공을 뜻하지 않는다. 공개 여부는
`PublicationMedia`와 `PublicationAttempt` 상태가 나타낸다.

#### 2.2 채널 전달 mapping과 binding

- `RemoteMedia`는 `(target, asset_checksum, presentation_hash)` WordPress mapping이다.
- `PublicDeliveryAsset`은 `(asset_checksum, presentation_hash)` Blogger 공개 객체 mapping이다.
- `PublicationMedia`는 정확한 publication/revision/cohort item과 위 mapping 중 하나를 연결한다.
  evidence/visualization snapshot과 remote/public delivery도 각각 XOR이며, identity 필드는 불변이다.
- 같은 binary라도 alt/caption/attribution이 다르면 presentation hash가 달라 별도 mapping을 쓴다.

현재 `VisualPlacement`는 pre-publication 정본으로 유지한다. `Published*Snapshot` FK를 editorial에
추가하면 editorial 0004와 publishing 0012 사이 migration 순환이 생기므로, 장기 발행 연결의
권위는 `PublicationMedia`에 둔다.

#### 2.3 작업 세대와 상태 전이

각 mapping은 단순 `lease_generation` 숫자만 신뢰하지 않는다. 다음 append-only 작업 행을 둔다.

- `MediaDeliveryOperation`: mapping 종류, action(`prepare|reconcile|delete`), generation, source event,
  consumer receipt generation/token hash, expected mapping state, expected object/media identity,
  state(`queued|running|succeeded|unknown|failed|superseded`), result hash.
- mapping마다 active operation은 정확히 하나다. generation은 1씩 증가하고 결과는 exact source
  event와 receipt capability가 current일 때만 projection한다.
- 원격 write가 시작된 뒤 결과가 불명확하면 동일 upload를 반복하지 않고 reconcile한다.
- 늦은 결과는 T021과 같은 원칙으로 factual observation만 남기고 current mapping을 바꾸지 않는다.

이벤트는 다음 exact payload를 사용한다.

- `media.upload_requested@2`: remote media ID, publication attempt/intent ID, operation generation,
  target snapshot/config hash.
- `media.reconcile_requested@2`: remote media ID, publication attempt/intent ID, operation generation.
- `delivery.prepare_requested@2`: public delivery asset ID, publication attempt/intent ID, operation
  generation.
- `delivery.reconcile_requested@2`: public delivery asset ID, publication attempt/intent ID, operation
  generation.
- `media.delete_requested@2`, `delivery.delete_requested@2`: mapping ID와 operation generation.

v1은 정확한 기존 binding이 증명되는 terminal replay만 허용하고 새 write에는 사용하지 않는다.

#### 2.4 외부 I/O와 검증

WordPress upload는 공식 media endpoint의 slug, alt text, caption, description marker를 사용한다.
업로드 전 slug+marker 조회가 정확히 0건일 때만 POST하고, 1건이면 재사용, 복수건이면 수동 확인이다.
2xx 응답은 exact media ID/source URL/slug/alt/caption/description을 인증 GET으로 확인한 뒤에만
available로 투영한다. 불명확 write 오류는 reconcile로 보낸다.

Blogger 전달 객체는 content-addressed key에 exact bytes를 PUT하고 반환된 object version,
checksum, size, MIME을 다시 HEAD/GET으로 확인한다. 공개 URL은 HTTPS이며 익명 bounded GET에서
동일 checksum/MIME/size가 확인돼야 available이다. 삭제는 DB generation CAS와 exact object
version을 모두 요구한다. version ID 없는 삭제로 delete marker만 만드는 경로는 허용하지 않는다.

#### 2.5 publication gate와 정리

dispatch가 attempt를 만들 때 media manifest의 각 항목에 대해 binding과 prepare event를 같은
transaction에서 만든다. T021 external-write gate는 다음을 모두 확인한다.

- final render의 media manifest가 승인된 preview/cohort hash와 동일하다.
- manifest item 수와 `PublicationMedia` exact set이 같다.
- 모든 binding이 `active`, mapping이 `available`, binding generation이 current다.
- WordPress URL 또는 Blogger public URL이 mapping의 검증된 immutable URL과 같다.
- 권리 상태가 여전히 publishable이고 열린 correction/rights hold가 없다.

참조 제거는 binding을 새 행 또는 단조 상태 전이로 기록한다. 자산 삭제 예약은 active/prepared
binding, scheduled/running/reconciling attempt, 공개·marked-withdrawn 원격 본문 참조, correction/
rights/retention hold를 모두 집계한다. 0건이면 최소 30일 유예 후 다시 계산한다. 재참조는 같은
transaction에서 pending delete를 취소하고 새 operation generation을 만든다.

### 3. 오류 처리와 마이그레이션

- 기존 `media_manifest=[]`인 render는 정상 empty cohort로 backfill한다.
- 기존 non-empty manifest가 snapshot provenance를 완전히 재구성할 수 없으면
  `legacy-unverifiable-v1` cohort로 격리하고 새 content publication을 거부한다.
- 기존 `RemoteMedia`, `PublicDeliveryAsset`, `PublicationMedia`가 identity/XOR/reference count를
  위반하면 임의 선택·삭제하지 않고 migration을 중단한다.
- SQLite와 PostgreSQL에 동일한 append-only, generation, parent-child, XOR, terminal 단조 trigger를
  설치한다. populated reverse는 명시적으로 irreversible이다.
- 외부 객체·WordPress 미디어 삭제 실패는 mapping tombstone을 성공으로 꾸미지 않고 retry/reconcile
  또는 manual 상태로 보존한다.

### 4. 집중 검증

- snapshot canonical replay와 raw/source/rights/locator/visual input 변조 거부
- 같은 bytes·다른 presentation 분리, 같은 presentation 채널별 재사용
- upload/prepare exact replay, 두 worker generation CAS, write 후 응답 유실 reconcile
- approved manifest와 binding exact set 불일치 시 article external write 0회
- Blogger exact version/checksum/public GET, version 없는 삭제 거부
- active/prepared/in-flight/remote-body/hold 참조가 있으면 orphan/delete 취소
- 유예 중 재참조가 delete generation을 fence
- legacy non-empty manifest 및 corrupt mapping migration fail-close
- SQLite 실제 trigger/MigrationExecutor와 PostgreSQL 생성 SQL parity

### 5. 공식 근거

- WordPress REST API media는 create/retrieve/update/delete와 `slug`, `alt_text`, `caption`,
  `description`을 제공한다: <https://developer.wordpress.org/rest-api/reference/media/>
- Blogger post는 HTML `content`, `images[]`, 공개·draft 상태와 insert/update/revert를 제공한다:
  <https://developers.google.com/blogger/docs/3.0/reference/posts>
- S3/MinIO versioned object는 `versionId`를 지정해야 정확한 버전을 영구 삭제하며, version 없는
  삭제는 delete marker가 될 수 있다:
  <https://docs.aws.amazon.com/AmazonS3/latest/userguide/DeletingObjectVersions.html>,
  <https://min.io/docs/minio/linux/administration/object-management/object-delete.html>

## English / AI-readable design

T022 uses an immutable per-revision publication asset cohort, channel delivery mappings, and append-only
media operation generations. The first intent/preview transaction freezes minimal evidence or visualization
snapshots and a canonical ordered media manifest; approval revalidates live rights and binds that exact
manifest. `PublicationMedia`, rather than editorial models, is the authoritative long-lived link to one
evidence-or-visualization snapshot and one WordPress-or-public-delivery mapping, avoiding a circular
editorial/publishing migration dependency.

WordPress media is keyed by `(target, asset checksum, presentation hash)` and verified through the official
media slug/alt/caption/description surface. Blogger assets use immutable content-addressed S3/MinIO object
versions and HTTPS public URLs. Every prepare, reconcile, or delete is represented by an append-only
generation bound to its source event and consumer capability. Ambiguous writes reconcile instead of
repeating. Publication external I/O remains closed until the approved manifest, exact bindings, current
mapping generations, rights, and verified URLs all match. Orphan cleanup requires zero active/prepared/
in-flight/remote-body/hold references, a 30-day grace period, a second locked recount, and exact object
version deletion. Legacy material that cannot prove this lineage is quarantined rather than upgraded by
assumption.
