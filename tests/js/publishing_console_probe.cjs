const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.dataset = {articleId: "article-fixture"};
  }
  get firstChild() { return this.children[0]; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  addEventListener(event, callback) { this.listeners[event] = callback; }
}

async function main() {
  const elements = new Map();
  const byId = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const safeHttpUrl = (value) => {
    if (typeof value !== "string") return null;
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  };
  const context = {
    document: {getElementById: byId, createElement: (tag) => new Element(tag)},
    window: {csrfToken: "fixture-csrf", WisdomeUrlSafety: {safeHttpUrl}},
    URLSearchParams,
    fetch: async (url) => {
      let payload;
      if (url === "/api/v1/articles/article-fixture") {
        payload = {claims: [], visualPlacements: [], evidenceSnapshots: [
          {sourceTitle: "Public evidence", sourceUrl: "https://example.com/notice",
            locator: {json_pointer: "/source_record/body_text"}},
          {sourceTitle: "Unsafe source", sourceUrl: "javascript:alert(1)",
            locator: {json_pointer: "/source_record/body_text"}},
        ]};
      } else if (url === "/api/v1/targets") {
        payload = [];
      } else if (url.endsWith("/publication-intents")) {
        payload = {item: null};
      } else {
        payload = {items: [], nextCursor: null};
      }
      return {ok: true, json: async () => payload};
    },
  };
  vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
  await new Promise((resolve) => setImmediate(resolve));
  const anchors = [];
  const walk = (element) => {
    if (element.tag === "a") anchors.push({href: element.href, rel: element.rel});
    for (const child of element.children) walk(child);
  };
  walk(byId("editorial-material"));
  console.log(JSON.stringify({anchors, error: byId("intent-message").textContent || ""}));
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
