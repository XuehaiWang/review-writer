const RELOAD_MARKER = "review-writer:dynamic-import-reload";
const RELOAD_GUARD_MS = 60_000;

export function isDynamicImportFailure(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error || "");
  return /Failed to fetch dynamically imported module|Importing a module script failed|ChunkLoadError|Loading chunk .* failed/i.test(message);
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function clearDynamicImportReloadMarker(): void {
  storage()?.removeItem(RELOAD_MARKER);
}

export function claimDynamicImportReload(error: unknown, now = Date.now()): boolean {
  if (!isDynamicImportFailure(error)) return false;
  const store = storage();
  if (!store) return false;
  const previous = Number(store.getItem(RELOAD_MARKER) || 0);
  if (previous > 0 && now - previous < RELOAD_GUARD_MS) return false;
  store.setItem(RELOAD_MARKER, String(now));
  return true;
}

export async function loadLazyModule<T>(loader: () => Promise<T>): Promise<T> {
  try {
    const loaded = await loader();
    clearDynamicImportReloadMarker();
    return loaded;
  } catch (error) {
    if (claimDynamicImportReload(error)) window.location.reload();
    throw error;
  }
}
