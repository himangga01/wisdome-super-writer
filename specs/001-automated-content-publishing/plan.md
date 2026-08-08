# Implementation Plan: 주제 기반 자동 블로그 발행

**Feature**: `001-automated-content-publishing` | **Git Branch**: `main`  
**Date**: 2026-07-17 | **Spec**: [spec.md](./spec.md)

**Planning Gate**: `PASS` — 자체 도메인 WordPress와 Google Blogger 모두 공식 발행
인터페이스로 생성·수정·철회 흐름을 구현할 수 있고, 승인된 채널 교체가 Constitution
1.0.1 및 FR-012와 일치함

**Input**: `specs/001-automated-content-publishing/spec.md`

## Summary

단일 관리자가 대한민국 부동산 청약정보와 한국·글로벌 반도체 뉴스를 주제별 공식
출처에서 수집하고, 텍스트·표·PDF·이미지·차트·첨부파일을 공통 증거 모델로 정규화한
뒤 스캔·복합 PDF와 OCR이 필요한 독립 이미지는 PaddleOCR 3.7.0의 PPStructureV3로
페이지별 구조와 신뢰도를 인식하고 주장과 근거가 연결된 한국어 글을 생성한다. 관리자는
채널별 미리보기와 검증 결과를
확인해 승인하며, 검증된 주제·채널에 한해 자동발행을 켤 수 있다. 장시간 수집·추출·편집·
발행 작업은 Celery 워커가 담당하고 PostgreSQL의 상태·멱등성 키를 기준으로 재시도와
부분 성공을 복구한다.

구현은 Python/Django 기반의 모듈형 단일 서비스로 시작한다. Django 관리 화면과 세션
인증, PostgreSQL, Redis, S3 호환 객체 저장소를 사용하고, 출처·추출기·생성 모델·발행
채널은 포트/어댑터 경계로 교체 가능하게 만든다. Google Blogger는 공식 API 계약을
따르고, 자체 도메인 WordPress는 공식 REST API와 HTTPS Application Password 계약을
따른다. WordPress를 주 발행·대표 원문 채널로 먼저 처리해 원격 URL을 확정한 다음,
Blogger에는 같은 사실·출처를 유지한 채 채널에 맞춘 본문과 WordPress 원문 링크를
발행한다. 공식·승인된 발행 수단이 없으면 로그인 브라우저 자동화로 우회하지 않는다.

## Technical Context

**Language/Version**: Python 3.12, JavaScript는 관리자 화면의 점진적 상호작용에만 최소 사용  
**Primary Dependencies**: Django 5.2 LTS, Celery 5.6, Redis, httpx, lxml/selectolax,
PyMuPDF/pypdf/pdfplumber, openpyxl, Pillow, `paddleocr[doc-parser]==3.7.0` + 호환
PaddlePaddle 3.x (`PPStructureV3`), boto3, Playwright  
**Storage**: PostgreSQL 17+를 상태·메타데이터의 기준 저장소로 사용하고, 원문·첨부·파생
이미지는 버전 관리와 체크섬을 켠 S3 호환 객체 저장소에 보관; Redis는 작업 브로커와
단기 캐시에만 사용  
**Testing**: pytest, pytest-django, Django TestCase, 외부 API 스텁/계약 테스트,
Testcontainers, Playwright 기반 관리자 E2E  
**Target Platform**: Linux 컨테이너; 로컬 개발은 Docker Compose, 운영은 관리형
PostgreSQL/Redis/S3와 컨테이너 런타임  
**Project Type**: 관리자 전용 서버 렌더링 웹 서비스 + 비동기 작업 워커  
**Performance Goals**: 정상 실행의 90%를 30분 이내 초안 생성, 일정의 95%를 예정 시각
전후 5분 이내 시작 또는 지연 사유 표시, 정정·철회의 95%를 검증 후 30분 이내 반영  
**Constraints**: 한국어 우선, `Asia/Seoul`, 공개·허용된 출처만 접근, 모든 사실 주장에
근거 연결, 권리 확인 자산만 게시, 채널별 정확히 한 번의 효과를 보장하는 멱등 처리,
원문·초안 90일 및 발행·감사 1년 보존, PDF·독립 이미지의 OCR·레이아웃 인식은
버전·모델·설정이 고정된 PaddleOCR 프로필만 사용하고 다른 OCR 엔진으로 자동 우회하지
않음  
**Scale/Scope**: 단일 관리자, 2개 주제, 초기 승인 출처 100개 이하, 하루 원문 1,000건·
초안 50건 이하, 동시에 실행되는 수집/발행 작업 10개 이하를 MVP 설계 기준으로 사용

