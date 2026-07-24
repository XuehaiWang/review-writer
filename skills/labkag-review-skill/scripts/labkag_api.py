from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

# Paper titles/authors routinely contain non-ASCII characters (unicode minus,
# accents, Greek letters). The default console encoding on some Windows
# locales (e.g. GBK) can't represent them, which crashes plain print() mid-batch.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_BASE_URL = os.environ.get("LABKAG_BASE_URL", "http://127.0.0.1:8001").rstrip("/")


def _load_json_file(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def cmd_health(args: argparse.Namespace) -> int:
    return _print_json_response("GET", f"{args.base_url}/health")


def cmd_upload(args: argparse.Namespace) -> int:
    with Path(args.file).open("rb") as file_handle:
        return _print_file_upload_response(
            f"{args.base_url}/v1/papers/upload",
            file_handle,
            Path(args.file).name,
            args.timeout,
        )


def cmd_extract(args: argparse.Namespace) -> int:
    payload = {
        "file_id": args.file_id,
        "project_id": args.project_id,
        "extract_level": args.extract_level,
        "return_chunks": args.return_chunks,
        "use_backup": args.use_backup,
        "mineru_output_dir": args.mineru_output_dir,
        "metadata_output_dir": args.metadata_output_dir,
    }
    return _print_json_response("POST", f"{args.base_url}/v1/papers/extract", json_payload=payload)


def cmd_ingest(args: argparse.Namespace) -> int:
    payload = {
        "project_id": args.project_id,
        "paper_extraction": _load_json_file(args.paper_extraction),
        "confirm": args.confirm,
    }
    return _print_json_response("POST", f"{args.base_url}/v1/papers/ingest", json_payload=payload)


def cmd_query(args: argparse.Namespace) -> int:
    payload = {
        "question": args.question,
        "project_id": args.project_id,
        "paper_id": args.paper_id,
        "top_k": args.top_k,
    }
    return _print_json_response(
        "POST",
        f"{args.base_url}/v1/literature/query",
        json_payload=payload,
    )


def cmd_search(args: argparse.Namespace) -> int:
    payload = {
        "query": args.query,
        "project_id": args.project_id,
        "paper_id": args.paper_id,
        "top_k": args.top_k,
    }
    return _print_json_response(
        "POST",
        f"{args.base_url}/v1/evidence/search",
        json_payload=payload,
    )


def cmd_knowledge(args: argparse.Namespace) -> int:
    url = f"{args.base_url}/v1/papers/{args.paper_id}/knowledge"
    params = {"project_id": args.project_id, "include_evidence": args.include_evidence}
    return _print_json_response("GET", url, params=params, timeout=args.timeout)


def cmd_papers_list(args: argparse.Namespace) -> int:
    url = f"{args.base_url}/v1/papers"
    params = {"project_id": args.project_id}
    return _print_json_response("GET", url, params=params, timeout=args.timeout)


def cmd_taxonomy_get(args: argparse.Namespace) -> int:
    url = f"{args.base_url}/v1/projects/{args.project_id}/taxonomy"
    return _print_json_response("GET", url, timeout=args.timeout)


def cmd_taxonomy_set(args: argparse.Namespace) -> int:
    payload = _load_json_file(args.taxonomy)
    payload.setdefault("project_id", args.project_id)
    url = f"{args.base_url}/v1/projects/{args.project_id}/taxonomy"
    params = {"confirm": args.confirm}
    return _print_json_response("POST", url, json_payload=payload, params=params, timeout=args.timeout)


def cmd_match_topic(args: argparse.Namespace) -> int:
    plan = _load_json_file(args.plan)
    plan.setdefault("project_id", args.project_id)
    payload = {
        "project_id": args.project_id,
        "plan": plan,
        "min_essential_signals": args.min_essential_signals,
        "include_borderline": not args.no_borderline,
    }
    if args.limit is not None:
        payload["limit"] = args.limit
    if args.target_count is not None:
        payload["target_count"] = args.target_count
    url = f"{args.base_url}/v1/papers/match-topic"
    return _print_json_response("POST", url, json_payload=payload, timeout=args.timeout)


def _load_manifest(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"papers": {}}


def _write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _with_retries(base_url: str, action_name: str, attempt_fn, max_attempts: int, retry_delay: float):
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            return attempt_fn(), None
        except requests.HTTPError as exc:
            last_error = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        print(f"  [retry {attempt}/{max_attempts}] {action_name}: {last_error}")
        if attempt < max_attempts:
            time.sleep(retry_delay)
    return None, last_error


def cmd_batch_extract(args: argparse.Namespace) -> int:
    """Upload + extract every PDF in --input-dir, caching each successful
    paper_extraction JSON locally under --extractions-dir (on this machine,
    not assumed to share a filesystem with the backend) so batch-ingest can
    read it back later without re-extracting."""
    input_dir = Path(args.input_dir)
    extractions_dir = Path(args.extractions_dir)
    manifest_path = Path(args.manifest) if args.manifest else extractions_dir / "extract_manifest.json"

    if not input_dir.is_dir():
        print(f"ERROR: input directory does not exist: {input_dir}", file=sys.stderr)
        return 1

    pdfs = sorted(input_dir.rglob("*.pdf"))
    if args.limit and args.limit > 0:
        pdfs = pdfs[: args.limit]

    manifest = _load_manifest(manifest_path)
    papers: dict = manifest.setdefault("papers", {})
    targets = [
        p for p in pdfs if args.force or papers.get(p.name, {}).get("status") != "ok"
    ]
    print(f"Found {len(pdfs)} PDF(s), {len(targets)} pending  |  manifest: {manifest_path}\n")

    extractions_dir.mkdir(parents=True, exist_ok=True)

    for index, pdf_path in enumerate(targets, start=1):
        def do_upload_and_extract(pdf_path=pdf_path):
            with pdf_path.open("rb") as handle:
                upload_resp = requests.post(
                    f"{args.base_url}/v1/papers/upload",
                    files={"file": (pdf_path.name, handle, "application/pdf")},
                    timeout=args.timeout,
                )
            upload_resp.raise_for_status()
            file_id = upload_resp.json()["data"]["file_id"]

            extract_payload = {
                "file_id": file_id,
                "project_id": args.project_id,
                "extract_level": args.extract_level,
            }
            if args.mineru_output_dir:
                extract_payload["mineru_output_dir"] = args.mineru_output_dir
            if args.metadata_output_dir:
                extract_payload["metadata_output_dir"] = args.metadata_output_dir
            extract_resp = requests.post(
                f"{args.base_url}/v1/papers/extract", json=extract_payload, timeout=args.timeout
            )
            extract_resp.raise_for_status()
            body = extract_resp.json()
            if body.get("status") != "success":
                errors = body.get("errors") or []
                raise RuntimeError(errors[0]["message"] if errors else "extraction failed")
            return body["data"]["paper_extraction"]

        result, error = _with_retries(
            args.base_url, pdf_path.name, do_upload_and_extract, args.max_attempts, args.retry_delay
        )
        if result is not None:
            document_id = result["document_id"]
            (extractions_dir / f"{document_id}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            papers[pdf_path.name] = {
                "status": "ok",
                "document_id": document_id,
                "paper_id": result.get("paper", {}).get("paper_id"),
                "title": result.get("paper", {}).get("title"),
            }
            print(f"[{index}/{len(targets)}] [ok] {pdf_path.name} -> {result.get('paper', {}).get('title', '')[:70]}")
        else:
            papers[pdf_path.name] = {"status": "failed", "error": error}
            print(f"[{index}/{len(targets)}] [failed] {pdf_path.name}: {error}")

        _write_manifest(manifest_path, manifest)
        if args.sleep_seconds:
            time.sleep(args.sleep_seconds)

    ok = sum(1 for p in papers.values() if p.get("status") == "ok")
    failed = sum(1 for p in papers.values() if p.get("status") == "failed")
    print(f"\n=== Summary ===\nOK: {ok}  Failed: {failed}  Total tracked: {len(papers)}")
    print(f"Manifest: {manifest_path}")
    return 0 if failed == 0 else 1


def cmd_batch_ingest(args: argparse.Namespace) -> int:
    """Ingest every cached paper_extraction JSON under --extractions-dir
    (populated by batch-extract) via /v1/papers/ingest."""
    extractions_dir = Path(args.extractions_dir)
    manifest_path = (
        Path(args.manifest) if args.manifest else extractions_dir / "ingest_manifest.json"
    )

    extraction_files = sorted(extractions_dir.glob("*.json"))
    extraction_files = [
        p for p in extraction_files if p.name not in {"extract_manifest.json", "ingest_manifest.json"}
    ]
    if args.limit and args.limit > 0:
        extraction_files = extraction_files[: args.limit]

    manifest = _load_manifest(manifest_path)
    papers: dict = manifest.setdefault("papers", {})
    targets = [
        p for p in extraction_files if args.force or papers.get(p.stem, {}).get("status") != "ok"
    ]
    print(f"Found {len(extraction_files)} cached extraction(s), {len(targets)} pending  |  manifest: {manifest_path}\n")

    for index, extraction_path in enumerate(targets, start=1):
        extraction = json.loads(extraction_path.read_text(encoding="utf-8"))

        def do_ingest(extraction=extraction):
            payload = {
                "project_id": args.project_id,
                "paper_extraction": extraction,
                "confirm": True,
            }
            resp = requests.post(
                f"{args.base_url}/v1/papers/ingest", json=payload, timeout=args.timeout
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("status") != "success":
                errors = body.get("errors") or []
                raise RuntimeError(errors[0]["message"] if errors else "ingest failed")
            return body["data"]

        result, error = _with_retries(
            args.base_url, extraction_path.stem, do_ingest, args.max_attempts, args.retry_delay
        )
        if result is not None:
            papers[extraction_path.stem] = {"status": "ok", **result}
            print(f"[{index}/{len(targets)}] [ok] {extraction_path.stem} -> {result}")
        else:
            papers[extraction_path.stem] = {"status": "failed", "error": error}
            print(f"[{index}/{len(targets)}] [failed] {extraction_path.stem}: {error}")

        _write_manifest(manifest_path, manifest)
        if args.sleep_seconds:
            time.sleep(args.sleep_seconds)

    ok = sum(1 for p in papers.values() if p.get("status") == "ok")
    failed = sum(1 for p in papers.values() if p.get("status") == "failed")
    print(f"\n=== Summary ===\nOK: {ok}  Failed: {failed}  Total tracked: {len(papers)}")
    print(f"Manifest: {manifest_path}")
    return 0 if failed == 0 else 1


def _print_json_response(
    method: str,
    url: str,
    *,
    json_payload=None,
    params=None,
    timeout: int = 60,
) -> int:
    try:
        response = requests.request(
            method,
            url,
            json=json_payload,
            params=params,
            timeout=timeout,
        )
        if response.status_code >= 400:
            _print_error(response)
            return 1
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        return 0
    except requests.RequestException as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _print_file_upload_response(url: str, file_handle, filename: str, timeout: int) -> int:
    try:
        response = requests.post(
            url,
            files={"file": (filename, file_handle, "application/pdf")},
            timeout=timeout,
        )
        if response.status_code >= 400:
            _print_error(response)
            return 1
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        return 0
    except requests.RequestException as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _print_error(response: requests.Response) -> None:
    try:
        body = response.json()
    except ValueError:
        print(response.text, file=sys.stderr)
        return
    print(json.dumps(body, ensure_ascii=False, indent=2), file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call the LabKAG API.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=int, default=60)

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health").set_defaults(func=cmd_health)

    upload = subparsers.add_parser("upload")
    upload.add_argument("--file", required=True)
    upload.set_defaults(func=cmd_upload)

    extract = subparsers.add_parser("extract")
    extract.add_argument("--file-id", required=True)
    extract.add_argument("--project-id")
    extract.add_argument("--extract-level", default="basic", choices=["basic", "detailed"])
    extract.add_argument("--return-chunks", action="store_true")
    extract.add_argument("--use-backup", action="store_true",
                         help="Use PyMuPDF backup (data/parsed_backup/) instead of MinerU output.")
    extract.add_argument("--mineru-output-dir",
                         help="Override MinerU output directory for this call "
                              "(default: server's PARSED_DIR setting).")
    extract.add_argument("--metadata-output-dir",
                         help="Also write a copy of the extraction result JSON here "
                              "(default: only the server's METADATA_DIR).")
    extract.set_defaults(func=cmd_extract)

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--project-id")
    ingest.add_argument("--paper-extraction", required=True)
    ingest.add_argument("--confirm", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    query = subparsers.add_parser("query")
    query.add_argument("--question", required=True)
    query.add_argument("--project-id")
    query.add_argument("--paper-id")
    query.add_argument("--top-k", type=int, default=5)
    query.set_defaults(func=cmd_query)

    search = subparsers.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--project-id")
    search.add_argument("--paper-id")
    search.add_argument("--top-k", type=int, default=10)
    search.set_defaults(func=cmd_search)

    knowledge = subparsers.add_parser("knowledge")
    knowledge.add_argument("--paper-id", required=True)
    knowledge.add_argument("--project-id")
    knowledge.add_argument(
        "--include-evidence",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    knowledge.set_defaults(func=cmd_knowledge)

    papers_list = subparsers.add_parser(
        "papers-list", help="List papers in a project, for taxonomy bootstrap reading."
    )
    papers_list.add_argument("--project-id", required=True)
    papers_list.set_defaults(func=cmd_papers_list)

    taxonomy_get = subparsers.add_parser(
        "taxonomy-get", help="Fetch a project's current taxonomy, if any."
    )
    taxonomy_get.add_argument("--project-id", required=True)
    taxonomy_get.set_defaults(func=cmd_taxonomy_get)

    taxonomy_set = subparsers.add_parser(
        "taxonomy-set",
        help="Create or edit a project's taxonomy. See references/taxonomy_bootstrap_prompt.md.",
    )
    taxonomy_set.add_argument("--project-id", required=True)
    taxonomy_set.add_argument("--taxonomy", required=True, help="Path to a ProjectTaxonomy JSON file.")
    taxonomy_set.add_argument(
        "--confirm",
        action="store_true",
        help="Required to apply a breaking edit (removed/renamed value).",
    )
    taxonomy_set.set_defaults(func=cmd_taxonomy_set)

    match_topic = subparsers.add_parser(
        "match-topic",
        help="Match a decomposed topic plan against a project's papers. See references/topic_decomposition_prompt.md.",
    )
    match_topic.add_argument("--project-id", required=True)
    match_topic.add_argument("--plan", required=True, help="Path to a TopicPlan JSON file.")
    match_topic.add_argument("--min-essential-signals", type=int, default=2)
    match_topic.add_argument(
        "--no-borderline",
        action="store_true",
        help="Exclude the borderline tier from the response, confirmed matches only.",
    )
    match_topic.add_argument("--limit", type=int, default=None, help="Optional per-tier cap.")
    match_topic.add_argument(
        "--target-count",
        type=int,
        default=None,
        help=(
            "Expected result size. Never truncates confirmed/borderline (still "
            "returned in full) -- adds summary.suggested_confirmed_count/"
            "suggested_borderline_count, a cutoff found at the steepest "
            "match_score drop near this target, not just at the Nth item."
        ),
    )
    match_topic.set_defaults(func=cmd_match_topic)

    batch_extract = subparsers.add_parser(
        "batch-extract",
        help="Upload + extract every PDF in a folder, resumable/checkpointed.",
    )
    batch_extract.add_argument("--input-dir", required=True, help="Folder of PDFs to scan recursively.")
    batch_extract.add_argument(
        "--extractions-dir",
        required=True,
        help="Local folder (on this machine) to cache each paper_extraction JSON in, "
        "for batch-ingest to read back later.",
    )
    batch_extract.add_argument("--project-id", required=True)
    batch_extract.add_argument("--extract-level", default="basic", choices=["basic", "detailed"])
    batch_extract.add_argument(
        "--mineru-output-dir", help="Optional: reuse a pre-parsed MinerU output directory."
    )
    batch_extract.add_argument(
        "--metadata-output-dir",
        help="Optional: also write a copy of each extraction result JSON here "
        "on the server (default: only the server's METADATA_DIR).",
    )
    batch_extract.add_argument("--manifest", help="Defaults to <extractions-dir>/extract_manifest.json")
    batch_extract.add_argument("--limit", type=int, default=0, help="Max PDFs to process. 0 = all.")
    batch_extract.add_argument("--force", action="store_true", help="Reprocess even if already succeeded.")
    batch_extract.add_argument("--max-attempts", type=int, default=3)
    batch_extract.add_argument("--retry-delay", type=float, default=10.0)
    batch_extract.add_argument("--sleep-seconds", type=float, default=1.0)
    batch_extract.set_defaults(func=cmd_batch_extract)

    batch_ingest = subparsers.add_parser(
        "batch-ingest",
        help="Ingest every paper_extraction cached by batch-extract, resumable/checkpointed.",
    )
    batch_ingest.add_argument(
        "--extractions-dir",
        required=True,
        help="Local folder populated by a prior batch-extract run.",
    )
    batch_ingest.add_argument("--project-id", required=True)
    batch_ingest.add_argument("--manifest", help="Defaults to <extractions-dir>/ingest_manifest.json")
    batch_ingest.add_argument("--limit", type=int, default=0, help="Max papers to process. 0 = all.")
    batch_ingest.add_argument("--force", action="store_true", help="Re-ingest even if already succeeded.")
    batch_ingest.add_argument("--max-attempts", type=int, default=3)
    batch_ingest.add_argument("--retry-delay", type=float, default=5.0)
    batch_ingest.add_argument("--sleep-seconds", type=float, default=0.2)
    batch_ingest.set_defaults(func=cmd_batch_ingest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.base_url = args.base_url.rstrip("/")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
