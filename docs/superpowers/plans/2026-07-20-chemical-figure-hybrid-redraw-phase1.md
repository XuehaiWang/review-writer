# Chemical Figure Hybrid Redraw Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the safe Phase 1 hybrid redraw pipeline that extracts chemical figure regions into an auditable IR, renders deterministic SVG/PNG outputs, computes verification risk, and prevents unapproved figures from reaching a manuscript.

**Architecture:** Keep `redraw_figures.py` as the backward-compatible CLI, but move structured processing into a focused `chemfig_redraw` package. The package resolves and preprocesses source assets, calls pluggable OCR/OCSR providers, writes versioned IR, renders confirmed structures deterministically, verifies the outputs, and returns manifest rows consumed by the existing orchestrator and draft insertion gates.

**Tech Stack:** Python 3.11+, standard-library dataclasses and JSON, Pillow, OpenCV headless, RDKit, optional DECIMER/MolNexTR command adapters, pytest, existing Markdown/JSON artifact contracts.

## Global Constraints

- Chemical key content must never be freely redrawn by a generative image model.
- Native PDF vector/text extraction takes priority over OCR/OCSR.
- Local open-source processing is the default; external APIs are optional fallbacks.
- Phase 1 supports single molecules and clear molecule panels; ordinary reaction composition may preserve source geometry, while catalytic cycles and curved-arrow mechanisms require human correction.
- Every output records source hash, tool/config version, recognition evidence, verification result, and human status.
- Phase 1 never auto-approves a figure; downstream use requires `status == "verified"` and `human_check_status == "approved"`.
- A CDXML failure or absence cannot be reported as complete D-level output; Phase 1 emits Molfile/SVG/PNG and reserves CDXML orchestration for Phase 2.
- Existing `--wire-api images` behavior remains available only as an explicit `legacy_image_edit` fallback with mandatory human review.
- Do not add secrets, downloaded model weights, generated figures, or test caches to Git.

---

## Planned File Structure

```text
skills/review-figure-style-redraw/
  SKILL.md                                      # Update Phase 1 usage and gates
  requirements-phase1.txt                      # Lightweight runtime/test dependencies
  scripts/
    redraw_figures.py                           # Backward-compatible CLI and mode selection
    chemfig_redraw/
      __init__.py                               # Public Phase 1 interfaces
      models.py                                 # Typed records and status constants
      io.py                                     # Atomic JSON, hashing, artifact paths
      style.py                                  # Structured style profile loading
      source.py                                 # Source resolution and preprocessing
      providers.py                              # OCR/OCSR protocols and command adapters
      consensus.py                              # Chemical prediction comparison
      ir.py                                     # Versioned IR construction and validation
      render.py                                 # RDKit molecule SVG and full-canvas SVG/PNG
      verify.py                                 # Chemical/text/layout checks and risk score
      pipeline.py                               # Per-figure state machine and manifest result
  references/
    chemical_figure_ir.schema.json              # IR JSON Schema
    style_profiles/
      organic-review-clean-v2.json              # Concrete publication style
  tests/
    conftest.py                                 # Import path and reusable fixtures
    test_models_io.py
    test_style.py
    test_source.py
    test_providers_consensus.py
    test_ir.py
    test_render.py
    test_verify.py
    test_pipeline.py
    test_cli_and_gates.py
skills/review-draft-merge-polish/scripts/
  insert_figures_into_draft.py                  # Consume only approved verified outputs
skills/review-writing-orchestrator/scripts/
  project_status.py                             # Enforce verified/approved redraw gate
.gitignore                                      # Track this skill's tests, ignore generated outputs
```

### Task 1: Establish typed contracts, atomic I/O, and tracked tests

**Files:**
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/__init__.py`
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/models.py`
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/io.py`
- Create: `skills/review-figure-style-redraw/tests/conftest.py`
- Create: `skills/review-figure-style-redraw/tests/test_models_io.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `SourceAsset`, `Region`, `RecognitionCandidate`, `VerificationReport`, `PipelineResult` dataclasses.
- Produces: `sha256_file(path: Path) -> str`, `read_json(path: Path) -> Any`, `write_json_atomic(path: Path, data: Any) -> None`.

- [ ] **Step 1: Add a narrow Git ignore exception and write failing contract tests**