## Architecture Decisions

### 실행 경계

- Django 웹 프로세스는 인증, 설정, 미리보기, 승인, 중지·재개 명령과 읽기 API를 담당한다.
- Celery 워커는 수집, 첨부 추출, OCR, 중복 병합, 주장 검증, 초안 생성과 채널 발행을
  큐별로 격리해 실행한다. PaddleOCR는 전용 `extract.ocr.paddle` 워커에서 실행하며
  발행 자격 증명에 접근하지 않는다.
- Celery Beat 단일 인스턴스는 1분마다 `dispatch_due_schedules`만 호출한다. 실제 일정과
  다음 실행 시각은 PostgreSQL에서 잠금과 고유 실행 키로 결정해 동적 관리자 일정을
  지원하고 중복 디스패치를 막는다.
- PostgreSQL은 실행·문서·승인·발행 상태의 유일한 기준이다. Redis 작업 결과만으로
  성공을 판정하지 않는다.

### 추출 프로필 부트스트랩

- 저장소의 `config/extraction-profiles/manifest.json`은 OCR과 generic profile 각각의
  key/version, engine, extractor/package/runtime/pipeline, implementation/model/config와
  calibration manifest hash를 열거한다. 실제 비밀이나 모델 bytes는 포함하지 않는다.
- `import_extraction_profiles` 관리 명령은 manifest와 로컬 파일 checksum을 검증하고
  `draft` ExtractionProfileSnapshot만 멱등 생성한다. 같은 material은 재사용하고 material이
  바뀌면 새 profile version이 필요하며 기존 행을 수정하지 않는다.
- 관리자는 재인증된 관리자 화면에서 `profile_material_hash`, 모델·calibration 근거와
  골든 평가를 확인한 뒤 approve/retire한다. 상태 전이는 AuditEvent에 남고 approved material
  필드는 불변이다. 배포 자동화나 import 명령은 자체적으로 approve할 수 없다.
- `verify_extraction_profile_snapshots --require-approved-mvp`는 native PDF, 한국어/영어
  PaddleOCR, HTML, structured, spreadsheet, HWPX, 격리 legacy HWP→PDF converter,
  browser capture, media와 manual profile 및
  아래 MVP key/version/hash를 로컬 manifest와 대조한다. generic MVP profile은
  deterministic/manual이며 calibrated generic은 실제 숫자 confidence 기능을 별도 승인할
  때만 추가한다. 하나라도 없거나
  draft/retired/mismatch면 해당 worker가 시작하지 않는다.

### 출처 레지스트리 부트스트랩

- `seed_source_registry`는 SourceDefinition draft와 각 주제의 draft
  SourceRegistrySnapshot membership/manifest만 멱등 생성하며 승인할 수 없다.
- 재인증 관리자는 source별 config hash, 접근·권리·robots·rate-limit 근거와 주제 전체
  membership manifest를 확인한 뒤 topic registry version을 원자적으로 approve/retire한다.
  한 source 변경 시 unchanged membership은 carry-forward되고 모든 상태 전이는 AuditEvent다.
