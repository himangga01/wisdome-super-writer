# Quickstart & Acceptance Guide

이 문서는 구현 후 로컬 환경과 MVP 수용 기준을 검증하기 위한 기준 절차다. 현재 저장소에는
아직 애플리케이션 코드가 없으므로 아래 명령은 `$speckit-tasks`와 구현이 끝난 뒤 제공되어야
하는 목표 인터페이스다.

## 0. 승인된 MVP 채널 범위

자체 도메인의 관리형 WordPress를 주 발행·대표 원문 채널로, Google Blogger를 보조
배포 채널로 사용한다. 발행 순서는 WordPress 원격 글의 실제 공개와 대표 URL 200 확인 후 Blogger
채널 맞춤본 발행이다. 두 채널은 공식 인터페이스와 별도 테스트 사이트에서만 검증하며,
DOM 클릭, 쿠키/세션 재사용 또는 비공식 내부 API로 글쓰기를 자동화하지 않는다.

## 1. 사전 준비

- Python 3.12와 `uv`
- Docker/Compose 또는 동등한 PostgreSQL 17+, Redis, S3 호환 저장소
- PDF 안전 검사·렌더링과 정적 이미지 검사용 시스템 패키지,
  `paddleocr[doc-parser]==3.7.0`, lock file로
  고정된 호환 PaddlePaddle 3.x, checksum manifest가 있는 한국어·영어 PP-OCRv5 모델
- 자체 도메인·HTTPS를 사용하는 관리형 WordPress 테스트 사이트, 전용 최소 권한 사용자와
  Application Password
- Google Cloud 프로젝트, Blogger API, OAuth 웹 클라이언트와 전용 테스트 블로그
- 공공데이터포털 서비스 키: 청약홈, LH, 마이홈, 필요 지역 운영기관
- SEC 등 출처 정책이 요구하는 연락 가능한 User-Agent
- 공개 인터넷으로 노출되지 않은 관리자 테스트 계정

운영/테스트 계정과 저장소는 분리한다. 실블로그에서 계약 테스트를 수행하지 않는다.
운영 target은 동일 채널 test target의 최신 canary를 참조하고 자체 읽기 전용 preflight와
관리자 승인 첫 실제 게시·공개 확인 파일럿을 통과한 뒤에만 자동발행을 켠다.

## 2. 설정 계약

구현은 `.env.example`에 아래 **키 이름만** 제공한다. 실제 값은 커밋하지 않는다.

```dotenv
DJANGO_SECRET_KEY=
DJANGO_ALLOWED_HOSTS=localhost
DATABASE_URL=postgresql://...
CELERY_BROKER_URL=redis://...
OBJECT_STORAGE_ENDPOINT=
OBJECT_STORAGE_BUCKET=
OBJECT_STORAGE_REGION=
OBJECT_STORAGE_ACCESS_KEY_REF=
OBJECT_STORAGE_SECRET_KEY_REF=
GOOGLE_OAUTH_CLIENT_ID_REF=
GOOGLE_OAUTH_CLIENT_SECRET_REF=
GOOGLE_OAUTH_REDIRECT_URI=
WORDPRESS_BASE_URL=https://blog.example.test
WORDPRESS_USERNAME_REF=
WORDPRESS_APPLICATION_PASSWORD_REF=
PUBLIC_ASSET_BASE_URL=
EXTRACTION_PROFILE_DIR=config/extraction-profiles
PADDLEOCR_MODEL_ROOT=/models/paddleocr
PADDLEOCR_DEVICE=cpu
DATA_GO_KR_SERVICE_KEY=
SEC_USER_AGENT=
DEFAULT_TIMEZONE=Asia/Seoul
GLOBAL_KILL_SWITCH=true
```

WordPress 평문 비밀번호, Blogger refresh token, 로그인 쿠키 또는 채널 편집기 내부
endpoint 설정은 존재해서는 안 된다. 위 `*_REF` 값은 비밀 자체가 아니라 운영 비밀
저장소의 참조 키다.
`DATA_GO_KR_SERVICE_KEY`는 예외적으로 `env://DATA_GO_KR_SERVICE_KEY` 참조가 가리키는
수집 worker 전용 환경 변수다. 값은 `worker-source-check`와 `worker-collect`에만 전달하고
web, 편집, evidence fan-out, 추출, 발행 worker에는 전달하지 않는다.

### 2.1 추출 프로필 최초 등록

모델·parser 설정은 파일이 존재한다는 이유만으로 실행할 수 없다. 아래 목표 명령은
`config/extraction-profiles/manifest.json`과 로컬 implementation/model/config/calibration
checksum을 검증해 immutable `draft` ExtractionProfileSnapshot만 만든다.

```powershell
uv run python src/manage.py import_extraction_profiles --root config/extraction-profiles
uv run python src/manage.py verify_extraction_profile_files --root config/extraction-profiles
```

그 다음 관리자가 관리자 화면에서 profile material hash, PaddleOCR 한국어/영어 모델,
generic engine별 locator/validation mode와 calibrated 골든 평가를 확인하고 개별 profile을
승인한다. import/배포 명령은 승인을 대신할 수 없고 모든 approve/retire는 재인증과
AuditEvent를 요구한다. 최소 승인 집합은 native PDF, 한국어/영어 PaddleOCR, HTML,
structured, spreadsheet, HWPX, 격리 legacy HWP→PDF converter, browser capture, media와 manual
profile이다. 숫자 confidence를
내는 generic profile은 calibration key/version/hash도 승인돼야 한다.

관리자 화면은 `GET /api/v1/extraction-profiles?approvalState=draft`와 단건 material/model/
calibration manifest를 보여주고, 최근 재인증 뒤
`GET /api/v1/extraction-profiles/{profileId}/report`의 subject material hash, sample·metric·threshold와
overall result를 object version/hash까지 검증한 다음
`POST /api/v1/extraction-profiles/{profileId}/decisions`에 expected material hash, expected latest
decision ID와 request key를 보내 approve/retire한다. stale CAS는 409, 동일 key/payload는 같은
결정을 반환해야 한다. 특히 `paddle-ko-v1`/`paddle-en-v1`은 package 3.7.0, PaddlePaddle 3.x,
PPStructureV3와 로컬 model checksum이 모두 일치하지 않으면 승인할 수 없다.

