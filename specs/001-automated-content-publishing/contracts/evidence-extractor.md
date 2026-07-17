# Evidence Extractor Contract

## 범위

이 계약은 원문·첨부에서 재현 가능한 `EvidenceAsset`을 만드는 추출기 포트를 정의한다.
네이티브 PDF 텍스트가 없는 페이지, 품질 기준을 통과하지 못한 페이지, 표·다단·
차트·수식 등 문서 구조 인식이 필요한 PDF 페이지, 그리고 내용 이해에 OCR이 필요한 독립
정적 이미지는 반드시 로컬
`paddleocr[doc-parser]==3.7.0`의 `PPStructureV3`로 처리한다. Tesseract, 다른 OCR 엔진,
PaddleOCR Hosted API로 자동 대체하지 않는다.
각 호출은 상위 `DocumentExtraction`에 속한 child `ExtractionRun` 하나만 완료한다. child는
전체 PDF/이미지 입력 성공이나 `evidence.document_ready`를 직접 선언할 수 없다. 독립 이미지는 상위
집계에서 가상 1페이지 문서로 다룬다.

공식 근거:

- [PaddleOCR v3.7.0](https://github.com/PaddlePaddle/PaddleOCR/releases/tag/v3.7.0)
- [PP-StructureV3 PDF 처리와 결과](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html)
- [PP-OCRv5 다국어 모델](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.html)

## 입력 DTO

| 필드 | 필수 | 계약 |
|---|---|---|
| `run_id`, `run_source_item_id` | 예 | CollectionRun과 그 실행에서 발견된 SourceItem join UUID |
| `document_extraction_id` | 예 | 원본 전체 페이지를 집계하는 상위 UUID |
| `extraction_run_id` | 예 | 사전에 DB에 생성된 UUID |
| `retry_of_run_id` | 아니오 | 대체 profile이면 종결된 원 run UUID |
| `source_item_id` | 예 | 불변 원문 UUID |
| `input_object_key`, `input_object_version` | 예 | 허용 저장소 안의 검증된 로컬 입력 참조 |
| `input_checksum` | 예 | 다운로드 후 다시 확인할 SHA-256 |
| `input_kind` | 예 | `pdf/standalone_image` |
| `mime_type` | 예 | 실제 sniff 결과가 `application/pdf`, `image/png`, `image/jpeg`, `image/tiff` 중 하나와 일치해야 함 |
| `input_frame_count` | 예 | PDF는 null, standalone image는 디코더가 확인한 정확히 `1` |
| `input_page_count` | 예 | 안전 파서가 확인한 전체 페이지 수 |
| `requested_page_indices` | 예 | 정렬·중복 제거된 0-based 페이지 목록 |
| `page_set_hash` | 예 | 요청 페이지 목록의 SHA-256 |
| `profile_snapshot_id` | 예 | DB의 승인된 불변 ExtractionProfileSnapshot UUID |
| `profile_material_hash` | 예 | snapshot 전체 nullable material의 NFC+RFC 8785 JCS SHA-256 |
| `profile_key`, `profile_version` | 예 | 승인되고 변경 불가능한 OCR profile |
| `fingerprint_schema_version` | 예 | 현재 `v1` |
| `deadline_at` | 예 | 작업 전체 시간 한도 |
| `correlation_id` | 예 | CollectionRun 상관관계 ID |

`standalone_image`는 `input_frame_count=1`, `input_page_count=1`,
`requested_page_indices=[0]`만 허용한다. 다중 frame TIFF/APNG와 애니메이션은 첫 frame만
처리하지 않고 입력 전체를 거절한다.
이벤트에는 PDF/이미지 바이너리, 모델 파일, 전체 OCR 결과 또는 비밀 값을 넣지 않는다. 워커는
`extraction_run_id`로 아래 profile을 조회하고, 제한된 읽기 권한으로 객체를 로컬 임시
영역에 내려받는다.

## PaddleOCR Profile

| 필드 | 고정 규칙 |
|---|---|
| `package` | `paddleocr[doc-parser]==3.7.0` |
| `runtime` | lock file로 고정된 호환 PaddlePaddle 3.x |
| `pipeline_name` | `PPStructureV3` |
| `implementation_manifest_hash` | 추출기 코드·패키지·런타임·pipeline manifest의 SHA-256 |
| `text_recognition_model` | 한국어·혼합 문서는 `korean_PP-OCRv5_mobile_rec`; 영어 전용은 승인된 `en_PP-OCRv5_mobile_rec` |
| `model_dirs` | 배포 시 사전 탑재된 읽기 전용 로컬 경로 |
| `model_manifest_hash` | 모델명·상대 경로·파일 SHA-256 목록의 SHA-256 |
| `config_hash` | 정규화된 전체 pipeline YAML의 SHA-256 |
| `features` | 방향 분류, 왜곡 보정, 텍스트라인 방향, 표, 수식 명시; 반도체 profile은 차트 인식도 명시적으로 활성화 |
| `limits` | 파일 bytes, 전체 페이지 수, page chunk, 페이지별/전체 timeout, memory, concurrency |

producer는 신규 run 생성 시 `approval_state=approved`인 profile snapshot을 고정한다. worker는
snapshot ID로 DB 기준 행을 다시 읽고 event hint와 extractor/package/runtime/pipeline 버전,
implementation/model manifest와 config hash를 로컬 파일과 대조한다. 어떤 기본값도 provenance에서 생략하지 않으며 하나라도
다르면 추론을 시작하지 않는다. retired snapshot은 과거 재현에만 사용하고 신규 run을
만들 수 없다.

## 출력 DTO

| 필드 | 계약 |
|---|---|
| `status` | `succeeded/low_confidence/failed` |
| `document_extraction_id` | 입력과 동일한 상위 집계 ID |
| `extraction_run_id` | 입력과 동일 |
| `input_kind` | 입력과 동일한 `pdf/standalone_image` |
| `input_frame_count` | PDF는 null, standalone image는 `1` |
| `profile_snapshot_id` | 입력과 동일한 승인 snapshot UUID |
| `profile_material_hash` | 입력·DB snapshot과 동일한 전체 material SHA-256 |
| `retry_of_run_id` | 대체 profile이면 원 run ID |
| `engine` | `paddleocr_ppstructurev3` |
| `fingerprint_schema_version`, `extraction_fingerprint` | `v1`과 정규 직렬화 SHA-256 |
| `package_version` | `3.7.0` |
| `runtime_version` | 실제 PaddlePaddle 버전 |
| `pipeline_name` | `PPStructureV3` |
| `model_manifest_hash`, `config_hash` | 실행에 사용한 값 |
| `requested_page_indices`, `processed_page_indices` | 정렬·중복 없는 실제 집합 |
| `run_complete` | 이 child의 두 집합이 동일하고 원본 범위 안이면 `true` |
| `result_object_key`, `result_checksum` | 정규화 JSON/Markdown manifest 위치와 지문 |
| `confidence_summary` | profile별 text/table/chart/formula 및 고위험 필드 요약 |
| `low_confidence_reasons_hash` | low_confidence이면 정규화 사유 SHA-256, succeeded면 null |
| `duration_ms`, `peak_memory_bytes`, `device_type` | 성능 회귀 측정값 |
| `error_code`, `error_detail_redacted` | 실패 시 분류 코드와 비민감 요약 |

`status=succeeded/low_confidence`에는 `run_complete=true`가 필수다. 저신뢰 고위험 값은
EvidenceAsset의 `manual_required` 검토 상태로 투영하며 관리자 판정이 child run 상태를
변경하지 않는다. 이는 child run의 완료일 뿐 PDF/이미지 입력 전체 완료를 뜻하지 않는다.
대용량 JSON, Base64 이미지와 Markdown
본문은 작업 이벤트에 넣지 않고 버전·checksum을 가진 객체로 저장한다.

## 전체 문서 집계 계약

상위 `DocumentExtraction`은 안전 파서가 확인한 `input_page_count`로
`expected_page_indices=[0..input_page_count-1]`을 직접 생성한다. 외부 요청이 expected
집합을 지정하거나 축소할 수 없다. 독립 이미지는 `[0]`으로 고정한다. 페이지별
native/PaddleOCR 라우팅과 최종 선택 child
run을 `routing_manifest`에 기록하고 다음 조건을 모두 만족할 때만 `document_complete=true`,
`coverage_manifest_hash`와 `selected_evidence_manifest_hash`를 확정한다.

1. 선택 child run의 processed page 합집합이 expected 집합과 정확히 같다.
2. 범위 밖 페이지, 누락, 중복 선택 또는 서로 충돌하는 선택 결과가 없다.
3. 모든 선택 child가 `succeeded`이거나, `low_confidence` child의 고위험 값에 대해 현재
   review subject hash와 일치하는 관리자의 명시적 승인이 완료됐다. child 상태는 그대로다.
4. coverage manifest에는 각 page index, 선택 run ID, engine, result checksum을 포함한다.
5. selected evidence manifest에는 정렬된 전체 EvidenceAsset ID, content/result checksum과
   locator hash를 포함한다.

`evidence.document_ready`는 상위 집계만 만들며 `run_id`, `run_source_item_id`,
`source_item_id`, `document_extraction_id`, `input_page_count`,
`coverage_manifest_hash`, `selected_evidence_manifest_hash`, `document_complete=true`를 포함한다.
단일 EvidenceAsset/extraction run ID는 넣지 않는다. 소비자는 상위 ID로 routing manifest와
선택 child/EvidenceAsset 전체를 조회하고 expected 범위·coverage를 다시 확인한다.
`run_id`, `run_source_item_id`, `source_item_id`는 DocumentExtraction의 RunSourceItem 계보와
정확히 같아야 하며 불일치 payload는 처리하지 않는다.
Admin API의 child EvidenceAsset provenance는 parent completion/coverage를 복제하지 않고
`document_extraction_id`만 참조한다. parent aggregate는
`GET /document-extractions/{documentExtractionId}`에서 한 번만 반환하며 publisher는 이
서버 계산 summary와 selected evidence manifest 포함 여부를 DB에서 다시 검증한다.

## 페이지·Block 정규화

각 페이지는 `page_index`, 원본 크기·회전, 전처리 결과 지문과 다음 block 배열을 가진다.

| 필드 | 계약 |
|---|---|
| `block_id` | 페이지 안에서 결정적인 ID |
| `block_type` | `title/text/list/table/chart/formula/image/header/footer/other` |
| `polygon`, `bbox` | 원본 페이지 좌표계의 위치 |
| `reading_order` | 페이지 안의 0-based 순서 |
| `text`, `confidence` | 정규화 문자열과 원시 인식 신뢰도 |
| `structured_ref` | 표 셀·HTML, 차트/수식 결과 객체 위치 nullable |
| `crop_ref` | 최소 영역 시각화 객체 위치 nullable |

표는 셀별 행·열 span, polygon/bbox, text와 confidence를 보존한다. 차트·수식은 원본 영역과
구조 결과를 함께 보존한다. EvidenceAsset locator는 `page_index`, `block_id`, polygon/bbox와
reading order를 잃지 않아야 한다.

## 라우팅과 멱등성

1. 안전 파서가 PDF 전체 페이지 수와 암호화·손상 여부 또는 독립 이미지의 실제 MIME·
   크기·픽셀 한도를 검사한다. 이미지는 가상 page index 0을 만든다.
2. 정상 네이티브 텍스트 페이지는 직접 추출할 수 있다.
3. 텍스트가 없거나 문자 품질이 낮거나 구조 인식이 필요한 페이지는 이 PaddleOCR 계약을
   사용한다.
4. fingerprint v1은 `fingerprint_schema_version + document_extraction_id + input_kind +
   mime_type + input_frame_count + input_checksum + page_set_hash + engine + profile_key +
   profile_snapshot_id + profile_material_hash + profile_version + package/runtime version + model_manifest_hash + config_hash`의 null
   표현까지 포함해 Unicode NFC 후 RFC 8785 JCS로 직렬화한 SHA-256이다.
5. 같은 `document_extraction_id`와 fingerprint의 성공 실행은 재사용하고 동시에 하나만
   실행한다. 동일 PDF가 다른 SourceItem의 새 DocumentExtraction으로 들어오면 별도 run을
   만들며 결과·권리·보존 수명 주기를 공유하지 않는다.
6. child 요청/완료 page index가 다르면 child를 성공 처리하지 않는다. child는 어떤 경우에도
   `evidence.document_ready`를 발행하지 않는다.

## 신뢰도와 실패 처리

- confidence 임계값은 하나의 전역 수치가 아니라 한국어 본문, 영문 본문, 표, 차트, 수식과
  청약 고위험 필드별 골든 표본으로 교정한 profile 정책을 사용한다.
- 저신뢰나 방향·왜곡 문제는 원 run을 terminal `low_confidence`로 끝낸다. 승인된 대체
  profile을 한 번 사용할 때는 새 run ID, event ID, dedupe key와 fingerprint를 만들고
  `retry_of_run_id`/`causation_id`로 연결한다. 기존 이벤트 payload를 변경하지 않는다.
- 청약 가격·날짜·자격처럼 영향도가 높은 값이 대체 profile 뒤에도 저신뢰이면 child run은
  `low_confidence`, EvidenceAsset은 `manual_required`이며 자동발행할 수 없다. 교차 근거는
  관리자 판단을 돕지만 `validated_auto`를 다시 허용하지 않는다. 승인은 현재 evidence
  review subject hash에 대한 별도 append-only 결정으로 기록하며 child 상태를 변경하지 않는다.
- review subject v1은 child run ID/fingerprint/result checksum/reason hash와 원문·locator·
  내용·confidence만 포함한다. DocumentExtraction state, covered pages, completion/coverage
  hash와 검토·권리·게시 projection은 제외해 승인이 자기 지문을 바꾸지 않게 한다.
- low-confidence reason은 impact, code, field/block ref, 관측 confidence, threshold와
  비민감 설명의 전체 정규 manifest로 보존하고 object key/version/hash를 run에 기록한다.
  high-impact 우선 뒤 scope/ref/code/value/threshold/message tie-break로 안정 정렬한 전체
  배열을 EvidenceAsset과 승인 화면에 제공하며 그 JCS hash가 provenance reason hash와
  일치해야 한다. 전체 배열을 로드·검증하지 못하면 승인을 금지한다.
- `encrypted_pdf`, `corrupt_pdf`, `unsafe_pdf`, `page_limit_exceeded`, `page_incomplete`,
  `model_manifest_mismatch`, `config_mismatch`, `timeout`, `out_of_memory`, `low_confidence`를
  구분한다.
- 다른 엔진으로 fallback하지 않는다. 모델/config 불일치, unsafe/손상 입력과 재처리 실패는
  ExtractionRun=`failed`, DocumentExtraction=`failed`로 끝내고 EvidenceAsset·document ready를
  만들지 않는다. 유효 결과가 있으나 신뢰도만 부족한 경우에만 run=`low_confidence`,
  EvidenceAsset=`manual_required/publishable=false`를 사용한다.

## 보안·운영 불변조건

- OCR 워커는 non-root, 읽기 전용 root filesystem과 제한된 임시 디렉터리에서 실행한다.
- 발행/OAuth/수집 자격 증명에 접근하지 않고 운영 추론 중 외부 네트워크를 사용하지 않는다.
- 모델은 빌드·배포 단계에서 공식 배포본으로 준비하고 SBOM, 라이선스와 SHA-256을 보존한다.
- 원문, OCR 텍스트, crop/Base64 이미지와 모델 경로를 애플리케이션 로그에 기록하지 않는다.
- 정상·실패·worker 종료 후 임시 파일을 지우고 원본·파생 객체는 프로젝트 보존 정책을 따른다.

## 계약 테스트

- 한국어, 영어, 혼합 언어, 네이티브 텍스트, 스캔, 회전·왜곡, 다단 문서와 독립 이미지
- 병합 셀 표, 수식, 차트, 이미지와 여러 페이지 PDF
- 암호화·손상·과대 PDF, 누락 page chunk, 최초 expected 요청 자체가 한 페이지를 빠뜨린
  경우, 빈 page set, timeout, OOM, model/config checksum 불일치
- 동일 `document_extraction_id`와 fingerprint를 100회 전달할 때 ExtractionRun 하나와 동일
  결과 checksum
- 독립 이미지는 page set `[0]`과 PaddleOCR provenance를 가지며 PDF와 동일한 모델/config
  재현성 및 수동 검토 규칙 적용
- multi-frame TIFF/APNG·애니메이션은 `unsupported_multiframe_image`, page 0 부분 성공과
  document ready 0건
- 승인 전후 DocumentExtraction completion이 바뀌어도 review subject v1 동일
- 대체 profile은 새 run/event/dedupe/fingerprint와 `retry_of_run_id`를 가지며 원 이벤트 불변
- child가 성공해도 전체 page 합집합이 `0..input_page_count-1`이 아니면 ready 이벤트 0건
- 같은 page coverage여도 selected evidence manifest에서 EvidenceAsset 하나가 누락되거나
  ID/content·result checksum/locator hash가 변조되면 document ready 이벤트 0건
- 골든 30페이지의 핵심 날짜·금액·자격·고유명사 정확도와 locator 완전성
- 다른 OCR 엔진 호출 0건, 런타임 모델 다운로드와 외부 네트워크 요청 0건
