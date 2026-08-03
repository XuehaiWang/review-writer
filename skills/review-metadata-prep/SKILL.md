---
name: review-metadata-prep
description: Prepare a MinerU-parsed review-writing paper library for metadata review. Use when Codex needs to extract or validate required paper metadata and eight fixed LLM classification tags from PDF/Markdown/content_list outputs.
---

# Review Metadata Prep

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

Use this skill to implement the writing-preparation stage for a review-writing agent.

The skill assumes PDFs have already been parsed by MinerU and that a `mineru-outputs/manifest.json` exists.

## Workflow

1. Build paper metadata:

```bash
python scripts/prepare_metadata.py \
  --review-root <review-root> \
  --mineru-output <review-root>/mineru-outputs \
  --pdf-root <review-root>/source-paper/<your-subfolder> \
  --discover-from-pdf-root \
  --append-registry
```

Use `--discover-from-pdf-root` when `manifest.json` only records the latest MinerU batch.
Use `--append-registry` when adding a new source-paper folder to an existing library.

2. Validate metadata:

```bash
python scripts/validate_metadata.py \
  --review-root <review-root>
```

3. Launch the local review dashboard from the separate view module when human audit is needed:

```bash
python <review-root>/view/serve_review_dashboard.py \
  --review-root <review-root> \
  --host 127.0.0.1 \
  --port 8765
```

Open:

```text
http://127.0.0.1:8765/library
```

## LLM Mode

By default, `prepare_metadata.py` uses deterministic fallback rules so the pipeline can run without API credentials.

For useful classification tags, use LLM mode. The LLM extracts required bibliographic fields and exactly eight structured tags:

```text
product
substrate
catalyst_or_method
organometallic_partner
ligand_or_chiral_source
leaving_group
reaction_type
document_scope
```

Each tag value must be selected from your project's classification rules file (e.g. `<your-classification-rules>.py`) under the matching category, or `not specified`. This repo ships `<review-root>/allene_classification_rules.py` as the default example.

To enable LLM enhancement, set:

```bash
export OPENAI_API_KEY=...
```

Then run:

```bash
python scripts/prepare_metadata.py \
  --review-root <review-root> \
  --mineru-output <review-root>/mineru-outputs \
  --pdf-root <review-root>/source-paper/<your-subfolder> \
  --discover-from-pdf-root \
  --append-registry \
  --use-llm \
  --base-url https://naiccc.com \
  --model gpt-5.4 \
  --reasoning-effort high
```

LLM extraction is constrained to the first-page blocks, title/author/abstract candidates, and early Markdown context. Do not send full papers unless explicitly needed.

To refresh only the eight LLM tags on an existing library without rebuilding paper IDs or paths:

```bash
python scripts/llm_retag_metadata.py \
  --review-root <review-root> \
  --model gpt-5.4 \
  --base-url https://naiccc.com \
  --reasoning-effort high \
  --api-key "$OPENAI_API_KEY"
```

For a full-library refresh, prefer the resumable batch runner. It processes three papers per round by default, skips already successful LLM-tagged papers, writes progress after every paper, and retries failures:

```bash
python scripts/batch_llm_retag_metadata.py \
  --review-root <review-root> \
  --batch-size 3 \
  --max-attempts 5 \
  --retry-delay 30 \
  --sleep-seconds 0.5
```

Use `--force` only when existing successful LLM tags should be overwritten. Use `--retry-forever` only when the API failures are known to be transient.

Useful options:

```text
--paper-id P001
--limit 5
--base-url <openai-compatible-base-url>
--api-key <key>
--reasoning-effort high
--sleep-seconds 0.5
```

Outputs:

```text
review-library/metadata/llm_retag_report.json
review-library/metadata/llm_retag_report.md
review-library/metadata/llm_retag_batch_report.json
review-library/metadata/llm_retag_batch_report.md
```

If old metadata files need the new `structured_tags` field before LLM retagging:

```bash
python scripts/backfill_structured_tags.py \
  --review-root <review-root>
```

This only writes `not specified` placeholders for schema compatibility. It does not replace LLM tagging.

## Outputs

The skill writes:

```text
review-library/
  registry/
    papers.jsonl
  metadata/
    papers/<paper_id>.metadata.json
    metadata_validation.json
    metadata_validation.md
    extraction_prompts/
      metadata_extraction_system.md
      metadata_schema.json
```

## Metadata Rules

Each paper metadata JSON must include:

```text
paper_id
slug
title
authors
year
journal
doi
abstract
structured_tags
source_paths
extraction
human_review
quality
```

Every extracted field should carry:

```text
value
source
confidence
human_checked
```

Use `human_review` for audit status and notes. Local paper retrieval uses only the eight values inside `structured_tags`; do not generate or rely on legacy `keywords`, `llm_tags`, `human_tags`, or category compatibility fields.

## Human Audit Dashboard

The dashboard code lives outside this skill:

```text
<review-root>/view/
```

The dashboard is a local review console, not the source of truth. The source of truth is the JSON file on disk.

The dashboard should support:

```text
paper list
PDF preview
MinerU Markdown preview
metadata view
JSON editing
save metadata
mark reviewed
basic search by title, author, keyword, tag
```

## Validation

Run validation after extraction and after manual edits. Treat these as blocking issues:

```text
missing paper_id
missing title
missing authors
missing year
missing abstract
missing structured_tags
missing any of the eight structured tag keys
missing source PDF
missing Markdown
missing metadata JSON
invalid JSON
```

Treat these as review warnings:

```text
missing journal
missing DOI
missing structured_tags
structured tag value is not specified
low confidence title
low confidence abstract
not human reviewed
```