- `verify_source_registry_snapshots --require-approved-mvp`가 두 주제의 approved registry
  snapshot, manifest hash와 membership 파일을 확인하기 전 collect worker/scheduler는
  시작하지 않는다.

### 모듈 경계

- `topics`: 주제 정책, 중요 속보 기준, 출처 레지스트리 버전을 관리한다.
- `collection`: 수집 실행과 출처 항목을 관리하며 주제별 수집 어댑터를 호출한다.
- `evidence`: 원문/첨부 저장, 추출, 권리 상태, 주장-근거 연결을 담당한다.
- `editorial`: 사건 병합, 구조화 초안, 품질 검사, 개정과 채널 렌더링을 담당한다.
- `publishing`: 발행 대상, 채널 기능, 멱등 발행/수정/철회와 부분 실패 복구를 담당한다.
- `scheduling`: 일정, 중복 실행 정책, 즉시 중지와 재개를 담당한다.
- `audit`: 모든 관리자·자동 상태 변경을 추가 전용 이벤트로 보존한다.
- `adapters`: 출처, 추출기, 생성 모델, 객체 저장소와 발행 채널의 외부 연동을 격리한다.

### 핵심 파이프라인

1. 일정 또는 관리자 명령이 고유한 `CollectionRun`을 생성한다.
2. 해당 시점의 주제별 SourceRegistrySnapshot membership 전체와 정책 버전을 스냅샷으로
   고정하고 자료를 수집한다. 한 source만 바뀐 새 registry version도 변경되지 않은 source
   snapshot membership을 원자적으로 carry-forward한다.
   재발견 가능한 전역 SourceItem과 CollectionRun은 `RunSourceItem` join으로 연결해 실행별
   evidence 조회와 모든 추출 event의 `run_id`를 DB 계보로 검증한다.
3. 원문과 첨부를 객체 저장소에 체크섬과 함께 보존하고, 허용된 형식만 격리된 추출
   워커로 보낸다. PDF는 안전 검사와 메타데이터·전체 페이지 수 확인 후 네이티브 텍스트
   레이어의 완전성·문자 품질을 페이지별로 판정한다. 정상 텍스트 페이지는 직접 추출하고,
   텍스트가 없거나 품질 기준에 미달하거나 표·다단·차트 등 구조 인식이 필요한 페이지는
   PaddleOCR 경로로 보낸다. OCR이 필요한 독립 정적 이미지는 디코더 frame 수가 정확히
   1일 때만 같은 계약에서 가상 1페이지 문서로 처리하고 다중 frame·애니메이션은 거절한다.
   HWPX는 안전한 ZIP/XML 검사 뒤 결정적 구조 parser로 읽고, legacy HWP는 승인된 pinned
   converter를 no-network·read-only-input·resource-limited sandbox에서 PDF로 변환한다.
   변환 report/output checksum이 유효한 PDF만 새 DocumentExtraction으로 넘겨 native 추출
   또는 같은 PaddleOCR 계약으로 인식하며, 실패한 legacy HWP의 부분 text는 사용하지 않는다.
   안전 파서가 만든 `DocumentExtraction`의 expected page 집합은 항상
   `0..input_page_count-1` 전체 범위이며 독립 이미지는 `[0]`이다.
