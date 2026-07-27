#!/usr/bin/env python3
"""Run the DOCX exporter with its workspace-local dependencies available.

The dashboard invokes this wrapper so a clean workstation can export a review
without requiring the user to install packages into their global Python.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEPS_DIR = SKILL_DIR / ".deps"
REQUIREMENTS = SKILL_DIR / "requirements.txt"
EXPORTER = SCRIPT_DIR / "md2docx.py"


def dependencies_ready() -> bool:
    return (
        (DEPS_DIR / "docx").is_dir()
        and (DEPS_DIR / "PIL").is_dir()
    )


def ensure_dependencies() -> None:
    if dependencies_ready():
        return
    DEPS_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--target",
            str(DEPS_DIR),
            "-r",
            str(REQUIREMENTS),
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not dependencies_ready():
        detail = (result.stderr or result.stdout or "dependency installation failed").strip()
        raise RuntimeError(f"Unable to prepare DOCX export dependencies: {detail}")


def main() -> None:
    ensure_dependencies()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(DEPS_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([sys.executable, str(EXPORTER), *sys.argv[1:]], env=env)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
