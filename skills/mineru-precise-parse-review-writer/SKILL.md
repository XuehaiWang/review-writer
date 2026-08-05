---
name: mineru-precise-parse-review-writer
description: Parse local literature PDFs under the active review-writer root into Markdown with the MinerU precise parsing batch API. Use when Codex needs to batch-convert a review paper library, preserve full MinerU zip sidecars, keep extracted images and JSON outputs, and skip files that were already parsed unless a force rerun is explicitly requested.
---

# MinerU Precise Parse For Review Writer

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

Use this skill when the task is to convert a local PDF library into review-ready Markdown with the MinerU precise parsing API.

This skill is for batch parsing only. It uploads local PDFs to MinerU, waits for batch completion, downloads the result zip for each PDF, extracts `full.md`, rewrites image paths, and writes a local manifest.

## Default Paths

- input root: `<review-root>`
- skill root: `<review-root>/skills/mineru-precise-parse-review-writer`
- output root: `<review-root>/mineru-outputs`

The parser scans the input root recursively for `*.pdf` files and ignores the skill directory and output directory.

## Auth

Token resolution order:

1. `--token <token>`
2. `MINERU_API_TOKEN`
3. `config/mineru_api_token.txt`

Keep credentials outside version control. Prefer `MINERU_API_TOKEN`; an optional
local `config/mineru_api_token.txt` may be used only when it is ignored by Git.

## Default Behavior

Parsing is incremental by default:

- if `mineru-outputs/markdown/<slug>.md` already exists, skip that PDF
- reparse only when the user explicitly wants a rerun and `--force` is passed

## Commands

Parse the whole local library:

```bash
python <review-root>/skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py \
  --input-dir <pdf-library-folder>
```

Parse only one or two files as a smoke test:

```bash
python <review-root>/skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py \
  --input-dir <pdf-library-folder> --limit 2
```

Force a full rerun:

```bash
python <review-root>/skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py \
  --input-dir <pdf-library-folder> --force
```

Parse a specific subtree:

```bash
python <review-root>/skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py \
  --input-dir <pdf-library-folder>/<subfolder>
```

Parse one specific PDF:

```bash
python <review-root>/skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py \
  --pdf <pdf-library-folder>/paper.pdf
```

Write a separate manifest for an independent single-PDF job:

```bash
python <review-root>/skills/mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py \
  --input-dir <pdf-library-folder> \
  --pdf <pdf-library-folder>/paper.pdf \
  --manifest-path <review-root>/mineru-outputs/manifests/paper.json
```

The stage-1 local upload route uses this single-PDF mode. It admits a PDF to
the Library only after MinerU Markdown, the extracted directory, and
`*_content_list.json` all exist and canonical metadata has been rebuilt from
those outputs. A failed or incomplete MinerU run is not exposed as a ready
paper to retrieval, drafting, or figure inventory stages.

## Outputs

The skill writes:

- `mineru-outputs/markdown/*.md`
- `mineru-outputs/extracted/<slug>/`
- `mineru-outputs/raw_zips/*.zip`
- `mineru-outputs/manifest.json`

The Markdown copies are the main deliverable.
The extracted directories keep `full.md`, images, and MinerU sidecar JSON for downstream chunking, provenance, and figure extraction.

## Boundary

Use this skill only for PDF-to-Markdown conversion.

Do not use it to:

- clean or rewrite the parsed Markdown
- synthesize the review itself
- treat MinerU output as already validated evidence
- replace later chunking, indexing, or citation-grounding stages
