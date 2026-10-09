"""One local, source-scoped evidence lookup; never another authoring pipeline."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

from review_writer_core.review_fact_readiness import REVIEW_READING_STRATEGY
from review_writer_core.atomic_io import atomic_write_json
from .evidence_resolution import merge_supplement_evidence


MAX_QUESTIONS = 3
MAX_HITS = 9


def supplement_request_id(section_id, requests):
    return hashlib.sha256(json.dumps({"section_id": section_id, "requests": requests},
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare_supplement(*, task, targets, verdicts, registry, state, persist, fetch):
    """A retrieval miss is a local observation, not a negative scientific fact.

    The host callback enforces ownership and pinned source versions. This layer
    additionally rejects unrelated citations and collisions with original input.
    The original request snapshot is intentionally neither replaced nor expanded.
    """
    if task.get("reading_strategy") != REVIEW_READING_STRATEGY:
        return []
    record = state.get("evidence_supplement")
    if record is None and callable(fetch) and targets:
        requests = []
        for claim in targets:
            verdict = verdicts.get(claim["claim_id"], {})
            # A concrete contradiction is not permission to hunt for agreement.
            if verdict.get("status") == "unsupported":
                continue
            requests.append({"claim_id": claim["claim_id"],
                "paper_ids": list(dict.fromkeys(ref["paper_id"] for ref in claim["evidence_refs"])),
                "query": " ".join((claim["claim"] + " " + str(verdict.get("reason") or "")).split())[:1200]})
            if len(requests) == MAX_QUESTIONS:
                break
        if not requests:
            return []
        record = {"requests": requests, "status": "pending", "evidence": []}
        state["evidence_supplement"] = record
        persist()
    if not isinstance(record, dict):
        return []
    if record.get("status") == "pending" and callable(fetch):
        response = fetch(deepcopy(record["requests"]))
        if not isinstance(response, dict) or response.get("status") not in {"ok", "unavailable"}:
            raise RuntimeError("Invalid local evidence lookup response; original draft was retained.")
        record.update(status=response["status"], evidence=response.get("evidence") or [],
                      error=str(response.get("error") or "")[:500],
                      source_manifest=deepcopy(response.get("source_manifest") or {}))
        persist()
    allowed = {pid for request in record.get("requests") or [] for pid in request.get("paper_ids") or []}
    additions = {}
    for row in record.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        key = row.get("evidence_key")
        if (not key or key in registry or key in additions or row.get("paper_id") not in allowed
                or not row.get("chunk_id") or not row.get("source_lineage_hash")
                or not str(row.get("content") or "").strip() or not isinstance(row.get("claim_eligible"), bool)):
            continue
        copied = deepcopy(row)
        # Raw retrieved passages do not certify any candidate relation.
        copied.pop("fact_bindings", None)
        additions[key] = copied
        if len(additions) == MAX_HITS:
            break
    accepted = list(additions.values())
    if record.get("evidence") != accepted:
        record["evidence"] = accepted
        persist()
    return accepted


def evidence_mailbox(directory, section_id, *, timeout=45):
    """Parallel sections have independent files; identical requests reuse replies."""
    if not directory:
        return None
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    validation_nonce = os.environ.get("REVIEW_SECTION_EVIDENCE_MAILBOX_NONCE")

    def retrieve(requests):
        request = {"section_id": section_id, "requests": requests}
        request_id = supplement_request_id(section_id, requests)
        atomic_write_json(folder / f"{request_id}.request.json", {**request, "request_id": request_id})
        response_file = folder / f"{request_id}.response.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if response_file.is_file():
                response = json.loads(response_file.read_text(encoding="utf-8"))
                if (response.get("request_id") == request_id and
                        (not validation_nonce or response.get("validation_nonce") == validation_nonce)):
                    return response
            time.sleep(0.2)
        raise RuntimeError("Local evidence lookup did not respond; the saved draft can be resumed.")

    return retrieve


def checkpoint_supplement_sections(sections, checkpoint):
    """Validation-only projection; keep base inputs for paid-request fingerprints.

    API hosts revalidate these source rows before accepting a checkpoint. CLI
    checkpoints remain local inputs, like the base evidence file itself.
    """
    result = deepcopy(sections)
    records = {sid: (entry.get("state") or {}).get("evidence_supplement") or {}
               for sid, entry in (checkpoint.get("authoring_states") or {}).items()}
    records.update({sid: ((entry.get("synthesis") or {}).get("source_review") or {}).get("evidence_supplement") or {}
                    for sid, entry in (checkpoint.get("entries") or {}).items()})
    for sid, record in records.items():
        if sid not in result or record.get("status") != "ok":
            continue
        result[sid] = merge_supplement_evidence(result[sid], record.get("evidence") or [])
    return result
