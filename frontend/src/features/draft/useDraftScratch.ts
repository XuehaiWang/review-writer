import { useState } from "react";

export const DRAFT_SCRATCH_PREFIX = "rw-draft-scratch:";
export function hasDraftScratch(key: string) {
  try { return localStorage.getItem(DRAFT_SCRATCH_PREFIX + key) !== null; } catch { return false; }
}
export function clearDraftScratch() {
  try {
    for (const key of Object.keys(localStorage)) if (key.startsWith(DRAFT_SCRATCH_PREFIX)) localStorage.removeItem(key);
  } catch { /* Storage may be disabled. */ }
}

// Mount the owner with a user/project/paragraph key. Never share scratch across identities.
export function useDraftScratch<T>(key: string | undefined, initial: T) {
  const read = () => {
    try { const raw = key ? localStorage.getItem(DRAFT_SCRATCH_PREFIX + key) : null; return raw ? JSON.parse(raw) as T : initial; }
    catch { return initial; }
  };
  const [stored, setStored] = useState(() => ({ key, value: read() }));
  const current = stored.key === key ? stored : { key, value: read() };
  if (stored.key !== key) setStored(current);
  const [storageFailed, setStorageFailed] = useState(false);
  const update = (next: T) => {
    setStored({ key, value: next });
    if (!key) return;
    try { if (next === null || next === "") localStorage.removeItem(DRAFT_SCRATCH_PREFIX + key);
      else localStorage.setItem(DRAFT_SCRATCH_PREFIX + key, JSON.stringify(next)); }
    catch { setStorageFailed(true); }
  };
  return [current.value, update, storageFailed] as const;
}
