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

  it.each([true, false])("preserves idempotency headers regardless of JSON spread order (%s)", async bodyLast => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { headers: { "content-type": "application/json" } }));
    const explicit = { headers: { "Idempotency-Key": "stable-request" } };
    await apiRequest("/api/v1/task", { method: "POST", ...(bodyLast ? { ...explicit, ...jsonBody({ ok: true }) } : { ...jsonBody({ ok: true }), ...explicit }) });
    const request = fetch.mock.calls[0][1]!;
    expect(new Headers(request.headers).get("Idempotency-Key")).toBe("stable-request");
    expect(new Headers(request.headers).get("Content-Type")).toBe("application/json");
    expect(request.body).toBe('{"ok":true}');
  });

  it("preserves raw upload content type and browser-generated multipart headers", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("", { status: 200 }));
    await apiRequest("/upload", { method: "POST", headers: { "Content-Type": "application/pdf" }, body: new Blob(["pdf"]) });
    expect(new Headers(fetch.mock.calls[0][1]?.headers).get("Content-Type")).toBe("application/pdf");
    await apiRequest("/upload", { method: "POST", body: new FormData() });
    expect(new Headers(fetch.mock.calls[1][1]?.headers).has("Content-Type")).toBe(false);
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

  it("preserves structured prerequisite details for the stage guide", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json({ error: {
      code: "WORKFLOW_STAGE_NOT_READY", message: "Not ready", details: { next_stage: "planning", project_id: "p" },
    } }, { status: 404 }));
    await expect(apiRequest("/stage")).rejects.toMatchObject({
      code: "WORKFLOW_STAGE_NOT_READY", details: { next_stage: "planning", project_id: "p" },
    });
  });
});
