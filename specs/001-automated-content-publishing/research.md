# Phase 0 Research: 주제 기반 자동 블로그 발행

**조사 기준일**: 2026-07-17  
**범위**: 기술 스택, 주제별 공식 출처, 멀티모달 처리, 발행 채널, 권리·정정·운영 정책

## 결론

수집·정제·관리자 승인·반복 실행과 두 채널 자동발행은 계획한 기술 구조로 구현 가능하다.
자체 도메인 WordPress는 Core REST API로 글 생성·수정·삭제, 초안·예약 상태와 미디어
업로드를 지원하며, HTTPS 환경에서 전용 최소 권한 사용자에 연결한 Application Password로
서버 간 인증을 구성할 수 있다. Google Blogger도 공식 OAuth 2.0과 Blogger API v3로 생성, 수정, 초안,
예약 발행과 초안 전환을 구현할 수 있다.

승인된 Phase 0 범위는 다음과 같다.

1. 자체 도메인의 관리형 WordPress를 주 발행·대표 원문 채널로 사용한다.
2. WordPress 원격 URL을 먼저 확정한 뒤 Google Blogger에 같은 사실·출처를 유지한
   채널 맞춤본과 WordPress 원문 링크를 발행한다.
3. WordPress에는 권리가 확인된 이미지를 공식 Media API로 업로드하고, Blogger에는
   공개가 허용된 HTTPS 자산 URL을 본문에 포함한다.
4. 두 채널 모두 공식 기능 범위, 최소 권한, 멱등 조정과 샌드박스 계약 테스트를 통과한
   target만 자동발행한다.
5. 네이버 블로그처럼 공식 쓰기 인터페이스가 없는 채널은 MVP 범위에서 제외하며,
   비공식 내부 API, 로그인 세션 재사용이나 DOM 클릭 자동화로 우회하지 않는다.
6. PDF와 내용 이해가 필요한 독립 이미지의 OCR·레이아웃 인식은 로컬
   `paddleocr[doc-parser]==3.7.0`의 PP-StructureV3와 checksum으로 고정된 모델·설정
   프로필만 사용한다. PDF 네이티브 텍스트는 먼저 직접 추출하되 스캔·저품질·복합
   레이아웃 페이지와 독립 이미지를 다른 OCR 엔진으로 우회하지 않는다.

## 결정 로그

