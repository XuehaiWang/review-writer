#!/usr/bin/env python3
"""Run the DOCX exporter with compatible dependencies available.

The deployment image already provides python-docx and Pillow.  For clean local
workstations, this wrapper keeps an interpreter/platform-specific fallback
cache under ``.deps``.  Import probing is deliberately stronger than checking
for package directories because Pillow requires a compiled ``PIL._imaging``
extension that cannot be shared between operating systems or Python ABIs.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEPS_ROOT = SKILL_DIR / ".deps"
REQUIREMENTS = SKILL_DIR / "requirements.txt"
EXPORTER = SCRIPT_DIR / "md2docx.py"
IMPORT_PROBE = (
    "import docx; "
    "from PIL import Image, _imaging; "
    "assert getattr(_imaging, '__file__', None)"
)


def runtime_cache_tag() -> str:
    """Return a filesystem-safe Python ABI/platform cache identifier."""
    implementation = sys.implementation.cache_tag or f"py{sys.version_info.major}{sys.version_info.minor}"
    platform_tag = sysconfig.get_platform() or platform.system() or "unknown-platform"
    machine = platform.machine() or "unknown-machine"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{implementation}-{platform_tag}-{machine}")


def dependency_dir() -> Path:
    return DEPS_ROOT / runtime_cache_tag()


def clean_pythonpath(extra: Path | None = None) -> str:
    """Exclude legacy/shared DOCX caches, then optionally add one valid cache."""
    entries: list[str] = []
    deps_root = DEPS_ROOT.resolve()
    for raw_entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if not raw_entry:
            continue
        try:
            candidate = Path(raw_entry).resolve()
            if candidate == deps_root or deps_root in candidate.parents:
                continue
        except (OSError, RuntimeError):
            pass
        entries.append(raw_entry)
    if extra is not None:
        entries.insert(0, str(extra))
    return os.pathsep.join(entries)


def probe_dependencies(extra: Path | None = None) -> tuple[bool, str]:
    """Verify python-docx and Pillow's native imaging extension in a child."""
    env = os.environ.copy()
    pythonpath = clean_pythonpath(extra)
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    else:
        env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", IMPORT_PROBE],
        env=env,
        text=True,
        capture_output=True,
    )
    detail = (result.stderr or result.stdout or "dependency import probe failed").strip()
    return result.returncode == 0, detail


def install_fallback_dependencies(target: Path) -> None:
    """Install and validate a fresh cache before publishing it for use."""
    DEPS_ROOT.mkdir(parents=True, exist_ok=True)
    staging = DEPS_ROOT / f".{target.name}.install-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--target",
                str(staging),
                "-r",
                str(REQUIREMENTS),
            ],
            text=True,
            capture_output=True,
        )
        ready, probe_detail = probe_dependencies(staging)
        if result.returncode != 0 or not ready:
            detail = (result.stderr or result.stdout or probe_detail or "dependency installation failed").strip()
            raise RuntimeError(f"Unable to prepare compatible DOCX export dependencies: {detail}")

        # Never reuse an incomplete directory. A concurrent exporter may have
        # finished first; retain its cache if it passes the same native import.
        existing_ready, _ = probe_dependencies(target) if target.is_dir() else (False, "")
        if not existing_ready:
            shutil.rmtree(target, ignore_errors=True)
            staging.replace(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def select_dependency_path() -> Path | None:
    """Prefer deployment dependencies and return a validated fallback if needed."""
    system_ready, _ = probe_dependencies()
    if system_ready:
        return None

    target = dependency_dir()
    cached_ready, _ = probe_dependencies(target) if target.is_dir() else (False, "")
    if not cached_ready:
        install_fallback_dependencies(target)
    ready, detail = probe_dependencies(target)
    if not ready:
        raise RuntimeError(f"DOCX dependency cache is unusable after installation: {detail}")
    return target


def main() -> None:
    dependency_path = select_dependency_path()
    env = os.environ.copy()
    pythonpath = clean_pythonpath(dependency_path)
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    else:
        env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, str(EXPORTER), *sys.argv[1:]], env=env)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