MVP manifest의 필수 key는 `native-pdf-v1`, `paddle-ko-v1`, `paddle-en-v1`,
`html-deterministic-v1`, `structured-deterministic-v1`, `spreadsheet-deterministic-v1`,
`hwpx-deterministic-v1`, `legacy-hwp-v1`, `browser-capture-deterministic-v1`,
`media-deterministic-v1`, `manual-entry-v1`이다. 초기 generic
profile은 deterministic/manual이고 calibrated generic key는 MVP 필수 집합이 아니다.

```powershell
uv run python src/manage.py verify_extraction_profile_snapshots --require-approved-mvp
```

동일 manifest 재가져오기는 새 snapshot을 만들지 않아야 하고, material 변경은 기존 행을
수정하지 않고 새 version의 draft를 만들어야 한다. 승인 집합이 없거나 draft/retired/hash
mismatch이면 관련 worker는 시작 전에 실패해야 한다.

### 2.2 Legacy HWP profile-admin과 활성화

운영 HWP manifest는 image에서 계산한 값을 재사용하지 않고 릴리스가 검토한 host file과
SHA-256을 주입한다. Paddle 모델, HWP 구현 파일, read-only manifest와 실제 UDS identity probe를
한 경계에서 확인하려면 다음 명시적 서비스를 사용한다.

```powershell
docker compose --profile admin run --rm profile-admin
```

기본 `legacy-hwp-v1@1.1.0`은 `golden_corpus_approved=false`인 불변 draft라 승인하지 않는다.
T032는 지원 corpus 성공뿐 아니라 unsupported, warning, missing-font, tamper, exit 20/21/22가
EvidenceAsset/ready/DocumentExtraction 0건과 `manual_required`로 끝나는 acceptance artifact를
먼저 고정한다. 그 artifact와 release image/manifest digest를 결속한 새 1.2.0 profile을
`golden_corpus_approved=true`로 import/승인하고, 그 다음 1.1.0을 retire한다.

기본 `worker-extract --concurrency=1`과 converter 하나는 한 쌍이다. 수평 확장은 worker마다
전용 UDS/input volume/converter를 배치한 Linux 운영 override가 있을 때만 허용한다. repo 기본
Compose는 gVisor를 강제하지 않는다.

## 3. 로컬 부팅 목표

```powershell
uv sync --all-groups
docker compose up -d postgres redis object-storage
uv run python src/manage.py migrate
uv run python src/manage.py createsuperuser
uv run python src/manage.py seed_source_registry
uv run python src/manage.py import_extraction_profiles --root config/extraction-profiles
docker compose up -d web worker-source-check beat
```

`worker-source-check`는 `outbox.dispatch`와 `source.check` 전용 큐만 소비한다. 따라서 승인
전에도 source-check outbox를 전달하고 외부 접근·파싱 점검을 수행하되 일반 수집·편집·발행
작업은 실행하지 않는다. 이 단계에서는 `GLOBAL_KILL_SWITCH=true`를 유지한다. 관리자 화면에서
2.1의 최소 profile과 두 주제의 draft source registry membership을 검토·승인한 다음에만
나머지 파이프라인 worker를 시작한다. seed/import 명령은 직접 승인할 수 없다.

```powershell
uv run python src/manage.py verify_extraction_profile_snapshots --require-approved-mvp
uv run python src/manage.py verify_source_registry_snapshots --require-approved-mvp
docker compose up -d worker-collect worker-extract ocr-worker worker-editorial worker-publish worker-publish-blogger worker-reconcile beat
```

필수 확인:

```powershell
uv run python src/manage.py check
uv run python src/manage.py makemigrations --check --dry-run
uv run pytest tests/unit tests/contract -q
```

`GLOBAL_KILL_SWITCH=true`로 처음 부팅해 외부 쓰기를 막는다. 관리자 화면에서 출처,
WordPress와 Blogger 테스트 target의 읽기 전용 preflight와 격리 쓰기 canary를 모두
통과한 뒤에만 테스트 환경의 스위치를 해제한다.
같은 topic/window라도 target 집합 또는 approval mode가 다르면 서로 다른 request fingerprint와
CollectionRun이 생겨야 한다. run 생성 뒤 PublicationTarget의 base URL, remote blog ID,
credential ref, capability 또는 validation state를 바꾸면 진행 run의 고정 target snapshot은
바뀌지 않고 기존 승인은 stale가 되며 외부 호출은 재검증·재승인 전 0건이어야 한다.
Schedule을 실행 중 수정해도 이미 만들어진 run의 schedule/target/mode snapshot에는 영향을
주지 않아야 한다.
target 없는 초안 run은 이후 별도 manual PublicationIntent로 WordPress/Blogger target을
선택할 수 있어야 한다. target snapshot 변경·재승인과 CorrectionCase update는 기존 run을
바꾸지 않고 superseding intent, 새 render와 새 Approval을 만들어야 한다. validated_auto
intent 생성 뒤 autoPublishEnabled를 끄거나 validation을 stale/revoked로 만들면 예약 시각의
외부 호출은 0건이어야 한다.
correction intent 생성 뒤 article current revision이 바뀌면 기존 intent/Approval로 update를
보내지 않고 409로 새 exact-revision intent를 요구해야 한다.

### 3.1 자동발행 validation과 activation

