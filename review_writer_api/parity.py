"""Machine-readable workflow parity inventory validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


REQUIRED_COLUMNS = (
    "ID",
    "Stage",
    "Current route/action",
    "Inputs",
    "Observable result",
    "Artifacts/state",
    "Native test",
    "Status",
)
ROW_ID_RE = re.compile(r"^[A-Z]{3}-\d{3}$")


class ParityInventoryError(ValueError):
    """Raised when the workflow parity inventory is incomplete or malformed."""


@dataclass(frozen=True)
class ParityRow:
    row_id: str
    stage: str
    current_action: str
    inputs: str
    observable_result: str
    artifacts_state: str
    native_test: str
    status: str


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def load_parity_rows(path: Path) -> list[ParityRow]:
    """Load and validate the first parity table in a Markdown document."""

    document = Path(path)
    if not document.is_file():
        raise ParityInventoryError(f"Parity inventory does not exist: {document}")
    lines = document.read_text(encoding="utf-8").splitlines()
    try:
        header_index = next(index for index, line in enumerate(lines) if _cells(line) == list(REQUIRED_COLUMNS))
    except StopIteration as exc:
        raise ParityInventoryError("Parity inventory is missing the required table header.") from exc
    rows: list[ParityRow] = []
    seen: set[str] = set()
    for line in lines[header_index + 2 :]:
        if not line.strip().startswith("|"):
            break
        values = _cells(line)
        if len(values) != len(REQUIRED_COLUMNS):
            raise ParityInventoryError(f"Parity row has {len(values)} columns; expected {len(REQUIRED_COLUMNS)}: {line}")
        row_id = values[0]
        if not ROW_ID_RE.fullmatch(row_id):
            raise ParityInventoryError(f"Invalid parity row ID: {row_id or '(blank)'}")
        if row_id in seen:
            raise ParityInventoryError(f"Duplicate parity row ID: {row_id}")
        missing = [column for column, value in zip(REQUIRED_COLUMNS, values) if not value]
        if missing:
            raise ParityInventoryError(f"{row_id} is missing required field(s): {', '.join(missing)}")
        seen.add(row_id)
        rows.append(ParityRow(row_id, *values[1:]))
    if not rows:
        raise ParityInventoryError("Parity inventory contains no workflow rows.")
    return rows
