import { useQuery } from "@tanstack/react-query";
import { useRef } from "react";
import { meQuery } from "../api/queries";
import { useDraftScratch } from "../features/draft/useDraftScratch";

/** Only non-sensitive form data. Null follows the latest server value; edits do not. */
export function useFormDraft<T>(scope: string, initial: T) {
  const identity = useQuery(meQuery);
  const [draft, setDraft, storageFailed] = useDraftScratch<T | null>(
    identity.data?.user_id ? `${identity.data.user_id}:form:${scope}` : undefined, null,
  );
  const value = draft ?? initial;
  const identityKey = `${identity.data?.user_id || ""}:${scope}`;
  const latest = useRef({ identityKey, value });
  latest.current = { identityKey, value };
  // A successful save must not erase edits made while that request was running.
  const checkpoint = () => {
    const snapshot = JSON.stringify(value);
    return () => {
      if (latest.current.identityKey === identityKey && JSON.stringify(latest.current.value) === snapshot) setDraft(null);
    };
  };
  const set = (next: T | ((current: T) => T)) => setDraft(typeof next === "function" ? (next as (current: T) => T)(value) : next);
  return { value, set, clear: () => setDraft(null), checkpoint, dirty: draft !== null, storageFailed };
}
