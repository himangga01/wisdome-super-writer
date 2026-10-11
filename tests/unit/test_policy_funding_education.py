from datetime import UTC, date, datetime


def test_agency_explanation_separates_certificate_guarantee_and_bank_approval():
    from apps.policy_funding.education import learning_content

    guide = learning_content()
    stages = guide["journeys"]["agency"]
    assert [step["id"] for step in stages] == [
        "select",
        "eligibility",
        "certificate",
        "guarantee",
        "bank",
        "disbursement",
        "repayment",
    ]
    assert next(step for step in stages if step["id"] == "guarantee")["conditional"] is True
    assert guide["principles"]["certificate_is_approval"] is False
    assert guide["principles"]["ceiling_is_award"] is False
    assert len(guide["glossary"]) >= 24
    assert all(
        term["plain"] and term["example"] and term["reference"] for term in guide["glossary"]
    )


def test_notice_explanation_preserves_unknown_terms_and_does_not_invent_awards():
    from apps.policy_funding.contracts import FundingNotice
    from apps.policy_funding.rendering import explain_notice, render_notice

    notice = FundingNotice(
        source_id="semas_agency",
        external_id="404",
        title="2026년 일반경영안정자금",
        issuer="소진공",
        official_url="https://ols.semas.or.kr/ols/man/SMAN052M/page.do?bltwtrSeq=404&bbsTypeCd=01",
        published_on=date(2026, 9, 11),
        application_period=None,
        regions=("national",),
        kinds=("agency_loan",),
        evidence_text="자금별 상세한 요건을 확인하세요.",
        fetched_at=datetime(2026, 10, 5, tzinfo=UTC),
    )
    explanation = explain_notice(notice)
    assert explanation["approved_amount"] is None
    assert explanation["loan_rate"] is None
    assert explanation["application_period"] == "원 공고에서 확인하세요"
    output = render_notice(notice)
    assert notice.official_url in output
    assert "7천만원" not in output
    assert "확인서" in output and "승인" in output


def test_hypothetical_examples_cannot_be_mistaken_for_program_rates():
    from apps.policy_funding.education import learning_content

    examples = learning_content()["examples"]
    assert examples
    assert all(item["hypothetical"] is True for item in examples)
    assert all("가정" in item["label"] for item in examples)
