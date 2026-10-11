"""Separate official notice facts from plain educational explanation."""

from apps.policy_funding.contracts import KINDS, FundingNotice
from apps.policy_funding.education import learning_content


def explain_notice(notice: FundingNotice) -> dict:
    kind = (
        "agency"
        if "agency_loan" in notice.kinds
        else "direct"
        if "direct_loan" in notice.kinds
        else None
    )
    guide = learning_content()
    purpose = "guidance"
    if any(term in notice.title for term in ("만기연장", "상환유예", "상환연장")):
        purpose = "repayment_relief"
    elif any(term in notice.title for term in ("변경", "수정", "개정")):
        purpose = "amendment"
    elif any(term in notice.title for term in ("접수", "신청", "모집", "융자사업", "지원 계획")):
        purpose = "application"
    summaries = {
        "repayment_relief": (
            "기존 대출의 만기나 상환 일정을 조정하는 안내입니다. "
            "신규 대출이나 빚 면제와는 다릅니다."
        ),
        "amendment": (
            "기존 지원사업의 조건을 바꾸는 공고입니다. 바뀐 항목과 적용일을 원문에서 확인하세요."
        ),
        "guidance": (
            "정책자금 이용과 관련된 안내입니다. 새로 신청하는 "
            "공고인지, 기존 이용자를 위한 내용인지 확인하세요."
        ),
    }
    return {
        "title": notice.title,
        "issuer": notice.issuer,
        "application_period": notice.application_period or "원 공고에서 확인하세요",
        "approved_amount": None,
        "loan_rate": None,
        "kind_labels": [KINDS[kind] for kind in notice.kinds],
        "purpose": purpose,
        "listing_label": {
            "open_list": "공식 접수중 목록에서 확인",
            "ended_list": "공식 마감 목록에서 확인",
            "unknown": "목록 접수 상태 미확인",
        }[notice.listing_state],
        "date_label": "기업마당 등록일"
        if notice.source_id == "bizinfo_finance"
        else "소진공 등록일",
        "plain_summary": (
            summaries[purpose]
            if purpose != "application"
            else (
                "정책기관의 지원대상 확인 뒤 은행에서 심사받는 대리대출 공고입니다."
                if kind == "agency"
                else "정책기관이 직접 심사하고 빌려주는 직접대출 공고입니다."
                if kind == "direct"
                else (
                    "이 공고는 사업에 필요한 자금이나 보증·비용 지원을 "
                    "안내합니다. 지원 방식을 원문에서 확인하세요."
                )
            )
        ),
        "journey": guide["journeys"].get(kind, []) if purpose == "application" else [],
        "confirmation_note": (
            "지원대상 확인서는 대출 승인이 아닙니다. 공고의 최대 "
            "한도도 내게 확정된 금액이 아닙니다."
        ),
        "unknown_terms_note": (
            "개인별 승인금액·실제 금리·보증여력은 이 화면에서 판단하지 않습니다. 자금별"
            " 원 공고와 보증기관·은행 심사로 확인하세요."
        ),
        "availability_note": (
            "수집 당시 공식 목록과 공고의 표시입니다. 예산 "
            "잔액이나 지금 신청 가능한지는 공식 접수 화면에서 다시"
            " 확인하세요."
        ),
    }


def _md(value: str) -> str:
    for symbol in ("\\", "[", "]", "*", "_", "#", "`"):
        value = value.replace(symbol, "\\" + symbol)
    return value


def render_notice(notice: FundingNotice) -> str:
    info = explain_notice(notice)
    lines = [
        f"# {_md(notice.title)}",
        "",
        info["plain_summary"],
        "",
        f"- 공고 기관: {_md(notice.issuer)}",
        (
            f"- {info['date_label']}: "
            f"{notice.published_on.isoformat() if notice.published_on else '원문에서 확인'}"
        ),
        f"- 수집 당시 상태: {info['listing_label']}",
        f"- 수집 시각: {notice.fetched_at.isoformat()}",
        f"- 신청기간 원문: {_md(info['application_period'])}",
        f"- 출처: [공식 공고]({notice.official_url})",
        "",
        "## 먼저 알아둘 점",
        "",
        info["confirmation_note"],
        "",
        info["unknown_terms_note"],
        "",
        info["availability_note"],
        "",
    ]
    if info["journey"]:
        lines += ["## 쉬운 진행 순서", ""]
        lines.extend(
            f"{index}. {step['title']}: {step['explanation']}"
            for index, step in enumerate(info["journey"], 1)
        )
    lines += [
        "",
        "## 공고에서 확인한 내용",
        "",
        _md(notice.evidence_text),
        "",
        "## 준비할 자료",
        "",
    ]
    lines.extend(f"- {item}" for item in learning_content()["checklist"])
    if notice.origin_url:
        lines += ["", f"원 발행기관 안내: [기관 원문]({notice.origin_url})"]
    if notice.application_url:
        lines += ["", f"신청 경로: [공식 신청 사이트]({notice.application_url})"]
    if notice.provenance == "fixture":
        lines.insert(2, "검증용 자료입니다. 실제 최신 접수 상태를 뜻하지 않습니다.")
    return "\n".join(lines) + "\n"


def render_guide() -> str:
    content = learning_content()
    rows = [
        "# 처음 배우는 소상공인 정책자금",
        "",
        "확인서는 대출 승인이 아닙니다. 최대 한도는 내게 확정된 돈이 아닙니다.",
        "",
    ]
    rows += ["## 대표님, 헷갈리는 부분부터 같이 볼까요?", ""]
    for answer in content["plain_answers"]:
        rows += [
            f"### {answer['question']}",
            "",
            answer["answer"],
            "",
            answer["example"],
            "",
            f"[관련 공식 안내]({answer['reference']})",
            "",
        ]
    for kind, title in (("agency", "대리대출"), ("direct", "직접대출"), ("grant", "지원금")):
        rows.extend([f"## {title} 진행 순서", ""])
        rows.extend(
            f"{i}. {step['title']} — {step['explanation']}"
            for i, step in enumerate(content["journeys"][kind], 1)
        )
        rows.append("")
    rows += ["## 쉬운 용어 사전", ""]
    for term in content["glossary"]:
        rows += [
            f"### {term['term']}",
            "",
            term["plain"],
            "",
            term["example"],
            "",
            f"[공식 설명 참고]({term['reference']})",
            "",
        ]
    rows += [
        "## 비교할 것",
        "",
        *[f"- {item}" for item in content["comparison"]],
        "",
        content["regional_note"],
        "",
        content["safety_note"],
        "",
    ]
    return "\n".join(rows)