Append these rules after the global test exclusions in `.gitignore`:

```gitignore
!/skills/review-figure-style-redraw/tests/
!/skills/review-figure-style-redraw/tests/**
/skills/review-figure-style-redraw/tests/.pytest_cache/
/skills/review-figure-style-redraw/tests/**/__pycache__/
```

Create `conftest.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
```

Create `test_models_io.py`:

```python
from pathlib import Path

from chemfig_redraw.io import read_json, sha256_file, write_json_atomic
from chemfig_redraw.models import SourceAsset


def test_source_asset_serializes_without_losing_hash(tmp_path: Path) -> None:
    image = tmp_path / "source.png"
    image.write_bytes(b"chemical-figure")
    asset = SourceAsset(
        image_path=image,
        sha256=sha256_file(image),
        width=100,
        height=50,
        source_kind="raster",
    )
    assert asset.to_dict()["sha256"] == sha256_file(image)


def test_atomic_json_replaces_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    write_json_atomic(target, {"status": "first"})
    write_json_atomic(target, {"status": "second"})
    assert read_json(target) == {"status": "second"}
    assert not target.with_suffix(".json.tmp").exists()
```

- [ ] **Step 2: Run the focused tests and confirm the missing package failure**

Run:

```powershell
python -m pytest skills/review-figure-style-redraw/tests/test_models_io.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'chemfig_redraw'`.

- [ ] **Step 3: Implement the minimal typed contracts and atomic I/O**

Implement immutable path-aware dataclasses with explicit `to_dict()` methods. The initial `SourceAsset` implementation must be:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class SourceAsset:
    image_path: Path
    sha256: str
    width: int
    height: int
    source_kind: Literal["raster", "pdf_image", "pdf_vector"]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["image_path"] = str(self.image_path)
        return data
```

Define the remaining dataclasses with only JSON-compatible fields and these mandatory fields:

```python
@dataclass(frozen=True)
class Region:
    region_id: str
    kind: Literal["molecule", "text", "arrow", "decoration"]
    bbox: tuple[int, int, int, int]
    crop_path: Path
    confidence: float
    panel_id: str = "panel_01"

@dataclass(frozen=True)
class RecognitionCandidate:
    provider: str
    isomeric_smiles: str | None
    molfile: str | None
    confidence: float | None
    error: str | None = None

@dataclass(frozen=True)
class VerificationReport:
    blocking_issues: list[str]
    warnings: list[str]
    risk_score: int
    checks: dict[str, Any]

@dataclass(frozen=True)
class PipelineResult:
    figure_id: str
    status: str
    processing_mode: str
    ir_path: Path | None
    svg_path: Path | None
    png_path: Path | None
    verification_report_path: Path | None
    risk_score: int | None
    human_check_status: str
    notes: str = ""
```

Implement I/O exactly with sibling temporary files and `Path.replace()`:

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
```

- [ ] **Step 4: Run tests and commit the contract boundary**

Run:

```powershell
python -m pytest skills/review-figure-style-redraw/tests/test_models_io.py -q
git diff --check
```

Expected: `2 passed`; whitespace check exits 0.

Commit:

```powershell
git add .gitignore skills/review-figure-style-redraw/scripts/chemfig_redraw skills/review-figure-style-redraw/tests
git commit -m "test: establish chemical figure redraw contracts"
```

### Task 2: Replace the free-form style name with a validated style profile

**Files:**
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/style.py`
- Create: `skills/review-figure-style-redraw/references/style_profiles/organic-review-clean-v2.json`
- Create: `skills/review-figure-style-redraw/tests/test_style.py`

**Interfaces:**
- Produces: `StyleProfile` dataclass.
- Produces: `load_style_profile(path: Path) -> StyleProfile`.

- [ ] **Step 1: Write tests for valid and invalid profiles**

```python
from pathlib import Path

import pytest

from chemfig_redraw.style import load_style_profile


def test_default_style_has_deterministic_chemical_dimensions() -> None:
    path = Path(__file__).resolve().parents[1] / "references" / "style_profiles" / "organic-review-clean-v2.json"
    style = load_style_profile(path)
    assert style.profile_id == "organic-review-clean-v2"
    assert style.bond_length_px == 24
    assert style.bond_width_px == 1.6
    assert style.structure_color == "#111111"