4. 각 native/PaddleOCR 페이지 묶음은 상위 DocumentExtraction에 속한 별도
   `ExtractionRun`이다. PaddleOCR 전용 워커는 상위 `document_extraction_id`, 원본 checksum,
   engine, profile key/version, 패키지·PaddlePaddle·PPStructureV3 모델 manifest와 설정
   hash로 멱등 fingerprint를 만든다. 성공 결과 재사용과 동시 실행 단일화는 같은 상위
   DocumentExtraction 안으로 제한한다. 신규 run/attempt는 DB의 `approved`
   ExtractionProfileSnapshot ID를 고정하고 worker가 event hint, extractor/package/runtime/
   pipeline 버전, implementation/model manifest, config와 calibration key/version/hash를
   기준 행에 다시 대조한다. snapshot의 전체 nullable material은 NFC+RFC 8785 JCS
   `profile_material_hash`로 고유화한다.
   한국어·영어 문서는
   `korean_PP-OCRv5_mobile_rec`, 영어 전용 문서는 승인된 영문 프로필을 사용하며 문서
   방향·왜곡·텍스트라인·표·수식·차트 인식 설정을 명시적으로 고정한다. 각 페이지의
   텍스트, 읽기 순서, 표, 차트, 수식과 시각 영역을 인식해 page index·bounding box·
   block type·순서·신뢰도와 파생 Markdown/JSON/이미지 객체를 `EvidenceAsset`으로
   정규화한다. 선택 child 결과의 page index 합집합이 expected 전체 범위와 같을 때만
   상위 문서를 완료하고 `evidence.document_ready`를 만든다. 대체 profile은 원 run을 저신뢰로 종결한
   뒤 새 run/event/fingerprint로 한 번만 실행한다. 고위험 값이 계속 저신뢰이면 다른
   근거가 있어도 자동발행하지 않고 관리자 검토를 요구한다. child run은
   `low_confidence`로 불변 보존하고, 관리자는 mutable completion/coverage를 제외한 불변
   child 결과의 evidence subject hash v1에 append-only 결정을 남긴다. subject/latest-decision
   CAS, 요청 멱등 키, decision/projection/outbox를 한 DB 트랜잭션으로 처리한다. 비문서 파생
   자산은 별도 GenericExtractionAttempt와
   `evidence.other_extract_requested/other_ready` 경로를 사용하고 engine-locator-validation
   매트릭스를 강제하며 골든 표본·metric·임계값 manifest의 승인된 key/version/hash가 있는
   추출기만 숫자 confidence를 사용한다.
5. 사건을 묶고 사실·해석·전망을 분리한 구조화 초안을 만든 뒤 모든 사실 주장을
   `ClaimEvidence`에 연결한다.
6. 차단형 품질 검사를 통과한 개정만 채널별 미리보기를 만든다. 발행 전 Blogger
   미리보기는 WordPress 원문 링크와 원격 media 자리를 `pending` 바인딩으로 표시하고,
   승인된 템플릿 지문, asset ID와 사실·출처 manifest를 고정한다.
7. 승인 또는 검증된 자동발행 정책이 채널별 `Publication`을 생성한다. 예약 시각은 내부
   스케줄러가 UTC로 정규화해 기준으로 삼고, 예정 시각에 WordPress를 `publish` 상태로
   생성·수정한 뒤 REST 상태와 비인증 공개 URL 200 응답을 확인한다. 그때만 승인된
   Blogger 템플릿에 URL을 결합해 보조 글을 발행한다. WordPress의 `future` 기능은
   샌드박스 capability 검증에는 포함하되 두 채널 운영 예약의 기준으로 사용하지 않는다.
8. 원문 변경 감시가 정정·철회를 발견하면 새 개정을 만든다. 정정·철회 표시는 WordPress
   기존 글과 공개 URL을 먼저 갱신한 뒤 Blogger 본문·원문 링크를 갱신한다. 완전
   unpublish는 WordPress가 withdrawn/draft/trash terminal state에 도달한 뒤 공개 URL 없이
   Blogger revert/delete를 실행한다.

### 신뢰성과 보안

- 네트워크 호출마다 연결/읽기 제한 시간, 출처별 속도 제한, 지수 백오프와 최대 재시도
  횟수를 둔다. 영구 실패는 격리하고 관리자가 영향 범위를 확인한 후 재개한다.
