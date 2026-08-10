# 발행 어댑터 계약

## 목적

채널별 API 차이를 숨기면서 생성, 수정, 철회, 상태 조정과 멱등 처리를 동일한 방식으로
검증한다. 이 계약을 충족하고 공식·승인된 연동 근거가 있는 채널만 자동발행 대상으로
활성화할 수 있다.

## 기능 선언

어댑터는 읽기 전용 preflight에서 선언 capability를 반환하고, 쓰기 canary 결과로 이를
검증한다.

```json
{
  "create": true,
  "update": true,
  "unpublish": false,
  "mark_withdrawn": true,
  "draft": true,
  "schedule": false,
  "media_upload": false,
  "max_title_chars": 0,
  "max_body_bytes": 0,
  "max_media_count": 0,
  "supported_media_types": []
}
```

숫자 `0`은 제한이 없다는 의미가 아니라 공식 값이 확인되지 않았음을 뜻한다. 이 경우
보수적 내부 제한을 두고 샌드박스/테스트 계정 검증 없이는 자동발행을 활성화하지 않는다.

## 입력 DTO

### RenderedArticle

| 필드 | 필수 | 설명 |
|---|---|---|
| `article_id` | 예 | 내부 글 UUID |
| `revision_no` | 예 | 불변 개정 번호 |
| `channel_role` | 예 | `primary_canonical` 또는 `secondary_distribution` |
| `render_stage` | 예 | `preview` 또는 `final` |
| `title` | 예 | 채널 제한 검증을 마친 제목 |
| `body_html` | 예 | 정화된 채널별 HTML |
| `labels` | 아니오 | 채널 태그/라벨 |
| `source_links` | 예 | 독자에게 표시할 출처 목록 |
| `included_claim_ids` | 예 | 해당 채널 본문에 포함한 검증 주장 ID |
| `canonical_source_url` | 조건부 | Blogger final 렌더에는 공개 확인된 WordPress URL 필수 |
| `canonical_link_state` | 예 | `not_applicable`, `pending` 또는 `resolved` |
| `template_hash` | 예 | 원문 링크·원격 media 자리표시자를 포함한 승인 템플릿 SHA-256 |
| `media` | 아니오 | 권리·alt 검증을 마친 게시 자산 |
| `correction_history` | 아니오 | 독자 표시 정정/철회 이력 |
| `content_hash` | 예 | 렌더 결과 SHA-256 |
| `source_manifest_hash` | 예 | 렌더에 사용한 사실·출처 목록 SHA-256 |

각 `media`는 `asset_id`, `delivery_kind: wordpress_remote/public_delivery`, `delivery_id`,
`delivery_url`, `mime_type`, `checksum`, `alt_text`, `caption`, `attribution`, `rights_status`를
가진다. Blogger의 `delivery_id/url`은 장기 `PublicDeliveryAsset`만 허용하며 90일 원근거
object key를 직접 노출하지 않는다.

### PublishCommand

| 필드 | 설명 |
|---|---|
| `publication_attempt_id` | 이미 생성된 단일 외부 동작 UUID; 모든 이벤트의 라우팅 기준 |
| `action` | current intent의 해당 target command에 고정된 `create/update/unpublish/mark_withdrawn` |
| `target_command_hash` | target snapshot, resolved action과 canonical dependency의 지문 |
| `idempotency_key` | 내부 전역 고유 키 |
| `remote_lookup_key` | 채널별 결정적 원격 조회 키 |
| `target_id` | 발행 대상 UUID |
| `publication_intent_id` | 현재 append-only dispatch intent UUID |
| `approval_id`, `approval_subject_hash` | 해당 target command의 current action-specific 승인 |
| `target_snapshot_id`, `target_config_hash` | current PublicationIntent·render·Approval에 고정된 immutable target snapshot |
| `publisher_contract_version`, `publisher_adapter_manifest_hash` | 승인 당시와 실행 배포의 공식 API mapping 구현 지문 |
| `auto_publish_activation_id`, `auto_publish_activation_hash` | validated_auto에서 intent가 고정한 enable 결정; manual은 null |
| `remote_post_id` | update/unpublish/mark_withdrawn에는 필수 |
| `rendered_article` | create/update/mark_withdrawn에는 필수 |
| `publish_at` | 내부 스케줄러가 UTC로 정규화한 예정 시각 |
| `requested_at` | UTC 시각 |
| `correlation_id` | CollectionRun 또는 CorrectionCase ID |

