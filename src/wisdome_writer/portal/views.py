"""Server-rendered topic inventories; collection remains an explicit operation."""

from django.http import HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import render

from apps.local_content.views import _page_response, _require_local_request
from apps.policy_funding.views import policy_page
from wisdome_writer.portal.periods import period_context, selection_from_query


def load_housing_snapshot(query="", period=None, stage="all"):
    from .housing import load_housing_snapshot as load

    return load(query=query, period=period, stage=stage)


def load_semiconductor_data(query="", period=None, inventory="auto"):
    from .semiconductor import load_semiconductor_data as load

    return load(query=query, period=period, inventory=inventory)


@policy_page
def home(request):
    return render(request, "portal/home.html")


def housing(request, section="overview"):
    _require_local_request(request)
    if request.method not in {"GET", "HEAD"}:
        return HttpResponseNotAllowed(["GET", "HEAD"])
    query = request.GET.get("q", "").strip()
    if len(query) > 200:
        return HttpResponseBadRequest("검색어는 200자 이내로 입력해 주세요.")
    try:
        period = selection_from_query(
            request.GET, default_basis="schedule", allowed_bases=("schedule",)
        )
        stage = request.GET.get("stage", "all")
        if stage not in {"all", "special", "first"} or len(request.GET.getlist("stage")) > 1:
            raise ValueError("청약 단계는 특별공급·1순위 중에서 선택해 주세요.")
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if section == "sources":
        context = {"section": section}
    else:
        data = (
            load_housing_snapshot(query=query, period=period, stage=stage)
            if period.active or stage != "all"
            else load_housing_snapshot(query=query)
        )
        rows = data["notices"]
        context = {"section": section, "data": data, "rows": rows, "q": query}
        context.update(
            period_selection=period,
            period_ui=period_context(
                request,
                period,
                basis_labels={
                    "schedule": "특별공급·1순위 접수 일정",
                },
                available_years=data.get("available_years", ()),
            ),
            selected_stage=stage,
        )
    response = _page_response(render(request, "portal/housing.html", context))
    response["Content-Security-Policy"] = response["Content-Security-Policy"].replace(
        "form-action 'none'", "form-action 'self'"
    )
    return response


@policy_page
def semiconductor(request, section="overview"):
    query = request.GET.get("q", "").strip()
    if len(query) > 200:
        return HttpResponseBadRequest("검색어는 200자 이내로 입력해 주세요.")
    try:
        period = selection_from_query(request.GET, allowed_bases=("published", "collected"))
        inventory = request.GET.get("inventory", "auto")
        if (
            inventory not in {"auto", "local", "database"}
            or len(request.GET.getlist("inventory")) > 1
        ):
            raise ValueError("자료원을 다시 선택해 주세요.")
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if inventory != "auto":
        data = load_semiconductor_data(query=query, period=period, inventory=inventory)
    else:
        data = (
            load_semiconductor_data(query=query, period=period)
            if period.active
            else load_semiconductor_data(query=query)
        )
    labels = {"published": "기사 발표일", "collected": "수집 날짜"}
    if section == "articles":
        labels = {"published": "초안 수정일", "collected": "초안 수정일"}
        data = {**data, "available": data.get("drafts_available", data["available"])}
        data["message"] = (
            "현재 저장된 반도체 콘텐츠 초안입니다."
            if data["available"]
            else "콘텐츠 초안 저장소를 불러올 수 없습니다."
        )
    source_names = {
        "motie": "산업통상부",
        "krx_kind": "KRX KIND",
        "samsung_global_newsroom": "Samsung Newsroom",
        "skhynix_newsroom": "SK hynix Newsroom",
        "semiconductor_industry_association": "Semiconductor Industry Association",
    }
    status_names = {
        "completed": "확인 완료",
        "partial": "일부 확인",
        "failed": "확인 실패",
        "unsupported": "수집 지원 준비 중",
        "disabled": "자동 수집 제외",
    }
    outcomes = []
    for item in data.get("source_outcomes", ()):
        waiting = item.get("error_code") == "source_poll_interval_reserved"
        outcomes.append(
            {
                "name": source_names.get(item["source_id"], item["source_id"]),
                "count": item["count"],
                "status": "다음 수집 대기"
                if waiting
                else status_names.get(item["status"], "확인 필요"),
            }
        )
    return render(
        request,
        "portal/semiconductor.html",
        {
            "section": section,
            "q": query,
            "data": data,
            "period_selection": period,
            "period_ui": period_context(request, period, basis_labels=labels),
            "source_outcome_display": outcomes,
            "selected_inventory": inventory,
        },
    )