target별 canary 보고서를 immutable object version/hash로 저장한 뒤
`POST /targets/{targetId}/auto-publish-validations`에 registry, source-adapter, extraction-profile,
generation pipeline, editorial, quality-gate, render/channel contract와 publisher-adapter manifest를
모두 제출한다. candidate는 draft이고, 관리자가 전체 보고서를 확인·재인증해 `/decisions`의
passed 결정을 남겨야 한다. 보고서 확인은
`GET /targets/{targetId}/auto-publish-validations/{validationId}/report`로 하며 validation material
hash와 report object version/hash가 다르면 결정할 수 없다. 이어
`/targets/{targetId}/auto-publish`에 current validation refs,
expected latest activation ID와 request key를 보내 enabled activation을 만든다.

validated-auto Schedule/CollectionRun/PublicationIntent마다 target/snapshot/validation/activation
target 집합이 정확히 같고 target별 하나씩이어야 한다. activation이 고정한 validation set과
요청 refs가 다르거나 source/extraction/generation/quality/channel/adapter material 하나라도
바뀌면 저장 또는 외부 호출은 409/0건이다. off→on은 새 activation을 만들며 과거 예약 intent를
되살리지 않고 새 superseding intent를 요구한다. concurrent enable/revoke 중 CAS 하나만 성공하고
동일 request key replay는 결정 한 건이어야 한다.

## 4. 관리자·보안 검증

1. 익명 사용자가 `/admin/`과 `/api/v1/*`에 접근하면 로그인 또는 401/403을 받는다.
2. staff가 아닌 계정은 로그인해도 서비스 기능에 접근할 수 없다.
3. OAuth callback을 제외하고 CSRF header가 없는 모든 same-origin API 읽기/쓰기 요청은
   로그인 session이 있어도 403을 반환한다.
4. 자동발행 활성화, 전체 kill switch 해제, 철회와 보존 삭제는 최근 재인증이 필요하다.
5. 관리자 화면, 로그, 오류와 AuditEvent에서 토큰/쿠키/API key/본문 전문이 검색되지 않는다.
6. Blogger OAuth 연결 해제 후 refresh token이 폐기되고 target이 `revoked`가 된다.
7. WordPress Application Password 폐기 또는 secret 참조 제거 후 쓰기가 거부되고
   자동발행이 비활성화된다.

## 5. 출처 레지스트리 검증

관리자 화면에서 각 source의 `base URL`, access method, authority tier, poll limit, terms/license
URL, MIME 목록과 registry version을 확인한다. `Check source`는 외부 게시를 일으키지 않고
다음 결과만 만든다.

- DNS/redirect/MIME/크기 정책 통과 여부
- API/RSS/HTML 최소 응답 스키마와 안정 ID 추출
- ETag/Last-Modified/콘텐츠 해시
- 권리 기본값과 수동 확인이 필요한 자산 유형
- rate limit 또는 인증 실패의 분류된 오류

승인되지 않은 source는 일정 실행에 포함될 수 없다.
external config, secret reference identity, robots/terms/license URL과 rate-limit policy가
definition snapshot hash에 포함돼야 한다. externalConfig에 authorization/cookie/token/password
또는 실제 secret 값을 넣은 요청은 거절되고 응답·로그·AuditEvent에도 노출되면 안 된다.
SourceDefinition의 topic 변경 PATCH는 거절돼야 한다. 한 source 설정만 바꿔 주제 registry
v2를 승인하면 바뀐 source의 새 definition snapshot과 변경되지 않은 모든 active source의
직전 snapshot membership이 v2 manifest에 함께 있어야 한다. v1 CollectionRun은 이후 v2가
생겨도 v1 registry snapshot/manifest를 계속 가리키며 disabled source까지 당시 membership을
재현해야 한다.
최초 seed는 draft registry만 만들어야 하고 재인증 관리자 승인/AuditEvent 전에는 수집
실행이 거절돼야 한다. 동일 seed 재실행은 같은 draft manifest를 중복 생성하지 않고,
approved/retired manifest 파일 불일치는 collect worker 시작을 차단해야 한다.

관리자 화면은 `/api/v1/source-registries`에서 base version 전체 membership을 carry-forward한
draft를 만들고, membership PUT의 row-version/manifest CAS로 source snapshot·enabled/order를
수정한다. 각 source의 independence group, owner/editorial control과 원보도/전재 판정도 함께
검토한다. `/decisions` approve/retire는 expected row version/manifest, request key와 최근 재인증을
요구한다. 동일 draft/mutation/decision replay는 하나만 남고 같은 key의 다른 payload나 stale
CAS는 409여야 한다. approved registry membership은 직접 수정할 수 없다.

수집 fixture는 SourceCollectionAttempt에 adapter name/version/implementation/config manifest와
response checksum을 고정해야 한다. 배포 adapter manifest 불일치, 다른 attempt의 RunSourceItem
연결과 승인 validation의 source-adapter manifest 불일치는 추출 전에 차단한다.

### 5.1 청약홈·LH 공공데이터 수집

`config/source-registry/housing_subscription.json`은 청약홈의 한국부동산원 ODCloud API와
LH 목록·상세·공급 API의 실제 요청 origin, page 상한, page 크기와
`env://DATA_GO_KR_SERVICE_KEY`, 공식 record host, request/elapsed budget, API·상세·첨부 MIME
계약만 고정한다. 인증 요청은 HTTPS의 adapter별 정확한 host/path에서만 실행하고 다른
환경 변수나 DB credential reference는 거부한다. 서비스 키 값, 임의 query/header 이름,
JSONPath, selector 또는 실행 가능한 식은 레지스트리에 저장하지 않는다.

청약홈은 API operation별 `category + HOUSE_MANAGE_NO + PBLANC_NO`, LH는
`CCR_CNNT_SYS_DS_CD + PAN_ID + UPP_AIS_TP_CD + AIS_TP_CD`를 stable identity로 사용한다.
목록 제목, page URL, ETag 또는 응답 순서는 lineage identity가 아니다. 목록 page가 반복되거나
필수 identity/date/detail 구조가 사라지거나 page 상한 안에 전체 범위를 소진하지 못하면
0건 성공이 아니라 source 실패다. `totalCount/ALL_CNT`보다 적게 받은 채 빈 page가 오거나
request/elapsed budget을 소진해도 부분 성공을 남기지 않는다.

