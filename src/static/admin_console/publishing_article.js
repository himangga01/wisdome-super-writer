(() => {
  "use strict";

  const root = document.getElementById("publishing-article");
  if (!root) return;

  const articleId = root.dataset.articleId;
  const csrfToken = window.csrfToken;
  const safeHttpUrl = window.WisdomeUrlSafety.safeHttpUrl;
  let article = null;
  let targets = [];
  let intent = null;
  let previews = new Map();
  let approvalRows = [];
  let approvalCursor = null;
  let publicationRows = [];

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  async function api(path, options = {}) {
    const response = await fetch(`/api/v1${path}`, {
      credentials: "same-origin",
      ...options,
      headers: {
        "X-CSRFToken": csrfToken,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || body.title || "요청이 거부되었습니다.");
    return body;
  }

  async function issueProof(scope) {
    const currentPassword = window.prompt("현재 관리자 비밀번호를 다시 입력하세요.");
    if (!currentPassword) throw new Error("재인증이 취소되었습니다.");
    const mfaCode = window.prompt("MFA 코드가 필요한 경우 입력하세요. 아니면 비워 두세요.");
    return api("/auth/reauth", {
      method: "POST",
      body: JSON.stringify({
        currentPassword,
        mfaCode: mfaCode || null,
        actionScopes: [scope],
      }),
    });
  }

  function usableTargets() {
    return targets.filter((row) =>
      row.currentSnapshotId &&
      row.currentConfigHash &&
      row.connectionState === "verified"
    );
  }

  function renderEditorialMaterial() {
    const container = document.getElementById("editorial-material");
    clear(container);
    const eligibility = article.runtimeEligibility || {};
    const eligibilityItem = element("div", null, "list-item");
    eligibilityItem.appendChild(element(
      "strong",
      eligibility.publishEligible ? "발행 자격 통과" : "발행 차단",
    ));
    eligibilityItem.appendChild(element(
      "span",
      (eligibility.blockingCodes || []).join(", ") || "blocking code 없음",
      "badge",
    ));
    container.appendChild(eligibilityItem);

    for (const claim of article.claims || []) {
      const item = element("div", null, "list-item");
      const summary = element("div");
      summary.appendChild(element("strong", `${claim.type}: ${claim.statement}`));
      const relations = (claim.evidenceLinks || [])
        .map((link) => `${link.relation}:${link.evidenceId}`)
        .join(", ");
      summary.appendChild(element("div", relations || "evidence link 없음", "muted"));
      item.append(summary, element("span", "CLAIM", "badge"));
      container.appendChild(item);
    }
    for (const evidence of article.evidenceSnapshots || []) {
      const item = element("div", null, "list-item");
      const summary = element("div");
      summary.appendChild(element("strong", evidence.sourceTitle || evidence.evidenceId));
      summary.appendChild(element(
        "div",
        `${evidence.publisher || "publisher 미상"} · rights ${evidence.rightsStatus || "unknown"}`,
        "muted",
      ));
      const locator = safeHttpUrl(evidence.locator);
      if (locator) {
        const anchor = element("a", "근거 원문 열기");
        anchor.href = locator;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        summary.appendChild(anchor);
      }
      item.append(summary, element("span", "EVIDENCE", "badge"));
      container.appendChild(item);
    }
    for (const visual of article.visualPlacements || []) {
      const item = element("div", null, "list-item");
      item.appendChild(element("strong", visual.altText || visual.id || "visual"));
      item.appendChild(element("span", `VISUAL · ${visual.rightsStatus || "unknown"}`, "badge"));
      container.appendChild(item);
    }
  }

  function renderTargetSelector() {
    const container = document.getElementById("target-selector");
    clear(container);
    const rows = usableTargets();
    if (!rows.length) {
      container.appendChild(element("p", "preflight를 통과한 발행 대상이 없습니다.", "muted"));
      return;
    }
    for (const row of rows) {
      const label = element("label", null, "list-item");
      const summary = element("span");
      summary.appendChild(element("strong", row.displayName));
      summary.appendChild(element("small", `${row.channel} · ${row.environment} · snapshot v${row.currentSnapshotVersion}`));
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "target";
      input.value = row.id;
      input.style.minWidth = "auto";
      label.append(summary, input);
      container.appendChild(label);
    }
  }

  function currentApproval(targetId) {
    return approvalRows.find((row) => row.targetId === targetId && row.isCurrent) || null;
  }

  function commandFor(targetId) {
    return (intent.targetCommands || []).find((row) => row.targetId === targetId);
  }

  function approvalSubject(targetId) {
    const preview = previews.get(targetId);
    const command = commandFor(targetId);
    if (!preview || !command) throw new Error("현재 preview/target command를 찾을 수 없습니다.");
    return {
      kind: "content_preview",
      action: command.resolvedAction,
      renderId: preview.renderId,
      targetId,
      targetSnapshotId: command.targetSnapshotId,
      targetConfigHash: command.targetConfigHash,
      templateHash: preview.templateHash,
      sourceManifestHash: preview.sourceManifestHash,
    };
  }

  async function decideTarget(targetId, decision) {
    const current = currentApproval(targetId);
    const decisionReason = window.prompt(`${decision} 결정 사유를 입력하세요.`);
    if (!decisionReason || !decisionReason.trim()) return;
    let reauthProofId = null;
    if (decision === "revoked") {
      reauthProofId = (await issueProof("approval_revoke")).id;
    }
    await api(`/articles/${articleId}/approvals`, {
      method: "POST",
      body: JSON.stringify({
        revisionNo: intent.revisionNo,
        publicationIntentId: intent.id,
        expectedLatestApprovalId: current ? current.currentHead.latestApprovalId : null,
        expectedHeadVersion: current ? current.currentHead.version : 0,
        requestKey: crypto.randomUUID(),
        reauthProofId,
        actionSubject: approvalSubject(targetId),
        decision,
        decisionReason: decisionReason.trim(),
      }),
    });
    await loadIntentMaterial();
  }

  function decisionButton(label, targetId, decision) {
    const button = element("button", label);
    button.type = "button";
    button.addEventListener("click", () => decideTarget(targetId, decision).catch(showApprovalError));
    return button;
  }

  function showApprovalError(error) {
    document.getElementById("approval-message").textContent = error.message;
  }

  function renderApprovalActions(container, targetId) {
    const current = currentApproval(targetId);
    const decision = current ? current.currentHead.decision : null;
    if (!decision || decision === "rejected" || decision === "revoked") {
      container.appendChild(decisionButton("승인", targetId, "approved"));
    }
    if (!decision) {
      container.appendChild(decisionButton("거절", targetId, "rejected"));
    }
    if (decision === "approved") {
      container.appendChild(decisionButton("승인 철회", targetId, "revoked"));
    }
  }

  function renderPreviews() {
    const container = document.getElementById("preview-list");
    clear(container);
    if (!intent) return;
    for (const ref of intent.targetSnapshots || []) {
      const preview = previews.get(ref.targetId);
      if (!preview) continue;
      const card = element("article", null, "card");
      const target = targets.find((row) => row.id === ref.targetId);
      card.appendChild(element("span", target ? target.channel : ref.targetId, "badge"));
      card.appendChild(element("span", preview.canonicalLinkState, "badge"));
      card.appendChild(element("h3", preview.title));
      const frame = document.createElement("iframe");
      frame.setAttribute("sandbox", "");
      frame.title = `${preview.title} 미리보기`;
      frame.srcdoc = preview.sanitizedHtml;
      card.appendChild(frame);
      const material = element("p", `template ${preview.templateHash.slice(0, 12)} · source ${preview.sourceManifestHash.slice(0, 12)}`, "muted");
      card.appendChild(material);
      const links = element("ul");
      for (const rawUrl of preview.sourceLinks || []) {
        const safeUrl = safeHttpUrl(rawUrl);
        if (!safeUrl) continue;
        const item = element("li");
        const anchor = element("a", "출처 열기");
        anchor.href = safeUrl;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        item.appendChild(anchor);
        links.appendChild(item);
      }
      card.appendChild(links);
      const actions = element("div");
      renderApprovalActions(actions, ref.targetId);
      card.appendChild(actions);
      container.appendChild(card);
    }
  }

  function renderApprovalHistory() {
    const container = document.getElementById("approval-history");
    clear(container);
    for (const row of approvalRows) {
      const item = element("div", null, "list-item");
      const summary = element("div");
      summary.appendChild(element("strong", `${row.decision} · target ${row.targetId}`));
      summary.appendChild(element("div", `head v${row.currentHead.version} · ${row.decisionReason}`, "muted"));
      item.append(summary, element("span", row.isCurrent ? "CURRENT" : "HISTORY", "badge"));
      container.appendChild(item);
    }
    document.getElementById("more-approvals").hidden = !approvalCursor;
  }

  function renderIntent() {
    const hasIntent = Boolean(intent);
    document.getElementById("intent-summary").hidden = !hasIntent;
    document.getElementById("preview-panel").hidden = !hasIntent;
    document.getElementById("approval-history-panel").hidden = !hasIntent;
    document.getElementById("dispatch-panel").hidden = !hasIntent;
    if (!hasIntent) return;
    document.getElementById("intent-builder").hidden = !["stale", "cancelled"].includes(intent.state);
    document.getElementById("intent-state").textContent = `${intent.state} · ${intent.approvalMode} · revision ${intent.revisionNo}`;
    document.getElementById("intent-material").textContent = `intent ${intent.id} · ${intent.intentHash.slice(0, 16)} · snapshots ${intent.targetSnapshotManifestHash.slice(0, 16)}`;
    const everyApproved = (intent.targetSnapshots || []).every((ref) => {
      const current = currentApproval(ref.targetId);
      return current && current.currentHead.decision === "approved" && current.dispatchEligible;
    });
    document.getElementById("publish-button").disabled = intent.state !== "approved" || !everyApproved;
  }

  async function loadApprovals(cursor = null, append = false) {
    if (!intent) {
      approvalRows = [];
      approvalCursor = null;
      return;
    }
    const query = new URLSearchParams({ publicationIntentId: intent.id, limit: "50" });
    if (cursor) query.set("cursor", cursor);
    const page = await api(`/articles/${articleId}/approvals?${query.toString()}`);
    approvalRows = append ? approvalRows.concat(page.items) : page.items;
    approvalCursor = page.nextCursor;
  }

  async function loadIntentMaterial() {
    intent = (await api(`/articles/${articleId}/publication-intents`)).item;
    previews = new Map();
    if (intent) {
      const pairs = await Promise.all((intent.targetSnapshots || []).map(async (ref) => [
        ref.targetId,
        await api(`/articles/${articleId}/preview?targetId=${encodeURIComponent(ref.targetId)}`),
      ]));
      previews = new Map(pairs);
    }
    await loadApprovals();
    renderIntent();
    renderPreviews();
    renderApprovalHistory();
  }

  async function loadPublications() {
    const page = await api(`/articles/${articleId}/publications?limit=50`);
    publicationRows = page.items;
    const enriched = await Promise.all(publicationRows.map(async (row) => ({
      ...row,
      attempts: (await api(`/publications/${row.id}/attempts?limit=50`)).items,
    })));
    const container = document.getElementById("publication-list");
    clear(container);
    if (!enriched.length) {
      container.appendChild(element("p", "발행 시도가 없습니다.", "muted"));
      return;
    }
    for (const row of enriched) {
      const item = element("div", null, "list-item");
      const summary = element("div");
      summary.appendChild(element("strong", row.channel));
      const remoteUrl = safeHttpUrl(row.remoteUrl);
      if (remoteUrl) {
        const anchor = element("a", "게시물 열기");
        anchor.href = remoteUrl;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        summary.appendChild(anchor);
      } else {
        summary.appendChild(element("div", "공개 URL 대기", "muted"));
      }
      if (row.errorCode) summary.appendChild(element("div", row.errorCode, "muted"));
      const actions = element("div");
      actions.appendChild(element("span", row.state, "badge"));
      const recoverable = row.attempts.find((attempt) =>
        ["retryable_failed", "unknown_outcome", "reconciling"].includes(attempt.state)
      );
      if (recoverable) {
        const retry = element("button", recoverable.state === "retryable_failed" ? "안전 재시도" : "원격 reconcile");
        retry.type = "button";
        retry.addEventListener("click", () => retryAttempt(recoverable.id).catch(showPublishError));
        actions.appendChild(retry);
      }
      item.append(summary, actions);
      container.appendChild(item);
    }
  }

  function showPublishError(error) {
    document.getElementById("publish-message").textContent = error.message;
  }

  async function retryAttempt(attemptId) {
    const reason = window.prompt("발행 복구 요청 사유를 입력하세요.");
    if (!reason || !reason.trim()) return;
    const proof = await issueProof("bulk_retry");
    const result = await api(`/publication-attempts/${attemptId}/retry`, {
      method: "POST",
      body: JSON.stringify({
        requestKey: crypto.randomUUID(),
        reauthProofId: proof.id,
        reason: reason.trim(),
      }),
    });
    document.getElementById("publish-message").textContent = `${result.action}: ${result.state}`;
    await loadPublications();
  }

  document.getElementById("intent-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const selected = [...event.target.querySelectorAll('input[name="target"]:checked')]
      .map((input) => targets.find((row) => row.id === input.value));
    if (!selected.length) {
      document.getElementById("intent-message").textContent = "발행 대상을 하나 이상 선택하세요.";
      return;
    }
    const wordpress = selected.find((row) => row.channel === "wordpress");
    if (selected.some((row) => row.channel === "blogger") && !wordpress) {
      document.getElementById("intent-message").textContent = "Blogger와 함께 primary WordPress 대상을 선택하세요.";
      return;
    }
    const reason = window.prompt("발행 의도 생성 사유를 입력하세요.");
    if (!reason || !reason.trim()) return;
    const targetSnapshots = selected.map((row) => ({
      targetId: row.id,
      targetSnapshotId: row.currentSnapshotId,
      targetConfigHash: row.currentConfigHash,
    }));
    const targetCommands = selected.map((row) => ({
      targetId: row.id,
      targetSnapshotId: row.currentSnapshotId,
      targetConfigHash: row.currentConfigHash,
      resolvedAction: publicationRows.some((item) => item.targetId === row.id && item.remotePostId) ? "update" : "create",
      canonicalDependencyTargetId: row.channel === "blogger" ? wordpress.id : null,
    }));
    try {
      intent = await api(`/articles/${articleId}/publication-intents`, {
        method: "POST",
        body: JSON.stringify({
          revisionNo: article.revisionNo,
          expectedRevisionContentHash: article.revisionContentHash,
          correctionCaseId: null,
          targetSnapshots,
          targetCommands,
          approvalMode: "manual",
          autoPublishValidationRefs: [],
          autoPublishActivationRefs: [],
          expectedLatestIntentId: intent ? intent.id : null,
          requestKey: crypto.randomUUID(),
          reason: reason.trim(),
        }),
      });
      await loadIntentMaterial();
    } catch (error) {
      document.getElementById("intent-message").textContent = error.message;
    }
  });

  document.getElementById("publish-button").addEventListener("click", async () => {
    const reason = window.prompt("승인된 채널 발행 사유를 입력하세요.");
    if (!reason || !reason.trim()) return;
    try {
      const result = await api(`/articles/${articleId}/publish`, {
        method: "POST",
        body: JSON.stringify({
          revisionNo: intent.revisionNo,
          publicationIntentId: intent.id,
          targetIds: intent.targetSnapshots.map((row) => row.targetId),
          expectedTargetSnapshots: intent.targetSnapshots,
          publishAt: null,
          requestKey: crypto.randomUUID(),
          reason: reason.trim(),
        }),
      });
      document.getElementById("publish-message").textContent = `dispatch ${result.publicationIntentId} 접수 완료`;
      await loadPublications();
    } catch (error) {
      showPublishError(error);
    }
  });

  document.getElementById("more-approvals").addEventListener("click", async () => {
    if (!approvalCursor) return;
    await loadApprovals(approvalCursor, true);
    renderApprovalHistory();
  });
  document.getElementById("refresh-status").addEventListener("click", () => loadPublications().catch(showPublishError));

  Promise.all([
    api(`/articles/${articleId}`),
    api("/targets"),
  ]).then(async ([articleResult, targetRows]) => {
    article = articleResult;
    targets = targetRows;
    renderEditorialMaterial();
    renderTargetSelector();
    await loadIntentMaterial();
    await loadPublications();
  }).catch((error) => {
    document.getElementById("intent-message").textContent = error.message;
  });
})();
