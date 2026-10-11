"""Read-only funding pages; source collection is an explicit CLI operation."""

import ipaddress
from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import render

from apps.policy_funding.catalog import REGIONS, load_catalog
from apps.policy_funding.consultant_case import reviewed_consultant_case
from apps.policy_funding.contracts import KINDS
from apps.policy_funding.education import learning_content
from apps.policy_funding.rendering import explain_notice
from apps.policy_funding.storage import latest_notices, list_runs, period_notices, read_run
from apps.policy_funding.verification import verify_provided_case
from wisdome_writer.portal.periods import period_context, selection_from_query


def policy_page(function):
    @wraps(function)
    def wrapped(request, *args, **kwargs):
        if request.method not in {"GET", "HEAD"}:
            return HttpResponseNotAllowed(["GET", "HEAD"])
        if settings.IS_LOCAL_RUNTIME:
            try:
                allowed = ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")).is_loopback
            except ValueError:
                allowed = False
        else:
            allowed = (
                request.user.is_authenticated and request.user.is_active and request.user.is_staff
            )
        if not allowed:
            raise PermissionDenied("Policy pages require local loopback or authorized staff")
        response = function(request, *args, **kwargs)
        response["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'none'; style-src 'self'; img-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Referrer-Policy"] = "no-referrer"
        return response

    return wrapped


def _base():
    return {"regions": REGIONS, "kinds": KINDS}


def _list_query(request, mode):
    parameters = {"mode": mode}
    parameters.update(
        (key, request.GET[key])
        for key in ("year", "month", "basis", "q", "region", "kind")
        if key in request.GET
    )
    return urlencode(parameters)


@policy_page
def index(request):
    mode = request.GET.get("mode", "live")
    region, kind = request.GET.get("region", ""), request.GET.get("kind", "")
    query = request.GET.get("q", "").strip()
    if (
        mode not in {"live", "fixture"}
        or region
        and region not in REGIONS
        or kind
        and kind not in KINDS
        or len(query) > 200
    ):
        return HttpResponseBadRequest("검색 조건을 확인해 주세요.")
    try:
        period = selection_from_query(request.GET, allowed_bases=("published",))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    try:
        runs = list_runs(settings.POLICY_FUNDING_ROOT)
        period_data = period_notices(settings.POLICY_FUNDING_ROOT, period, mode=mode)
        rows = period_data["notices"]
    except (ValueError, OSError):
        raise Http404("검증할 수 없는 정책자금 저장 경로입니다.") from None
    filtered = [
        row
        for row in rows
        if (not region or region in row.regions or "national" in row.regions)
        and (not kind or kind in row.kinds)
        and (
            not query
            or query.casefold()
            in (row.title + " " + row.issuer + " " + row.evidence_text).casefold()
        )
    ]
    filtered.sort(
        key=lambda row: (row.published_on.isoformat() if row.published_on else "", row.title),
        reverse=True,
    )
    cards = [
        {
            "notice": row,
            "run_name": period_data["notice_runs"][row.stable_id],
            "explanation": explain_notice(row),
            "region_labels": [
                REGIONS.get(key, "지역 확인 필요" if key == "unspecified" else "전국 기관")
                for key in row.regions
            ],
        }
        for row in filtered[:100]
    ]
    return render(
        request,
        "policy_funding/index.html",
        {
            **_base(),
            "notices": cards,
            "total": len(filtered),
            "runs": runs[:5],
            "mode": mode,
            "list_query": _list_query(request, mode),
            "q": query,
            "selected_region": region,
            "selected_kind": kind,
            "connected_source_count": sum(source.enabled for source in load_catalog()),
            "glossary_count": len(learning_content()["glossary"]),
            "period_selection": period,
            "period_ui": period_context(
                request,
                period,
                basis_labels={
                    "published": "공고 게시·등록일",
                },
                available_years=period_data["available_years"],
            ),
            "unknown_date_count": period_data["unknown_date_count"],
            "coverage_run_count": period_data["coverage_run_count"],
        },
    )


@policy_page
def guide(request):
    return render(
        request,
        "policy_funding/guide.html",
        {
            **_base(),
            "guide": learning_content(),
            "case": verify_provided_case(latest_notices(settings.POLICY_FUNDING_ROOT)),
            "consultant_case": reviewed_consultant_case(),
        },
    )


@policy_page
def sources(request):
    return render(request, "policy_funding/sources.html", {**_base(), "sources": load_catalog()})


@policy_page
def glossary(request):
    return render(request, "policy_funding/glossary.html", {"guide": learning_content()})


@policy_page
def history(request):
    try:
        period = selection_from_query(
            request.GET, default_basis="collected", allowed_bases=("collected",)
        )
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    try:
        runs = [
            run
            for run in list_runs(settings.POLICY_FUNDING_ROOT, limit=100)
            if period.matches(run["captured_at"])
        ][:30]
    except (ValueError, OSError):
        raise Http404("검증할 수 없는 정책자금 저장 경로입니다.") from None
    return render(
        request,
        "policy_funding/history.html",
        {
            "runs": runs,
            "period_selection": period,
            "period_ui": period_context(request, period, basis_labels={"collected": "수집 날짜"}),
        },
    )


@policy_page
def detail(request, notice_id):
    mode = request.GET.get("mode", "live")
    if mode not in {"live", "fixture"}:
        return HttpResponseBadRequest("실행 유형을 확인해 주세요.")
    selected_run = request.GET.get("run")
    try:
        if selected_run:
            snapshot = read_run(settings.POLICY_FUNDING_ROOT, selected_run)
            if snapshot["mode"] != mode:
                raise ValueError("Policy run mode differs")
            rows = snapshot["notices"]
        else:
            rows = latest_notices(settings.POLICY_FUNDING_ROOT, mode=mode)
    except (ValueError, OSError):
        raise Http404 from None
    row = next((item for item in rows if item.stable_id == notice_id), None)
    if row is None:
        raise Http404
    return render(
        request,
        "policy_funding/detail.html",
        {
            **_base(),
            "notice": row,
            "explanation": explain_notice(row),
            "guide": learning_content(),
            "mode": mode,
            "list_url": "/policy-funding/?" + _list_query(request, mode),
        },
    )


@policy_page
def run_detail(request, run_name):
    try:
        run = read_run(settings.POLICY_FUNDING_ROOT, run_name)
    except (ValueError, OSError):
        raise Http404 from None
    return render(request, "policy_funding/run.html", {**_base(), "run": run})