원격 proof는 호출자가 별도 DTO 필드로 제공하지 않는다. 서버의
`publication_content_marker(command)`가 `remote_lookup_key`,
`rendered_article.content_hash`, `target_command_hash`를 `wisdome-publication-v1` schema로
결속해 결정적으로 만든다. adapter는 이 server-derived marker와 exact action, remote identity,
target/blog 및 기대 remote state를 함께 검증한다.

## 출력 DTO

```json
{
  "status": "succeeded",
  "remote_post_id": "external-id",
  "remote_url": "https://example.invalid/post",
  "remote_state": "published",
  "remote_revision": "optional-etag-or-version",
  "scheduled_for": null,
  "published_at": "2026-07-17T00:00:00Z",
  "request_id": "provider-request-id",
  "reconcile_required": false
}
```

`status`는 `succeeded`, `retryable_failed`, `permanent_failed`, `unknown_outcome`,
`manual_required` 중 하나다.
`remote_state`는 `draft`, `scheduled`, `published`, `withdrawn`, `deleted`, `unknown` 중
하나이며 `scheduled_for`와 `published_at`은 해당 상태에 맞게 nullable이다.
본문/토큰/개인정보를 오류 객체에 넣지 않는다. `remote_url`은 `http(s)` scheme과 최대 1000자를
모두 만족할 때만 저장한다. invalid scheme 또는 초과 URL은 raw 값을 버리고 SHA-256만
AuditEvent에 남긴다.

## 필수 동작

0. 외부 호출 전에 PublicationAttempt가 current PublicationIntent의 정확한 target command를
   가리키며 resolved action/command hash, publisher contract/adapter manifest와 target snapshot
   ID/hash가 action-specific Approval,
   적용 가능한 render, current target snapshot과 모두 같은지 트랜잭션으로
   확인한다. 하나라도 다르면 `target_snapshot_stale`로 외부 쓰기 0건이며 재검증·재승인이
   필요하다. target ID와 snapshot ref의 target ID도 같아야 하며 현재 배포 adapter manifest가
   다르면 manual/auto 모두 재-render·재승인 전 외부 호출은 0건이다. 기존 CollectionRun은
   최초 의도 감사값일 뿐 외부 dispatch의 current snapshot gate가 아니다. target 변경 또는
   worker reclaim 뒤에는 actual mutating HTTP 직전 별도 pre-I/O fence가 statement-time의 exact
   `execution_attempt_no`, `execution_generation`, source event와 current receipt
   `lease_generation`을 다시 확인한다. lease가 current이고 유효하거나 같은 transaction에서
   갱신된 경우만 write를 허용하며 이전 transaction의 시각이나 hash만으로 승인하지 않는다.
   current raw token/generation의 결과 정산은 더 높은 reclaim이 없다면 단순 clock expiry만으로
   거부하지 않지만, reclaim된 worker의 result는 current projection을 바꾸지 않고 factual
   late-result observation만 남긴다. raw receipt/domain/terminal capability token은 DB·worker
   context 내부 exact 비교에만 쓰고 event·audit·log·API는 hash만 노출한다. hash는 감사용이며
   capability 판정에 사용하지 않는다.
   CorrectionCase는 새 superseding PublicationIntent와 새 render/Approval을 만든다.
   `approval_mode=validated_auto`이면 intent의 AutoPublishActivation ID/version/hash가 target의
   latest enabled activation과 정확히 같고 그 operational config hash 및 AutoPublishValidation
   ID/material/status도 intent refs와 같은지 호출 직전에 확인한다. revoke/재활성화가 있거나
   validation이 stale/revoked면 과거 intent는 stale이며 새 activation을 참조하는 superseding
   intent 없이 재사용할 수 없다. manual intent는 auto flag와 무관하지만 verified
   connection/current snapshot 및 명시적 관리자 승인은 여전히 필수다.
1. `preflight_connection()`은 대상 블로그 소유권, API 경로, 인증 사용자 capability와
   토큰 만료를 읽기 전용으로 확인한다. 이는 실제 쓰기 성공을 보장하지 않는다.
   `run_canary()`는 `environment=test`로 확인된 격리 target에서 생성→수정→미디어→공개
   확인→철회/삭제와 정리를 실행한다. 운영 target은 동일 채널의 현재 canary를 참조하고
   자체 preflight와 관리자 승인 첫 실제 게시·공개 확인(pilot)을 모두 통과해야
   자동발행을 활성화할 수 있다.