기간 판정은 수정 시각을 우선하고 없으면 게시 시각을 사용한다. 같은 실행에서 같은 stable
identity가 두 번 나오면 병합하지 않고 실패한다. 정정·취소·접근 불가는 출처의 명시적
상태/flag만 사용하며 timeout, 5xx, JSON 구조 변경 또는 목록 일시 누락을
`unavailable`로 추론하지 않는다. 시간창 밖에서는 과거에 성공적으로 관측한 ID만 승인된
reconciliation 기간 안에서 재조회하여 정정·철회·복원을 찾는다. API 응답과 공식 상세
페이지에서 발견한 첨부는 frozen `recordHosts` 안의 canonical URL, file ID, MIME, 크기,
checksum과 권리 상태를 SourceItem에 전달한다. 직접 `href`나 안전하게 해석 가능한 handler
URL이 없는 scripted download 표시는 누락하지 않고 source check/collection을 실패시킨다.
첨부 다운로드는 frozen `attachmentContentTypes`와 선언·sniff MIME을 모두 검사한 뒤에만
확장자별 extractor로 보낸다.

source snapshot v3는 adapter version, frozen config hash, adapter·HTTP 보안·secret·canonical
hash 구현 파일 checksum과 Python/direct dependency version을 함께 고정한다.
SourceCollectionAttempt의 요청 지문·응답 checksum과 SourceItem/RunSourceItem은 한 source
transaction에서 성공 상태와 함께 커밋된다. 실제 HTTP redirect/IP 시도마다 request budget을
차감하며 응답의 credential 계열 field는 저장 전에 마스킹한다. 동시 delivery는 성공 checksum이
같을 때만 replay로 인정하고 다음 단계로 간 run을 수집 상태로 되돌리지 않는다.

legacy 호환 첫 관측은 현재 schema 기준 SourceItem을 append하고 `unchanged`로 표시한다.
SourceItem과 RunSourceItem은 ORM/PostgreSQL guard에서 update/delete가 거부되며,
RunSourceItem은 활성 registry membership과 성공한 직전 관측만 가리킨다. `unchanged`는 변경
이벤트와 신규 evidence가 0건이며, `new_version/corrected/restored`만 추출한다.
`retracted/unavailable/restored`는 전체 관측 lineage에서 기존 게시물 영향을 평가한다.
복원은 적용 전 terminal case를 닫고 별도 restoration case를 만든다.

로컬에서 source check 또는 주택 수집 worker를 실행하기 전에 실제 값을 `.env`에만 둔다.

```dotenv
DATA_GO_KR_SERVICE_KEY=발급받은_서비스_키
```

### 5.2 T012 접근·권리·최신성 정책 확인 절차

다음 순서로 확인한다. 정책 변경 뒤 기존 승인이나 health를 그대로 재사용하지 않는다.

1. source draft의 `externalConfig.accessPolicy`에 review 시각, 연락 가능한 User-Agent,
   collection/source-check traffic scope, HTTP/retry/redirect/request/elapsed 예산과 origin별
   purpose·method·path·robots 규칙을 넣는다.
2. `externalConfig.rightsPolicy`에 record, document attachment, media attachment별 status,
   근거 종류/URL, 귀속 문구와 publishable 판정을 넣는다. 첨부 권리는 record보다 넓을 수 없다.
3. `POST /api/v1/sources/{sourceId}/check`를 요청하고 `lastHealth`가
   `source-check-taxonomy-v1`, 현재 snapshot/config/access-policy/rights-policy hash,
   `status=passed`, ETag/Last-Modified/content hash와 record count를 가리키는지 확인한다.
4. 같은 hash의 최근 source check가 통과한 draft만 registry decision으로 승인한다. draft나
   정책 hash가 바뀌면 source check부터 다시 수행한다.
5. 수동 CollectionRun을 만든 뒤 상세 API에서 run의 TopicPolicy ID/version/hash,
   freshness cutoff, allowed authority tiers, registry ID/version/hash와 source별
   attempt/observation을 확인한다.

운영 확인 시 다음 실패를 서로 구분한다.

- robots deny, 승인되지 않은 host/path/method/purpose, MIME 불일치는 영구 policy/schema
  실패이며 차단 뒤 후속 record/evidence가 없어야 한다.
- poll/rate/concurrency 제한, 429/502/503/504, DNS/transport/Redis 일시 오류는
  `retry_scheduled`와 redacted observation을 남기고 `Retry-After` 우선으로 source 하나만
  최대 5회 전달한다.
- TopicPolicy authority tier 밖의 source와 cutoff보다 오래되거나 미래인 active record는
  분류된 제외로 남고 SourceItem/evidence를 만들지 않는다.
- 권리 policy가 `prohibited`이거나 근거가 없는 visual/document는 다운로드·게시 가능
  evidence를 만들지 않는다. metadata-only media는 원격 bytes를 받지 않고 metadata evidence만
  남긴다.
- 일부 source만 성공하면 run은 실패 수를 보존하고 extraction으로 진행한다. 모든 source가
  실패하면 run/collect step이 failed이고 `run.evidence_requested`가 없어야 한다.

### 5.1 Housing collection contract (English)

The housing registry freezes approved HTTPS API targets, exact record hosts, request and
elapsed budgets, split API/detail/attachment MIME contracts, and the
`env://DATA_GO_KR_SERVICE_KEY` reference. The reference is purpose-bound to the exact
adapter host/path profile. Its value is resolved immediately before the request inside
source-check or collection workers and is never copied into frozen configuration,
canonical URLs, metadata, errors, or audit records.

