"""Small atomic filesystem writers shared by migration and configuration code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write UTF-8 JSON and atomically replace the destination.

    The temporary file deliberately lives beside the destination so ``replace``
    stays on the same filesystem and cannot expose a partially written JSON file.
    """

    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep the temporary name short. Hosted Windows workspaces can already be
    # close to MAX_PATH, so a UUID suffix would make otherwise valid jobs fail.
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