2. `render(stage)`는 canonical body block을 채널 허용 HTML로 변환하고 제한 위반 목록을
   반환한다. 근거·정정 이력은 렌더 단계에서 제거할 수 없다.
   WordPress preview/final은 `canonical_link_state=not_applicable`이며 final은 승인 media
   바인딩 후 외부 게시 전에 생성한다. Blogger preview는 원문 링크 자리와
   `canonical_link_state=pending`을 표시할 수 있다. Blogger final은 WordPress Publication의
   실제 공개 상태와 비인증 GET 200이 확인된 `remote_url`을 결합하고
   `canonical_link_state=resolved`로 만든다. preview와 final의
   `template_hash`는 같아야 하며 승인된 대표 URL·asset ID의 원격 media URL 바인딩 외
   본문 변경은 재승인을 요구한다. 주 채널과
   같은 승인 개정의 `source_manifest_hash`를 유지하고, Blogger의 `included_claim_ids`는
   WordPress 집합의 부분집합이어야 하며 포함한 주장의 의미·출처는 같아야 한다.
3. `prepare_media()`는 WordPress `(target, asset_checksum, presentation_hash)` 매핑을 잠그고
   alt/caption/attribution까지 같은 기존 원격 media만 재사용한다. 새 업로드는 checksum과
   표시 지문 기반 slug 및 description marker를 사용하고 응답
   유실 시 이를 조회해 정확히 한 건만 조정한다. 승인 템플릿의 해당 asset ID 자리만
   원격 media ID/URL로 치환하고
   필요 시 `featured_media`를 지정한다. 외부 호출 전에 revision별 PublicationMedia를
   `prepared`로 저장하고 asset lease generation을 고정한다. `prepared/active` binding과
   scheduled/in-progress publish/update/withdraw/reconcile attempt, 공개 `published`와 공개 상태의
   `marked_withdrawn`을 모두 보호 참조로 계산한다. 완전 `withdrawn/deleted` 또는 원격 본문에서
   참조 제거가 hash/시각과 함께 확인된 binding만 `removed`로 제외한다.
   재사용은 asset 행을 잠그고 `orphaned/pending_delete`를 취소한 뒤 lease generation을 올린다.
   cleanup도 같은 행 잠금과 expected generation CAS에서 보호 refs, in-flight attempts, 열린
   정정/권리 hold와 최신 원격 본문 확인을 다시 계산한다. 하나라도 바뀌면 삭제를 취소한다.
   grace 뒤에는 외부 bytes만 삭제하고 ID/URL/checksum/presentation/deleted 시각·사유·reconcile
   hash mapping tombstone은 발행 감사 만료까지 유지한다.
   Blogger는 `(asset_checksum, presentation_hash)`의 immutable PublicDeliveryAsset을 만들거나
   재사용하고 장기 공개 HTTPS URL만 final 렌더에 결합한다. 공개/marked-withdrawn 글 참조가
   남으면 90일 evidence purge 뒤에도 URL을 유지한다. 모든 원격 본문에서 제거/철회가 확인되고
   ref=0, 열린 정정·권리 hold 없음, 30일 grace 경과를 같은 lock/CAS에서 재확인한 뒤에만
   전달 bytes를 삭제한다. 재참조와 delete가 경합하면 lease generation이 바뀌어 stale delete가
   반드시 무효화된다.
4. `execute(command)`는 fenced execution generation에서 외부 쓰기 한 번만 수행한다.
   WordPress create는 Publication UUID 기반 slug와 versioned HTML marker, Blogger create는
   UUID 기반 전용 label+versioned HTML marker를 사용한다. marker는 stable lookup identity와
   `target_command_hash`, rendered `content_hash`를 포함하고 raw secret이나 관리자 사유는
   포함하지 않는다.