ApplyHome identity is `category + HOUSE_MANAGE_NO + PBLANC_NO`; LH identity is
`CCR_CNNT_SYS_DS_CD + PAN_ID + UPP_AIS_TP_CD + AIS_TP_CD`. Repeated pages, duplicate
identities, missing required dates/identity/detail shapes, and an exhausted page cap fail
closed, including short coverage against `totalCount/ALL_CNT`. Only previously observed
identities are scanned outside the requested window within a bounded reconciliation
horizon, and the candidate set is limited to successful observations inside that horizon.
API and official detail-page attachments retain official file IDs and frozen-host
provenance. Unresolvable script-only downloads fail closed. Declared and sniffed MIME
must satisfy the frozen attachment contract before extension-specific extraction.

Snapshot v3 binds the adapter version, adapter/security/secret/canonical-hash files,
Python runtime, and direct dependency versions. Physical HTTP attempts consume the
request budget and credential-like response fields are redacted before persistence.
Source persistence, attempt success, and change events commit atomically. A compatible
legacy observation appends a current-schema baseline but is classified unchanged.
SourceItem and RunSourceItem are append-only and registry-bound. Concurrent replay must
match the succeeded response checksum and cannot regress the run stage. Unchanged and
terminal observations create no fresh evidence; new/corrected/restored observations enter
extraction. Terminal/restored impact walks the full lineage and creates or converges
article correction cases. Only explicit provider state creates corrected, retracted, or
unavailable records; transport and schema failures remain collection failures.

### 5.2 T012 access-policy verification (English)

Create or update a source draft with complete accessPolicy v1 and rightsPolicy v1 material,
then run `POST /api/v1/sources/{sourceId}/check`. Approval requires a recent passed
`source-check-taxonomy-v1` result bound to the exact source snapshot, config hash,
access-policy hash, and rights-policy hash. Any draft or policy change invalidates that
evidence and requires a new check before registry approval.

A CollectionRun detail response must expose its pinned TopicPolicy and registry material,
freshness cutoff, allowed authority tiers, and every source attempt/observation. Permanent
robots, scope, method, path, MIME, authentication, and rights failures do not produce
records or evidence. Retryable poll/rate/concurrency, 429/502/503/504, DNS, transport, and
Redis failures record `retry_scheduled` and honor Retry-After within the source route's
five-delivery budget. Stale/future records and disallowed authority tiers are excluded
with durable categories. Partial source success advances to extraction; zero successes
fail collection and emit no evidence fan-out.

## 6. PaddleOCR PDF 인식 검증

운영 OCR worker는 외부 네트워크가 차단된 상태에서 사전 탑재 모델만 사용해야 한다.

```powershell
uv run python -c "import importlib.metadata, paddle; paddle.utils.run_check(); assert importlib.metadata.version('paddleocr') == '3.7.0'"
uv run python src/manage.py verify_ocr_manifest --profiles=config/extraction-profiles/paddleocr
uv run pytest tests/contract/test_evidence_extractor.py tests/integration/test_pdf_extraction.py -q
```

고정 fixture는 정상 네이티브 텍스트, 한국어·영어·혼합 언어 스캔, 회전·왜곡, 다단,
병합 셀 표, 수식, 차트, 10페이지 초과, 암호화·손상·과대 PDF를 포함한다.

1. 정상 네이티브 텍스트 페이지는 직접 추출되고 불필요한 OCR이 실행되지 않는지 확인한다.
2. 스캔·저품질·복합 구조 페이지는 `engine=paddleocr_ppstructurev3`,
   `packageVersion=3.7.0`, `pipelineName=PPStructureV3`로 기록되는지 확인한다.
3. 한국어·혼합 문서는 `korean_PP-OCRv5_mobile_rec` profile을 사용하고, 표·수식 및
   반도체 profile의 차트 인식이 설정 hash에 포함되는지 확인한다.
4. 각 문서 파생 결과가 page index, polygon/bbox, block type, 읽기 순서, confidence와
   package/runtime/model manifest/config hash를 가지는지 확인한다. 독립 정적 이미지는
   `inputKind=standalone_image`, `inputFrameCount=1`, page set `[0]`, locator page 0과 PaddleOCR
   provenance를 가져야 한다. multi-frame TIFF/APNG·애니메이션은 첫 frame 성공이 아니라
   `unsupported_multiframe_image`로 실패하고 document ready가 0건이어야 한다.
   문서 파생 자산에서 provenance·locator·confidence를 빼거나 incomplete 입력을
   `publishable=true`로 만든 음성 응답은 Admin API 계약 검증에 실패해야 한다.
   비문서 파생 자산은 engine-locator-validation 매트릭스를 따라야 한다. `calibrated`일
   때는 활성 calibration profile key/version/hash와 숫자 confidence가 모두 필수이고,
   `manual_entry+calibrated` 및 임의 enum 선언은 거절해야 한다.
5. DocumentExtraction의 expected page가 항상 `0..input_page_count-1`이고, 선택 child
   결과의 합집합이 전체 범위와 같을 때만 `document_complete=true`가 되는지 확인한다.
   최초 요청에서 한 페이지를 빼거나 child만 완료하면 `evidence.document_ready`는 0건이어야 한다.
   page coverage가 같아도 selected evidence manifest에서 EvidenceAsset 하나를 누락하거나
   ID/content·result checksum/locator hash를 변조하면 ready 이벤트는 0건이어야 한다.
   child EvidenceAsset은 parent completion을 복제하지 않고 documentExtractionId만 가져야
   하며 parent GET은 집계 하나만 반환해야 한다. 같은 parent의 한 저신뢰 child만 승인하고
   다른 selected child를 대기 상태로 둔 경우 모든 관련 EvidenceAsset의 publishable과
   documentComplete는 false여야 한다.
