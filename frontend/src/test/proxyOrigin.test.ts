import { describe, expect, it } from "vitest";

import { resolveProxyRequestOrigin } from "../dev/proxyOrigin";

describe("resolveProxyRequestOrigin", () => {
  it("maps a legitimate Vite origin to the API port on the same host", () => {
    expect(resolveProxyRequestOrigin({
      browserOrigin: "http://192.168.0.5:5175",
      requestHost: "192.168.0.5:5175",
      apiTarget: "http://127.0.0.1:8770",
    })).toBe("http://192.168.0.5:8770");
  });

  it("uses an explicit public origin when the API is behind a named endpoint", () => {
    expect(resolveProxyRequestOrigin({
      browserOrigin: "http://127.0.0.1:5175",
      requestHost: "127.0.0.1:5175",
      apiTarget: "http://127.0.0.1:8770",
      configuredPublicOrigin: "https://review.example.org",
    })).toBe("https://review.example.org");
  });

  it("does not rewrite a third-party origin", () => {
    expect(resolveProxyRequestOrigin({
      browserOrigin: "https://malicious.example",
      requestHost: "192.168.0.5:5175",
      apiTarget: "http://127.0.0.1:8770",
    })).toBe("https://malicious.example");
  });

  it("leaves requests without an Origin header unchanged", () => {
    expect(resolveProxyRequestOrigin({
      requestHost: "192.168.0.5:5175",
      apiTarget: "http://127.0.0.1:8770",
    })).toBeUndefined();
  });
});
