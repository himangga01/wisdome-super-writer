(() => {
  "use strict";

  function safeHttpUrl(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const parsed = new URL(value.trim());
      return parsed.protocol === "http:" || parsed.protocol === "https:"
        ? parsed.href
        : null;
    } catch (_error) {
      return null;
    }
  }

  window.WisdomeUrlSafety = Object.freeze({ safeHttpUrl });
})();