6. 저신뢰 대체 profile은 원 run/event payload를 바꾸지 않고 새 run ID, event ID,
   dedupe key와 fingerprint를 사용하며 `retry_of_run_id`로 연결되는지 확인한다.
   같은 `document_extraction_id`와 fingerprint를 반복 전달하면 run 하나만 남고, 동일 PDF
   bytes라도 다른 SourceItem의 새 DocumentExtraction이면 별도 run이 생성돼야 한다.
   같은 SourceItem을 두 CollectionRun에서 재발견하면 SourceItem은 하나, RunSourceItem은
   둘이어야 한다. 다른 run/run-source 조합을 넣은 document/generic 요청·ready 이벤트는
   거절되고 `/runs/{runId}/evidence`에는 해당 join의 근거만 보여야 한다.
7. 대체 profile 뒤에도 청약 핵심 값이 저신뢰이면 child run은 `low_confidence`로 남고
   EvidenceAsset은 `manual_required`·`publishable=false`인지 확인한다. 관리자 판정은 현재
   `reviewSubjectHash`를 넣어 `/evidence/{evidenceId}/review-decisions`에 append-only로
   생성하며 승인 후에도 child 상태는 바뀌지 않아야 한다. 승인으로 document completion이
   바뀌어도 immutable child 입력만 사용한 review subject v1은 그대로여야 한다. stale
   subject/expected latest decision은 409이고, 같은 request key·같은 payload는 기존 결정을
   반환하며 approve/reject 동시 요청 중 CAS 한 건만 유효해야 한다. 승인 결정 없이 flag만
   바꾼 저신뢰 자산은 게시 계약을 통과하면 안 된다. 다른 OCR 엔진·Hosted
   API 호출, 런타임 모델 다운로드와 외부 네트워크 요청은 모두 0건이어야 한다.
8. 골든 30페이지에서 SC-012 정확도·locator·저신뢰 자동게시 차단 기준을 충족하고 CPU
   profile이 SC-001 시간 목표를 충족하는지 측정한다. 실패하면 동일 계약의 GPU profile을
   별도 승인한다.
9. HTML, structured, spreadsheet, browser capture, media, manual fixture가 각각
   `evidence.other_extract_requested`에서 GenericExtractionAttempt를 거쳐 정확히 한 번
   `evidence.other_ready`로 이어지는지 확인한다. failed attempt는 EvidenceAsset/other ready가
   0건이어야 한다.
10. document/generic worker가 `profileSnapshotId`로 DB의 approved snapshot을 다시 읽고
    event·로컬 extractor/package/runtime/pipeline, implementation/model manifest,
    config와 calibration key/version/hash를 대조하는지 확인한다. draft/retired/없는
    snapshot과 hint 불일치는 실행 전에 실패해야 한다. low-confidence 결과는 hash뿐 아니라
    impact·code·field/block ref·관측값·임계값·비민감 설명 전체를 관리자에게 보여야 한다.
    nullable material 조합이 같으면 `profile_material_hash` 고유 제약으로 중복 승인이
    거절돼야 하며, 100개를 넘는 reason도 단건 상세/승인 화면에서 high-impact 우선 결정 순서로 전부 표시되고 전체
    manifest hash와 검토 subject가 일치해야 한다. 한 항목을 숨기거나 변조하면 승인이
    거절돼야 한다.
    run evidence 목록은 high-impact 우선 20개 preview와 전체 count/truncated를 반환하고,
    단건 GET은 전체 manifest를 반환해야 한다. 승인 API는 목록 preview가 아니라 단건의
    전체 manifest hash를 다시 검증해야 한다.

### 6.1 HWPX와 legacy HWP 첨부 검증

청약/MOTIE fixture에 HWPX 문단·병합 표·embedded image와 legacy HWP 한 건씩 포함한다.
HWPX는 section/paragraph/table/row/column locator를 보존하고 zip-slip, 과도한 압축비,
중첩 archive, macro/OLE와 외부 link 실행을 거절해야 한다. legacy HWP는 승인된 pinned converter를
no-network/read-only-input/resource-limited sandbox에서만 실행한다. PDF output과 conversion report
checksum이 맞으면 새 DocumentExtraction으로 넘기고 실제 PDF 인식은 native extraction 또는
`paddleocr_ppstructurev3`만 사용한다. sandbox escape/network 요청, report/output 변조, timeout,
지원 불가 fixture는 `manual_required`이며 partial text, document-ready와 자동게시가 모두 0건이다.
HWPX 결과는 `locatorType=hwpx_path`, legacy 변환 provenance는
`locatorType=hwp_conversion`으로 저장하고, 두 enum/check 값이 먼저 적용된 DB migration과
engine↔locator contract test를 통과하기 전 HWP profile을 승인해서는 안 된다.

## 7. 청약 수동 실행

1. `대한민국 부동산 청약정보`와 최근 24시간을 선택하고 발행 target 없이 실행한다.
2. 청약홈 또는 LH fixture/테스트 응답의 신규 공고 한 건과 정정 공고 한 건을 포함한다.
   정정본이 같은 stable external ID와 다른 content hash를 가지면 새 SourceItem 버전과
   `supersedes_id`가 생기고, 동일 hash 재수집은 기존 버전과 새 RunSourceItem join만
   재사용해야 한다.
   본문 hash가 같아도 status가 retracted로 바뀌거나 의미 있는 수정 메타데이터가 바뀌면
   새 `sourceVersionHash` 버전이 생겨야 한다. RunSourceItem에 다른 registry version의
   SourceDefinitionSnapshot을 연결하면 거절돼야 한다.
   `unchanged` 재발견은 RunSourceItem만 추가하고 item-changed/extraction 이벤트는 0건이어야
   한다. `new_version/corrected/restored`는 추출·검증으로, `retracted/unavailable`은 신규
   추출 없이 정정·철회 영향 평가로 정확히 분기해야 한다.
   active A→unavailable B→동일 hash active A 복구는 최초 A SourceItem을 재사용하되 세 번째
   RunSourceItem이 B 관측을 `previousRunSourceItemId`로 가리키고 `restored`가 되어야 한다.
