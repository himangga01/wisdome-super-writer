"""Portable, dated manual research evidence; not a current-program approval."""

OLS = "https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01"
YS = "https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId=PBLN_000000000126815"
ATTACHMENT_HASH = "5e7b9902d109f71ab5aa90bb18388c734bf0b921df0ed0ac757fa6667141958c"
PH = "https://gbsinbo.co.kr/page/10045/10003.tc?gdsNo=5&importUrl=%2Fguaranteegoods%2Fdetail.tc&pageDtlOrdrNo=1&pageIndex="
YC = "https://gbsinbo.co.kr/page/10045/10003.tc?gdsNo=12&importUrl=%2Fguaranteegoods%2Fdetail.tc&pageDtlOrdrNo=1&pageIndex="


def evidence_for(claim_id: str) -> list[dict]:
    common = {
        "checked_on": "2026-10-05",
        "method": "dated_manual_primary_source_review",
        "attachment_sha256": None,
        "source_date": None,
    }
    if claim_id in {"national_opening", "certificate_not_approval"}:
        return [
            {
                **common,
                "issuer": "소상공인시장진흥공단",
                "source_date": "2026-09-11",
                "url": OLS,
                "locator": "OLS notice 404 / #cntnDiv / 접수기간 and 유의사항",
                "excerpt": (
                    "접수기간 : 2026. 10. 6.(화) 10:00 ~ 예산소진 시까지"
                    if claim_id == "national_opening"
                    else "지원대상임을 확인하는 서류로 자금대출을 보증하지 않습니다."
                ),
                "scope": (
                    "2026 Q4 general management "
                    "stability agency loan; "
                    "certificate is not loan "
                    "approval"
                ),
            }
        ]
    if claim_id.startswith("yangsan_"):
        youth = "youth" in claim_id
        return [
            {
                **common,
                "issuer": "양산시",
                "source_date": "2026-09-28",
                "url": YS,
                "locator": "Notice 2026-2635, HWPX section 3 (youth)"
                if youth
                else "Notice 2026-2635, HWPX section 2 (ordinary Q4)",
                "excerpt": (
                    "2026. 2. 4.(수) 09:00"
                    if claim_id == "yangsan_youth_start"
                    else "2026. 10. 6.(화) 09:00"
                    if claim_id == "yangsan_general_start"
                    else "청년창업 특별자금 5천만원 / 최대 4년간 연 3% / 신용보증수수료 1년분 전액"
                    if youth
                    else (
                        "경영안정자금 (사업자등록 후 6개월 이상) 7천만원 /"
                        " 최대 4년간 연 2.5% / 창업자금 5천만원"
                    )
                ),
                "scope": (
                    "Youth: maximum KRW50m, up to 4 years/3% subsidy; age/business-age conditions"
                )
                if youth
                else (
                    "Ordinary Q4: working "
                    "KRW70m/startup KRW50m; up to 4"
                    " years/2.5%; duration depends "
                    "on repayment choice"
                ),
                "attachment_sha256": ATTACHMENT_HASH,
                "attachment_url": "https://www.bizinfo.go.kr/cmm/fms/fileDown.do?atchFileId=FILE_000000000774792&fileSn=0",
                "filename_qualifier": (
                    "Linked filename includes (안); retain municipal notice identity and qualifier"
                ),
            }
        ]
    if claim_id.startswith("pohang_"):
        return [
            {
                **common,
                "issuer": "경북신용보증재단 / 포항시 상품",
                "url": PH,
                "locator": "2026 product 5 / 보증규모, 보증한도, 이차보전; issue date not stated",
                "excerpt": "1,200억원 / 5천만원 / 1억원 / 2년간 연 3%",
                "scope": (
                    "Retrieved table only; "
                    "youth/multiple-child "
                    "eligibility differs. Does not "
                    "prove later expansion to "
                    "KRW150bn."
                ),
            }
        ]
    if claim_id.startswith("yeongcheon_"):
        return [
            {
                **common,
                "issuer": "경북신용보증재단 / 영천시 상품",
                "url": YC,
                "locator": (
                    "2026 product 12 / 보증규모, 한도, 상환, 이차보전; issue date not stated"
                ),
                "excerpt": "117.6억원 / 3천만원 / 5천만원 / 2년간 연 3%",
                "scope": (
                    "Retrieved table only; maximum "
                    "5-year term includes "
                    "conditional "
                    "extension/repayment choices. "
                    "Later expansion unverified."
                ),
            }
        ]
    return []