def test_style_rejects_non_positive_bond_length(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"profile_id":"bad","bond_length_px":0,"bond_width_px":1.0,"font_family":"Arial","atom_font_size_px":16,"condition_font_size_px":14,"arrow_width_px":1.8,"structure_color":"#111111","mechanism_arrow_color":"#A33A2B","highlight_color":"#2F6F9F","molecule_arrow_gap_px":28,"condition_arrow_gap_px":10,"canvas_margin_px":24}', encoding="utf-8")
    with pytest.raises(ValueError, match="bond_length_px"):
        load_style_profile(path)
```

- [ ] **Step 2: Run tests and confirm import failure**

Run `python -m pytest skills/review-figure-style-redraw/tests/test_style.py -q`.

Expected: collection fails because `chemfig_redraw.style` does not exist.

- [ ] **Step 3: Implement `StyleProfile`, strict numeric validation, and the default JSON**

The JSON must contain exactly these values:

```json
{
  "profile_id": "organic-review-clean-v2",
  "bond_length_px": 24,
  "bond_width_px": 1.6,
  "font_family": "Arial",
  "atom_font_size_px": 16,
  "condition_font_size_px": 14,
  "arrow_width_px": 1.8,
  "structure_color": "#111111",
  "mechanism_arrow_color": "#A33A2B",
  "highlight_color": "#2F6F9F",
  "molecule_arrow_gap_px": 28,
  "condition_arrow_gap_px": 10,
  "canvas_margin_px": 24
}
```

`load_style_profile()` must reject missing keys, unknown keys, non-positive dimensions, and colors that do not match `^#[0-9A-Fa-f]{6}$`.

- [ ] **Step 4: Run and commit**

Run `python -m pytest skills/review-figure-style-redraw/tests/test_style.py -q` and the Task 1 tests.

Expected: `4 passed` total.

Commit:

```powershell
git add skills/review-figure-style-redraw/scripts/chemfig_redraw/style.py skills/review-figure-style-redraw/references/style_profiles skills/review-figure-style-redraw/tests/test_style.py
git commit -m "feat: add structured chemical figure style profiles"
```

### Task 3: Resolve, hash, and preprocess source images without changing chemistry

