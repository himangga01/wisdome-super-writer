(() => {
  "use strict";

  const API_ROOT = "/api/v1";
  const FALLBACK_TOPICS = [
    {code: "housing_subscription", name: "주택 청약"},
    {code: "semiconductor_news", name: "반도체 뉴스"},
  ];
  const SECRET_KEY_PATTERN =
    /(^|[_-])(auth|authorization|cookie|credential|password|passwd|secret|token|key|api[_-]?key|private[_-]?key|service[_-]?key|subscription[_-]?key|sig|signature|x[_-]?auth)($|[_-])/i;
  const SECRET_VALUE_PATTERNS = [
    /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/i,
    /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/i,
  ];

  const state = {
    topics: [],
    topic: null,
    sources: [],
    registries: [],
    head: null,
    selectedSource: null,
    selectedRegistry: null,
  };

  const elements = {
    topicFilter: document.querySelector("#topic-filter"),
    sourceTopic: document.querySelector("#source-topic"),
    message: document.querySelector("#source-message"),
    sourceList: document.querySelector("#source-list"),
    registryList: document.querySelector("#registry-list"),
    registryHead: document.querySelector("#registry-head"),
    sourceForm: document.querySelector("#source-form"),
    sourceId: document.querySelector("#source-id"),
    expectedDraftId: document.querySelector("#expected-draft-id"),
    sourceEditorTitle: document.querySelector("#source-editor-title"),
    sourceSnapshotSummary: document.querySelector("#source-snapshot-summary"),
    sourceStatus: document.querySelector("#source-status"),
    sourceHealth: document.querySelector("#source-health"),
    checkSource: document.querySelector("#check-source"),
    registrySummary: document.querySelector("#registry-summary"),
    registryStatus: document.querySelector("#registry-status"),
    manifestMeta: document.querySelector("#manifest-meta"),
    membershipList: document.querySelector("#membership-list"),
    registryDecision: document.querySelector("#registry-decision"),
    decisionReason: document.querySelector("#decision-reason"),
    decisionPassword: document.querySelector("#decision-password"),
    decisionMfa: document.querySelector("#decision-mfa"),
    approveRegistry: document.querySelector("#approve-registry"),
    retireRegistry: document.querySelector("#retire-registry"),
  };

  class ApiError extends Error {
    constructor(status, payload) {
      const issueText = Array.isArray(payload?.issues)
        ? payload.issues.map((issue) => `${issue.path}: ${issue.code}`).join(", ")
        : "";
      super(
        payload?.detail ||
          payload?.title ||
          issueText ||
          `요청을 처리하지 못했습니다. (HTTP ${status})`,
      );
      this.status = status;
      this.payload = payload;
    }
  }

  async function api(path, options = {}) {
    const headers = {
      Accept: "application/json",
      "X-CSRFToken": window.csrfToken,
      ...(options.headers || {}),
    };
    if (options.body !== undefined && !(options.body instanceof FormData)) {
      headers["Content-Type"] =
        headers["Content-Type"] || "application/json";
    }
    const response = await fetch(`${API_ROOT}${path}`, {
      credentials: "same-origin",
      ...options,
      headers,
    });
    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (contentType.includes("json")) {
      payload = await response.json();
    } else {
      const text = await response.text();
      payload = text ? {detail: text} : null;
    }
    if (!response.ok) {
      throw new ApiError(response.status, payload);
    }
    return payload;
  }

  function asList(payload) {
    if (Array.isArray(payload)) return payload;
    if (Array.isArray(payload?.items)) return payload.items;
    if (Array.isArray(payload?.results)) return payload.results;
    return [];
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function setMessage(message, tone = "info") {
    elements.message.textContent = message;
    elements.message.className = `console-message ${tone}`;
  }

  function clearMessage() {
    elements.message.textContent = "";
    elements.message.className = "console-message hidden";
  }

  function statusOf(row) {
    return row?.status || row?.state || "unknown";
  }

  function statusLabel(status) {
    return (
      {
        draft: "초안",
        approved: "승인됨",
        retired: "폐기됨",
        passed: "정상",
        failed: "실패",
        pending: "대기",
      }[status] || status || "미확인"
    );
  }

  function topicLabel(code) {
    const topic = state.topics.find((row) => row.code === code);
    return topic?.name || topic?.title || code;
  }

  function fullHash(value) {
    return value || "없음";
  }

  function field(name) {
    return elements.sourceForm.elements.namedItem(name);
  }

  function setField(name, value) {
    const input = field(name);
    if (!input) return;
    if (input.type === "checkbox") {
      input.checked = Boolean(value);
    } else {
      input.value = value ?? "";
    }
  }

  function isSecretKey(key) {
    const normalized = String(key).replace(
      /([a-z0-9])([A-Z])/g,
      "$1_$2",
    );
    return SECRET_KEY_PATTERN.test(normalized);
  }

  function sanitizeForDisplay(value) {
    let redacted = 0;
    const walk = (current) => {
      if (Array.isArray(current)) return current.map(walk);
      if (current && typeof current === "object") {
        return Object.fromEntries(
          Object.entries(current).map(([key, child]) => {
            if (isSecretKey(key)) {
              redacted += 1;
              return [key, "[표시 차단]"];
            }
            return [key, walk(child)];
          }),
        );
      }
      if (
        typeof current === "string" &&
        SECRET_VALUE_PATTERNS.some((pattern) => pattern.test(current))
      ) {
        redacted += 1;
        return "[표시 차단]";
      }
      return current;
    };
    return {value: walk(value), redacted};
  }

  function assertNoSecretMaterial(value, path = "externalConfig") {
    if (Array.isArray(value)) {
      value.forEach((child, index) =>
        assertNoSecretMaterial(child, `${path}[${index}]`),
      );
      return;
    }
    if (value && typeof value === "object") {
      Object.entries(value).forEach(([key, child]) => {
        if (isSecretKey(key)) {
          throw new Error(
            `${path}.${key}에는 비밀값을 둘 수 없습니다. Secret Ref 포인터를 사용하세요.`,
          );
        }
        assertNoSecretMaterial(child, `${path}.${key}`);
      });
      return;
    }
    if (
      typeof value === "string" &&
      SECRET_VALUE_PATTERNS.some((pattern) => pattern.test(value))
    ) {
      throw new Error(
        `${path}에서 비밀값으로 보이는 문자열을 찾았습니다. Secret Ref 포인터를 사용하세요.`,
      );
    }
  }

  function parseExternalConfig() {
    const raw = field("externalConfig").value.trim();
    const parsed = raw ? JSON.parse(raw) : {};
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error("외부 설정은 JSON 객체여야 합니다.");
    }
    assertNoSecretMaterial(parsed);
    return parsed;
  }

  function renderHealth(health) {
    if (!health) {
      elements.sourceHealth.textContent = "아직 점검 기록이 없습니다.";
      return;
    }
    const sanitized = sanitizeForDisplay(health).value;
    const values = [
      ["상태", statusLabel(sanitized.status || sanitized.state)],
      ["점검 시각", sanitized.checkedAt || sanitized.checked_at],
      ["레코드 수", sanitized.recordCount ?? sanitized.record_count],
      ["오류 코드", sanitized.errorCode || sanitized.error_code],
    ].filter(([, value]) => value !== undefined && value !== null && value !== "");
    if (!values.length) {
      elements.sourceHealth.textContent = JSON.stringify(sanitized, null, 2);
      return;
    }
    elements.sourceHealth.innerHTML = values
      .map(
        ([label, value]) =>
          `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`,
      )
      .join("");
  }

  function topicOptions(selected) {
    return state.topics
      .map(
        (topic) =>
          `<option value="${escapeHtml(topic.code)}"${
            topic.code === selected ? " selected" : ""
          }>${escapeHtml(topic.name || topic.title || topic.code)}</option>`,
      )
      .join("");
  }

  async function loadTopics() {
    const payload = await api("/topics");
    const deduplicated = new Map();
    asList(payload).forEach((topic) => {
      if (!deduplicated.has(topic.code)) deduplicated.set(topic.code, topic);
    });
    state.topics = [...deduplicated.values()];
    if (!state.topics.length) state.topics = FALLBACK_TOPICS;
    if (!state.topics.some((topic) => topic.code === state.topic)) {
      state.topic = state.topics[0].code;
    }
    elements.topicFilter.innerHTML = topicOptions(state.topic);
    elements.sourceTopic.innerHTML = topicOptions(state.topic);
  }

  function renderSourceList() {
    const rows = [...state.sources].sort((left, right) =>
      String(left.name || left.displayName || "").localeCompare(
        String(right.name || right.displayName || ""),
        "ko",
      ),
    );
    elements.sourceList.innerHTML =
      rows
        .map((source) => {
          const active = state.selectedSource?.id === source.id ? " active" : "";
          return `
            <button type="button" class="list-item console-list-button${active}" data-source-id="${escapeHtml(source.id)}">
              <span>
                <strong>${escapeHtml(source.name || source.displayName || source.id)}</strong>
                <small>${escapeHtml(source.publisher || source.baseUrl || "")}</small>
              </span>
              <span class="item-badges">
                <span class="badge">${escapeHtml(statusLabel(statusOf(source)))}</span>
                <span class="badge ${source.enabled ? "" : "badge-muted"}">${source.enabled ? "사용" : "중지"}</span>
              </span>
            </button>`;
        })
        .join("") ||
      '<p class="muted">이 주제에 등록된 출처가 없습니다.</p>';
    elements.sourceList
      .querySelectorAll("[data-source-id]")
      .forEach((button) =>
        button.addEventListener("click", () =>
          selectSource(button.dataset.sourceId),
        ),
      );
  }

  function resetSourceEditor({keepSelection = false} = {}) {
    elements.sourceForm.reset();
    elements.sourceId.value = "";
    elements.expectedDraftId.value = "";
    elements.sourceTopic.disabled = false;
    elements.sourceTopic.innerHTML = topicOptions(state.topic);
    elements.sourceTopic.value = state.topic;
    setField("externalConfig", "{}");
    if (!keepSelection) state.selectedSource = null;
    elements.sourceEditorTitle.textContent = "새 출처 정의";
    elements.sourceSnapshotSummary.textContent =
      "저장하면 첫 번째 초안 스냅샷이 생성됩니다.";
    elements.sourceStatus.textContent = "신규";
    elements.checkSource.disabled = true;
    renderHealth(null);
    renderSourceList();
  }

  function populateSourceEditor(source) {
    state.selectedSource = source;
    elements.sourceId.value = source.id;
    elements.expectedDraftId.value = source.latestDraftSnapshotId || "";
    elements.sourceTopic.innerHTML = topicOptions(source.topic);
    elements.sourceTopic.value = source.topic;
    elements.sourceTopic.disabled = true;
    setField("name", source.name || source.displayName);
    setField("publisher", source.publisher);
    setField("authorityTier", source.authorityTier);
    setField("independenceGroupId", source.independenceGroupId);
    setField("ownerName", source.ownerName);
    setField("editorialControlName", source.editorialControlName);
    setField("baseUrl", source.baseUrl);
    setField("accessMethod", source.accessMethod);
    setField("adapterKey", source.adapterKey);
    const safeConfig = sanitizeForDisplay(source.externalConfig || {});
    setField("externalConfig", JSON.stringify(safeConfig.value, null, 2));
    setField("secretRef", source.secretRef);
    setField(
      "allowedMimeTypes",
      Array.isArray(source.allowedMimeTypes)
        ? source.allowedMimeTypes.join(", ")
        : "",
    );
    setField("defaultRightsStatus", source.defaultRightsStatus);
    setField("termsUrl", source.termsUrl);
    setField("robotsUrl", source.robotsUrl);
    setField("licenseUrl", source.licenseUrl);
    setField("pollIntervalSeconds", source.pollIntervalSeconds || 3600);
    setField(
      "maxConcurrency",
      source.rateLimitPolicy?.maxConcurrency ?? 1,
    );
    setField(
      "requestsPerMinute",
      source.rateLimitPolicy?.requestsPerMinute ?? 30,
    );
    setField("burst", source.rateLimitPolicy?.burst ?? 1);
    setField("enabled", source.enabled);
    elements.sourceEditorTitle.textContent =
      source.name || source.displayName || "출처 정의";
    elements.sourceSnapshotSummary.textContent = [
      `승인 버전 ${source.latestApprovedSnapshotVersion ?? "없음"}`,
      `초안 버전 ${source.latestDraftSnapshotVersion ?? "없음"}`,
      `초안 해시 ${source.latestDraftConfigHash ?? "없음"}`,
    ].join(" · ");
    elements.sourceStatus.textContent = statusLabel(statusOf(source));
    elements.checkSource.disabled = false;
    renderHealth(source.lastHealth);
    renderSourceList();
    if (safeConfig.redacted) {
      setMessage(
        "외부 설정에서 비밀값으로 보이는 항목을 가렸습니다. 해당 항목을 제거하고 Secret Ref 포인터로 교체하세요.",
        "warning",
      );
    }
  }

  async function selectSource(sourceId) {
    clearMessage();
    try {
      const source = await api(`/sources/${encodeURIComponent(sourceId)}`);
      populateSourceEditor(source);
    } catch (error) {
      setMessage(error.message, "error");
    }
  }

  function sourcePayload() {
    const mimeTypes = field("allowedMimeTypes")
      .value.split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    if (!mimeTypes.length) {
      throw new Error("허용 MIME 유형을 하나 이상 입력하세요.");
    }
    const nullableUrl = (name) => field(name).value.trim() || null;
    return {
      topic: elements.sourceTopic.value,
      name: field("name").value.trim(),
      publisher: field("publisher").value.trim(),
      authorityTier: field("authorityTier").value,
      independenceGroupId: field("independenceGroupId").value.trim(),
      ownerName: field("ownerName").value.trim(),
      editorialControlName: field("editorialControlName").value.trim(),
      baseUrl: field("baseUrl").value.trim(),
      accessMethod: field("accessMethod").value,
      adapterKey: field("adapterKey").value.trim(),
      externalConfig: parseExternalConfig(),
      secretRef: field("secretRef").value.trim() || null,
      allowedMimeTypes: mimeTypes,
      defaultRightsStatus: field("defaultRightsStatus").value,
      termsUrl: nullableUrl("termsUrl"),
      robotsUrl: nullableUrl("robotsUrl"),
      licenseUrl: nullableUrl("licenseUrl"),
      pollIntervalSeconds: Number(field("pollIntervalSeconds").value),
      rateLimitPolicy: {
        maxConcurrency: Number(field("maxConcurrency").value),
        requestsPerMinute: Number(field("requestsPerMinute").value),
        burst: Number(field("burst").value),
      },
      enabled: field("enabled").checked,
    };
  }

  async function saveSource(event) {
    event.preventDefault();
    clearMessage();
    const submitButton = elements.sourceForm.querySelector(
      'button[type="submit"]',
    );
    submitButton.disabled = true;
    try {
      const payload = sourcePayload();
      const sourceId = elements.sourceId.value;
      let source;
      if (sourceId) {
        delete payload.topic;
        payload.expectedLatestDraftSnapshotId =
          elements.expectedDraftId.value || null;
        payload.requestKey = crypto.randomUUID();
        source = await api(`/sources/${encodeURIComponent(sourceId)}`, {
          method: "PATCH",
          headers: {"Content-Type": "application/merge-patch+json"},
          body: JSON.stringify(payload),
        });
      } else {
        payload.requestKey = crypto.randomUUID();
        source = await api("/sources", {
          method: "POST",
          body: JSON.stringify(payload),
        });
      }
      state.selectedSource = source;
      setMessage("출처 초안 스냅샷을 저장했습니다.", "success");
      await loadTopicData({preserveMessage: true});
      await selectSource(source.id);
      setMessage("출처 초안 스냅샷을 저장했습니다.", "success");
    } catch (error) {
      setMessage(
        error instanceof SyntaxError
          ? "외부 설정 JSON 문법을 확인하세요."
          : error.message,
        "error",
      );
    } finally {
      submitButton.disabled = false;
    }
  }

  async function checkSelectedSource() {
    const sourceId = elements.sourceId.value;
    if (!sourceId) return;
    elements.checkSource.disabled = true;
    clearMessage();
    try {
      const job = await api(`/sources/${encodeURIComponent(sourceId)}/check`, {
        method: "POST",
      });
      setMessage(
        `접근·파싱 점검을 요청했습니다. 작업 ID: ${job.jobId}`,
        "success",
      );
    } catch (error) {
      setMessage(error.message, "error");
    } finally {
      elements.checkSource.disabled = false;
    }
  }

  function renderRegistryList() {
    const rows = [...state.registries].sort(
      (left, right) => Number(right.version) - Number(left.version),
    );
    elements.registryHead.innerHTML = state.head
      ? `현재 헤드: <strong>v${escapeHtml(state.head.version)}</strong> · <code>${escapeHtml(state.head.manifestHash)}</code>`
      : "현재 승인된 레지스트리 헤드가 없습니다.";
    elements.registryList.innerHTML =
      rows
        .map((registry) => {
          const active =
            state.selectedRegistry?.id === registry.id ? " active" : "";
          return `
            <button type="button" class="list-item console-list-button${active}" data-registry-id="${escapeHtml(registry.id)}">
              <span>
                <strong>버전 ${escapeHtml(registry.version)}</strong>
                <small>행 버전 ${escapeHtml(registry.rowVersion)} · ${escapeHtml(registry.createdAt || "")}</small>
              </span>
              <span class="badge">${escapeHtml(statusLabel(statusOf(registry)))}</span>
            </button>`;
        })
        .join("") ||
      '<p class="muted">이 주제의 레지스트리가 없습니다.</p>';
    elements.registryList
      .querySelectorAll("[data-registry-id]")
      .forEach((button) =>
        button.addEventListener("click", () =>
          selectRegistry(button.dataset.registryId),
        ),
      );
  }

  function snapshotOptions(source, membership) {
    const options = [];
    const currentId =
      membership?.sourceDefinitionSnapshotId || membership?.snapshotId;
    if (currentId) {
      options.push({
        id: currentId,
        label: `현재 v${membership.sourceDefinitionSnapshotVersion ?? "?"} (${statusLabel(membership.sourceDefinitionSnapshotStatus)})`,
      });
    }
    if (
      source?.latestDraftSnapshotId &&
      source.latestDraftSnapshotId !== currentId
    ) {
      options.push({
        id: source.latestDraftSnapshotId,
        label: `최신 초안 v${source.latestDraftSnapshotVersion ?? "?"}`,
      });
    }
    return options;
  }

  function membershipRow(source, membership, editable) {
    const sourceId = source?.id || membership?.sourceId;
    const sourceName =
      source?.name || source?.displayName || sourceId || membership?.key;
    const options = snapshotOptions(source, membership);
    const currentHash =
      membership?.sourceDefinitionConfigHash || "아직 멤버십 없음";
    if (!editable || !sourceId) {
      return `
        <div class="membership-row">
          <div>
            <strong>${escapeHtml(sourceName)}</strong>
            <small>스냅샷 ${escapeHtml(membership?.sourceDefinitionSnapshotId || membership?.snapshotId || "없음")}</small>
            <code>${escapeHtml(currentHash)}</code>
          </div>
          <div class="membership-state">
            <span class="badge">${membership?.enabled ? "사용" : "중지"}</span>
            <span>순서 ${escapeHtml(membership?.displayOrder ?? "-")}</span>
          </div>
        </div>`;
    }
    return `
      <div class="membership-row membership-edit" data-membership-source="${escapeHtml(sourceId)}">
        <div>
          <strong>${escapeHtml(sourceName)}</strong>
          <small>현재 설정 해시</small>
          <code>${escapeHtml(currentHash)}</code>
        </div>
        <label>스냅샷
          <select data-role="snapshot"${options.length ? "" : " disabled"}>
            ${
              options.length
                ? options
                    .map(
                      (option) =>
                        `<option value="${escapeHtml(option.id)}">${escapeHtml(option.label)}</option>`,
                    )
                    .join("")
                : '<option value="">사용 가능한 초안 없음</option>'
            }
          </select>
        </label>
        <label class="checkbox-label">
          <input data-role="enabled" type="checkbox"${membership?.enabled ? " checked" : ""}>
          사용
        </label>
        <label>표시 순서
          <input data-role="display-order" type="number" min="0" value="${escapeHtml(membership?.displayOrder ?? 0)}">
        </label>
        <button type="button" data-action="save-membership"${options.length ? "" : " disabled"}>
          ${membership ? "CAS 저장" : "멤버십 추가"}
        </button>
      </div>`;
  }

  function renderRegistryDetail() {
    const registry = state.selectedRegistry;
    if (!registry) {
      elements.registrySummary.textContent =
        "위 목록에서 레지스트리를 선택하세요.";
      elements.registryStatus.textContent = "선택 안 됨";
      elements.manifestMeta.classList.add("hidden");
      elements.membershipList.innerHTML = "";
      elements.registryDecision.classList.add("hidden");
      return;
    }
    const registryStatus = statusOf(registry);
    elements.registrySummary.textContent =
      `${topicLabel(registry.topic)} · 버전 ${registry.version} · 행 버전 ${registry.rowVersion}`;
    elements.registryStatus.textContent = statusLabel(registryStatus);
    elements.manifestMeta.classList.remove("hidden");
    elements.manifestMeta.innerHTML = `
      <div><span>레지스트리 ID</span><code>${escapeHtml(registry.id)}</code></div>
      <div><span>기준 승인 버전</span><strong>${escapeHtml(registry.baseApprovedVersion ?? "없음")}</strong></div>
      <div><span>기준 레지스트리 ID</span><code>${escapeHtml(registry.baseApprovedRegistryId || "없음")}</code></div>
      <div class="manifest-hash"><span>기준 승인 매니페스트 해시</span><code>${escapeHtml(fullHash(registry.baseApprovedManifestHash))}</code></div>
      <div class="manifest-hash"><span>매니페스트 해시</span><code>${escapeHtml(fullHash(registry.manifestHash))}</code></div>
    `;

    const memberships = Array.isArray(registry.memberships)
      ? registry.memberships
      : Array.isArray(registry.sources)
        ? registry.sources
        : [];
    const membershipBySource = new Map(
      memberships
        .filter((membership) => membership.sourceId)
        .map((membership) => [membership.sourceId, membership]),
    );
    const editable = registryStatus === "draft";
    let rows;
    if (editable) {
      rows = state.sources.map((source) =>
        membershipRow(source, membershipBySource.get(source.id), true),
      );
      const knownIds = new Set(state.sources.map((source) => source.id));
      rows.push(
        ...memberships
          .filter(
            (membership) =>
              !membership.sourceId || !knownIds.has(membership.sourceId),
          )
          .map((membership) => membershipRow(null, membership, false)),
      );
    } else {
      rows = memberships.map((membership) =>
        membershipRow(
          state.sources.find((source) => source.id === membership.sourceId),
          membership,
          false,
        ),
      );
    }
    elements.membershipList.innerHTML =
      rows.join("") ||
      '<p class="muted">이 매니페스트에 포함된 출처가 없습니다.</p>';
    elements.membershipList
      .querySelectorAll('[data-action="save-membership"]')
      .forEach((button) =>
        button.addEventListener("click", () => saveMembership(button)),
      );

    const canApprove = registryStatus === "draft";
    const canRetire =
      registryStatus === "approved" && state.head?.id === registry.id;
    elements.registryDecision.classList.toggle(
      "hidden",
      !canApprove && !canRetire,
    );
    elements.approveRegistry.hidden = !canApprove;
    elements.retireRegistry.hidden = !canRetire;
    renderRegistryList();
  }

  async function selectRegistry(registryId) {
    clearMessage();
    try {
      const registry = await api(
        `/source-registries/${encodeURIComponent(registryId)}`,
      );
      state.selectedRegistry = registry;
      renderRegistryDetail();
    } catch (error) {
      setMessage(error.message, "error");
    }
  }

  async function createRegistryDraft() {
    const button = document.querySelector("#create-registry");
    button.disabled = true;
    clearMessage();
    try {
      const payload = {
        topic: state.topic,
        baseRegistryId: state.head?.id || null,
        expectedLatestRegistryVersion: state.head?.version ?? null,
        expectedLatestRegistryManifestHash:
          state.head?.manifestHash || null,
        requestKey: crypto.randomUUID(),
      };
      const registry = await api("/source-registries", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.selectedRegistry = registry;
      setMessage(
        state.head
          ? "현재 승인 헤드의 전체 멤버십을 이어받은 초안을 만들었습니다."
          : "첫 레지스트리 초안을 만들었습니다.",
        "success",
      );
      await loadTopicData({preserveMessage: true});
      await selectRegistry(registry.id);
      setMessage(
        state.head
          ? "현재 승인 헤드의 전체 멤버십을 이어받은 초안을 만들었습니다."
          : "첫 레지스트리 초안을 만들었습니다.",
        "success",
      );
    } catch (error) {
      setMessage(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function saveMembership(button) {
    const row = button.closest("[data-membership-source]");
    const sourceId = row.dataset.membershipSource;
    const snapshotId = row.querySelector('[data-role="snapshot"]').value;
    if (!snapshotId || !state.selectedRegistry) return;
    button.disabled = true;
    clearMessage();
    try {
      const payload = {
        expectedRowVersion: state.selectedRegistry.rowVersion,
        expectedManifestHash: state.selectedRegistry.manifestHash,
        sourceDefinitionSnapshotId: snapshotId,
        enabled: row.querySelector('[data-role="enabled"]').checked,
        displayOrder: Number(
          row.querySelector('[data-role="display-order"]').value,
        ),
        requestKey: crypto.randomUUID(),
      };
      const registry = await api(
        `/source-registries/${encodeURIComponent(state.selectedRegistry.id)}/memberships/${encodeURIComponent(sourceId)}`,
        {
          method: "PUT",
          body: JSON.stringify(payload),
        },
      );
      state.selectedRegistry = registry;
      const listIndex = state.registries.findIndex(
        (candidate) => candidate.id === registry.id,
      );
      if (listIndex >= 0) state.registries[listIndex] = registry;
      setMessage(
        "멤버십을 CAS로 저장하고 전체 매니페스트 해시를 갱신했습니다.",
        "success",
      );
      renderRegistryDetail();
    } catch (error) {
      setMessage(
        error.status === 409
          ? "레지스트리가 다른 요청으로 변경되었습니다. 새로고침한 뒤 다시 시도하세요."
          : error.message,
        "error",
      );
      button.disabled = false;
    }
  }

  async function decideRegistry(decision) {
    const registry = state.selectedRegistry;
    if (!registry) return;
    const reason = elements.decisionReason.value.trim();
    let currentPassword = elements.decisionPassword.value;
    let mfaCode = elements.decisionMfa.value.trim() || null;
    elements.decisionPassword.value = "";
    elements.decisionMfa.value = "";
    if (reason.length < 3) {
      currentPassword = "";
      setMessage("결정 사유를 3자 이상 입력하세요.", "error");
      return;
    }
    if (!currentPassword) {
      setMessage("재인증을 위해 현재 비밀번호를 입력하세요.", "error");
      return;
    }
    const actionText = decision === "approved" ? "승인" : "폐기";
    const warning =
      decision === "approved"
        ? `버전 ${registry.version}의 정확한 매니페스트를 승인하시겠습니까?`
        : "현재 헤드를 폐기하면 승인 헤드가 없어집니다. 계속하시겠습니까?";
    if (!window.confirm(warning)) {
      currentPassword = "";
      mfaCode = null;
      return;
    }
    const button =
      decision === "approved"
        ? elements.approveRegistry
        : elements.retireRegistry;
    button.disabled = true;
    clearMessage();
    try {
      const proof = await api("/auth/reauth", {
        method: "POST",
        body: JSON.stringify({
          currentPassword,
          mfaCode,
          actionScopes: ["registry_decision"],
        }),
      });
      currentPassword = "";
      mfaCode = null;
      const head = state.head;
      await api(
        `/source-registries/${encodeURIComponent(registry.id)}/decisions`,
        {
          method: "POST",
          body: JSON.stringify({
            decision,
            expectedRowVersion: registry.rowVersion,
            expectedManifestHash: registry.manifestHash,
            expectedCurrentHeadRegistryId: head?.id || null,
            expectedCurrentHeadVersion: head?.version ?? null,
            expectedCurrentHeadManifestHash: head?.manifestHash || null,
            expectedLatestDecisionId: registry.latestDecisionId || null,
            requestKey: crypto.randomUUID(),
            reauthProofId: proof.id,
            reason,
          }),
        },
      );
      elements.decisionReason.value = "";
      setMessage(`레지스트리 ${actionText} 결정을 기록했습니다.`, "success");
      await loadTopicData({preserveMessage: true});
      const refreshed = state.registries.find(
        (candidate) => candidate.id === registry.id,
      );
      if (refreshed) await selectRegistry(refreshed.id);
      setMessage(`레지스트리 ${actionText} 결정을 기록했습니다.`, "success");
    } catch (error) {
      setMessage(error.message, "error");
    } finally {
      currentPassword = "";
      mfaCode = null;
      elements.decisionPassword.value = "";
      elements.decisionMfa.value = "";
      button.disabled = false;
    }
  }

  async function loadTopicData({preserveMessage = false} = {}) {
    if (!preserveMessage) clearMessage();
    const selectedSourceId = state.selectedSource?.id;
    const selectedRegistryId = state.selectedRegistry?.id;
    const [sourcePayloadValue, registryPayloadValue] = await Promise.all([
      api(`/sources?topic=${encodeURIComponent(state.topic)}`),
      api(`/source-registries?topic=${encodeURIComponent(state.topic)}`),
    ]);
    state.sources = asList(sourcePayloadValue);
    state.registries = asList(registryPayloadValue);
    state.head =
      [...state.registries]
        .filter(
          (registry) =>
            statusOf(registry) === "approved" &&
            Boolean(registry.latestDecisionId),
        )
        .sort(
          (left, right) => Number(right.version) - Number(left.version),
        )[0] || null;

    const selectedSource = state.sources.find(
      (source) => source.id === selectedSourceId,
    );
    if (selectedSource) {
      populateSourceEditor(selectedSource);
    } else if (selectedSourceId) {
      resetSourceEditor();
    } else {
      renderSourceList();
    }

    const registryCandidate =
      state.registries.find(
        (registry) => registry.id === selectedRegistryId,
      ) ||
      [...state.registries]
        .sort(
          (left, right) => Number(right.version) - Number(left.version),
        )
        .find((registry) => statusOf(registry) === "draft") ||
      state.head ||
      [...state.registries].sort(
        (left, right) => Number(right.version) - Number(left.version),
      )[0];
    state.selectedRegistry = registryCandidate || null;
    renderRegistryList();
    if (registryCandidate) {
      await selectRegistry(registryCandidate.id);
    } else {
      renderRegistryDetail();
    }
  }

  async function initialize() {
    try {
      await loadTopics();
      resetSourceEditor();
      await loadTopicData();
    } catch (error) {
      setMessage(error.message, "error");
    }
  }

  elements.topicFilter.addEventListener("change", async (event) => {
    state.topic = event.target.value;
    state.selectedSource = null;
    state.selectedRegistry = null;
    elements.sourceTopic.innerHTML = topicOptions(state.topic);
    resetSourceEditor();
    try {
      await loadTopicData();
    } catch (error) {
      setMessage(error.message, "error");
    }
  });
  document
    .querySelector("#reload-all")
    .addEventListener("click", async () => {
      try {
        await loadTopicData();
      } catch (error) {
        setMessage(error.message, "error");
      }
    });
  document
    .querySelector("#new-source")
    .addEventListener("click", () => resetSourceEditor());
  document
    .querySelector("#create-registry")
    .addEventListener("click", createRegistryDraft);
  elements.sourceForm.addEventListener("submit", saveSource);
  elements.checkSource.addEventListener("click", checkSelectedSource);
  elements.approveRegistry.addEventListener("click", () =>
    decideRegistry("approved"),
  );
  elements.retireRegistry.addEventListener("click", () =>
    decideRegistry("retired"),
  );

  initialize();
})();