- `Publication`의 `(article_id, target_id)`와 `PublicationAttempt`의 요청 지문에 고유
  제약을 둔다. WordPress create에는 UUID 기반 결정적 slug, Blogger create에는 결정적
  전용 label과 HTML comment marker를 넣는다. 응답 유실 시 이 조회 키로 원격 글을
  정확히 한 건 확인하고 원격 ID를 저장한 뒤에만 성공 처리하며, 0건 또는 복수건이면
  create를 반복하지 않고 수동 조정으로 보낸다.
- WordPress 미디어는 `(target_id, asset_checksum, presentation_hash)` 고유 매핑으로 원격
  media ID·URL·결정적 media slug를 보존한다. 표시 문맥이 같은 매핑만 재사용하고 각
  Publication과의 참조 조인을 저장한다. 업로드 응답 유실은 media slug와 내부 marker로
  조정하며, 활성 글 참조가 0인 원격 자산만 유예 기간 후 고아 정리한다.
- 비밀 값은 환경 변수에 직접 흩뿌리지 않고 운영 비밀 저장소의 참조 키만 DB에 저장한다.
  OAuth 토큰은 암호화하고 최소 범위, 회전, 폐기, 로그 마스킹 정책을 적용한다.
- Django 세션·CSRF·staff 권한을 기본으로 하고 공개 회원가입 및 공개 쓰기 API는 두지
  않는다. 자동발행 활성화, 대량 재시도, 철회는 재인증이 필요한 고위험 작업으로 둔다.
- 외부 HTML과 첨부는 신뢰하지 않는다. MIME/크기/해시 검사, 압축 폭탄·매크로 차단,
  격리 추출, HTML 정화와 SSRF 방지용 대상 허용 목록을 적용한다.
- 운영 PaddleOCR 모델은 빌드·배포 전에 공식 배포본을 내려받아 패키지 버전과 각 모델
  파일 checksum을 manifest로 고정한다. OCR 워커는 런타임 모델 다운로드와 임의 외부
  네트워크 접근을 차단하고, 메모리·CPU/GPU·페이지 시간 한도와 임시 파일 정리를 적용한다.
- 채널 연결은 읽기 전용 preflight, 격리 test target의 쓰기 canary와 운영 target의
  관리자 승인 파일럿으로 나눈다. preflight는 운영 target의 경로·인증 주체·정적
  capability를 확인하고, canary는 동일 채널의 test target에서 생성→수정→미디어→공개
  확인→철회/삭제와 정리를 수행한다. 운영 target은 현재 정책 canary를 통과한 test
  target을 참조하고 첫 관리자 승인 게시·공개 확인까지 성공해야 자동발행할 수 있다.

## Constitution Check

*GATE: Phase 0 조사 전 확인하고 Phase 1 설계 후 다시 확인한다.*

| Gate | Phase 0 | Phase 1 설계 증거 |
|---|---|---|
| 모든 사실 출력에 주장-출처 연결과 고위험 정보 검증 정책 | PASS | `data-model.md`의 Claim/ClaimEvidence와 발행 불변조건 |
| 출처 접근, 인용, 이미지·첨부 사용의 약관·권리·호출 제한 준수 | PASS | `research.md` 출처·권리 매트릭스와 EvidenceAsset 권리 게이트 |
| 주제별 출처 레지스트리와 수집 어댑터 독립성 | PASS | `adapters/sources/housing`, `adapters/sources/semiconductor` 경계 |
| 관리자 인증, 외부 플랫폼 비밀 관리와 감사 로그 | PASS | Django staff 세션, SecretRef, 추가 전용 AuditEvent |
| 미리보기·승인·자동발행 전환, 즉시 중지와 중복·부분 발행 방지 | PASS | pending 링크 미리보기, WordPress 실제 공개·URL 200 확인, Blogger 후속 발행, 채널별 승인·멱등 키·부분 성공 상태와 kill switch 계약 |
| 파싱, 정규화, 출처 연결, 편집, 발행, 일정과 복구 테스트 | PASS | PaddleOCR 골든 PDF·추출기 계약을 포함한 단위/계약/통합/E2E 계층과 고장 주입 시나리오 |
| 단계별 지표, 재시도 한도와 수동 복구 절차 | PASS | 상관관계 ID, 단계 이벤트, 제한 재시도, `quickstart.md` 운영 검증 |