**Files:**
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/source.py`
- Create: `skills/review-figure-style-redraw/tests/test_source.py`
- Modify: `skills/review-figure-style-redraw/scripts/redraw_figures.py`

**Interfaces:**
- Consumes: existing figure candidate dictionaries and `sha256_file()`.
- Produces: `resolve_source_asset(review_root: Path, figure: dict[str, Any], work_dir: Path) -> SourceAsset | None`.
- Produces: `preprocess_raster(source: Path, destination: Path, upscale_below_px: int = 1200) -> tuple[int, int]`.

- [ ] **Step 1: Write tests that preserve pixels for adequate sources and upscale only low-resolution sources**

Use Pillow to create a white 1400×700 image with a black bond line. Assert that adequate input is copied byte-for-byte and that a 300×150 image becomes at least 1200 pixels wide using Lanczos resampling. Also assert the returned asset hash matches the written working source.

```python
def test_preprocess_does_not_resample_adequate_source(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    Image.new("RGB", (1400, 700), "white").save(source)
    output = tmp_path / "output.png"
    assert preprocess_raster(source, output) == (1400, 700)
    assert output.read_bytes() == source.read_bytes()
```

- [ ] **Step 2: Run the tests and verify the missing implementation failure**

Run `python -m pytest skills/review-figure-style-redraw/tests/test_source.py -q`.

Expected: collection fails because `chemfig_redraw.source` does not exist.

- [ ] **Step 3: Move source lookup behind the new interface and preserve compatibility**

Move the existing direct-path and `content_list.json` caption-score logic into `source.py`. Resolve relative candidate paths against `review_root`, not the process working directory. Preprocessing may only perform lossless copy or Lanczos upscaling in Phase 1; do not erase backgrounds, binarize, sharpen, or generatively repair chemical pixels.

Keep a compatibility wrapper in `redraw_figures.py`:

```python
def resolve_source_image(review_root: Path, figure: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    asset = resolve_source_asset(review_root, figure, review_root / ".chemfig-source-cache")
    if asset is None:
        return None, {"resolution_method": None}
    return asset.image_path, dict(asset.metadata)
```

The final pipeline in Task 8 will stop using the cache-like compatibility path; this wrapper exists only for legacy mode and old callers.

- [ ] **Step 4: Run source and existing CLI dry-run checks, then commit**

Run:

```powershell
python -m pytest skills/review-figure-style-redraw/tests/test_source.py -q
python skills/review-figure-style-redraw/scripts/redraw_figures.py --help
```

Expected: source tests pass and help exits 0 with existing options still listed.

Commit with `git commit -m "refactor: isolate chemical figure source preprocessing"` after staging the three touched paths.

### Task 4: Add pluggable OCR/OCSR providers and chemical consensus

**Files:**
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/providers.py`
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/consensus.py`
- Create: `skills/review-figure-style-redraw/tests/test_providers_consensus.py`

**Interfaces:**
- Produces: `TextRecognition` dataclass with `text`, `character_confidence`, `provider`, `error`.
- Produces: `CommandOCRProvider.recognize(image_path: Path) -> TextRecognition`.
- Produces: `CommandOCSRProvider.recognize(image_path: Path) -> RecognitionCandidate`.
- Produces: `compare_candidates(candidates: Sequence[RecognitionCandidate]) -> ConsensusResult`.

Define the new records with these exact fields:

```python
@dataclass(frozen=True)
class TextRecognition:
    text: str
    character_confidence: list[float]
    provider: str
    error: str | None = None


@dataclass(frozen=True)
class ConsensusResult:
    agreement: Literal["exact", "stereo_conflict", "connectivity_conflict", "single", "invalid"]
    accepted_smiles: str | None
    accepted_molfile: str | None
    requires_human_check: bool
    candidates: list[RecognitionCandidate]
```

- [ ] **Step 1: Write provider protocol and consensus tests with fake executable scripts**

The fake OCSR command prints one JSON object per invocation:

```json
{"isomeric_smiles":"F[C@H](Cl)Br","molfile":null,"confidence":0.93}
```

Tests must prove:

- command arguments are passed without `shell=True`;
- timeout produces an error candidate instead of terminating the pipeline;
- identical stereochemical SMILES return `agreement == "exact"`;
- `F[C@H](Cl)Br` versus `F[C@@H](Cl)Br` returns `agreement == "stereo_conflict"` and `requires_human_check is True`;
- invalid SMILES returns `agreement == "invalid"`.

- [ ] **Step 2: Run tests and confirm missing modules**

Run `python -m pytest skills/review-figure-style-redraw/tests/test_providers_consensus.py -q`.

Expected: import failure for `providers` or `consensus`.

- [ ] **Step 3: Implement safe subprocess adapters and RDKit consensus**

Provider construction uses a list of arguments containing `{image}` as a whole-list token:

```python
provider = CommandOCSRProvider(
    name="decimer",
    command=["python", "run_decimer.py", "--image", "{image}"],
    timeout_seconds=120,
)
```

Replace only exact `{image}` tokens; invoke `subprocess.run(command, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)` with no shell. Parse stdout as JSON and convert all failures to structured error results.

Consensus must parse predictions with `Chem.MolFromSmiles`, compare canonical isomeric SMILES first, then canonical non-isomeric SMILES to distinguish stereo conflicts from connectivity conflicts.

- [ ] **Step 4: Run and commit**

Run the provider tests and `python -m pytest skills/review-figure-style-redraw/tests -q`.

Expected: all current tests pass.

Commit with `git commit -m "feat: add auditable OCR and OCSR provider adapters"`.

### Task 5: Define and validate the versioned chemical figure IR

**Files:**
- Create: `skills/review-figure-style-redraw/references/chemical_figure_ir.schema.json`
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/ir.py`
- Create: `skills/review-figure-style-redraw/tests/test_ir.py`
- Modify: `skills/review-figure-style-redraw/scripts/chemfig_redraw/models.py`

**Interfaces:**
- Consumes: `SourceAsset`, regions, consensus results, OCR results, and style profile ID.
- Produces: `build_figure_ir(*, source: SourceAsset, paper_id: str | None, source_label: str | None, regions: list[Region], molecule_results: dict[str, ConsensusResult], text_results: dict[str, TextRecognition], style_profile: str) -> dict[str, Any]`.
- Produces: `validate_figure_ir(data: dict[str, Any]) -> None` raising `ValueError` with JSON path context.

- [ ] **Step 1: Write failing schema tests**

Test a minimal valid IR with `schema_version`, `source`, `canvas`, `panels`, `objects`, `relations`, and `style_profile`. Test rejection of missing source hashes, out-of-canvas bounding boxes, duplicate object IDs, unknown relation targets, and molecule objects without either an accepted structure or a recorded recognition error.

- [ ] **Step 2: Run tests and verify failure**

Run `python -m pytest skills/review-figure-style-redraw/tests/test_ir.py -q`.

Expected: import failure for `chemfig_redraw.ir`.

- [ ] **Step 3: Implement JSON Schema validation plus semantic validation**

The schema must use `additionalProperties: false` for the top-level record and object records. `validate_figure_ir()` first runs Draft 2020-12 validation, then explicitly checks bounding boxes, unique IDs, relation references, risk-relevant stereo flags, and local artifact paths.

Use this stable top-level contract:

```python
def build_figure_ir(
    *,
    source: SourceAsset,
    paper_id: str | None,
    source_label: str | None,
    regions: list[Region],
    molecule_results: dict[str, ConsensusResult],
    text_results: dict[str, TextRecognition],
    style_profile: str,
) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    for region in regions:
        item: dict[str, Any] = {
            "id": region.region_id,
            "type": region.kind,
            "bbox": list(region.bbox),
            "source_crop": str(region.crop_path),
            "confidence": region.confidence,
            "panel_id": region.panel_id,
        }
        if region.kind == "molecule":
            result = molecule_results[region.region_id]
            item["chemical"] = {
                "agreement": result.agreement,
                "isomeric_smiles": result.accepted_smiles,
                "molfile": result.accepted_molfile,
                "requires_human_check": result.requires_human_check,
                "candidates": [candidate.to_dict() for candidate in result.candidates],
            }
        elif region.kind == "text":
            result = text_results[region.region_id]
            item["text"] = {
                "value": result.text,
                "character_confidence": result.character_confidence,
                "provider": result.provider,
                "error": result.error,
            }
        objects.append(item)
    return {
        "schema_version": "1.0",
        "source": {
            "image_path": str(source.image_path),
            "sha256": source.sha256,
            "paper_id": paper_id,
            "source_label": source_label,
            "source_kind": source.source_kind,
        },
        "canvas": {"width": source.width, "height": source.height},
        "panels": sorted({region.panel_id for region in regions}),
        "objects": objects,
        "relations": [],
        "style_profile": style_profile,
    }
```

The schema and constructor must stay aligned; do not perform rendering or filesystem writes in this function.

- [ ] **Step 4: Run all tests and commit**

Run `python -m pytest skills/review-figure-style-redraw/tests -q`.

Expected: all tests pass.

Commit with `git commit -m "feat: add versioned chemical figure intermediate representation"`.

### Task 6: Render deterministic molecule SVG, canvas SVG, and PNG

**Files:**
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/render.py`
- Create: `skills/review-figure-style-redraw/tests/test_render.py`
- Create: `skills/review-figure-style-redraw/requirements-phase1.txt`

**Interfaces:**
- Consumes: validated IR and `StyleProfile`.
- Produces: `render_molecule_svg(smiles: str, style: StyleProfile, size: tuple[int, int]) -> str`.
- Produces: `render_figure(ir: dict[str, Any], style: StyleProfile, output_dir: Path) -> tuple[Path, Path]`.

- [ ] **Step 1: Write deterministic rendering tests**

Tests must assert:

- chiral SMILES produce an SVG containing bond paths and RDKit metadata;
- repeated renders produce byte-identical SVG after stable XML serialization;
- atom labels and source text are XML-escaped;
- all SVG objects remain inside the canvas;
- PNG dimensions equal the IR canvas dimensions.

- [ ] **Step 2: Run tests and confirm missing renderer failure**

Run `python -m pytest skills/review-figure-style-redraw/tests/test_render.py -q`.

Expected: import failure for `chemfig_redraw.render`.

- [ ] **Step 3: Implement deterministic rendering without generative edits**

Use `rdMolDraw2D.MolDraw2DSVG`, set fixed width/height, fixed font/bond options from `StyleProfile`, call `AddMoleculeMetadata()`, and embed each molecule SVG as a translated group in the full canvas. Phase 1 text objects use exact confirmed text. Unknown/blocked objects render a red review bounding box in `source_comparison.html` but are omitted from publishable `figure.svg` until corrected.

Use CairoSVG for the single SVG-to-PNG conversion path and pin runtime floors:

```text
Pillow>=10.0
opencv-python-headless>=4.8
rdkit>=2024.3
jsonschema>=4.20
cairosvg>=2.7
pytest>=8.0
```

- [ ] **Step 4: Run rendering and full tests, inspect one fixture, then commit**

Run:

```powershell
python -m pytest skills/review-figure-style-redraw/tests/test_render.py -q
python -m pytest skills/review-figure-style-redraw/tests -q
```

Open the generated test PNG with the local image viewer and confirm the bond, atom labels, canvas bounds, and white background are visible. Delete only the test temporary directory created by pytest.

Commit with `git commit -m "feat: render deterministic chemical figure SVG and PNG"`.

### Task 7: Verify chemistry, text, layout, and compute risk

**Files:**
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/verify.py`
- Create: `skills/review-figure-style-redraw/tests/test_verify.py`

**Interfaces:**
- Consumes: confirmed IR, render paths, optional round-trip provider results.
- Produces: `verify_figure(ir: dict[str, Any], roundtrip: dict[str, RecognitionCandidate], rendered_text: dict[str, TextRecognition]) -> VerificationReport`.
- Produces: `write_verification_report(path: Path, report: VerificationReport) -> None`.

- [ ] **Step 1: Write risk and blocker tests**

Encode exact risk contributions:

```text
invalid_structure             blocking, +100
connectivity_mismatch         blocking, +80
stereochemistry_mismatch      blocking, +70
critical_text_mismatch        blocking, +60
relation_target_missing       blocking, +60
single_ocsr_provider          warning,  +20
low_source_resolution         warning,  +15
roundtrip_unavailable         warning,  +10
layout_overlap                warning,  +10
```

Cap the score at 100. Tests must prove that any blocking issue prevents verified status regardless of numeric score, and that an exact dual-provider structure with exact text and available round-trip has score 0.

- [ ] **Step 2: Run tests and confirm missing verifier failure**

Run `python -m pytest skills/review-figure-style-redraw/tests/test_verify.py -q`.

Expected: import failure for `chemfig_redraw.verify`.

- [ ] **Step 3: Implement comparison checks and stable report serialization**

Critical text comparison must preserve digits, signs, degree symbols, percent signs, decimal points, element case, `ee`, `dr`, and units. It may normalize only Unicode normalization form and repeated whitespace. Relation and bounding-box checks reuse IR semantic validation rather than duplicating schemas.

Return sorted unique issue codes so reports are deterministic.

- [ ] **Step 4: Run all tests and commit**

Run `python -m pytest skills/review-figure-style-redraw/tests -q`.

Expected: all tests pass.

Commit with `git commit -m "feat: verify chemical figure redraw fidelity"`.

### Task 8: Build the per-figure state machine and structured CLI mode

**Files:**
- Create: `skills/review-figure-style-redraw/scripts/chemfig_redraw/pipeline.py`
- Create: `skills/review-figure-style-redraw/tests/test_pipeline.py`
- Modify: `skills/review-figure-style-redraw/scripts/redraw_figures.py`

**Interfaces:**
- Produces: `PipelineConfig` dataclass.
- Produces: `process_figure(figure_id: str, candidate: dict[str, Any], config: PipelineConfig) -> PipelineResult`.
- CLI adds: `--processing-mode structured|legacy-image-edit`, `--style-config`, `--ocr-command`, repeatable `--ocsr-command NAME=COMMAND_JSON`, and `--regions-file`.

- [ ] **Step 1: Write pipeline tests using fixture regions and fake providers**

Generate the source PNG and region JSON inside `tmp_path`; do not check generated images into Git. Tests must cover:

- state artifacts written under `03_figure_redraw/F001/`;
- rerun skips unchanged completed stages when source/config hashes match;
- style change invalidates render and verification but preserves recognition;
- source change invalidates every downstream artifact;
- provider timeout yields `needs_human_correction`, not `verified`;
- successful verification yields `status: verified` and `human_check_status: unreviewed`;
- explicit legacy mode retains current image edit behavior and records `processing_mode: legacy_image_edit`.

- [ ] **Step 2: Run tests and verify missing pipeline failure**

Run `python -m pytest skills/review-figure-style-redraw/tests/test_pipeline.py -q`.

Expected: import failure for `chemfig_redraw.pipeline`.

- [ ] **Step 3: Implement the state machine and CLI dispatch**

Structured mode is the new default. It must write `state.json` after each successful stage using atomic writes. The state record includes `source_sha256`, `recognition_config_sha256`, `style_config_sha256`, stage status, and artifact hashes.

Structured runs write `processing_mode: hybrid_structured` into manifest rows; the CLI spelling `structured` is only the user-facing selector.

Do not pass API keys into structured mode. Preserve all legacy CLI flags, but reject `--wire-api responses` in `legacy-image-edit` mode with a clear error because the current responses request does not transmit the source image.

- [ ] **Step 4: Run CLI and pipeline tests, then commit**

Run:

```powershell
python -m pytest skills/review-figure-style-redraw/tests/test_pipeline.py -q
python skills/review-figure-style-redraw/scripts/redraw_figures.py --help
python -m pytest skills/review-figure-style-redraw/tests -q
```

Expected: all tests pass; help shows both processing modes and structured mode is the default.

Commit with `git commit -m "feat: orchestrate structured chemical figure redraws"`.

### Task 9: Strengthen manifests, manuscript insertion, and orchestrator gates

**Files:**
- Create: `skills/review-figure-style-redraw/tests/test_cli_and_gates.py`
- Modify: `skills/review-figure-style-redraw/scripts/redraw_figures.py`
- Modify: `skills/review-draft-merge-polish/scripts/insert_figures_into_draft.py`
- Modify: `skills/review-writing-orchestrator/scripts/project_status.py`
- Modify: `skills/review-final-audit-release/scripts/final_audit_scan.py`

**Interfaces:**
- Manifest rows add `processing_mode`, `ir_path`, `svg_path`, `png_path`, `verification_report`, `risk_score`, `human_check_status`, and artifact hashes.
- Downstream eligibility is centralized as `is_approved_redrawn_figure(row: dict[str, Any]) -> bool` in `chemfig_redraw.models`.

- [ ] **Step 1: Write failing gate tests around temporary review projects**

Create fixtures with four manifest variants:

1. `redrawn` legacy row without verification;
2. `verified` structured row with `human_check_status: unreviewed`;
3. `verified` structured row with `human_check_status: approved` and valid paths/hashes;
4. approved row whose PNG hash is stale.

Assert that only variant 3 is insertable and stage-complete. Assert specific status issues for the others:

```text
legacy_redraw_requires_verification
figure_requires_human_approval
redrawn_artifact_hash_mismatch
```

- [ ] **Step 2: Run tests and observe that current gates accept legacy `redrawn` rows**

Run `python -m pytest skills/review-figure-style-redraw/tests/test_cli_and_gates.py -q`.

Expected: assertions fail because current code checks only `status == "redrawn"` and path presence.

- [ ] **Step 3: Implement one eligibility predicate and use it in all consumers**

Implement:

```python
def is_approved_redrawn_figure(row: dict[str, Any]) -> bool:
    return (
        row.get("status") == "verified"
        and row.get("processing_mode") == "hybrid_structured"
        and row.get("human_check_status") == "approved"
        and bool(row.get("png_path"))
        and bool(row.get("verification_report"))
    )
```

Consumers must additionally resolve the files and compare recorded hashes before accepting the row. `insert_figures_into_draft.py` uses `png_path` and no longer falls back to source candidates when a redraw manifest exists but lacks an approved figure. The existing explicit nonempty `skip_reason.md` remains the only no-figure opt-out.

- [ ] **Step 4: Run gate, audit, and full tests, then commit**

Run:

```powershell
python -m pytest skills/review-figure-style-redraw/tests/test_cli_and_gates.py -q
python -m pytest skills/review-figure-style-redraw/tests -q
python skills/review-writing-orchestrator/scripts/project_status.py --help
python skills/review-final-audit-release/scripts/final_audit_scan.py --help
```

Expected: all tests pass and both scripts retain usable CLIs.

Commit with `git commit -m "feat: gate manuscripts on verified chemical figures"`.

### Task 10: Document Phase 1 operation and run a clean end-to-end acceptance

**Files:**
- Modify: `skills/review-figure-style-redraw/SKILL.md`
- Modify: `skills/技能工作流说明.md`
- Modify: `README.md`
- Modify: `skills/review-figure-style-redraw/agents/openai.yaml`
- Test: `skills/review-figure-style-redraw/tests/test_cli_and_gates.py`

**Interfaces:**
- Documents the structured provider JSON contract, artifact tree, approval operation, fallback policy, and exact acceptance command.

- [ ] **Step 1: Add a documentation assertion test**

Read `SKILL.md` in the test and assert it contains all of:

```python
required = {
    "--processing-mode structured",
    "chemical_figure_ir.json",
    "verification_report.json",
    "human_check_status",
    "legacy-image-edit",
    "--wire-api responses",
}
assert required <= set(item for item in required if item in skill_text)
```

- [ ] **Step 2: Run the assertion and verify it fails against the old documentation**

Run `python -m pytest skills/review-figure-style-redraw/tests/test_cli_and_gates.py -q`.

Expected: documentation assertion fails with the missing terms.

- [ ] **Step 3: Update documentation with exact Phase 1 commands and limitations**

Document a dry structured run using the checked-in fixture region file, two JSON-emitting command providers, and the default style profile. State explicitly that Phase 1 does not claim reliable curved-arrow reconstruction or complete CDXML and that no figure is insertable before human approval.

Document the manifest approval edit as a deliberate human action and require rerunning verification immediately before approval; do not provide an `--auto-approve` option.

- [ ] **Step 4: Run fresh full verification**

From a clean checkout with Phase 1 dependencies installed, run:

```powershell
python -m pytest skills/review-figure-style-redraw/tests -q
python skills/review-figure-style-redraw/scripts/redraw_figures.py --help
python -m compileall -q skills/review-figure-style-redraw/scripts skills/review-draft-merge-polish/scripts skills/review-writing-orchestrator/scripts skills/review-final-audit-release/scripts
git diff --check
```

Expected: zero failing tests, CLI exit 0, compileall exit 0, and whitespace check exit 0.

Render the synthetic fixture and inspect its PNG and SVG. Confirm that the visible chemical structure matches the fixture annotation, the output uses the v2 style values, and the verification report identifies no blocking issue.

- [ ] **Step 5: Commit the Phase 1 documentation and acceptance state**

```powershell
git add README.md skills/技能工作流说明.md skills/review-figure-style-redraw
git commit -m "docs: describe verified chemical figure redraw workflow"
git status --short
```

Expected: commit succeeds and final status is clean.

## Deferred Plans

Create separate reviewed plans after Phase 1 acceptance:

1. **Phase 2 — reaction reconstruction and Ketcher review UI:** reaction arrows, condition ownership, multi-step relations, KET/CDXML export, and browser correction workflow.
2. **Phase 3 — mechanisms and scale:** curved-arrow/catalytic-cycle semantics, worker queue, GPU scheduling, evaluation feedback, and any proposal for low-risk auto-approval.

Neither deferred phase may weaken the Phase 1 verified-and-approved downstream gate.

## Spec Coverage Matrix

| Confirmed design requirement | Phase 1 task |
|---|---|
| Structured style profile | Task 2 |
| Source-first resolution, hashes, safe preprocessing | Task 3 |
| Pluggable local OCR/OCSR and dual-engine consensus | Task 4 |
| Versioned, editable chemical IR | Task 5 |
| Deterministic Molfile/SVG/PNG rendering | Task 6 |
| Chemical, text, layout, and risk verification | Task 7 |
| Recoverable state machine and explicit legacy fallback | Task 8 |
| Verified-and-approved manuscript gate | Task 9 |
| Operator documentation and clean acceptance run | Task 10 |
| Full reaction ownership, Ketcher correction UI, CDXML | Deferred Phase 2 plan |
| Curved-arrow mechanisms, queueing, GPU scheduling | Deferred Phase 3 plan |

The matrix intentionally leaves no Phase 1 requirement without an implementation task. Deferred items retain the same IR and verification contracts so later plans can extend the system without changing downstream safety semantics.