3. 결과에서 원출처 ID, 공고/정정 관계, 원본·첨부 SHA-256, 일정·공급·가격 표와 자격 조항
   locator를 확인한다.
4. 같은 입력을 다시 실행해 SourceItem과 DraftArticle이 중복 생성되지 않는지 확인한다.
5. 제목이 같지만 공식 공고번호가 다른 두 공고가 자동 병합되지 않는지 확인한다.
6. 저신뢰 PaddleOCR 가격/날짜는 다른 근거가 있어도 자동발행이 차단되고 관리자 검토 전
   `manual_required`가 유지되며, 검토 후에도 child run은 `low_confidence`로 남는지 확인한다.
7. 게시 가능한 모든 자료에는 rights basis가 있어야 하고, `attribution_required` 자료에는
   attribution이, image/chart/screenshot에는 alt text가 있어야 한다.

예상 결과: 공고별 한국어 초안, 정정 표시, 모든 사실의 Evidence 연결, 제외 자료와 이유.

## 8. 반도체 수동 실행

두 시나리오를 실행한다.

### 일일 요약

- 공식 규제/공시/IR 자료가 서로 다른 사건으로 군집되는지 확인한다.
- 기업 발표의 “세계 최초”가 독립 사실이 아니라 `company_claim`으로 표시되는지 확인한다.
- 재난 신호만 있는 팹 후보가 게시되지 않고 `candidate_disruption`으로 보류되는지 확인한다.

### 중요 속보

- 허용 4개 범주 중 하나 + 직접 1차 출처인 fixture는 속보로 승격한다.
- 범주는 맞지만 검색 결과 하나뿐인 fixture는 일일 요약 후보로 보류한다.
- 독립 2개 출처 조건을 시험할 때 같은 보도자료를 재전송한 두 페이지를 독립 출처로
  세지 않는다.

예상 결과: 조건 미충족 속보 발행 0건, 사실·기업 주장·해석·전망의 명시적 구분.

## 9. 편집·권리 게이트

모든 review-ready 개정에서 다음을 확인한다.

- `fact` Claim마다 supports Evidence가 하나 이상이다.
- 청약 고위험 수치와 반도체 속보는 강화된 검증 규칙을 통과한다.
- 원문 URL은 모델 출력이 아니라 SourceItem에서 가져온다.
- 존재하지 않는 인터뷰, 경험, 전문 자격, 인간 저자 또는 직접 인용이 없다.
- 정정/철회 이력은 독자에게 보이는 본문 블록으로 포함된다.
- 기업 이미지나 PDF 캡처의 권리가 불명확하면 내부 미리보기에는 보이더라도 게시 렌더에서
  제외된다.
- 자체 차트는 원자료, 단위, 기준일, 변환식, alt text와 출처를 가진다.
- 관리자가 본문을 편집해 새 revision을 만들면 이전 승인이 무효가 된다.

## 10. WordPress 샌드박스 발행

1. 자체 도메인·HTTPS 테스트 사이트에 전용 최소 권한 사용자를 만들고 개별 폐기 가능한
   Application Password로 연결한다. 사용자는 게시·삭제·업로드에 필요한 capability만
   갖고 사이트·사용자·플러그인 관리 권한은 갖지 않는다.
2. 읽기 전용 preflight에서 대상 base URL과 사용자, posts/media route와 선언 capability를
   확인한다. 실제 create/update/trash와 업로드 제한은 통과로 간주하지 않는다.
3. 관리자가 격리 테스트 target임을 확인한 뒤 canary를 실행해 draft 생성, 수정, media,
   공개 확인, 철회/삭제와 정리를 실제 검증한다. canary 성공 전에는 자동발행을 켤 수 없다.
4. 권리·MIME·alt text 검사를 통과한 테스트 이미지를 Media API로 올리고 반환된 media ID와
   URL을 내부 자산 checksum 매핑에 기록한다. WordPress 응답에서 checksum을 기대하지
   않으며 final 렌더는 승인된 asset ID 자리만 바꾸고 template hash를 유지한다.
5. 업로드 응답 유실을 주입해 media slug/marker로 기존 자산을 조정하고 같은 checksum·
   표시 지문을 다시 업로드하지 않는지 확인한다. `published`뿐 아니라 철회 표시 후에도
   공개 중인 `marked_withdrawn` 글의 참조가 남은 media는 삭제되지 않고, 완전 철회 또는
   본문 참조 제거가 확인된 뒤 활성 참조가 0인 자산만 고아 정리 대상이 된다.
6. 테스트 개정을 수동 승인해 `draft`로 한 번 만들고 같은 post ID를 update한다.
7. 같은 create 메시지를 100회 재전달해 결정적 post slug로 조정되고 원격 post ID가
   하나인지 확인한다.
8. `future`는 capability canary로만 확인한다. 운영 예약 fixture는 내부 `publish_at`에
   WordPress를 `publish`로 전환하고 REST 상태와 비인증 공개 URL 200을 확인한다.
9. 공개 확인 전에는 Blogger 호출이 없고, 확인된 원격 `link`만 Publication의 대표 원문
   URL로 저장되는지 확인한다.
10. 원문 정정 fixture로 같은 post ID와 정정 이력을 갱신하고, 철회 fixture로 draft
   전환 또는 trash 정책을 수행한다.
11. 외부 성공 직후 응답 유실을 주입해 create 반복 없이 reconcile이 결정적 slug의
   기존 post를 찾는지 확인한다.
12. Application Password를 폐기한 뒤 쓰기가 거부되고 target 상태가 갱신되는지 확인한다.

## 11. Google Blogger 샌드박스 발행

1. 전용 테스트 블로그에 OAuth로 연결하고 최소 `blogger` 범위와 offline token 저장을
   확인한다.
2. 읽기 전용 preflight에서 OAuth 범위·blog 소유권과 선언 capability를 확인하고, 격리
   test blog canary에서 create/update/draft/schedule/revert와 정리를 실제 검증한다.
   media upload 미지원도 정확히 표시되어야 한다.
