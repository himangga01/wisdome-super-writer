(() => {
  "use strict";

  const state = {
    kill: null,
    schedules: [],
    schedule: null,
    runs: [],
    run: null,
    corrections: [],
    correction: null,
    retention: null,
    retentionCursor: null,
    auditCursor: null,
  };

  const byId = (id) => document.getElementById(id);
  const clear = (element) => element.replaceChildren();
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const line = (label, value) => {
    const row = node("div", null, "operations-line");
    row.append(node("strong", label), node("span", value ?? "-"));
    return row;
  };
  const requestKey = (prefix) => `${prefix}:${crypto.randomUUID()}`;

  function showMessage(message, kind = "success") {
    const target = byId("operations-message");
    target.textContent = message;
    target.className = `console-message ${kind}`;
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": window.csrfToken,
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      let message = `요청 실패 (${response.status})`;
      try {
        const problem = await response.json();
        message = problem.title || problem.code || message;
      } catch (_error) {
        // 응답 원문은 화면에 그대로 노출하지 않는다.
      }
      throw new Error(message);
    }
    return response.json();
  }

  async function reauthenticate(actionScope) {
    const currentPassword = window.prompt("현재 비밀번호를 입력하세요.");
    if (!currentPassword) throw new Error("재인증이 취소되었습니다.");
    const proof = await api("/api/v1/auth/reauth", {
      method: "POST",
      body: JSON.stringify({currentPassword, actionScopes: [actionScope]}),
    });
    return proof.id;
  }

  function safeLink(label, value) {
    const href = window.WisdomeUrlSafety.safeHttpUrl(value);
    if (!href) return node("span", `${label}: 링크 없음`);
    const link = node("a", label);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  async function loadKillSwitch() {
    state.kill = await api("/api/v1/operations/kill-switch");
    const target = byId("kill-state");
    clear(target);
    target.append(
      line("상태", state.kill.enabled ? "외부 쓰기 차단" : "외부 쓰기 허용"),
      line("버전", state.kill.version),
      line("최근 사유", state.kill.reason || "기록 없음"),
      line("변경 시각", state.kill.changedAt || "초기 안전 상태"),
    );
    byId("kill-toggle").textContent = state.kill.enabled
      ? "재인증 후 외부 쓰기 허용"
      : "즉시 외부 쓰기 차단";
  }

  async function toggleKillSwitch() {
    const enabled = !state.kill.enabled;
    const reauthProofId = enabled ? null : await reauthenticate("kill_switch_disable");
    await api("/api/v1/operations/kill-switch", {
      method: "PUT",
      body: JSON.stringify({
        enabled,
        expectedVersion: state.kill.version,
        requestKey: requestKey("kill-switch"),
        reauthProofId,
        reason: byId("kill-reason").value.trim(),
      }),
    });
    await loadKillSwitch();
    showMessage("Kill Switch 상태를 변경했습니다.");
  }

  function parseUuidList(value) {
    return value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
  }

  function parseJsonArray(id) {
    const parsed = JSON.parse(byId(id).value || "[]");
    if (!Array.isArray(parsed)) throw new Error("refs는 JSON 배열이어야 합니다.");
    return parsed;
  }

  function resetScheduleForm() {
    state.schedule = null;
    byId("schedule-form").reset();
    byId("schedule-id").value = "";
    byId("schedule-version").value = "";
    byId("schedule-cron").value = "0 */2 * * *";
    byId("schedule-timezone").value = "Asia/Seoul";
    byId("schedule-window").value = "1440";
    byId("schedule-validations").value = "[]";
    byId("schedule-activations").value = "[]";
    byId("schedule-reason").value = "관리자 일정 변경";
    byId("schedule-disable").disabled = true;
  }

  function selectSchedule(row) {
    state.schedule = row;
    byId("schedule-id").value = row.id;
    byId("schedule-version").value = String(row.version);
    byId("schedule-name").value = row.name;
    byId("schedule-topic").value = row.topic;
    byId("schedule-cron").value = row.cronExpression;
    byId("schedule-timezone").value = row.timezone;
    byId("schedule-window").value = String(row.windowMinutes || 1440);
    byId("schedule-targets").value = row.targetIds.join("\n");
    byId("schedule-mode").value = row.approvalMode;
    byId("schedule-validations").value = JSON.stringify(row.autoPublishValidationRefs, null, 2);
    byId("schedule-activations").value = JSON.stringify(row.autoPublishActivationRefs, null, 2);
    byId("schedule-overlap").value = row.overlapPolicy;
    byId("schedule-enabled").checked = row.enabled;
    byId("schedule-disable").disabled = !row.enabled;
  }

  function renderSchedules() {
    const target = byId("schedule-list");
    clear(target);
    if (!state.schedules.length) target.append(node("p", "등록된 일정이 없습니다.", "muted"));
    state.schedules.forEach((row) => {
      const button = node("button", null, "list-item console-list-button");
      const text = node("span");
      text.append(node("strong", row.name), node("small", `${row.cronExpression} · ${row.timezone}`));
      button.append(text, node("span", `${row.enabled ? "활성" : "중지"} · v${row.version}`, "badge"));
      button.type = "button";
      button.addEventListener("click", () => selectSchedule(row));
      target.append(button);
    });
  }

  async function loadSchedules() {
    const page = await api("/api/v1/schedules");
    state.schedules = page.items;
    renderSchedules();
  }

  function scheduleBody() {
    return {
      name: byId("schedule-name").value.trim(),
      topic: byId("schedule-topic").value,
      cronExpression: byId("schedule-cron").value.trim(),
      timezone: byId("schedule-timezone").value.trim(),
      windowMinutes: Number(byId("schedule-window").value),
      targetIds: parseUuidList(byId("schedule-targets").value),
      approvalMode: byId("schedule-mode").value,
      autoPublishValidationRefs: parseJsonArray("schedule-validations"),
      autoPublishActivationRefs: parseJsonArray("schedule-activations"),
      overlapPolicy: byId("schedule-overlap").value,
      enabled: byId("schedule-enabled").checked,
      requestKey: requestKey("schedule"),
      reason: byId("schedule-reason").value.trim(),
    };
  }

  async function saveSchedule(event) {
    event.preventDefault();
    const body = scheduleBody();
    const existing = state.schedule;
    const url = existing ? `/api/v1/schedules/${existing.id}` : "/api/v1/schedules";
    const method = existing ? "PATCH" : "POST";
    if (existing) body.expectedVersion = existing.version;
    const row = await api(url, {method, body: JSON.stringify(body)});
    await loadSchedules();
    selectSchedule(row);
    showMessage(existing ? "일정을 수정했습니다." : "일정을 생성했습니다.");
  }

  async function disableSchedule() {
    if (!state.schedule) return;
    const row = await api(`/api/v1/schedules/${state.schedule.id}`, {
      method: "DELETE",
      body: JSON.stringify({
        expectedVersion: state.schedule.version,
        requestKey: requestKey("schedule-disable"),
        reason: byId("schedule-reason").value.trim(),
      }),
    });
    await loadSchedules();
    selectSchedule(row);
    showMessage("일정을 중지했습니다.");
  }

  function renderRuns() {
    const target = byId("run-list");
    clear(target);
    state.runs.forEach((row) => {
      const button = node("button", null, "list-item console-list-button");
      button.type = "button";
      button.append(node("strong", row.displayId), node("span", row.state, "badge"));
      button.addEventListener("click", () => loadRun(row.id));
      target.append(button);
    });
  }

  function renderRun(row) {
    const detail = byId("run-detail");
    clear(detail);
    detail.append(
      line("실행", row.displayId),
      line("상태", row.state),
      line("복구 상태", row.recovery.state),
      line("영향", JSON.stringify(row.terminalImpact || {})),
      line("오류", JSON.stringify(row.errorSummary || {})),
    );
    byId("run-stop").disabled = !row.recovery.canStop;
    const retries = byId("run-retry-list");
    clear(retries);
    row.recovery.allowedRetryScopes.forEach((scope) => {
      const key = Object.keys(scope)[0];
      const button = node("button", `${key}: ${scope[key]}`, "secondary-button");
      button.type = "button";
      button.addEventListener("click", () => retryRun(scope));
      retries.append(button);
    });
    if (!row.recovery.allowedRetryScopes.length) retries.append(node("p", "선택 재시도 가능한 terminal 단위가 없습니다.", "muted"));
  }

  async function loadRuns() {
    const page = await api("/api/v1/runs?limit=50");
    state.runs = page.items;
    renderRuns();
  }

  async function loadRun(id) {
    state.run = await api(`/api/v1/runs/${id}`);
    renderRun(state.run);
  }

  async function stopRun() {
    if (!state.run) return;
    const reauthProofId = await reauthenticate("run_stop");
    await api(`/api/v1/runs/${state.run.id}/stop`, {
      method: "POST",
      body: JSON.stringify({
        expectedState: state.run.state,
        requestKey: requestKey("run-stop"),
        reauthProofId,
        reason: byId("run-reason").value.trim(),
      }),
    });
    await loadRun(state.run.id);
    showMessage("실행 중지를 요청했습니다.");
  }

  async function retryRun(scope) {
    if (!state.run) return;
    const reauthProofId = await reauthenticate("bulk_retry");
    await api(`/api/v1/runs/${state.run.id}/retry`, {
      method: "POST",
      body: JSON.stringify({
        scope,
        requestKey: requestKey("run-retry"),
        reauthProofId,
        reason: byId("run-reason").value.trim(),
      }),
    });
    await loadRun(state.run.id);
    showMessage("선택 재시도 결정을 기록했습니다.");
  }

  function renderCorrections() {
    const target = byId("correction-list");
    clear(target);
    state.corrections.forEach((row) => {
      const button = node("button", null, "list-item console-list-button");
      button.type = "button";
      button.append(node("strong", `${row.kind} · ${row.articleId}`), node("span", row.state, "badge"));
      button.addEventListener("click", () => selectCorrection(row));
      target.append(button);
    });
    if (!state.corrections.length) target.append(node("p", "검토할 정정이 없습니다.", "muted"));
  }

  function selectCorrection(row) {
    state.correction = row;
    const target = byId("correction-detail");
    clear(target);
    target.append(
      line("상태", row.state),
      line("변경 요약", JSON.stringify(row.diffSummary || {})),
      line("영향 claim", (row.affectedClaimIds || []).join(", ") || "없음"),
      line("채널", (row.publications || []).map((item) => `${item.channel}:${item.state}`).join(", ") || "없음"),
      safeLink("현재 출처", row.source && row.source.url),
    );
    if (row.priorSource) target.append(safeLink("이전 출처", row.priorSource.url));
  }

  async function loadCorrections() {
    const page = await api("/api/v1/corrections?limit=50");
    state.corrections = page.items;
    renderCorrections();
  }

  async function decideCorrection(decision) {
    const row = state.correction;
    if (!row) return;
    const reauthProofId = await reauthenticate("correction_decision");
    const correctedRevisionId = decision === "verified"
      ? byId("correction-revision").value.trim()
      : null;
    const result = await api(`/api/v1/corrections/${row.id}/decisions`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        expectedSubjectHash: row.subjectHash,
        expectedDiffManifestHash: row.diffManifestHash,
        correctedRevisionId,
        expectedLatestDecisionId: row.latestDecisionId,
        expectedDecisionVersion: row.decisionVersion,
        requestKey: requestKey("correction-decision"),
        reauthProofId,
        reason: byId("correction-reason").value.trim(),
      }),
    });
    selectCorrection(result.correctionCase);
    await loadCorrections();
    showMessage("정정 검증 결정을 기록했습니다.");
  }

  function renderRetentionBatch(batch) {
    const target = byId("retention-summary");
    clear(target);
    target.append(
      line("batch", batch.id),
      line("상태", batch.state),
      line("버전", batch.version),
      line("preview hash", batch.previewHash),
      line("예상 항목", batch.expectedItemCount),
      line("예상 bytes", batch.expectedByteCount),
      line("진행", JSON.stringify(batch.counters || {})),
      line("복구", batch.remediation || "없음"),
    );
    byId("retention-execute").disabled = !["preview", "approved", "failed"].includes(batch.state);
  }

  function renderRetentionItems(items, append = false) {
    const target = byId("retention-items");
    if (!append) clear(target);
    items.forEach((item) => {
      const card = node("div", null, "list-item retention-item");
      const text = node("span");
      text.append(
        node("strong", `${item.entityType} · ${item.entityId}`),
        node("small", `${item.objectKeyRedacted || "DB row"} · ${item.holdReason || item.reasonCode || "ready"}`),
      );
      card.append(text, node("span", item.state, "badge"));
      target.append(card);
    });
  }

  async function loadRetentionItems(append = false) {
    if (!state.retention) return;
    const cursor = append && state.retentionCursor ? `?cursor=${encodeURIComponent(state.retentionCursor)}` : "";
    const page = await api(`/api/v1/retention/batches/${state.retention.id}/items${cursor}`);
    state.retentionCursor = page.nextCursor;
    renderRetentionItems(page.items, append);
    byId("retention-more").disabled = !page.nextCursor;
  }

  async function createRetentionPreview(event) {
    event.preventDefault();
    const localCutoff = new Date(byId("retention-cutoff").value);
    state.retention = await api("/api/v1/retention/previews", {
      method: "POST",
      body: JSON.stringify({
        scope: byId("retention-scope").value,
        cutoffAt: localCutoff.toISOString(),
        requestKey: requestKey("retention-preview"),
        reason: byId("retention-reason").value.trim(),
      }),
    });
    state.retentionCursor = null;
    renderRetentionBatch(state.retention);
    await loadRetentionItems(false);
    showMessage("보존 삭제 preview를 생성했습니다.");
  }

  async function executeRetention() {
    if (!state.retention) return;
    const reauthProofId = await reauthenticate("retention_execute");
    state.retention = await api(`/api/v1/retention/batches/${state.retention.id}/execute`, {
      method: "POST",
      body: JSON.stringify({
        expectedVersion: state.retention.version,
        previewHash: state.retention.previewHash,
        requestKey: requestKey("retention-execute"),
        reauthProofId,
        reason: byId("retention-reason").value.trim(),
      }),
    });
    renderRetentionBatch(state.retention);
    await loadRetentionItems(false);
    showMessage("보존 삭제 실행을 요청했습니다.");
  }

  function auditQuery(cursor = null) {
    const params = new URLSearchParams();
    const values = {
      actorType: byId("audit-actor-type").value,
      actorId: byId("audit-actor-id").value.trim(),
      correlationId: byId("audit-correlation").value.trim(),
      action: byId("audit-action").value.trim(),
    };
    Object.entries(values).forEach(([key, value]) => { if (value) params.set(key, value); });
    if (cursor) params.set("cursor", cursor);
    return params.toString();
  }

  function renderAuditEvents(items, append = false) {
    const target = byId("audit-list");
    if (!append) clear(target);
    items.forEach((event) => {
      const card = node("div", null, "list-item audit-item");
      const text = node("span");
      const result = event.metadataRedacted && event.metadataRedacted.result;
      text.append(
        node("strong", event.action),
        node("small", `${event.actorType}:${event.actorId || "-"} · ${event.entityType}:${event.entityId}`),
        node("small", `before ${event.beforeHash || "-"} → after ${event.afterHash || "-"}`),
        node("small", `사유 ${event.reasonCode || "-"} · 결과 ${result || "-"}`),
      );
      card.append(text, node("span", event.occurredAt, "badge"));
      target.append(card);
    });
  }

  async function loadAudit(append = false) {
    const query = auditQuery(append ? state.auditCursor : null);
    const page = await api(`/api/v1/audit-events?${query}`);
    state.auditCursor = page.nextCursor;
    renderAuditEvents(page.items, append);
    byId("audit-more").disabled = !page.nextCursor;
  }

  async function guard(action) {
    try {
      await action();
    } catch (error) {
      showMessage(error.message, "error");
    }
  }

  byId("kill-toggle").addEventListener("click", () => guard(toggleKillSwitch));
  byId("schedule-new").addEventListener("click", resetScheduleForm);
  byId("schedule-form").addEventListener("submit", (event) => guard(() => saveSchedule(event)));
  byId("schedule-disable").addEventListener("click", () => guard(disableSchedule));
  byId("run-stop").addEventListener("click", () => guard(stopRun));
  byId("correction-verify").addEventListener("click", () => guard(() => decideCorrection("verified")));
  byId("correction-reject").addEventListener("click", () => guard(() => decideCorrection("rejected")));
  byId("retention-form").addEventListener("submit", (event) => guard(() => createRetentionPreview(event)));
  byId("retention-execute").addEventListener("click", () => guard(executeRetention));
  byId("retention-more").addEventListener("click", () => guard(() => loadRetentionItems(true)));
  byId("audit-form").addEventListener("submit", (event) => {
    event.preventDefault();
    state.auditCursor = null;
    guard(() => loadAudit(false));
  });
  byId("audit-more").addEventListener("click", () => guard(() => loadAudit(true)));

  resetScheduleForm();
  const defaultCutoff = new Date(Date.now() - (90 * 24 * 60 * 60 * 1000));
  byId("retention-cutoff").value = defaultCutoff.toISOString().slice(0, 16);
  guard(async () => {
    await Promise.all([loadKillSwitch(), loadSchedules(), loadRuns(), loadCorrections(), loadAudit(false)]);
  });
})();
