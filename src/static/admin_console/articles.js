(function () {
  "use strict";

  const CLAIM_TYPE_LABELS = {
    fact: "사실",
    company_claim: "기업 주장",
    interpretation: "해석",
    outlook: "전망",
  };
  const RELATION_LABELS = {
    supports: "지지",
    contradicts: "반박",
    context: "맥락",
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function appendLabelValue(parent, label, value) {
    const row = element("div");
    row.append(element("strong", null, label), element("span", null, value ?? "-"));
    parent.append(row);
  }

  function badge(value, muted) {
    return element("span", muted ? "badge badge-muted" : "badge", value || "unknown");
  }

  function safeExternalLink(label, value) {
    if (!value) return null;
    let parsed;
    try {
      parsed = new URL(value);
    } catch (_error) {
      return null;
    }
    if (!['http:', 'https:'].includes(parsed.protocol)) return null;
    const link = element("a", "button-link", label);
    link.href = parsed.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  function detailsNode(value) {
    const node = element("pre", "markdown");
    node.textContent = JSON.stringify(value || {}, null, 2);
    return node;
  }

  function titledSection(title) {
    const section = element("section", "health-card");
    section.append(element("h3", null, title));
    return section;
  }

  function canPublish(row) {
    return Boolean(
      row.revalidationState === "passed"
      && row.qualityState === "passed"
      && row.runtimeEligibility
      && row.runtimeEligibility.publishEligible === true
      && row.runtimeEligibility.policyCurrent === true
      && row.runtimeEligibility.evidenceCurrent === true
      && (row.runtimeEligibility.blockingCodes || []).length === 0
    );
  }

  function renderClaimEvidence(parent, link, snapshot) {
    const card = element("article", "list-item");
    const content = element("div");
    content.append(
      element("strong", null, RELATION_LABELS[link.relation] || link.relation),
      element("p", null, link.sourceSpan || "인용 구간 없음")
    );
    appendLabelValue(content, "검증 강도", link.verificationStrength);
    appendLabelValue(content, "독립 출처 그룹", link.independenceGroup);
    if (snapshot) {
      appendLabelValue(content, "발행자", snapshot.publisher);
      appendLabelValue(content, "권리", snapshot.rightsStatus);
      appendLabelValue(content, "게시 시각", snapshot.publishedAt);
      appendLabelValue(content, "수집 시각", snapshot.retrievedAt);
      appendLabelValue(content, "freshness 기준", snapshot.freshnessCutoff);
      appendLabelValue(content, "평가 시 발행 적격", snapshot.evaluatedPublishEligible);
      appendLabelValue(content, "locator", JSON.stringify(snapshot.locator || {}));
      const sourceLink = safeExternalLink("원문 열기", snapshot.sourceUrl);
      if (sourceLink) content.append(sourceLink);
    } else {
      content.append(element("p", "console-message error", "고정 근거 snapshot 없음"));
    }
    card.append(content, badge(link.relation, link.relation === "context"));
    parent.append(card);
  }

  function renderArticle(container, row) {
    container.classList.remove("hidden");
    const nodes = [];
    const header = element("header", "section-head");
    const heading = element("div");
    heading.append(
      element("p", "eyebrow", "ARTICLE DETAIL"),
      element("h2", null, row.title || "제목 없음"),
      element("p", null, row.revision && row.revision.summary || "요약 없음")
    );
    const states = element("div", "item-badges");
    states.append(
      badge(`재검증: ${row.revalidationState || "unknown"}`),
      badge(`품질: ${row.qualityState || "unknown"}`)
    );
    header.append(heading, states);
    nodes.push(header);

    const runtime = titledSection("현재 발행 적격성");
    appendLabelValue(runtime, "정책 최신", row.runtimeEligibility && row.runtimeEligibility.policyCurrent);
    appendLabelValue(runtime, "근거 최신", row.runtimeEligibility && row.runtimeEligibility.evidenceCurrent);
    appendLabelValue(runtime, "발행 가능", row.runtimeEligibility && row.runtimeEligibility.publishEligible);
    appendLabelValue(
      runtime,
      "차단 코드",
      (row.runtimeEligibility && row.runtimeEligibility.blockingCodes || []).join(", ") || "없음"
    );
    appendLabelValue(runtime, "판정 시각", row.runtimeEligibility && row.runtimeEligibility.evaluatedAt);
    if (canPublish(row)) {
      const publish = element("a", "button-link publish-link", "미리보기 · 승인 · 발행");
      publish.href = `/console/publishing/articles/${encodeURIComponent(row.id)}/`;
      runtime.append(publish);
    }
    nodes.push(runtime);

    const body = titledSection("본문 블록");
    for (const block of row.revision && row.revision.bodyBlocks || []) {
      const card = element("article", "list-item");
      const content = element("div");
      content.append(
        element("strong", null, `${block.type} · ${block.id}`),
        element("p", "markdown", block.content)
      );
      card.append(content);
      body.append(card);
    }
    nodes.push(body);

    const snapshotsById = new Map((row.evidenceSnapshots || []).map(item => [item.evidenceId, item]));
    const claims = titledSection("주장과 근거 연결");
    for (const claim of row.claims || []) {
      const card = element("article", "health-card");
      const claimHead = element("div", "section-head");
      const title = element("div");
      title.append(
        element("h4", null, claim.statement),
        element("p", "muted", `블록 ${claim.blockId}`)
      );
      const claimStates = element("div", "item-badges");
      claimStates.append(
        badge(CLAIM_TYPE_LABELS[claim.type] || claim.type),
        badge(`위험: ${claim.riskLevel}`, claim.riskLevel !== "high"),
        badge(`검증: ${claim.verificationState}`, claim.verificationState === "verified")
      );
      claimHead.append(title, claimStates);
      card.append(claimHead);
      const semantics = element("div", "claim-semantics");
      appendLabelValue(semantics, "주체", claim.actor);
      appendLabelValue(semantics, "귀속·발표 출처", claim.attribution);
      appendLabelValue(semantics, "전망 기간", claim.horizon);
      appendLabelValue(semantics, "불확실성", claim.uncertaintyNote);
      appendLabelValue(
        semantics,
        "파생 입력 주장",
        (claim.derivedFromClaimIds || []).join(", ") || "없음"
      );
      card.append(semantics);
      for (const link of claim.evidenceLinks || []) {
        renderClaimEvidence(
          card,
          link,
          snapshotsById.get(link.evidenceId)
        );
      }
      claims.append(card);
    }
    nodes.push(claims);

    const exclusions = titledSection("제외 · 중복 · 상충 자료");
    for (const item of row.excludedMaterials || []) {
      const card = element("article", "list-item");
      const content = element("div");
      content.append(
        element("strong", null, item.sourceTitle || "제목 없음"),
        element("p", null, item.reason || "사유 없음")
      );
      appendLabelValue(content, "발행자", item.publisher);
      appendLabelValue(content, "권리", item.rightsStatus);
      const sourceLink = safeExternalLink("원문 열기", item.sourceUrl);
      if (sourceLink) content.append(sourceLink);
      card.append(content, badge(item.classification));
      exclusions.append(card);
    }
    if (!(row.excludedMaterials || []).length) exclusions.append(element("p", "muted", "제외 자료 없음"));
    nodes.push(exclusions);

    const checks = titledSection("품질 검사 상세");
    for (const check of row.qualityChecks || []) {
      const card = element("article", "health-card");
      const head = element("div", "section-head");
      head.append(
        element("strong", null, check.code),
        badge(`${check.result}${check.blocking ? " · blocking" : ""}`, check.result === "passed")
      );
      card.append(head, detailsNode(check.details));
      checks.append(card);
    }
    nodes.push(checks);

    const visuals = titledSection("시각 자료 배치");
    for (const visual of row.visualPlacements || []) {
      const card = element("article", "list-item");
      const content = element("div");
      content.append(
        element("strong", null, visual.caption || "캡션 없음"),
        element("p", null, visual.altText || "대체 텍스트 없음")
      );
      appendLabelValue(content, "권리", visual.rightsStatus);
      appendLabelValue(content, "locator", JSON.stringify(visual.locator || {}));
      card.append(content);
      visuals.append(card);
    }
    if (!(row.visualPlacements || []).length) visuals.append(element("p", "muted", "배치된 시각 자료 없음"));
    nodes.push(visuals);

    container.replaceChildren(...nodes);
  }

  async function api(url) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "X-CSRFToken": window.csrfToken || "" },
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  async function showArticle(id) {
    const row = await api(`/api/v1/articles/${encodeURIComponent(id)}`);
    const detail = document.querySelector("#article-detail");
    if (detail) renderArticle(detail, row);
  }

  function renderList(container, items) {
    const nodes = [];
    for (const row of items) {
      const button = element("button", "list-item console-list-button");
      button.dataset.id = row.id;
      const content = element("span");
      content.append(
        element("strong", null, row.title || "제목 없음"),
        element("small", null, `${row.topic} · revision ${row.currentRevisionNo || "-"}`)
      );
      button.append(content, badge(row.state));
      button.addEventListener("click", () => showArticle(row.id));
      nodes.push(button);
    }
    if (!nodes.length) nodes.push(element("p", "muted", "초안이 없습니다."));
    container.replaceChildren(...nodes);
  }

  async function load() {
    const list = document.querySelector("#article-list");
    if (!list) return;
    try {
      const data = await api("/api/v1/articles");
      renderList(list, data.items || []);
    } catch (error) {
      list.replaceChildren(element("p", "console-message error", error.message));
    }
  }

  window.WisdomeArticleAdmin = { canPublish, renderArticle, renderList };
  if (!window.WISDOME_DISABLE_ARTICLE_AUTOLOAD) load();
}());
