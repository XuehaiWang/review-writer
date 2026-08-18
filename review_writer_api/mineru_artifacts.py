"""Shared validation for immutable MinerU artifact paths."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


def mineru_storage_paths(
    user_root: Path,
    paper_id: str,
    relative_path: str,
) -> tuple[Path, Path, Path]:
    """Return the registered content, version, and extraction paths.

    The filesystem version directory is intentionally independent from the
    database artifact UUID. New uploads use a compact directory component to
    stay below Windows path limits, while migrated artifacts may still use a
    UUID directory. The registered relative path is therefore the source of
    truth for the immutable version boundary.
    """

    raw = str(relative_path or "")
    relative = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    expected = ("review-library", ".artifacts", str(paper_id))
    if (
        not raw
        or relative.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or len(relative.parts) < 6
        or tuple(relative.parts[:3]) != expected
        or relative.parts[4] != "extracted"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("Invalid immutable MinerU artifact path.")
    version_root = user_root.joinpath(*relative.parts[:4])
    extracted_root = version_root / "extracted"
    content_path = user_root.joinpath(*relative.parts)
    return content_path, version_root, extracted_root