| ID | 결정 | 근거 | 검토 후 제외한 대안 |
|---|---|---|---|
| D-01 | Python 3.12 + Django 5.2 LTS의 모듈형 단일 서비스 | 단일 관리자 CRUD, 세션 인증, CSRF, ORM, 마이그레이션과 서버 렌더링 UI를 한 프레임워크에서 제공한다. Django 5.2는 Python 3.12를 지원하고, PaddleOCR 고성능 추론의 공식 지원 범위가 Python 3.8~3.12이므로 공통 런타임을 3.12로 맞춘다. [Django 5.2 릴리스](https://docs.djangoproject.com/en/5.2/releases/5.2/), [PaddleOCR 고성능 추론](https://www.paddleocr.ai/main/en/version3.x/inference_deployment/local_inference/high_performance_inference.html) | Python 3.13은 현재 PaddleOCR 고성능 추론 공식 지원 범위를 벗어나고, SPA+별도 API와 마이크로서비스는 MVP 운영면과 상태 일관성 부담을 늘린다. |
| D-02 | Celery 5.6 + Redis로 장시간 작업 격리 | Celery는 Django를 공식 지원하고 작업 재전달, 재시도, 상태와 큐 분리를 제공한다. 작업은 멱등하게 작성해야 한다. [Django 통합](https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html), [작업 가이드](https://docs.celeryq.dev/en/stable/userguide/tasks.html) | 웹 프로세스의 인프로세스 background task는 OCR·PDF·외부 발행 같은 장시간 작업과 재시작 복구에 부적합하다. |
| D-03 | Celery Beat는 단일 DB 디스패처만 1분마다 실행 | 공식 문서는 Beat를 한 인스턴스만 운영해야 중복 작업을 막을 수 있다고 설명한다. 실제 동적 일정은 PostgreSQL 행 잠금과 고유 실행 키로 선택한다. [Celery 주기 작업](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html) | 고정 `beat_schedule`만으로는 관리자 동적 일정을 안전하게 변경하기 어렵고, 스케줄러를 여러 개 두면 중복 발행 위험이 있다. |
| D-04 | PostgreSQL을 상태 기준, S3 호환 저장소를 원본 기준으로 사용 | 관계·트랜잭션·JSONB·검색은 PostgreSQL에 적합하고, 큰 원본/파생 파일은 체크섬과 버전을 제공하는 객체 저장소에 적합하다. [PostgreSQL 전문 검색](https://www.postgresql.org/docs/current/functions-textsearch.html), [S3 체크섬](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html), [S3 버전 관리](https://docs.aws.amazon.com/AmazonS3/latest/userguide/versioning-workflows.html) | 바이너리를 DB에 직접 넣으면 백업·수명주기 비용이 커지고, Redis를 상태 기준으로 쓰면 영속성과 감사 재현성이 부족하다. |
| D-05 | 구조화 자료 우선 수집 | API/RSS/XML/XBRL → canonical HTML → PDF/XLSX/HWP 계열 → 이미지 OCR 순서가 정확성, 변경 탐지, 비용과 중복 제거에 유리하다. | 전체 사이트 크롤링과 OCR 우선 처리는 구조 변경·권리·정확성 비용이 크다. |
| D-06 | 공통 증거 모델과 두 단계 생성 | 먼저 문서에서 검증 가능한 주장 후보를 구조화하고, 그 주장만으로 글을 편집한다. 사실/해석/전망을 분리하고 `ClaimEvidence`가 없는 사실은 발행하지 않는다. | 원문 전체를 한 번에 모델에 넣고 자유 본문을 받으면 출처 경계와 재현성이 약하다. |
| D-07 | 자체 도메인 WordPress를 주 발행·대표 원문 채널로 사용 | Core REST API가 글 생성·수정·삭제, `draft`·`future`·`publish` 상태와 Media 업로드를 제공한다. 자체 도메인은 대표 URL과 콘텐츠 소유권을 유지할 수 있다. [Posts API](https://developer.wordpress.org/rest-api/reference/posts/), [Media API](https://developer.wordpress.org/rest-api/reference/media/) | WordPress.com은 운영 부담이 낮지만 서비스 계정·정책 의존성이 커지고, Ghost는 안정적인 Admin API가 있으나 MVP의 대표 CMS 도달 범위와 일반 첨부 처리에서 WordPress보다 우선하지 않는다. |
| D-08 | Google Blogger를 보조 배포 채널로 사용 | API v3가 게시물 생성·수정·삭제·초안·예약·초안 복귀를 제공한다. OAuth offline access로 승인된 계정의 백그라운드 실행이 가능하다. WordPress URL을 포함한 채널 맞춤본으로 동일 본문 복제를 피한다. | Mail2Blogger는 수정·정정·예약 제어가 약하고, 동일 전체 본문 복제는 채널 역할을 불명확하게 만든다. |
| D-09 | 네이버 블로그 자동발행은 제외 | 공식 쓰기 API와 앱 URL Scheme가 종료됐고 현재 API 목록에는 검색만 있다. 정책을 우회하는 UI/내부 API 자동화는 금지한다. | Playwright/Selenium DOM 클릭, 쿠키 재사용, undocumented endpoint 호출은 운영 안정성과 정책 준수를 충족하지 못한다. |
| D-10 | Playwright는 관리자 E2E와 허용된 공개 페이지 캡처에만 사용 | 공식 Python 라이브러리는 브라우저 자동화·스크린샷과 pytest 통합을 제공한다. [Playwright Python](https://playwright.dev/python/docs/library) | 채널 로그인 편집기 자동발행에는 사용하지 않는다. |
| D-11 | 생성 모델은 공급자 중립 포트로 격리 | 구조화 JSON 스키마, 모델/프롬프트/정책 버전, 입력 증거 ID와 결과 지문을 저장하면 공급자 교체와 회귀 검증이 가능하다. | 한 모델 SDK를 도메인 전역에 직접 결합하면 변경·감사·테스트가 어려워진다. |
| D-12 | PDF·독립 이미지 OCR과 구조 인식은 로컬 PaddleOCR 3.7.0 PP-StructureV3로 고정 | PP-StructureV3는 PDF와 이미지 입력에서 레이아웃, 읽기 순서, 표, 수식, 차트, 좌표, 신뢰도 및 JSON/Markdown 결과를 제공한다. Apache-2.0 라이선스이며 전용 워커 안에서 원문을 외부 OCR 서비스로 전송하지 않고 처리할 수 있다. [v3.7.0 릴리스](https://github.com/PaddlePaddle/PaddleOCR/releases/tag/v3.7.0), [PP-StructureV3](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html), [LICENSE](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE) | Tesseract와 다른 OCR로 자동 fallback하면 사용자가 지정한 엔진과 재현성이 깨진다. Hosted OCR은 원문 외부 전송과 자격 증명·비용 의존성이 생기며, 네이티브 텍스트 추출만으로는 스캔·표·복합 레이아웃 또는 독립 이미지 내용을 처리할 수 없다. |

## 발행 채널 조사

### 기능 매트릭스

| 기능 | WordPress(주 채널) | Google Blogger(보조 채널) |
|---|---|---|
| 공식 자동 생성 | `POST /wp/v2/posts` | `posts.insert` |
| 초안 생성 | `status=draft/pending` | `isDraft` |
| 기존 글 수정 | `POST /wp/v2/posts/{id}` | `posts.patch/update` |
| 플랫폼 예약 capability | 미래 `date` + `status=future`; canary 전용 | draft 후 `posts.publish(publishDate)`; canary 전용 |
| 철회/초안 복귀 | published→draft 또는 trash/delete | `posts.revert`, 필요 시 delete |
| 바이너리 이미지 업로드 | `POST /wp/v2/media`, alt/caption 관리 | 별도 업로드 엔드포인트 없음; 허용된 HTTPS 자산 URL을 HTML에 포함 |
| 무인 인증 | HTTPS + 사용자별 Application Password | 사용자 동의 OAuth 2.0 + offline refresh token |
| 채널 역할 | 전체 원문과 대표 URL | 채널 맞춤본과 WordPress 원문 링크 |
| MVP 판정 | **GO** | **GO** |

### WordPress

- [Posts API](https://developer.wordpress.org/rest-api/reference/posts/)는
  `POST /wp/v2/posts` 생성, `POST /wp/v2/posts/{id}` 수정,
  `DELETE /wp/v2/posts/{id}`의 trash/삭제와 `draft`, `pending`, `publish`, `future`,
  `private` 상태를 제공한다. 정정 시 저장된 원격 post ID를 수정하며 새 글을 만들지 않는다.
- [Media API](https://developer.wordpress.org/rest-api/reference/media/)는 이미지·파일
  업로드와 `alt_text`, 캡션, 설명을 지원한다. 업로드 전에 프로젝트의 권리·MIME·크기
  게이트를 통과해야 하며, WordPress가 반환한 media ID와 URL을 저장한다.
- 외부 자동화 인증은 WordPress 5.6 이상에서 제공하는
  [Application Passwords](https://developer.wordpress.org/advanced-administration/security/application-passwords/)를
  HTTPS에서 사용한다. Application Password 자체에는 세부 scope가 없고 연결된 사용자의
  권한을 그대로 사용하므로, `read`, `edit_posts`, `edit_published_posts`, `publish_posts`,
  `delete_posts`, `delete_published_posts`, `upload_files`만 업무에 맞게 가진 전용 역할을
  사용하고 사용자·플러그인·테마·사이트 관리 권한은 주지 않는다. 비밀 저장소 참조,
  회전, 개별 폐기와 로그 마스킹을 적용한다.
- WordPress 예약 작업은 기본적으로 방문 요청에 영향을 받는 WP-Cron 특성이 있으므로
  [공식 Cron 설명](https://developer.wordpress.org/plugins/cron/)에 따라 관리형 호스트의
  실제 cron 또는 외부 스케줄 환경을 canary에서 확인한다. `future` 기능은 capability
  canary에만 사용하고 운영 예약에는 사용하지 않는다.
- 두 채널 운영 예약에서는 WP-Cron과 Blogger 예약이 독립적으로 어긋나는 문제를 피하기
  위해 플랫폼 예약을 사용하지 않는다. 내부 스케줄러가 `publish_at`을 UTC로 정규화하고
  예정 시각에 WordPress를 `publish`로 전환한 뒤 REST의 공개 상태와 비인증 GET 200을
  확인해야 Blogger를 공개한다. WordPress 공개가 늦으면 Blogger도 보류하고 지연 사유를
  기록한다.
- 설치된 플러그인, 보안 프록시 또는 호스팅 정책이 REST 경로·Authorization 헤더·MIME을
  제한할 수 있으므로 target preflight에서 posts/media route, 현재 사용자 권한과 HTTPS를
  확인하고 쓰기 canary에서 실제 업로드 크기, permalink와 공개 상태를 확인한다.
- WordPress가 반환한 공개 `link`를 대표 원문 URL로 저장한다. 글에는 자체 참조 대표 URL을
  유지하고, Blogger 맞춤본에는 이 URL을 독자가 볼 수 있는 원문 링크로 제공한다.
- WordPress create에는 Publication UUID에서 만든 결정적 slug를 사용하고, 인증된
  posts 목록을 해당 slug와 이 서비스가 쓰는 `draft/future/publish/pending` 상태로 조회해 응답
  유실을 조정한다. 결과가 0건 또는 복수건이면 자동 create를 반복하지 않는다. 미디어는
  자산 checksum과 alt/caption/attribution 표시 지문 기반 결정적 slug 및 내부 설명
  marker를 사용해 원격 media ID/URL을 조정한다. 동일 표시 지문의 매핑만 재사용하고
  Publication별 참조를 저장하며, 활성 참조가 0인 자산만 고아 삭제한다. WordPress
  응답에는 프로젝트의 원본 checksum이 없으므로 해당 값은 내부 증거에서만 유지한다.
- WordPress의 RSS와 sitemap은
  [네이버 Search Advisor 피드 제출 가이드](https://searchadvisor.naver.com/guide/request-feed)에
  따라 검색 발견용으로 등록할 수 있다. 이는 네이버 통합·웹 검색 수집 경로이며 네이버
  블로그 탭 발행이나 노출을 보장하지 않는다. MVP는 피드를 제공하되 Search Advisor의
  관리자 등록 자동화는 발행 채널 범위에 포함하지 않는다.

### Google Blogger

- [Blogger API 소개](https://developers.google.com/blogger)와
  [Posts 리소스](https://developers.google.com/blogger/docs/3.0/reference/posts)가 v3의
  현재 게시 기능을 정의한다.
- 생성은 [`posts.insert`](https://developers.google.com/blogger/docs/3.0/reference/posts/insert),
  수정은 [`posts.patch`](https://developers.google.com/blogger/docs/3.0/reference/posts/patch)
  또는 [`posts.update`](https://developers.google.com/blogger/docs/3.0/reference/posts/update)를
  사용한다. 정정 시 저장된 `blogId`와 `postId`를 수정하며 새 글을 만들지 않는다.
- 초안/예약은 [`posts.publish`](https://developers.google.com/blogger/docs/3.0/reference/posts/publish)의
  미래 `publishDate`, 철회는 [`posts.revert`](https://developers.google.com/blogger/docs/3.0/reference/posts/revert),
  삭제는 [`posts.delete`](https://developers.google.com/blogger/docs/3.0/reference/posts/delete)를
  capability에 따라 사용한다.
- 인증은 `https://www.googleapis.com/auth/blogger` 범위와
  [OAuth 웹 서버 흐름의 offline access](https://developers.google.com/identity/protocols/oauth2/web-server)를
  사용한다. 일반 서비스 계정으로 쓸 수 있다고 가정하지 않는다.
- 공개 문서에는 장기적으로 고정된 게시 한도가 명시되어 있지 않다. 프로젝트별 Cloud
  Console 한도를 검증하고 낮은 빈도의 파일럿, 429/5xx 백오프와 모니터링을 적용한다.
- API에는 바이너리 이미지 업로드가 문서화되어 있지 않다. 게시 권리가 확인된 파생
  이미지를 우리 객체 저장소/CDN의 HTTPS URL로 제공하고 HTML에 삽입한다.
- Posts API는 게시물별 외부 대표 URL 태그 설정을 제공하지 않으므로 동일 전체 본문을
  그대로 복제하지 않는다. 검증된 사실·출처 의미는 유지하되 요약·구조를 Blogger에 맞게
  구성하고 WordPress 원문 URL을 명시한다. Google의
  [대표 URL 선택 가이드](https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls)에
  맞춰 WordPress sitemap과 자체 참조 대표 URL을 일관되게 유지한다.
- OAuth 동의 화면, 개인정보처리방침, 최소 권한, 토큰 암호화와 폐기는
  [Google API 사용자 데이터 정책](https://developers.google.com/terms/api-services-user-data-policy)을
  따른다. 중복·대량 자동 글은 [Blogger 콘텐츠 정책](https://www.blogger.com/content-policy?hl=ko)에
  맞춰 승인, 고유성 검사와 빈도 제한을 둔다.
- Blogger create에는 Publication UUID에서 만든 전용 label과 비표시 HTML comment marker를
  함께 넣는다. 응답 유실 시 인증된 posts 목록을 draft/live/scheduled 상태별로 조회하면서
  label로 좁히고 marker가 정확히 일치하는 글 한 건만 기존 결과로 채택한다. 0건 또는
  복수건이면 create를 반복하지 않고 수동 조정한다.

### 검토 후 제외: 네이버 블로그

- 네이버는 2020-05-06 로그인 방식의 블로그 글쓰기 API를 종료했다. 종료 공지는 반복적
  기계 생성·대량 게시의 악용을 종료 이유로 설명한다.
  [공식 API 종료 공지](https://developers.naver.com/notice/article/7527)
- 블로그 앱 글쓰기 URL Scheme도 2022-12-23 종료됐다.
  [공식 URL Scheme 종료 공지](https://developers.naver.com/notice/article/8595)
- 현재 [네이버 Open API 목록](https://developers.naver.com/docs/common/openapiguide/apilist.md)과
  [NAVER API HUB](https://api.ncloud-docs.com/docs/naver-api-hub-overview)는 블로그 검색을
  제공하지만 글 생성·수정·삭제 API를 제공하지 않는다.
- [공식 블로그 공유 API](https://developers.naver.com/docs/share/share/)는 URL과 제목을
  사용자에게 넘겨 공식 작성 화면을 여는 human-in-the-loop 보조 수단이다. 로그인,
  본문 완성, 검토와 게시를 사용자가 직접 해야 하므로 자동발행으로 볼 수 없다.
- 사용자가 WordPress로 대체하는 범위를 승인했으므로 네이버 PublicationTarget과 게시
  패키지는 MVP에 만들지 않는다. 향후 문서화된 쓰기 API 또는 서면 파트너 권한이 확인될
  때 별도 기능 명세로 재평가한다.

## 부동산 청약 출처 레지스트리

### 우선 출처

| 우선 | 출처 | 역할 | 안정 ID·변경 신호 | 접근·권리 메모 |
|---|---|---|---|---|
| P1 | [청약홈/한국부동산원](https://www.applyhome.co.kr/co/coa/selectMainView.do) | 민영 APT, 오피스텔, 잔여세대, 임대·선택공급 공고/일정/가격/경쟁 결과 | `category + houseManageNo + pblancNo`; 모델 `modelNo`; 응답/첨부 해시 | [공식 분양정보 API](https://www.data.go.kr/data/15098547/openapi.do) 우선. API 재사용 범위와 웹/PDF 권리는 별도 기록 |
| P1 | [LH청약플러스](https://apply.lh.or.kr/lhapply/main.do) | LH 임대·분양 공고, 정정/취소, 일정·공급·자격·가격·첨부 | `CCR_CNNT_SYS_DS_CD + PAN_ID + UPP_AIS_TP_CD + AIS_TP_CD`; `fileid`; 정정 계보 | [목록 API](https://www.data.go.kr/data/15058530/openapi.do), [상세 API](https://www.data.go.kr/data/15057999/openapi.do), [공급 API](https://www.data.go.kr/data/15056765/openapi.do) 우선 |
| P1 | [마이홈포털](https://m.myhome.go.kr/hws/portal/main/getMgtMainPage.do) | 전국 공공임대·분양 발견/일정 보완 | API가 반환한 원 ID와 운영기관 상세 URL; `atchFileId + fileSn` | [공식 모집공고 API](https://www.data.go.kr/data/15108420/openapi.do). 집계원이므로 원 운영기관 자료 우선 |
| P1B | [SH 공고](https://www.i-sh.co.kr/app/lay2/program/S1T294C295/www/brd/m_241/list.do) | 서울 공고·정정·첨부 | board `seq`; RSS 링크에서 ID 추출 | [공식 RSS](https://www.i-sh.co.kr/main/lay2/program/S1T294C295/www/rss/rssNoticeWrite.do). 첨부 세션 요구 시 무리한 다운로드 금지 |
| P1B | [GH 공고](https://www.gh.or.kr/gh/announcement-of-salerental001.do?mode=list) / [GH 청약센터](https://apply.gh.or.kr/) | 경기 공고·정정·표·첨부 | `articleNo`, `pbancNo`, 첨부 ID | 공개 쓰기 API 없음. 보수적 HTML 조회와 공개 데이터 백필만 사용 |
| P1B | [iH 공고](https://www.ih.co.kr/main/bbs/bbsMsgList.do?bcd=notice&pgdiv=general) | 인천 공고·정정·첨부 | `bcd + msg_seq`; `fileno` | [공식 API](https://www.data.go.kr/data/15149725/openapi.do)와 [RSS](https://www.ih.co.kr/rss/bbsNotice.do). KOGL 제3유형은 출처 표시·변경 금지 |
| 법적 기준 | [국가법령정보센터 주택공급에 관한 규칙](https://www.law.go.kr/LSW/lsInfoP.do?lsId=008243) | 공고일 기준 자격·절차의 법적 근거 | `lsId`, 시행일, 역사 버전 `lsiSeq` | 법령 버전을 공고일과 연결. 일반 안내를 법률 판정 엔진으로 사용하지 않음 |

### 청약 정규화 규칙

- 원출처 고유 키를 먼저 사용한다.
  - `applyhome:{category}:{houseManageNo}:{pblancNo}`
  - `lh:{ccrCnntSysDsCd}:{panId}:{uppAisTpCd}:{aisTpCd}`
  - `sh:{seq}`, `gh-apply:{pbancNo}`, `gh-board:{articleNo}`, `ih:{bcd}:{msg_seq}`
- 출처 간에는 운영기관, 정규화 사업/블록명, 공식 공고번호, 공고일, 주소와 첨부 해시로
  **후보 군집**만 만들고 제목만으로 자동 병합하지 않는다.
- 충돌 우선순위는 `최신 정정 PDF > 운영기관 상세/현재 상태 > 운영기관 구조화 API >
  마이홈 집계 > 연/분기 예측·스냅샷`이다.
- 청약홈 구조화 값도 실제 입주자모집공고 PDF와 충돌하면 PDF와 최신 정정 공고가 법적
  해석의 기준이다.
- 자격은 공고일, 가구 구성, 소득·자산 기준일과 개별 문구에 좌우된다. 조항을 추출·인용할
  뿐 사용자에게 확정적 적격/부적격 법률 판정을 내리지 않는다.
- 정정은 덮어쓰지 않고 새 SourceItem과 `supersedes` 관계, 필드 diff, 원문/첨부 해시로
  보존한다.

### 청약 자료 유형

| 자료 | 우선 처리 | 게시 원칙 |
|---|---|---|
| 일정·공급·가격·경쟁률 | 공식 JSON/XML/CSV 테이블 | 핵심 숫자는 정정 PDF와 대조하고 출처 링크 제공 |
| 자격·주의사항 | 현재 공고 HTML/PDF의 조항 | 필요한 범위만 인용; 일반화·당첨 보장 금지 |
| PDF/HWP/HWPX/XLSX | 원본 보존 후 텍스트/표 추출 | 원문 전체 재배포 금지; 권리 불명 시 링크만 제공 |
| 평면도·지도·조감도 | 첨부 메타데이터와 권리 확인 | 명시적 허용 없으면 내부 분석만; 자체 요약표/지도 대체 |
| 화면 캡처 | 구조 설명이 꼭 필요할 때 최소 영역 | 개별 권리와 출처가 확인될 때만 게시 |

## 반도체 출처 레지스트리

### 규제·수출통제

| 관할 | 1차 출처 | 안정 식별자/형식 |
|---|---|---|
| 한국 | [산업통상자원부 보도자료](https://motie.go.kr/kor/article/ATCL3f49a5a8c/), [국가법령정보센터](https://www.law.go.kr/), [무역안보관리원](https://www.kosti.or.kr/main), [전략물자관리시스템](https://www.yestrade.go.kr/) | MOTIE article ID, 법령/행정규칙 버전, KOSTI `bbsSn`; HTML/PDF/HWP/표 |
| 미국 | [BIS Federal Register Notices](https://www.bis.gov/regulations/federal-register-notices), [EAR](https://www.bis.gov/regulations/ear), [Federal Register API](https://www.federalregister.gov/developers/documentation/api/v1), [eCFR API](https://www.ecfr.gov/developers/documentation/api/v1), [GovInfo API](https://www.govinfo.gov/features/api) | FR document number/citation/docket, CFR title/part/section/date; JSON/XML/PDF |
| EU | [Dual-use 정책](https://policy.trade.ec.europa.eu/help-exporters-and-importers/exporting-dual-use-items_en), [Regulation 2021/821](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32021R0821), [EUR-Lex Webservice](https://eur-lex.europa.eu/content/help/data-reuse/webservice.html?locale=en) | CELEX, ELI, OJ citation; 개정/corrigendum 관계 |
| 네덜란드 | [전략물자 수출 법령](https://www.government.nl/themes/economy/export-controls-of-strategic-goods/laws-and-rules-on-the-export-of-strategic-goods) | 관보 번호·공표/시행일 |
| 일본 | [METI 안전보장무역관리](https://www.meti.go.jp/policy/anpo/), [e-Gov 법령](https://elaws.e-gov.go.jp/) | 공표·시행일, 첨부 해시 |
| 대만 | [MOEA 수출통제](https://www.trade.gov.tw/english/Pages/Detail.aspx?nodeid=298&pid=687872) | 공고번호, 목록 버전 |
| 중국 | [MOFCOM 수출통제정보망](https://exportcontrol.mofcom.gov.cn/), [MOFCOM](https://www.mofcom.gov.cn/) | 중국어 공고번호·공표일·첨부 해시 |

한국은 산업부·법령 원문이 기준이고 KOSTI는 해설 자료로 취급한다. EU 통합본은 탐색에
쓰고 법적 기준은 Official Journal 원문과 개정 관계다.

### 팹 중단·공급 차질

- 기업·거래소 확인: [OpenDART](https://opendart.fss.or.kr/guide/main.do)의 `corp_code +
  rcept_no`, [KRX KIND](https://kind.krx.co.kr/), [SEC EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)의
  CIK+accession, [EDINET API](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WEEK0060.html)의
  `docID`, [대만 MOPS](https://mops.twse.com.tw/)를 우선한다.
- SEC는 [공식 개발자 정책](https://www.sec.gov/about/developer-resources)에 맞는 User-Agent와
  호출 제한을 적용한다. EDINET은 API 키와 정정/철회 메타데이터를 사용한다.
- 재난 신호는 [기상청 API 허브](https://apihub.kma.go.kr/apiInfo.do),
  [JMA 방재 XML](https://www.data.jma.go.jp/developer/),
  [대만 CWA](https://opendata.cwa.gov.tw/dist/opendata-swagger.html),
  [USGS 지진 API](https://earthquake.usgs.gov/fdsnws/event/1/)를 사용할 수 있다.
- 재난 신호만으로 팹 중단을 발행하지 않는다. `candidate_disruption`을 만든 뒤 회사 IR,
  거래소 공시나 규제기관 원문으로 실제 생산 영향을 확인한다.

### M&A·중대 실적

- 실적: 거래소·감독기관 공시 원문을 기준으로 하고 기업 IR 슬라이드는 설명/시각화
  보조자료로 사용한다.
- 기업결합: [공정거래위원회](https://www.ftc.go.kr/www/ReportUserList.do?key=10&rpttype=1),
  [미국 FTC Cases](https://www.ftc.gov/legal-library/browse/cases-proceedings),
  [DOJ Antitrust](https://www.justice.gov/atr/antitrust-case-filings),
  [EU Merger Case Search](https://competition-policy.ec.europa.eu/mergers/practical-information/cases-search-user-guide_en),
  [영국 CMA Cases](https://www.gov.uk/cma-cases)를 사용한다.
- M&A 상태는 `announced → filed → under_review → approved/blocked → closed/terminated`로
  분리한다. 발표만으로 거래 완료라고 쓰지 않는다.

### 핵심 기술·양산 발표 워치리스트

MVP Tier 1은 아래 공식 뉴스룸과 IR로 제한한다.

- 한국: [Samsung Newsroom](https://news.samsung.com/global/category/corporate/press-release),
  [Samsung IR](https://www.samsung.com/global/ir/reports-disclosures/public-disclosure/),
  [SK hynix Newsroom](https://news.skhynix.com/), [SK hynix IR](https://www.skhynix.com/ir/UI-FR-IR01/)
- 파운드리·메모리: [TSMC Press Center](https://pr.tsmc.com/english/latest-news),
  [TSMC IR](https://investor.tsmc.com/english), [Intel Newsroom](https://newsroom.intel.com/),
  [Micron News](https://www.micron.com/about/press/news)
- 설계: [NVIDIA News RSS](https://nvidianews.nvidia.com/rss),
  [NVIDIA IR](https://investor.nvidia.com/), [AMD Newsroom](https://www.amd.com/en/newsroom.html),
  [AMD IR](https://ir.amd.com/news-events/press-releases)
- 장비: [ASML Press Releases](https://www.asml.com/en/news/press-releases),
  [Applied Materials IR](https://ir.appliedmaterials.com/news-releases/),
  [Lam Research](https://newsroom.lamresearch.com/press-releases),
  [KLA IR](https://ir.kla.com/news-events/press-releases), [Tokyo Electron IR](https://www.tel.com/ir/)

기술 상태는 `developed → sampled → production_ready → volume_production →
customer_qualified → commercial_shipment`로 분리한다. “세계 최초”, 수율, 고객 채택은
독립 확인 전 `company_claim`으로 표시하고 “회사가 발표했다”는 범위를 넘지 않는다.

### 반도체 속보 승격 규칙

1. 범주가 규제·수출통제, 팹 중단·공급 차질, M&A·중대 실적, 핵심 기술·양산 발표 중
   하나여야 한다.
2. 1차 출처 하나가 사건 사실을 직접 뒷받침하거나 서로 독립적인 허용 출처 둘이
   일치해야 한다.
3. 재난 경보, 검색 결과, 기업의 홍보성 주장만으로는 속보로 승격하지 않는다.
4. 기준 미달 자료는 일일 요약 후보로 보류하고 검증 상태를 표시한다.
5. 정정공시, Correction/Amendment/Withdrawal과 다국어 정정 표지를 감시하고 새 개정과
   독자 표시 정정 이력을 만든다.

## 멀티모달 수집·추출 정책

### 처리 순서

1. JSON/XML/CSV/RSS/XBRL 등 구조화 원문
2. canonical HTML과 명시적 메타데이터
3. PDF 안전 검사·페이지 수 확인 후 페이지별 네이티브 텍스트 품질 판정
4. 정상 PDF 텍스트 직접 추출; 텍스트가 없거나 저품질이거나 표·다단·차트 구조 인식이
   필요한 페이지는 PaddleOCR PP-StructureV3
5. XLSX 및 매크로 없는 표 형식
6. HWPX 구조 분석; legacy HWP는 격리 변환기가 검증된 경우만 처리
7. 독립 이미지 문서의 OCR도 승인된 PaddleOCR 프로필 사용
8. 영상·웹캐스트는 공식 링크나 허용 embed만 사용; 기본적으로 다운로드·재업로드·프레임
   캡처하지 않음

### PaddleOCR PDF 인식 프로필

- 패키지는 `paddleocr[doc-parser]==3.7.0`과 호환 PaddlePaddle 3.x를 lock file과 SBOM에
  고정한다. 운영 모델은 배포 시 미리 내려받고 모델명·파일 SHA-256·PaddleOCR/PaddlePaddle
  버전·pipeline YAML hash를 child `ExtractionRun`에 기록한다. 전체 PDF와 OCR 대상 독립
  이미지는 별도 `DocumentExtraction`이 원본 페이지 범위와 선택 child 결과를 집계한다.
  독립 이미지는 디코더 frame 수가 정확히 1일 때만 page set `[0]`인 가상 1페이지 입력이다.
  multi-frame TIFF/APNG와 애니메이션은 첫 frame만 처리하지 않는다. 런타임 다운로드는 금지한다.
- 문서·비문서 실행은 DB의 immutable ExtractionProfileSnapshot을 공통 기준으로 사용한다.
  producer는 `approved` snapshot ID와 key/version/hash hint를 고정하고 worker는 snapshot을
  다시 읽어 event, 로컬 extractor/package/runtime/pipeline 버전, implementation/model
  manifest, config와 calibration key/version/hash를 검증한다. nullable 필드를 명시한 NFC+
  RFC 8785 JCS `profile_material_hash` 고유 제약으로 PostgreSQL null 비교의 빈틈을 피한다.
  draft/retired snapshot은 신규 실행에 사용할 수 없다.
- 한국어·영어·숫자가 섞인 공고는 `korean_PP-OCRv5_mobile_rec`, 영어 전용 문서는
  `en_PP-OCRv5_mobile_rec` 기반의 승인 프로필을 사용한다. 다국어 모델은 실제 청약·
  반도체 골든 표본으로 교정한다. [PP-OCRv5 다국어 모델](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.html)
- 문서 방향, 왜곡 보정, 텍스트라인 방향, 표, 수식과 차트 인식 설정을 profile YAML에
  명시한다. 특히 차트 인식은 기본 비활성이므로 반도체 보고서 프로필에서 명시적으로
  활성화한다.
- DocumentExtraction의 expected page 집합은 안전 파서가 `0..input_page_count-1` 전체로
  만들며 외부 요청으로 축소할 수 없다. 선택 child 결과의 합집합이 이 전체 범위와
  같은지 비교해 누락·중복·충돌이 있으면 성공으로 처리하지 않는다. 파일·페이지·시간·
  메모리 한도를 적용하고, 필요한 경우 결정적 chunk로 나누되 전체 coverage manifest를
  확정하기 전 `evidence.document_ready`를 내보내지 않는다.
- child fingerprint에는 `document_extraction_id`와 원본 checksum을 모두 포함하고
  `(document_extraction_id, extraction_fingerprint)`를 고유하게 만든다. 성공 결과 재사용과
  동시 실행 단일화는 같은 상위 집계 안에서만 수행한다. 동일 bytes가 다른 SourceItem으로
  다시 들어오면 별도 DocumentExtraction/run을 사용해 권리 판정과 보존 수명 주기를 섞지
  않는다.
- 최초 실패나 저신뢰는 원 run을 종결하고 동일 PaddleOCR의 승인된 방향·왜곡·경량
  profile로 새 run/event/dedupe/fingerprint를 한 번만 만든다. `retry_of_run_id`로 원 실행을
  연결하며 기존 event payload는 변경하지 않는다. 다른 OCR 엔진 또는 Hosted API로
  묵시적 fallback하지 않는다.
- 대체 profile 뒤에도 청약 가격·날짜·자격 등 고위험 값이 저신뢰이면 독립 근거가 있어도
  EvidenceAsset을 `manual_required`로 두고 자동발행하지 않는다. child ExtractionRun은
  `low_confidence`로 불변 보존한다. 교차 근거는 관리자 검토 자료이며 `validated_auto`
  재활성화 조건이 아니다. 관리자는 현재 원문·locator·내용·confidence·provenance를 묶은
  subject hash v1에 대해 append-only 결정을 남긴다. 지문은 child run의 불변 fingerprint·
  result/reason hash만 포함하고 상위 completion/coverage와 권리·검토·게시 projection은
  제외한다. subject/latest-decision CAS와 요청 멱등 키를 한 트랜잭션에서 검사하며 불변
  입력이 바뀌면 과거 승인은 자동 무효다.
- MVP는 로컬 Python 워커로 실행한다. CPU를 기준 기능으로 유지하고 실제 골든 표본이
  SC-001을 충족하지 못하면 동일 계약의 NVIDIA GPU profile을 활성화한다. PaddleX serving은
  worker 분리 확장이 필요할 때 재검토한다. [PaddleOCR 로컬 추론](https://www.paddleocr.ai/latest/en/version3.x/inference_deployment/local_inference/inference_engine.html), [자가 호스팅](https://www.paddleocr.ai/main/en/version3.x/inference_deployment/serving/serving.html)

### 파일 안전과 품질

- 다운로드 전 허용 도메인, DNS/IP 재검증, 리디렉션 수, Content-Length와 MIME 허용 목록을
  검사해 SSRF와 과대 파일을 막는다.
- 원본 파일명, 공식 첨부 ID, MIME, 바이트 크기, SHA-256, ETag/Last-Modified와 객체
  버전을 보존한다.
- 압축 해제 크기·파일 수 한도, 매크로/실행파일 차단과 격리 워커를 둔다. 안전 검사나
  파싱 전에 실패하면 해당 DocumentExtraction/GenericExtractionAttempt와 RunStep을
  `failed`로 두고 EvidenceAsset/ready를 만들지 않으며 다른 자료 처리는 계속한다. 유효한
  결과의 신뢰도만 부족할 때 EvidenceAsset `manual_required`를 사용한다.
- 표/OCR은 페이지·시트·셀/영역 locator, DocumentExtraction·child run, PaddleOCR
  패키지·runtime·pipeline·모델 manifest·설정 hash와 block별 신뢰도를 기록한다. 청약
  가격·일정 같은 고위험 값이 저신뢰이면 교차 근거와 무관하게 관리자 검토 전 게시하지
  않는다.
- HTML·구조화 데이터·스프레드시트·브라우저 캡처·미디어·수동 입력 같은 비문서 파생
  자산은 GenericExtractionAttempt와 별도 request/ready 이벤트를 사용하고 engine-locator-
  validation 매트릭스를 강제한다. deterministic/manual 결과는 confidence와 calibration
  profile을 null로 둔다. 실제 골든 표본·metric·임계값 manifest의 승인된 key/version/hash가
  모두 있는 `calibrated` 추출기만 0..1 값을 기록하며 `manual_entry`는 항상 manual이다.
- legacy HWP를 신뢰성 있게 변환하지 못하면 원본과 메타데이터만 보관하고 관리자에게
  수동 검토를 요청한다. 확장자만으로 형식을 판단하지 않는다.

### 시각 자료와 권리

- 분석을 위한 수집 권한과 블로그 재게시 권한을 별도로 판정한다.
- 공공데이터 API의 “이용허락범위 제한 없음”이 운영기관 웹 페이지, PDF, 사진까지 자동으로
  확장된다고 보지 않는다. 개별 KOGL/라이선스와 크레디트 문구를 스냅샷으로 저장한다.
- 기업 보도자료의 사진·로고·차트는 공개되어 있다는 이유만으로 재게시하지 않는다.
  명시적 editorial-use 허가가 없으면 내부 근거로만 보관한다.
- PDF/화면 캡처는 기본적으로 내부 증거다. 게시 권리가 확인되어도 이해에 필요한 최소
  영역, 출처 링크, 캡션과 대체 텍스트를 요구한다.
- 게시용 시각화는 가능하면 허용된 공시 숫자와 사실에서 자체 표·차트를 생성한다. 이때
  변환식, 단위, 기준일과 원출처를 함께 기록한다.

## 편집·검증 설계

### 두 단계 생성

1. **Evidence pass**: 허용 증거에서 원자적 주장, 수치·단위·시각, 사실/회사 주장 구분,
   지지·충돌 증거와 locator를 구조화 JSON으로 만든다.
2. **Editorial pass**: 검증된 주장 집합만 입력해 제목, 요약, 핵심 사실, 배경, 의미,
   분석/전망, 주의사항과 출처 블록을 만든다.

생성 결과는 `body_blocks` 스키마를 통과해야 하며 모델이 만든 URL이나 인용은 허용하지
않는다. 모든 URL은 SourceItem에서만 선택한다. 초안은 기계적인 반복, 과장, 번역투를
줄이되 실제 취재, 체험, 전문 자격, 감정이나 인간 인용을 만들어내지 않는다.

### 차단형 품질 검사

- 모든 사실 주장에 허용 EvidenceAsset 연결
- 청약 핵심 사실과 반도체 속보의 강화 검증 수 충족
- 원문끼리 충돌할 때 우선순위와 제외 이유 기록
- 사실/기업 주장/해석/전망의 시각적·문장적 구분
- 이미지/캡처/차트의 권리, 크레디트와 대체 텍스트
- 존재하지 않는 인용·경험·자격·저자 표현 0건
- 채널별 HTML 정화, 제목/본문/자산 제한과 출처 링크 유지
- 새 개정의 내용·정책 지문이 바뀌면 기존 승인 무효화

## 운영·복구 정책

- target 검증은 읽기 전용 preflight, 격리 test target의 쓰기 canary와 운영 target의
  관리자 승인 파일럿으로 분리한다. preflight는 API root, 인증 사용자/블로그, 경로와
  선언 capability를 확인할 뿐 실제 쓰기 성공을 보장하지 않는다. canary는 동일 채널의
  test target에서 글·미디어 생성, 수정, 공개 확인, 철회/삭제와 정리를 실행한다. 운영
  target은 해당 canary의 정책 버전을 참조하고 첫 관리자 승인 실제 게시와 공개 확인을
  성공한 뒤에만 자동발행할 수 있다.
- 모든 작업 메시지는 `job_id`, `correlation_id`, `entity_id`, `operation`, `attempt`,
  `dedupe_key`, `policy_version`을 가진다.
- 외부 호출에는 연결/읽기 timeout, 출처별 속도 제한, Retry-After 우선, 지수 백오프와
  지터를 적용한다. validation 오류·권한 거부는 자동 재시도하지 않는다.
- 워커가 재전달할 수 있음을 전제로 작업을 멱등하게 만들고 DB 트랜잭션 커밋 후 큐에
  후속 작업을 보낸다.
- 외부 발행 성공 직후 응답이 유실되면 `unknown_outcome`으로 두고 원격 조정 전 create를
  반복하지 않는다.
- 전역 kill switch는 새 수집·발행 디스패치를 차단한다. 이미 전송된 외부 요청은 취소로
  가정하지 않고 결과를 조정한다.
- 구조화 로그에 본문, 원문 전문, 쿠키, OAuth 토큰을 기록하지 않는다. 상관관계 ID,
  상태, 건수, 시간, 분류된 오류 코드만 기록한다.

## 테스트 결정

- 공식 API 응답의 최소 익명 fixture를 고정하고 스키마 변경 계약 테스트를 둔다.
- PDF/HWPX/XLSX/OCR fixture는 한국어·영어·혼합 언어, 정상 텍스트, 스캔, 회전·왜곡,
  다단, 표·수식·차트, 손상·암호화 파일, 페이지 누락, 과대 파일과 저신뢰 숫자를 포함한다.
  PaddleOCR 결과는 package/runtime/pipeline/model/config provenance, 전체 page index,
  좌표·읽기 순서와 골든 핵심 값을 비교한다. 최초 요청 집합 자체가 한 페이지를 누락한
  경우와 child만 완료된 경우 document ready가 차단되는지, 대체 profile이 새 run/event/fingerprint를
  사용하는지, 계속 저신뢰인 고위험 값이 교차 근거가 있어도 자동발행되지 않는지 검증한다.
- WordPress는 승인된 테스트 사이트에서 preflight 후 media upload→draft create→update→
  publish→공개 GET 200→draft/trash canary를 검증한다. `future`는 capability 검사로만
  검증하고 두 채널 운영 예약에는 사용하지 않는다. Application Password 폐기 후 접근
  거부도 확인한다.
- Blogger는 승인된 테스트 블로그에서 create→patch→future publish→revert/delete를 검증한다.
- WordPress가 실제 공개 상태이고 공개 URL이 비인증 GET 200을 반환하기 전 Blogger
  dispatch가 대기하고, 이후 Blogger 렌더가 해당 URL과 동일한 사실·출처 의미를
  포함하는지 검증한다.
- 동일 publish 메시지 100회, 채널 한쪽 실패, 응답 유실, 토큰 만료, 정정 연쇄, 일정 중복,
  kill switch와 워커 재시작을 고장 주입한다.
- 관리자 E2E는 로그인→수동 수집→증거/제외 사유 확인→개정→승인→WordPress 발행→
  Blogger 맞춤본 발행→정정→중지를 검증한다. Playwright는 채널 로그인 게시 자동화에
  사용하지 않는다.

## 해결된 미확정 사항과 계획 게이트

| 항목 | 결과 |
|---|---|
| 관리자 UI/백엔드 구조 | Django 서버 렌더링 단일 서비스 |
| 장시간/일정 작업 | Celery+Redis, 단일 Beat 디스패처, DB 멱등 잠금 |
| 데이터/파일 저장 | PostgreSQL + 버전/체크섬 S3 호환 저장소 |
| 주제별 초기 출처 | 공식 출처 레지스트리와 ID/정정/권리 규칙 확정 |
| 멀티모달 형식 | 구조화→네이티브 문서→PaddleOCR 순서, HWP/PaddleOCR 실패 시 수동 검토 |
| PDF OCR·구조 인식 | 로컬 PaddleOCR 3.7.0 PP-StructureV3, 한국어·영어 고정 모델과 표·수식·차트 profile, 모델/config checksum, 다른 OCR fallback 금지 |
| WordPress 자동발행 | Core REST API로 가능; HTTPS Application Password·media·cron·permalink 계약 검증 필요 |
| Google Blogger 자동발행 | 공식 API로 가능; OAuth·이미지 URL·정책 게이트 필요 |
| 대표 원문과 보조 배포 순서 | WordPress 실제 공개와 공개 URL 200 확인 후 Blogger 맞춤본을 발행하도록 확정 |
| 네이버 블로그 자동발행 | 공식 수단 부재로 제외하고 WordPress 대체를 사용자 승인으로 확정 |

모든 계획 미확정 사항이 해소됐고 Constitution 1.0.1의 공식 채널 제약과 일치한다.
WordPress와 Blogger 샌드박스 계약 테스트가 실패하면 해당 target의 자동발행만 차단하며,
비공식 인터페이스로 우회하지 않는다. 따라서 `$speckit-tasks` 단계로 진행할 수 있다.