2026-07-17 공식 조사에서 WordPress Core REST API는 글과 미디어 생성·수정·삭제,
초안과 예약 상태를 지원하고, Blogger API v3도 글 생성·수정·초안·예약·초안 복귀를
지원함을 확인했다. 두 어댑터는 읽기 전용 preflight와 샌드박스 쓰기 canary를 통과한
경우에만 자동발행할 수 있다. WordPress 실제 공개와 공개 URL 200 확인 전에는 Blogger 작업을 시작하지 않고,
WordPress 성공 뒤 Blogger 실패는 부분 성공으로 보존해 Blogger만 재시도한다.
PaddleOCR 공식 문서는 PP-StructureV3가 PDF를 페이지별로 처리해 레이아웃·표·차트·수식·
읽기 순서와 Markdown 결과를 제공함을 명시한다. 고성능 추론의 공식 Python 지원 범위가
3.8~3.12이므로 서비스 기준을 Python 3.12로 고정한다.

## Project Structure

### Documentation (this feature)

```text
specs/001-automated-content-publishing/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── admin-api.openapi.yaml
│   ├── evidence-extractor.md
│   ├── generic-extractor.md
│   ├── job-events.md
│   └── publisher-adapter.md
└── tasks.md                 # 다음 $speckit-tasks 단계에서 생성
```

### Source Code (repository root)

```text
pyproject.toml
compose.yaml
src/
├── manage.py
├── wisdome_writer/
│   ├── settings/
│   ├── urls.py
│   ├── celery.py
│   └── observability.py
├── apps/
│   ├── accounts/
│   ├── topics/
│   ├── collection/
│   ├── evidence/
│   ├── editorial/
│   ├── publishing/
│   ├── scheduling/
│   └── audit/
├── adapters/
│   ├── sources/
│   │   ├── housing/
│   │   └── semiconductor/
│   ├── extractors/
│   │   └── paddleocr/
│   ├── generators/
│   ├── storage/
│   └── publishers/
│       ├── blogger/
│       └── wordpress/
├── templates/
└── static/
config/
├── source-registry/
├── editorial-policies/
├── extraction-profiles/
│   ├── manifest.json
│   ├── paddleocr/
│   └── generic/
├── calibration-manifests/
└── retention/
tests/
├── unit/
├── contract/
├── integration/
├── e2e/
├── fixtures/
└── fault_injection/
deploy/
└── containers/
    └── paddleocr-worker/
```

**Structure Decision**: 단일 배포 단위 안에서 Django 앱과 외부 어댑터를 모듈 경계로
분리한다. 관리 화면과 내부 JSON API를 같은 서비스가 제공하고, 웹·워커·스케줄러는 같은
코드와 스키마를 사용하되 런타임 프로세스만 분리한다. MVP의 단일 관리자와 두 주제에는
별도 프런트엔드 저장소나 마이크로서비스가 필요하지 않다.

## Verification Strategy

- 단위: 주제별 최신성/속보 기준, 파서, 해시·중복, 권리 판정, 주장 연결, 상태 전이를
  순수 함수와 고정 fixture로 검증한다.
- 계약: PaddleOCR `PP-StructureV3` 입출력 정규화, 모델 manifest·설정 hash, 페이지·block
  locator, DocumentExtraction 전체 page coverage와 저신뢰 관리자 차단을 골든 PDF와 독립
  이미지로
  검증한다. generic extractor의 engine-locator-validation/calibration 매트릭스와 별도
  request/ready 이벤트를 모든 engine fixture로 검증한다. 승인된 출처 fixture와 발행 샌드박스/
  공식 테스트 계정으로 요청·응답 스키마,
  인증 만료, 제한 응답, preflight/canary, 결정적 원격 조회 키, 미디어 매핑과
  생성·수정·철회 가능 범위를 검증한다.
