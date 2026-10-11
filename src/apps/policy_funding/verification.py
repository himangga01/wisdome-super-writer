"""Dated regression case: reviewed evidence is separate from live matching notices."""

import re

from apps.policy_funding.contracts import FundingNotice
from apps.policy_funding.reference_evidence import PH, evidence_for

OLS = "https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01"
YANGSAN = "https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_000000000126815"
YEONGCHEON = "https://gbsinbo.co.kr/page/10045/10003.tc?gdsNo=12&importUrl=%2Fguaranteegoods%2Fdetail.tc&pageDtlOrdrNo=1&pageIndex="
REVIEWED_PROGRAMS = {
    "404": "2026년 4분기 일반경영안정자금(대리대출) 접수 안내",
    "405": "2026년 4분기 긴급경영안정자금(일시적경영애로)(대리대출) 접수 안내",
    "406": "2026년 4분기 장애인기업지원자금(대리대출) 접수 안내",
    "407": "2026년 4분기 청년고용연계자금(대리대출) 접수 안내",
}


def verify_provided_case(notices: tuple[FundingNotice, ...]) -> dict:
    """Evaluate the supplied dated example without asserting personal eligibility."""
    matches = [
        row
        for row in notices
        if row.source_id == "semas_agency"
        and row.title == REVIEWED_PROGRAMS.get(row.external_id)
        and row.regions == ("national",)
        and row.kinds == ("agency_loan",)
        and row.published_on is not None
        and row.published_on.year == 2026
        and re.match(
            r"^2026\.\s*10\.\s*6\.\s*(?:\([^)]*\)\s*)?10:00\s*(?:~|부터)",
            row.application_period or "",
        )
    ]
    live = [row for row in matches if row.provenance == "official_fetch"]
    opening_status = (
        "supported_by_collected_notice" if live else "fixture_only" if matches else "unverified"
    )
    claims = [
        {
            "id": "national_opening",
            "claim": "2026년 4분기 대리대출 10월 6일 10시 접수",
            "status": opening_status,
            "sources": [row.official_url for row in live] or [OLS],
            "explanation": (
                "소진공 원 공고 404–407의 해당 자금 일정입니다."
                " 모든 정책자금이 같은 날 시작한다는 뜻은 아닙니다."
            ),
        },
        {
            "id": "certificate_not_approval",
            "claim": "확인서와 표시 한도는 대출 승인이 아님",
            "status": "supported_by_dated_review",
            "sources": [OLS],
            "explanation": (
                "공고는 확인서가 대출을 보증하지 않는다고 명시합니다. "
                "보증·은행 심사와 약정·실행을 구분합니다."
            ),
        },
        {
            "id": "pohang_expansion",
            "claim": "포항 300억원 추가·연간 1500억원",
            "status": "unverified",
            "sources": [PH],
            "explanation": (
                "조회한 재단 표에는 1200억원이 표시됐습니다. 추가 공급을 확인하는 최신 "
                "원 공고가 필요합니다. 기존 표만으로 확대가 없었다고 단정하지 않습니다."
            ),
        },
        {
            "id": "pohang_terms",
            "claim": "일반 5천만원·청년/다자녀 1억원·2년간 3% 이자지원",
            "status": "supported_with_conditions",
            "sources": [PH],
            "explanation": (
                "조회된 상품 조건의 최대 범위입니다. 세부 대상·추가 "
                "공급 버전과 실제 심사를 확인해야 합니다."
            ),
        },
        {
            "id": "yangsan_general_start",
            "claim": "양산 일반자금 10월 6일 시작",
            "status": "supported_by_dated_review",
            "sources": [YANGSAN],
            "explanation": (
                "2026-2635호 첨부문서 일반자금은 2026년 "
                "10월 6일 오전 9시입니다. 전국 소진공의 오전 "
                "10시와 다릅니다."
            ),
        },
        {
            "id": "yangsan_general_terms",
            "claim": "경영안정 7천만원·창업 5천만원·최대 4년 2.5% 지원",
            "status": "supported_with_conditions",
            "sources": [YANGSAN],
            "explanation": (
                "업력과 상환 방식에 따라 다릅니다. 3년 일시상환은 "
                "지원도 3년입니다. 최대 4년을 모든 신청자에게 "
                "적용하면 안 됩니다."
            ),
        },
        {
            "id": "yangsan_youth_start",
            "claim": "양산 청년특별자금도 동일한 10월 6일 신규 접수로 해석",
            "status": "different_program_date",
            "sources": [YANGSAN],
            "explanation": (
                "같은 첨부문서의 청년특별자금 절은 2026년 2월 4일 오전 9시를 적고 "
                "있습니다. 원문에 없는 같은 시작일을 추가하지 마세요."
            ),
        },
        {
            "id": "yangsan_youth_terms",
            "claim": "청년 5천만원·최대 4년 3%·첫해 보증료 지원",
            "status": "supported_with_conditions",
            "sources": [YANGSAN],
            "explanation": (
                "연령·창업기간·상환 방식 조건이 있습니다. 첫해 보증료 지원은 보증대출에 "
                "해당하며 분할 지원입니다. 이자 전체 면제가 아닙니다."
            ),
        },
        {
            "id": "yeongcheon_expansion",
            "claim": "영천 10억원 추가·연간 약128억원",
            "status": "unverified",
            "sources": [YEONGCHEON],
            "explanation": (
                "조회한 표는 117.6억원입니다. 합계가 비슷하게 계산된다는 이유만으로 추가"
                " 출연을 확정하지 않습니다. 최신 추가 공고가 필요합니다."
            ),
        },
        {
            "id": "yeongcheon_terms",
            "claim": "일반3천만원·청년5천만원·최대5년·2년간3%지원",
            "status": "supported_with_conditions",
            "sources": [YEONGCHEON],
            "explanation": (
                "조회된 상품표 기준이며 청년 연령·창업기간 조건이 "
                "있습니다. 대출 최대 기간과 이자지원 기간은 서로 "
                "다릅니다."
            ),
        },
    ]
    for claim in claims:
        claim["evidence"] = evidence_for(claim["id"])
    return {
        "schema_version": 1,
        "as_of": "2026-10-05",
        "input_provenance": "user_provided",
        "loan_approval": False,
        "claims": claims,
        "review_note": (
            "2026-10-05 조사 사례입니다. 날짜가 지난 뒤 최신 조건으로 사용하지"
            " 마세요. 첨부문서는 표의 적용 범위를 검토한 기록이며 자동 파서의 정확도 "
            "보장이 아닙니다."
        ),
    }