3. 대응하는 WordPress 글이 실제 공개 상태이고 대표 URL이 비인증 GET 200을 반환하기
   전에는 Blogger 발행 작업이 대기하는지 확인한다.
4. 발행 전 Blogger 미리보기는 원문 링크 자리를 pending으로 표시하고, WordPress 공개
   확인 후 final 렌더가 같은 template hash에서 URL만 결합하는지 확인한다.
5. WordPress 실제 공개와 대표 URL 200 확인 후 같은 사실·출처 manifest를 사용하는
   Blogger 맞춤본을 생성하고, 본문에서 해당 원문 링크를 확인한다.
6. 같은 publish 메시지를 100회 재전달해 전용 label+HTML marker로 조정되고 원격 post
   ID가 하나인지 확인한다.
7. `publishDate`는 capability canary로만 확인하고 운영 예약은 WordPress 공개 확인 뒤
   Blogger를 공개하는지 확인한다.
8. 원문 정정 fixture를 적용해 같은 post ID를 patch하고 정정 이력과 WordPress 원문 링크가
   유지되는지 확인한다.
9. 원문 철회 fixture를 적용해 revert/delete capability 정책대로 처리한다.
10. 외부 성공 직후 응답 유실을 주입해 create 반복 없이 reconcile이 label+marker로
   기존 post를 찾는지 확인한다.

테스트가 끝나면 두 채널의 원격 글, 업로드한 테스트 자산과 자격 증명을 폐기한다.

## 12. 일정·중지·부분 실패

1. 5분 간격 검증 일정을 만들고 `Asia/Seoul`, overlap=`skip`으로 두 번 실행한다.
2. 첫 실행을 지연시켜 두 번째 실행이 새 글을 만들지 않고 차단 사유를 기록하는지 확인한다.
3. WordPress 성공 뒤 Blogger 실패를 주입하고 성공한 WordPress 글이 다시 생성되지 않으며
   Blogger만 재시도되는지 확인한다.
4. WordPress 실패 또는 공개 URL 비정상 응답을 주입해 Blogger 호출이 시작되지 않고
   보류 사유가 기록되는지 확인한다.
5. 완전 철회 fixture에서 WordPress가 draft/trash terminal state에 도달하면 공개 URL
   확인 없이 Blogger revert/delete가 시작되는지 확인한다.
6. 전역 kill switch를 켜 새 디스패치가 멈추고 진행 중 외부 호출은 `reconciling` 또는 완료로
   귀결되는지 확인한다.
7. 안전한 단계만 재개하고 run/step/publication의 correlation ID가 이어지는지 확인한다.
8. 예정 시각 ±5분 기준과 WordPress 공개 지연을 포함한 지연 사유 보고를 측정한다.

## 13. 자동 검증 모음

```powershell
uv run pytest tests/unit -q
uv run pytest tests/contract -q
uv run pytest tests/integration -q
uv run pytest tests/fault_injection -q
uv run pytest tests/e2e -q
uv run python src/manage.py verify_success_criteria --format=json --output=.artifacts/sc-report.json
uv run python src/manage.py check --deploy
```

릴리스 후보는 SC-001~SC-012 보고서, PaddleOCR model/config manifest와 골든 PDF 검증,
출처 레지스트리 승인, WordPress Application Password/REST 계약과 Google OAuth 정책 확인,
권리 표본 검토와 보안 점검을 모두
통과해야 한다. 자동발행은 주제·target별 계약 테스트 버전이 현재 정책 버전과 일치할
때만 활성화한다.

## 14. 주요 수용 기준 추적

| 기준 | 검증 증거 |
|---|---|
| SC-001 30분 이내 초안 90% | run 단계 histogram과 고정 표본 배치 |
| SC-002 사실 출처 100% | DB invariant + `all_fact_claims_sourced` 보고서 |
| SC-003 시각 자료 메타데이터 100% | rights/attribution/alt 차단 검사 |
| SC-004 핵심 사실 95%, 허위 인용 0 | 주제별 골든 표본 20건 |
| SC-005 중복 게시 0 | 동일 메시지 100회 + 응답 유실 고장 주입 |
| SC-006 일정 ±5분 95% | scheduler dispatch 지표와 지연 이유 |
| SC-007 실패 원인 2분 내 발견 | 관리자 E2E 사용성 타이머 |
| SC-008 경미 편집만으로 게시 80% | 블라인드 관리자 평가 기록 |
| SC-009 정정 30분 내 95% | CorrectionCase detected/completed 시각 |
| SC-010 잘못된 속보 0% | 음성/경계 골든 표본 |
| SC-011 Blogger 원문 링크 100%, 사실·출처 불일치 0 | 채널 렌더 manifest 비교 + 공개 URL 검사 |
| SC-012 PDF 핵심 값 95%, locator 누락·저신뢰 자동게시 0 | PaddleOCR 골든 30페이지 보고서 + publishability 차단 검사 |

WordPress와 Blogger target이 각각 공식 연결·계약 검증을 통과하고 위 시나리오가 성공하면
FR-012, FR-023, FR-024와 전체 수용 결과는 PASS다.

## English / AI-readable — Legacy HWP deployment

Use `docker compose --profile admin run --rm profile-admin` with the externally reviewed
manifest file and SHA-256. The command boundary contains both Paddle and HWP implementation
material and verifies the real UDS socket type, bounded protocol identity, manifest, and policy.
Profile 1.1.0 is an immutable inactive draft. T032 creates the acceptance artifact and a new
1.2.0 profile with `golden_corpus_approved=true`, then retires 1.1.0. Unsupported, warning,
missing-font, exit 20/21/22, and tamper cases must remain permanent/manual-required with zero
EvidenceAsset, ready event, or DocumentExtraction. The default topology is one concurrency-one
extract worker plus one dedicated converter; scale-out requires isolated socket/input volumes
and a converter per worker.