5. `reconcile()`은 응답 유실 등 `unknown_outcome`에서 `remote_lookup_key`로 원격 결과를
   찾는다. create는 exact target/blog와 lookup/command/content marker, 기대 remote state가
   모두 일치하는 정확히 한 건만 성공이다. update/mark/unpublish는 목록의 유사 객체가 아니라
   command의 exact `remote_post_id`를 GET한다. update/mark는 command/content marker와 공개
   상태, unpublish는 WordPress `draft|trash` 또는 Blogger `draft`처럼 실제 비공개 상태를
   확인한다. 0건은 create를 반복하지 않고 bounded backoff로 최대 5회 read-only reconcile한
   뒤 `manual_required`, 복수건은 즉시 `manual_required`다. Blogger 목록 조회는
   `nextPageToken`을 따르며 live/draft/scheduled 전체 합산 최대 15 page·750 item·wall 30초
   (`maxResults=50/page`) 한도 소진을 성공이나 0건으로 해석하지 않는다.
6. `fetch_remote_state()`는 원격 게시물이 관리자나 외부 정책으로 바뀌었는지 확인한다.
7. 정정은 항상 기존 `remote_post_id`를 수정하며 새 글을 만들지 않는다.
8. 철회 API가 없으면 capability에 `unpublish=false`, `mark_withdrawn=true`를 선언하고 기존
   본문 상단에 철회 안내·시각·출처를 넣는 update를 수행한다.
9. 예약 요청은 내부 상태를 `scheduled`로 두고 `publish_at`에 WordPress를 `publish`로
   생성·수정한다. WordPress REST 상태와 비인증 공개 URL 200 응답이 확인된 뒤에만
   Blogger를 공개한다. WordPress 실패·비공개·지연이면 Blogger도 보류한다. 플랫폼의
   `future`/`publishDate`는 canary 검증에만 사용하고 두 채널 운영 예약에는 사용하지 않는다.
   canary에서는 UTC `publish_at`을 WordPress `date_gmt`와 Blogger RFC 3339
   `publishDate`로 변환하고 왕복 응답 시각을 비교한다.
10. 정정 또는 `mark_withdrawn`은 WordPress 기존 글과 최신 공개 URL을 먼저 확인한 다음
   Blogger 기존 글을 수정한다. WordPress permalink가 달라졌다면 Blogger의
   `canonical_source_url`과 독자 표시 원문 링크를 같은 시도에서 함께 갱신한다.
11. 완전 `unpublish`는 WordPress가 `withdrawn/deleted/draft` terminal state에 도달한 뒤
   Blogger `revert/delete`를 실행한다. 이 분기에는 공개 URL 200이나
   `canonical_source_url`이 필요하지 않으며 이전 URL은 감사용으로만 보존한다.

각 Blogger command와 논리 attempt는 같은 intent·revision·article·environment의 정확한
`primary_canonical` WordPress command/attempt를 `depends_on_attempt_id`와
`publication-dependency-v1` subject hash로 결속한다. dispatch는 WordPress attempt를 먼저 만들고
Blogger attempt는 그 ID를 참조한 채 대기한다. release, execution gate, final render와 reconcile은
target ID나 생성 시각으로 WordPress Publication을 다시 검색하지 않고 이 FK만 따른다. dependency
subject는 immutable lineage를, Blogger final render의 `canonical_source_url`·본문·content hash는
실제 검증 URL을 동결한다. test와 production dependency를 섞거나 legacy material을 추측하지 않는다.

create의 내부 preflight는 bounded read가 정상 완료되고 응답 schema, target/blog,
pagination이 정확하며 lookup/marker match가 0건임을 증명할 때만 POST를 허용한다. timeout,
408/429/5xx, JSON/schema 오류, 불완전 pagination, target/blog 불일치와 복수 match는
`exact not-found`가 아니므로 외부 write는 0건이다. create/update/mark의 mutating 2xx도 exact
target/blog·remote ID·기대 state·title·marker를 제거한 canonical body를 모두 검증한 뒤에만
`succeeded`다. unpublish는 exact target/blog·remote ID와 실제 private state를 검증하고
`withdrawn`을 합성하지 않는다. mutation 응답에 proof 필드가 없으면 인증 read-back으로
동일 검증을 수행하며, 그래도 증명할 수 없으면 unknown/manual reconcile이다.

## 오류 분류

