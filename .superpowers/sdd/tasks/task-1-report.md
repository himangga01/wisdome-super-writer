# T001 구현 보고서

## 한국어

### 구현 내용

- `http_safety.py`에 HTTP/HTTPS 전용 공통 outbound 경계를 추가했다.
- URL마다 DNS 결과 전체를 검사하여 global 주소만 허용하고 loopback, link-local, private,
  multicast, unspecified, reserved 주소를 거부한다.
- 검증된 IP로 직접 연결하고 원래 Host와 HTTPS SNI를 유지하여 DNS 검사와 실제 연결 사이의
  TOCTOU 간격을 줄였다. 환경 proxy는 사용하지 않는다.
- redirect를 자동 추적하지 않고 최대 5회까지 각 hop의 URL, host, DNS/IP를 다시 검사한다.
- 응답을 64 KiB 단위로 읽으며 Content-Length 사전 검사와 실제 스트림 누적 검사를 함께
  적용한다. 호출별 전체 경과 시간 deadline도 적용한다.
- URL userinfo를 전송하지 않으며, 저장·예외용 URL에서 userinfo, query, fragment를 제거한다.
  네트워크 예외는 원본 예외 문자열 대신 redacted URL과 예외 종류만 남긴다.
- 공통 경계를 source collection, evidence attachment download, WordPress public URL verification에
  연결했다. 수집·evidence·WordPress 결과에 보존되는 URL도 redaction했다.

### 집중 검사 및 출력

- RED 확인: 새 모듈 구현 전 import 검사에서
  `ModuleNotFoundError: No module named 'wisdome_writer.infrastructure.http_safety'`를 확인했다.
- 인라인 런타임 검사: 스킴 차단, loopback/link-local 차단, URL redaction, 스트리밍 크기 제한,
  redirect hop 재검증을 확인했고 `runtime safety checks: 8 assertions passed`가 출력됐다.
- 대상 4개 Python 파일 `compileall`: exit code 0.
- 대상 파일 import 정렬 검사: `All checks passed!`.
- 신규 공통 모듈 전체 Ruff 검사: `All checks passed!`.
- 계획에서 미룬 T032 자동화 테스트는 추가하거나 실행하지 않았다.

### 변경 파일

- `src/wisdome_writer/infrastructure/http_safety.py`
- `src/adapters/sources/http.py`
- `src/apps/evidence/tasks.py`
- `src/adapters/publishers/wordpress/client.py`
- `.superpowers/sdd/tasks/task-1-report.md`

### 자체 리뷰

- 공개 주소 판정은 모든 DNS 응답이 허용 가능한 경우에만 통과하도록 fail-closed로 구현했다.
- redirect 응답 본문은 버퍼링하지 않으며 다음 hop 검증 전에 Location을 외부로 기록하지 않는다.
- 각 IP 시도마다 새 client를 만들어 서로 다른 hostname이 같은 IP를 사용할 때 TLS 연결이
  재사용되지 않도록 했다.
- 기존 adapter의 생성자·수집·다운로드·검증 public interface는 유지했다.
- T002 이후 기능, T032 테스트, controller 소유 `tasks.md`는 변경 범위에 포함하지 않았다.

### 우려 사항

- 계획에 따라 실제 외부 TLS/DNS 서버를 이용한 end-to-end 및 전체 회귀 검증은 T032로
  연기되어 있다. 이번 검사는 deterministic한 집중 런타임 검사와 정적 검사만 수행했다.

## AI-readable English

### Implementation

- Added a shared HTTP/HTTPS-only outbound boundary with fail-closed DNS/IP validation.
- Pins connections to validated public IPs while retaining the original Host header and TLS SNI.
- Disables environment proxies, validates every manual redirect hop, and caps redirects at five.
- Streams in 64 KiB chunks with Content-Length, accumulated-byte, per-operation timeout, and
  whole-request deadline enforcement.
- Redacts userinfo, query data, and fragments from persisted URLs and safe exception messages.
- Integrated the boundary into source collection, evidence downloads, and WordPress public URL
  verification without changing their public interfaces.

### Focused verification

- Pre-implementation import failed for the missing module as expected.
- Focused runtime script: 8 assertions passed.
- Python compileall: exit 0.
- Import-order Ruff check on touched Python files: passed.
- Full Ruff check on the new shared module: passed.
- Deferred T032 tests were neither created nor run.

### Concern

- Live external TLS/DNS integration and full regression coverage remain intentionally deferred to T032.

---

## 수정 라운드 1

### 상태와 커밋

- 상태: `DONE_WITH_CONCERNS`
- 구현 커밋: `f83a07a316e25cedb01caaaf34cfa8888216c7f2 fix: close outbound URL redaction gaps`
- 보고서 경로:
  `D:\project\wisdome-super-writer\.superpowers\sdd\tasks\task-1-report.md`

### 수정 내용

- attachment 링크 자체뿐 아니라 링크 label과 fallback title에 포함된 URL 비밀도 제거했다.
- RSS GUID가 HTTP/HTTPS URL일 때만 userinfo, query, fragment를 제거하고 UUID/URN 등 비 URL
  identity는 그대로 보존한다.
- OpenData row의 중첩 mapping/list/tuple을 재귀 순회하여 URL-shaped 문자열을 redaction하고,
  비 URL `id`와 `noticeId`는 변경하지 않는다.
- WordPress public URL 검증에 configured `site_url` host allowlist를 전달해 최초 URL과 모든
  redirect가 같은 host 경계 안에 있도록 했다.

### 집중 검사

- 수정 전 attachment fallback title assertion이 raw signed URL 때문에 실패하는 것을 확인했다.
- attachment URL/title, RSS URL GUID, OpenData stable identity와 중첩 URL metadata redaction:
  8개 assertion 통과.
- WordPress same-host redirect 허용 및 cross-host redirect 거부: 4개 assertion 통과.
- 수정된 3개 Python 파일 `compileall`: exit code 0.
- 수정된 파일 Ruff import/undefined-name 검사: `All checks passed!`.
- 수정 diff whitespace 검사: exit code 0.
- T032 자동화 테스트는 추가하거나 실행하지 않았다.

### 우려 사항

- 실제 외부 TLS/DNS end-to-end와 전체 회귀 검증은 계획대로 T032에 남아 있다.

## Fix round 1 — AI-readable English

- Status: `DONE_WITH_CONCERNS`
- Implementation commit:
  `f83a07a316e25cedb01caaaf34cfa8888216c7f2 fix: close outbound URL redaction gaps`
- Sanitized attachment titles, URL-shaped RSS GUIDs, and recursively nested OpenData metadata
  while preserving non-URL external identities.
- Restricted WordPress public URL verification and every redirect to the configured site host.
- Focused checks: 8 persistence-redaction assertions and 4 redirect-host assertions passed;
  compileall, focused Ruff, and diff checks passed.
- Concern: live external integration and the full regression suite remain deferred to T032.
