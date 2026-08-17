type ProxyOriginOptions = {
  browserOrigin?: string;
  requestHost?: string;
  apiTarget: string;
  configuredPublicOrigin?: string;
};

function bareHttpOrigin(value: string, label: string): string {
  const parsed = new URL(value);
  if (
    !["http:", "https:"].includes(parsed.protocol)
    || parsed.username
    || parsed.password
    || (parsed.pathname && parsed.pathname !== "/")
    || parsed.search
    || parsed.hash
  ) {
    throw new Error(`${label} must be a bare HTTP(S) origin.`);
  }
  return parsed.origin;
}

/**
 * Return the canonical Origin that a same-origin Vite proxy request should
 * present to the hosted API. Cross-origin requests are deliberately left
 * unchanged so the API's CSRF middleware can reject them.
 */
export function resolveProxyRequestOrigin({
  browserOrigin,
  requestHost,
  apiTarget,
  configuredPublicOrigin,
}: ProxyOriginOptions): string | undefined {
  if (!browserOrigin || !requestHost) return browserOrigin;

  let incoming: URL;
  try {
    incoming = new URL(browserOrigin);
  } catch {
    return browserOrigin;
  }
  if (incoming.host.toLowerCase() !== requestHost.trim().toLowerCase()) {
    return browserOrigin;
  }

  if (configuredPublicOrigin?.trim()) {
    return bareHttpOrigin(configuredPublicOrigin.trim(), "VITE_DEV_PUBLIC_ORIGIN");
  }

  const target = new URL(bareHttpOrigin(apiTarget, "VITE_DEV_API_TARGET"));
  const targetPort = target.port ? `:${target.port}` : "";
  return `${target.protocol}//${incoming.hostname}${targetPort}`;
}