| 분류 | 예 | 처리 |
|---|---|---|
| 인증 갱신 가능 | 만료 access token | 한 번 갱신 후 동일 멱등 키 재시도 |
| 읽기 속도 제한/일시 장애 | read 408/429/5xx/timeout | Retry-After delay-seconds 또는 IMF-fixdate를 bounded seconds로 정규화해 우선, 지수 백오프+지터, 최대 횟수 제한 |
| mutating 결과 불명 | write timeout/연결 종료 또는 미적용을 증명하지 못한 408/429/5xx | `unknown_outcome`→read-only reconcile; create 직접 반복 금지 |
| 영구 요청 오류 | 권한 부족, 형식/용량 위반 | 즉시 영구 실패, 관리자 조치 안내 |
| 결과 불명 | 외부 성공 뒤 연결 종료 | `unknown_outcome`→reconcile; create 즉시 반복 금지 |
| 정책 차단 | 공식 쓰기 수단 부재/폐기 | target을 `blocked`, 자동발행 비활성화 |

## 채널 구현 게이트

- WordPress 어댑터는 Core REST API의 posts/media route와 HTTPS Application Password를
  사용한다. Application Password에는 세부 scope가 없으므로 전용 사용자 역할이
  `read`, `edit_posts`, `edit_published_posts`, `publish_posts`, `delete_posts`,
  `delete_published_posts`, `upload_files` 등 필요한 capability만 갖고 사이트·사용자·
  플러그인 관리 권한은 갖지 않아야 한다. preflight와 쓰기 canary를 분리해
  대상 자체 도메인, permalink, draft/update/trash/media와 공개 확인을 검증한다.
- Google Blogger 어댑터는 공식 OAuth와 Blogger API의 posts 리소스가 제공하는 실제
  기능만 선언한다. 별도 바이너리 미디어 업로드는 `false`로 두고 권리가 확인된 공개
  HTTPS 자산 URL만 본문에 포함한다.
- 로그인 세션을 Playwright/Selenium으로 조작해 편집기를 자동 클릭하는 방식은 이
  계약의 합법적 전송 수단으로 인정하지 않는다.

## 계약 테스트

- 동일 create 명령 100회 전달 시 원격 글은 하나다.
- 첫 create가 적용됐지만 500을 반환하고 원격 검색 반영이 지연돼도 두 번째 create는 0건이다.
- create 성공 직후 응답 유실에서 WordPress slug 또는 Blogger label+marker로 reconcile이
  원격 글을 정확히 한 건 찾아낸다.
- WordPress 미디어 업로드 응답 유실에서 원격 media를 조정하고 같은 checksum을 다시
  업로드하지 않는다. 표시 지문이 다른 자산은 분리되고, 다른 `published` 또는
  `marked_withdrawn` 공개 글의 PublicationMedia 참조가 남은 자산은 고아 삭제되지 않는다.
- 한 채널 실패가 다른 Publication 상태를 되돌리지 않는다.
- WordPress가 실제 공개되고 공개 URL이 200을 반환하기 전 Blogger create가 실행되지
  않으며, Blogger final 렌더의 원문 링크가 해당 URL과 일치한다.
- Blogger preview는 pending 링크를 표시하고 final은 같은 `template_hash`에서 URL만
  결합한다. 그 외 차이가 있으면 이전 승인을 사용할 수 없다.
- WordPress final은 공개 전에 생성되고 `canonical_link_state=not_applicable`이며 Blogger
  final만 WordPress 공개 확인 뒤 생성된다.
- WordPress와 Blogger 렌더의 `source_manifest_hash`가 다르면 발행이 차단된다.
- Blogger의 `included_claim_ids`에 WordPress 원문에 없는 주장이 있거나 포함 주장의
  출처 의미가 달라지면 발행이 차단된다.
- update가 동일 remote post를 변경하고 정정 이력을 유지한다.
- create preflight는 schema-complete bounded 조회가 exact target/blog의 0 match를 증명한 경우에만
  POST하며 timeout·오류·불완전 pagination·복수 match에서는 write가 0건이다.
- create/update/mark의 mutating 2xx는 응답 자체 또는 그 직후 bounded authenticated exact GET이
  exact target/blog·remote ID·state·title·actual canonical body를 증명하지 못하면 성공하지 않으며,
  unpublish는 remote가 여전히 live/published이면 withdrawn으로 투영하지 않는다.
- marker를 보존한 채 remote body 단락을 변경해도 성공하지 않는다. provider가
  반환한 actual raw body에서 marker를 분리한 canonical body hash가 frozen render와
  일치해야 하며 marker 내 expected hash만으로 content proof를 대체하지 않는다.
