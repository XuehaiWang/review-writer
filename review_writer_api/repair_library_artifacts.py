"""Repair hosted Library rows whose legacy source files still exist."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from review_writer_api.config import ApiSettings
from review_writer_api.database import User, database_session, create_session_factory, utc_now
from review_writer_api.domain_services.library import LibraryService
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import LibraryArtifact, LibraryPaper
from review_writer_api.workspaces import HostedWorkspaceManager


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def load_legacy_metadata(legacy_root: Path) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    index: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    directory = legacy_root / "review-library" / "metadata" / "papers"
    for path in sorted(directory.glob("*.metadata.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        digest = str((payload.get("source_file") or {}).get("sha256") or "").lower()
        if len(digest) == 64:
            index.setdefault(digest, []).append((path, payload))
    return index


def source_path(legacy_root: Path, raw: Any) -> Path:
    path = Path(str(raw or ""))
    return path.resolve() if path.is_absolute() else (legacy_root / path).resolve()


def missing_record(root: Path, row: LibraryPaper) -> bool:
    pdf = root.joinpath(*row.pdf_relative_path.split("/"))
    markdown = root.joinpath(*row.markdown_relative_path.split("/"))
    return not pdf.is_file() or not markdown.is_file()


def repair_metadata(
    current: dict[str, Any], legacy: dict[str, Any]
) -> dict[str, Any]:
    repaired = deepcopy(current or legacy)
    legacy_paths = deepcopy(legacy.get("source_paths") or {})
    repaired["source_paths"] = legacy_paths
    legacy_extraction = legacy.get("extraction") or {}
    extraction = deepcopy(repaired.get("extraction") or legacy_extraction)
    inputs = deepcopy(extraction.get("inputs") or {})
    legacy_inputs = legacy_extraction.get("inputs") or {}
    for key in ("content_list", "extracted_dir", "manifest"):
        if legacy_inputs.get(key):
            inputs[key] = legacy_inputs[key]
    extraction["inputs"] = inputs
    repaired["extraction"] = extraction
    return repaired


def main() -> int:
    args = parse_args()
    user_id = str(uuid.UUID(args.user_id))
    legacy_root = args.legacy_root.expanduser().resolve()
    settings = ApiSettings.from_env()
    sessions, engine = create_session_factory(settings.database_url)
    manager = HostedWorkspaceManager(settings.hosted_workspace_root)
    service = LibraryService(sessions, manager)
    user_root = manager.user_root(user_id)
    legacy_index = load_legacy_metadata(legacy_root)
    try:
        with sessions() as session:
            user = session.get(User, uuid.UUID(user_id))
            if user is None:
                raise RuntimeError("Repair user does not exist.")
            rows = session.scalars(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == user.id,
                    LibraryPaper.deleted_at.is_(None),
                )
            ).all()
            candidates: list[dict[str, Any]] = []
            for row in rows:
                if not missing_record(user_root, row):
                    continue
                matches = legacy_index.get(row.content_sha256.lower(), [])
                if len(matches) != 1:
                    raise RuntimeError(
                        f"{row.paper_id} has {len(matches)} legacy SHA-256 matches."
                    )
                metadata_path, legacy = matches[0]
                paths = legacy.get("source_paths") or {}
                pdf = source_path(legacy_root, paths.get("pdf"))
                markdown = source_path(legacy_root, paths.get("markdown"))
                content_list = source_path(legacy_root, paths.get("content_list"))
                extracted = source_path(legacy_root, paths.get("extracted_dir"))
                if not (
                    pdf.is_file()
                    and markdown.is_file()
                    and content_list.is_file()
                    and extracted.is_dir()
                ):
                    raise RuntimeError(f"Legacy files are incomplete for {row.paper_id}.")
                if service._digest(pdf) != row.content_sha256:
                    raise RuntimeError(f"Legacy PDF digest drifted for {row.paper_id}.")
                candidates.append(
                    {
                        "paper_id": row.paper_id,
                        "metadata_path": metadata_path,
                        "legacy_metadata": legacy,
                        "pdf": pdf,
                        "markdown": markdown,
                        "content_list": content_list,
                        "extracted": extracted,
                    }
                )
            snapshot = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id,
                "legacy_root": str(legacy_root),
                "candidate_count": len(candidates),
                "papers": [],
            }
            for candidate in candidates:
                row = next(row for row in rows if row.paper_id == candidate["paper_id"])
                artifacts = session.scalars(
                    select(LibraryArtifact).where(
                        LibraryArtifact.user_id == user.id,
                        LibraryArtifact.paper_id == row.paper_id,
                    )
                ).all()
                snapshot["papers"].append(
                    {
                        "paper_id": row.paper_id,
                        "pdf_relative_path": row.pdf_relative_path,
                        "markdown_relative_path": row.markdown_relative_path,
                        "metadata": row.metadata_json,
                        "artifacts": [
                            {
                                "id": str(artifact.id),
                                "kind": artifact.kind,
                                "relative_path": artifact.relative_path,
                                "availability": artifact.availability,
                            }
                            for artifact in artifacts
                        ],
                    }
                )
            email = user.email

        print(json.dumps({"candidate_count": len(candidates)}, ensure_ascii=False))
        if not args.apply:
            return 0
        if args.snapshot is None:
            raise RuntimeError("--snapshot is required with --apply.")
        snapshot_path = args.snapshot.expanduser().resolve()
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        principal = Principal(user_id, frozenset({Role.USER}), email)
        repair_staging = manager.trusted_user_directory(user_id, ".library-repair")
        repaired_count = 0
        for candidate in candidates:
            paper_id = candidate["paper_id"]
            stored_metadata: dict[str, Any] | None = None
            staging = Path(tempfile.mkdtemp(prefix="r-", dir=repair_staging))
            try:
                staged_extracted = staging / "extracted"
                shutil.copytree(candidate["extracted"], staged_extracted)
                content_relative = candidate["content_list"].relative_to(
                    candidate["extracted"]
                )
                staged_content = staged_extracted / content_relative
                if not staged_content.is_file():
                    raise RuntimeError(
                        f"Staged MinerU content is incomplete for {paper_id}."
                    )
                with database_session(sessions) as session:
                    row = session.scalar(
                        select(LibraryPaper)
                        .where(
                            LibraryPaper.user_id == uuid.UUID(user_id),
                            LibraryPaper.paper_id == paper_id,
                        )
                        .with_for_update()
                    )
                    if row is None or row.deleted_at is not None:
                        raise RuntimeError(
                            f"Library row changed during repair: {paper_id}."
                        )
                    if not missing_record(user_root, row):
                        continue
                    metadata = repair_metadata(
                        dict(row.metadata_json or {}), candidate["legacy_metadata"]
                    )
                    source_paths = dict(metadata.get("source_paths") or {})
                    source_paths["extracted_dir"] = str(staged_extracted)
                    source_paths["content_list"] = str(staged_content)
                    metadata["source_paths"] = source_paths
                    extraction = dict(metadata.get("extraction") or {})
                    inputs = dict(extraction.get("inputs") or {})
                    inputs["extracted_dir"] = str(staged_extracted)
                    inputs["content_list"] = str(staged_content)
                    extraction["inputs"] = inputs
                    metadata["extraction"] = extraction
                    pdf_relative, markdown_relative, stored_metadata, artifacts = (
                        service._publish_library_triplet(
                            principal,
                            paper_id,
                            pdf_source=candidate["pdf"],
                            markdown_source=candidate["markdown"],
                            metadata=metadata,
                        )
                    )
                    for old in session.scalars(
                        select(LibraryArtifact).where(
                            LibraryArtifact.user_id == uuid.UUID(user_id),
                            LibraryArtifact.paper_id == paper_id,
                        )
                    ):
                        old.availability = "missing"
                        old.updated_at = utc_now()
                    session.add_all(artifacts)
                    row.pdf_relative_path = pdf_relative
                    row.markdown_relative_path = markdown_relative
                    row.metadata_json = stored_metadata
                    row.updated_at = utc_now()
                    session.flush()
                    service._write_compatibility_metadata(
                        principal, paper_id, stored_metadata
                    )
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            repaired_count += 1
            print(f"REPAIRED {paper_id}")
        print(json.dumps({"repaired_count": repaired_count}, ensure_ascii=False))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
