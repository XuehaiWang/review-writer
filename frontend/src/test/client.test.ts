import { afterEach, describe, expect, it, vi } from "vitest";

import { apiRequest, jsonBody, newIdempotencyKey } from "../api/client";

describe("apiRequest", () => {
  afterEach(() => vi.restoreAllMocks());

  it("normalizes FastAPI errors and preserves the request id", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: { code: "stale", message: "Regenerate first." } }), {
        status: 409,
        headers: {
          "content-type": "application/json",
          "x-request-id": "request-7",
        },
      }),
    );

    await expect(apiRequest("/api/v1/example")).rejects.toMatchObject({
      status: 409,
      code: "stale",
      requestId: "request-7",
      message: "Regenerate first.",
    });
  });

  it("adds JSON headers without overriding explicit request headers", () => {
    expect(jsonBody({ ok: true })).toEqual({
      body: '{"ok":true}',
      headers: { "Content-Type": "application/json" },
    });
  });

  it("creates an RFC 4122 idempotency key without crypto.randomUUID", () => {
    const cryptoWithoutRandomUUID = {
      getRandomValues: (values: Uint8Array) => {
        values.fill(0x2a);
        return values;
      },
    } as Crypto;

    expect(newIdempotencyKey(cryptoWithoutRandomUUID)).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });
});