- Blogger exact match가 두 번째 page에 있어도 bounded `nextPageToken` 순회로 찾고,
  3개 state 합산 15 page·750 item·wall 30초(`maxResults=50/page`) 초과는 fail-closed한다.
- execution/reconcile generation의 늦은 result와 terminal callback은 current Publication을
  변경하지 않고 factual stale observation만 append한다.
- mutating call 전 read/preflight 429는 write 0건, write marker 0건으로 정산하고
  `Retry-After`의 delay-seconds와 IMF-fixdate를 모두 유지한다. 같은 source event의 running reconcile을 더 높은
  current receipt lease가 reclaim해도 같은 reconcile generation을 재개하며, 이전
  capability result는 exact `PublicationReconcileDeliveryObservation` FK를 가진
  reconcile-parented `PublicationLateExecutionResult`에만 append한다. claim/reclaim마다
  generation+receipt lease generation이 고유한 delivery observation을 남긴다.
- terminal callback은 receipt가 `processing`인 동안 `terminal_reserved_at`,
  internal raw `terminal_lease_token`, `terminal_lease_generation`, `terminal_lease_token_hash`,
  `terminal_error_code`를
  all-or-none·set-once로 먼저 저장·검증하고, domain을 `delivery_failed/manual_required`로 투영한 뒤에만
  receipt/event를 DLQ로 전이한다. 예약된 receipt 삭제·성공 전이는 금지되고 전 단계는 한
  transaction이다.
- execution AuditEvent identity는 exact source event·`execution_generation`·receipt
  consumer name·`lease_generation`·token hash·write authorization/marker를 결속하고 reconcile
  audit는 delivery observation과 terminal reservation hash/error까지 결속한다. raw token은
  감사에 포함하지 않는다. retry/reconcile는 T020 Dispatch ledger의 frozen
  target/Publication/logical Attempt cohort에 행을 추가하지 않는다.
- 철회 capability별로 unpublish 또는 mark_withdrawn이 선택된다.
- 완전 unpublish는 WordPress terminal state 뒤 공개 URL 없이 Blogger revert/delete를
  실행하고, mark_withdrawn은 공개 URL을 유지해 두 채널 본문을 갱신한다.
- 토큰·본문이 구조화 로그와 AuditEvent 오류 세부에 나타나지 않는다.
- 운영 target의 preflight, 동일 채널 test target의 현재 정책 canary와 관리자 승인
  파일럿 중 하나라도 통과하지 않으면 자동발행을 켤 수 없다.
- target snapshot mismatch에서 외부 요청 0건; credential/capability 변경 후 재승인까지 차단
- validated_auto run/intent 생성 뒤 auto flag off 또는 validation stale 시 외부 요청 0건
- auto off→on 뒤 과거 activation을 참조한 예약 intent는 되살아나지 않고 새 activation·intent 없이는 외부 요청 0건
- WordPress unpublish와 Blogger mark_withdrawn처럼 target별 action이 달라도 각 command/승인이 intent hash에 고정됨
- unpublish는 render 없이 remote post/state·사유·정정 근거 hash 승인으로 실행되고 content action은 preview render 없이는 차단
- 게시 91일 뒤 원 EvidenceAsset을 purge해도 공개 Blogger 본문의 모든 PublicDeliveryAsset URL이 200이고, 참조 0+30일 전에는 삭제되지 않음
- pending delete worker가 lock을 기다리는 동안 같은 자산을 새 scheduled publication이 prepare하면 lease CAS가 실패해 bytes 삭제 0건
- media/delivery delete 성공 직후 응답 유실을 재전달해도 physical delete는 한 번이고 mapping tombstone·result hash는 동일
- base URL/remote blog ID 변경 PATCH 거절; 새 target의 기존 remote post ID 재사용 0건
- WordPress Application Password와 Blogger OAuth token 폐기 후 target이 각각
  `revoked` 또는 `expired`로 전환되고 쓰기가 거부된다.

## English / AI-readable — T024 canonical publication dependency

