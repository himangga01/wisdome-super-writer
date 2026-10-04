const fs = require("node:fs");
const vm = require("node:vm");
const crypto = require("node:crypto");

class Element {
  constructor() {
    this.children = [];
    this.listeners = {};
    this.value = "";
    this.checked = false;
    this.disabled = false;
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  reset() {}
}

async function main() {
  const elements = new Map();
  const byId = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const schedule = {
    id: "11111111-1111-4111-8111-111111111111", version: 4,
    name: "Official check", topic: "housing_subscription", cronExpression: "0 */2 * * *",
    timezone: "Asia/Seoul", windowMinutes: 1440,
    targetIds: ["33333333-3333-4333-8333-333333333333"],
    approvalMode: "manual", autoPublishValidationRefs: [], autoPublishActivationRefs: [],
    overlapPolicy: "skip", enabled: true,
  };
  const calls = [];
  let killEnabled = true;
  const prompts = ["fixture-password", "654321"];
  const context = {
    document: { getElementById: byId, createElement: () => new Element() },
    window: { csrfToken: "fixture-csrf", prompt: () => prompts.shift(),
      WisdomeUrlSafety: { safeHttpUrl: (value) => value } },
    crypto, URLSearchParams, Date, console,
    fetch: async (url, options) => {
      const method = options.method || "GET";
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({url, method, body});
      let payload;
      if (url === "/api/v1/operations/kill-switch") {
        if (method === "PUT") killEnabled = body.enabled;
        payload = {enabled: killEnabled, version: 1, reason: "", changedAt: null};
      } else if (url === "/api/v1/schedules" && method === "GET") {
        payload = {items: [schedule], nextCursor: null};
      } else if (url.startsWith("/api/v1/schedules") && method !== "GET") {
        payload = {...schedule, ...body, version: 5};
      } else if (url === "/api/v1/auth/reauth") {
        payload = {id: "44444444-4444-4444-8444-444444444444"};
      } else {
        payload = {items: [], nextCursor: null};
      }
      return {ok: true, status: 200, json: async () => payload};
    },
  };
  vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
  await new Promise((resolve) => setImmediate(resolve));
  byId("schedule-list").children[0].listeners.click();
  const topicDisabledDuringEdit = byId("schedule-topic").disabled;
  await byId("schedule-form").listeners.submit({preventDefault() {}});
  byId("schedule-new").listeners.click();
  const topicDisabledDuringCreate = byId("schedule-topic").disabled;
  byId("schedule-name").value = "New official check";
  byId("schedule-topic").value = "housing_subscription";
  byId("schedule-targets").value = schedule.targetIds[0];
  byId("schedule-mode").value = "manual";
  byId("schedule-overlap").value = "skip";
  await byId("schedule-form").listeners.submit({preventDefault() {}});
  byId("kill-reason").value = "Resume approved work";
  await byId("kill-toggle").listeners.click();
  const reauth = calls.find((call) => call.url === "/api/v1/auth/reauth");
  console.log(JSON.stringify({
    patch: calls.find((call) => call.method === "PATCH").body,
    create: calls.find((call) => call.url === "/api/v1/schedules" && call.method === "POST").body,
    topicDisabledDuringEdit, topicDisabledDuringCreate,
    reauthScopes: reauth.body.actionScopes,
    mfaProvided: reauth.body.mfaCode === "654321",
  }));
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
