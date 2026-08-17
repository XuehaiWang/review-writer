import type { ApiErrorPayload } from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;

  constructor(message: string, status: number, code = "", requestId = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

export function newIdempotencyKey(cryptoSource: Crypto | undefined = globalThis.crypto): string {
  if (typeof cryptoSource?.randomUUID === "function") {
    try {
      return cryptoSource.randomUUID();
    } catch {
      // Some HTTP LAN browsers expose the method but reject it outside a secure context.
    }
  }
  const bytes = new Uint8Array(16);
  if (typeof cryptoSource?.getRandomValues === "function") {
    cryptoSource.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

function payloadMessage(payload: ApiErrorPayload): { message: string; code: string } {
  const detail = payload.detail;
  const error = payload.error;
  if (typeof detail === "string") return { message: detail, code: "" };
  if (detail && typeof detail === "object") {
    return { message: detail.message || "Request failed.", code: detail.code || "" };
  }
  if (typeof error === "string") return { message: error, code: "" };
  if (error && typeof error === "object") {
    return { message: error.message || "Request failed.", code: error.code || "" };
  }
  return { message: payload.message || "Request failed.", code: "" };
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "same-origin",
    ...init,
    headers,
  });
  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get("content-type") || "";
  if (!response.ok) {
    const payload = contentType.includes("application/json")
      ? ((await response.json().catch(() => ({}))) as ApiErrorPayload)
      : ({ message: await response.text().catch(() => "") } as ApiErrorPayload);
    const normalized = payloadMessage(payload);
    throw new ApiError(
      normalized.message || `HTTP ${response.status}`,
      response.status,
      normalized.code,
      response.headers.get("x-request-id") || "",
    );
  }
  if (contentType.includes("application/json")) return (await response.json()) as T;
  return (await response.text()) as T;
}

export function jsonBody(value: unknown): Pick<RequestInit, "body" | "headers"> {
  return {
    body: JSON.stringify(value),
    headers: { "Content-Type": "application/json" },
  };
}