Every Blogger command, including unpublish, names the single `primary_canonical` WordPress target in the
same intent and environment. Dispatch creates the WordPress logical attempt first and freezes its exact ID
on the Blogger attempt through `depends_on_attempt_id`. `publication-dependency-v1` hashes both immutable
attempt lineages, while the Blogger final render separately freezes the verified WordPress URL in
`canonical_source_url`, body HTML, and the content hash. Release, execution, final rendering, and
reconciliation follow only that FK; they never select another WordPress Publication by timestamp, target
channel, or article. Create/update require a public dependency and verified URL, mark-withdrawn preserves
the last verified URL, and unpublish waits for the WordPress withdrawal terminal result without requiring
a public URL. Cross-environment and unverifiable legacy dependencies fail closed.

## English / AI-readable — T021 execution and reconciliation contract

- T020 response `attemptNo=1` is an immutable acceptance value. A publication event uses
  `execution_attempt_no` for business write generation 1..5 and `execution_generation`
  for the domain claim fence; the receipt owns a separate `lease_generation` and
  reconciliation owns `reconcile_attempt_no` 1..5.
- New execution is routed by exact `publication.requested@2` payload
  `{publication_attempt_id, execution_attempt_no}`. Version 1 may only replay an exact
  existing source-event binding or bind a proven virgin generation one. Every other v1
  delivery is an audited `legacy-unverifiable-v1` no-op quarantine and never guesses the
  current counter.
- The pre-write fence rechecks the current execution generation, source event, consumer
  receipt lease, approval, intent, target snapshot, kill switch, command and marker at
  statement time immediately before external I/O.
  Settlement by the still-current token and generation is not rejected solely because
  the lease clock elapsed. A reclaimed worker can append a factual late-result
  observation but cannot write remotely or mutate the current projection. Raw receipt,
  domain, and terminal capability tokens remain authoritative internally; their hashes
  are audit-only and are the only values exposed by events, audit, logs, and APIs.
- A mutating timeout, disconnect, or 408/429/5xx without proof of non-application is an
  unknown outcome. It enters read-only reconciliation and never repeats create directly.
- Create mutates only after a schema-complete bounded preflight proves exact target/blog,
  complete pagination, and zero lookup/marker matches. Timeout, 408/429/5xx, malformed
  JSON/schema, incomplete pagination, target mismatch, or multiple matches authorize no write.
- A mutation 2xx succeeds only when either that response or an immediate bounded authenticated exact GET,
  and every reconciliation success, proves exact action, remote identity, target/blog, expected state,
  title, versioned server-derived lookup/command/content marker, and a canonical hash recomputed from the
  actual provider-returned body. The marker is
  computed by `publication_content_marker(command)` from `remote_lookup_key`, rendered
  content hash and target command hash under `wisdome-publication-v1`; it is not a
  caller-supplied `PublishCommand` field and its embedded expected hash is not actual-body
  proof. Update, mark, and unpublish
  use the exact remote ID. Zero matches receive at most five bounded reads, multiple
  matches manualize immediately, and Blogger follows `nextPageToken` across
  live/draft/scheduled for at most 15 pages, 750 items and 30 wall-clock seconds
  (`maxResults=50` per page).
- A pre-write read failure settles with no authorized external write or write marker and
  preserves both delay-seconds and IMF-fixdate Retry-After as bounded seconds. A higher
  current receipt lease rebinds the same running reconcile generation; every claim/reclaim
  appends an identity-frozen `PublicationReconcileDeliveryObservation` whose state/finish time may advance
  monotonically, and the prior capability
  can append only a factual late result through that exact observation FK.
- Terminal callbacks bind the exact source event and execution/reconcile generation. They
  reserve the all-or-none, set-once `terminal_reserved_at`, internal raw
  `terminal_lease_token`, `terminal_lease_generation`, `terminal_lease_token_hash`, and
  `terminal_error_code` while the receipt remains processing, verify them in the domain
  callback, then atomically project delivery-failed/manual-required, audit, release
  dependents, and dead-letter. Reserved receipt deletion/success is forbidden. A replay is a no-op.
- Execution/reconcile audit identity includes source event, domain/reconcile generation,
  consumer name, receipt lease generation, token hash, write authorization/marker, delivery
  observation, and terminal reservation hash/error; raw tokens are excluded. Valid remote
  URLs are bounded to 1000-character HTTP(S), while invalid raw URLs are discarded and only
  their SHA-256 is audited. Retry and reconcile never mutate the immutable Dispatch-ledger cohort.
