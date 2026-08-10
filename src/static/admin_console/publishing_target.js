(() => {
  "use strict";

  const root = document.getElementById("publishing-target");
  if (!root) return;

  const targetId = root.dataset.targetId;
  const channel = root.dataset.channel;
  const environment = root.dataset.environment;
  const csrfToken = window.csrfToken;
  const safeHttpUrl = window.WisdomeUrlSafety.safeHttpUrl;
  let target = null;
  let validations = [];

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

  function setState() {
    if (!target) return;
    document.getElementById("connection-state").textContent = target.connectionState;
    document.getElementById("preflight-state").textContent = target.preflightState;
    document.getElementById("canary-state").textContent = target.canaryState;
    document.getElementById("pilot-state").textContent = target.pilotState;
    document.getElementById("auto-publish-state").textContent = target.autoPublishEnabled ? "ON" : "OFF";
    document.getElementById("snapshot-state").textContent = `v${target.currentSnapshotVersion} · ${(target.currentConfigHash || "").slice(0, 12)}`;
  }

  function validationRef(row) {
    return {
      validationId: row.id,
      targetId,
      targetSnapshotId: row.targetSnapshotId,
      materialHash: row.materialHash,
    };
  }

  function currentValidationRefs() {
    return validations
      .filter((row) => row.status === "passed" && row.targetSnapshotId === target.currentSnapshotId)
      .map(validationRef);
  }

  async function decideValidation(row, decision) {
    const reason = window.prompt(`${decision === "passed" ? "통과" : "철회"} 결정 사유를 입력하세요.`);
    if (!reason || !reason.trim()) return;
    const proof = await issueProof("validation_decision");
    await api(`/targets/${targetId}/auto-publish-validations/${row.id}/decisions`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        expectedLatestDecisionId: row.latestDecisionId,
        requestKey: crypto.randomUUID(),
        reauthProofId: proof.id,
        reason: reason.trim(),
      }),
    });
    await loadAll();
  }

  async function showReport(row, container) {
    const report = await api(`/targets/${targetId}/auto-publish-validations/${row.id}/report`);
    const details = element("div", null, "muted");
    details.appendChild(element("strong", `보고서: ${report.overallResult}`));
    const list = element("ul");
    for (const stage of report.stageResults || []) {
      list.appendChild(element("li", `${stage.code}: ${stage.result}`));
    }
    details.appendChild(list);
    container.appendChild(details);
  }

  function renderValidations() {
    const list = document.getElementById("validation-list");
    clear(list);
    if (!validations.length) {
      list.appendChild(element("p", "아직 검증 material이 없습니다.", "muted"));
      return;
    }
    for (const row of validations) {
      const item = element("article", null, "list-item");
      const summary = element("div");
      summary.appendChild(element("strong", row.topic));
      summary.appendChild(element("div", `${row.materialVersion} · ${(row.materialHash || "").slice(0, 16)}`, "muted"));
      summary.appendChild(element("span", row.status, "badge"));
      const actions = element("div");
      const report = element("button", "보고서", null);
      report.type = "button";
      report.addEventListener("click", () => showReport(row, summary).catch(showValidationError));
      actions.appendChild(report);
      if (row.status === "draft" || row.status === "revoked") {
        const pass = element("button", "통과 결정");
        pass.type = "button";
        pass.addEventListener("click", () => decideValidation(row, "passed").catch(showValidationError));
        actions.appendChild(pass);
      }
      if (row.status === "passed") {
        const revoke = element("button", "검증 철회");
        revoke.type = "button";
        revoke.addEventListener("click", () => decideValidation(row, "revoked").catch(showValidationError));
        actions.appendChild(revoke);
      }
      item.append(summary, actions);
      list.appendChild(item);
    }
  }

  function showValidationError(error) {
    document.getElementById("validation-message").textContent = error.message;
  }

  async function loadAll() {
    [target, validations] = await Promise.all([
      api(`/targets/${targetId}`),
      api(`/targets/${targetId}/auto-publish-validations`),
    ]);
    setState();
    renderValidations();
  }

  document.getElementById("preflight").addEventListener("click", async () => {
    const reason = window.prompt("읽기 전용 preflight 실행 사유를 입력하세요.");
    if (!reason || !reason.trim()) return;
    try {
      const job = await api(`/targets/${targetId}/preflight`, {
        method: "POST",
        body: JSON.stringify({ requestKey: crypto.randomUUID(), reason: reason.trim() }),
      });
      document.getElementById("gate-message").textContent = `preflight ${job.jobId} 접수`;
    } catch (error) {
      document.getElementById("gate-message").textContent = error.message;
    }
  });

  const canaryButton = document.getElementById("canary");
  if (canaryButton) canaryButton.addEventListener("click", async () => {
    const reason = window.prompt("격리 쓰기 canary 실행 사유를 입력하세요.", "공식 API 연결 검증");
    if (!reason || !reason.trim()) return;
    try {
      const job = await api(`/targets/${targetId}/canary`, {
        method: "POST",
        body: JSON.stringify({
          confirmIsolatedTestTarget: true,
          requestKey: crypto.randomUUID(),
          reason: reason.trim(),
        }),
      });
      document.getElementById("gate-message").textContent = `canary ${job.jobId} 접수`;
    } catch (error) {
      document.getElementById("gate-message").textContent = error.message;
    }
  });

  const oauthButton = document.getElementById("oauth-connect");
  if (oauthButton) oauthButton.addEventListener("click", async () => {
    const reason = window.prompt("Google OAuth 연결 사유를 입력하세요.");
    if (!reason || !reason.trim()) return;
    try {
      const result = await api(`/targets/${targetId}/oauth/start`, {
        method: "POST",
        body: JSON.stringify({ requestKey: crypto.randomUUID(), reason: reason.trim() }),
      });
      const authorizationUrl = safeHttpUrl(result.authorizationUrl);
      if (!authorizationUrl) throw new Error("안전하지 않은 OAuth URL이 거부되었습니다.");
      window.location.assign(authorizationUrl);
    } catch (error) {
      document.getElementById("gate-message").textContent = error.message;
    }
  });

  document.getElementById("disconnect").addEventListener("click", async () => {
    const reason = window.prompt("자격 증명 연결 해제 사유를 입력하세요.");
    if (!reason || !reason.trim()) return;
    try {
      const proof = await issueProof("credential_disconnect");
      const job = await api(`/targets/${targetId}/connection`, {
        method: "DELETE",
        body: JSON.stringify({
          expectedTargetSnapshotId: target.currentSnapshotId,
          expectedTargetConfigHash: target.currentConfigHash,
          requestKey: crypto.randomUUID(),
          reauthProofId: proof.id,
          reason: reason.trim(),
        }),
      });
      document.getElementById("gate-message").textContent = `연결 해제 ${job.jobId} 접수`;
      await loadAll();
    } catch (error) {
      document.getElementById("gate-message").textContent = error.message;
    }
  });

  document.getElementById("validation-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    try {
      await api(`/targets/${targetId}/auto-publish-validations`, {
        method: "POST",
        body: JSON.stringify({
          topic: form.get("topic"),
          requestKey: crypto.randomUUID(),
          reason: String(form.get("reason") || "").trim(),
        }),
      });
      document.getElementById("validation-message").textContent = "서버 material 검증 후보를 생성했습니다.";
      await loadAll();
    } catch (error) {
      showValidationError(error);
    }
  });

  document.getElementById("activation-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const enabled = form.get("enabled") === "true";
    const refs = enabled ? currentValidationRefs() : [];
    if (enabled && !refs.length) {
      document.getElementById("activation-message").textContent = "현재 snapshot의 passed validation이 필요합니다.";
      return;
    }
    try {
      const proof = await issueProof("auto_publish_change");
      const result = await api(`/targets/${targetId}/auto-publish`, {
        method: "PUT",
        body: JSON.stringify({
          enabled,
          validationRefs: refs,
          expectedLatestActivationId: target.autoPublishActivationId,
          requestKey: crypto.randomUUID(),
          reauthProofId: proof.id,
          reason: String(form.get("reason") || "").trim(),
        }),
      });
      target = result.target;
      setState();
      document.getElementById("activation-message").textContent = `activation v${result.activation.version}: ${result.activation.decision}`;
    } catch (error) {
      document.getElementById("activation-message").textContent = error.message;
    }
  });

  if (environment !== "test" && channel === "blogger" && !document.getElementById("oauth-connect")) {
    document.getElementById("gate-message").textContent = "Blogger OAuth 연결 버튼을 사용할 수 없습니다.";
  }
  loadAll().catch((error) => {
    document.getElementById("gate-message").textContent = error.message;
  });
})();