- 통합: PostgreSQL/Redis/S3 호환 저장소를 실제 컨테이너로 띄워 트랜잭션, 잠금, 객체
  체크섬, 작업 재전달과 부분 성공 복구를 검증한다.
- E2E: 관리자가 수동 실행→근거 검토→승인→WordPress 대표 원문 확인→Blogger 맞춤본과
  원문 링크 확인→정정 반영→즉시 중지를 수행하는 경로를 Playwright로 검증한다.
- 고장 주입: 동일 메시지 100회, 글·미디어 성공 직후 응답 유실, WordPress 공개 지연,
  Blogger 부분 실패, 토큰 만료, 파서 구조 변경, PaddleOCR 모델 불일치·OOM·timeout·
  저신뢰, 최초 expected page 요청 누락, child만 완료된 상태, 대체 profile의 새 이벤트와
  일정 중복·워커 종료를 재현한다.
- 출시 게이트: SC-001~SC-012 자동 보고서, PaddleOCR 골든 PDF 보고서, 보안/권리
  체크리스트, 출처 레지스트리 승인,
  채널별 공식 연동 증거가 모두 있어야 자동발행을 활성화한다.

## Complexity Tracking

현재 선택한 모듈형 단일 서비스는 별도 마이크로서비스나 SPA를 도입하지 않으므로 정당화가
필요한 구조적 예외가 없다. WordPress 선발행과 Blogger 후속 발행의 의존 순서는 대표
원문 URL과 부분 성공 복구를 보장하기 위한 도메인 규칙이며 별도 서비스 도입을 요구하지
않는다.

## T015 2차 격리 결정

- dedicated UID 0 supervisor만 CHOWN/KILL/SETUID/SETGID를 보유하고 startup proc status를
  exact 검증한다. parser 65533과 validator 65531은 exec 전후 capability 0을 증명한다.
- one-shot volume bootstrap, supervisor-owned read-only input, child-owned output, supervisor
  streaming snapshot, validator-readable trusted PDF, child subreaper/descendant zero 검증을 사용한다.
- 한 요청은 하나의 absolute deadline을 공유한다. client budget은 sidecar deadline과 bounded
  response margin을 합한 값이다.
- golden=true는 immutable T032 acceptance object key/version/SHA-256, target OCI digest,
  converter manifest hash, schema/all-pass 없이는 활성화할 수 없다. 1.1.0은 1.2.0 승인 후에도
  retire하지 않고 immutable superseded draft로 유지한다.
- 현재 activation gate는 reference 형식이 올바르더라도 무조건 false다. T032가 exact versioned
  object bytes fetch, byte hash/schema/subject/OCI/manifest/all-results 대조와 외부 trust root 기반
  release signature 검증을 구현한 뒤에만 gate 구현을 교체할 수 있다. 그 전까지 golden=true
  import/verify/converter construction은 모두 fail closed다.
- 현재 network build/runtime self-report는 hermetic 또는 정책 증거가 아니다. T032/release가
  signed trust root, vendored/offline source·crate·deb checksum·SBOM·attestation을 제공하기 전
  production activation을 차단한다.

## English / AI-readable — T015 second hardening plan

Use a trusted UID 0 supervisor with exactly CHOWN/KILL/SETUID/SETGID, zero-capability parser 65533
and validator 65531, a child subreaper, split input/output directories, and a supervisor-owned
streaming snapshot. One request has one absolute deadline. Golden activation requires immutable
T032 artifact and release digest bindings. Profile 1.1.0 remains an immutable superseded draft;
signed hermetic build and artifact trust material remain mandatory T032/release blockers. The
activation gate remains unconditionally false until T032 implements exact artifact-byte and
release-signature verification against an external trust root; runtime self-report is never
admission evidence.
