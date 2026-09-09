---
name: review-metadata-prep
description: Prepare a MinerU-parsed review-writing paper library for metadata review. Use when Codex needs to extract or validate project-neutral bibliographic metadata from PDF/Markdown/content_list outputs.
---

# Review Metadata Prep

Use this skill to implement the writing-preparation stage for a review-writing agent.

The skill assumes PDFs have already been parsed by MinerU and that a `mineru-outputs/manifest.json` exists.

## Workflow

1. Build paper metadata:

```bash
python <review-root>/skills/review-metadata-prep/scripts/prepare_metadata.py \
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
python <review-root>/skills/review-metadata-prep/scripts/validate_metadata.py \
  --review-root <review-root>
```

3. Launch the FastAPI application when human audit is needed:

```bash
python -m review_writer_api \
  --review-root <review-root> \
  --host 127.0.0.1 \
  --port 8770
```

Open:

```text
/library
```

## LLM Mode

By default, `prepare_metadata.py` extracts project-neutral bibliographic
metadata and writes reusable structured Tags as `not specified`. Domain rules
are reserved for query expansion and project Matrix work; they do not classify
uploaded papers or populate Library Metadata Tags.

LLM mode may enhance title, authors, year, and abstract extraction. It never
generates or updates `structured_tags`.

To enable LLM enhancement, set:

```bash
export OPENAI_API_KEY=...
```

Then run:

```bash
python <review-root>/skills/review-metadata-prep/scripts/prepare_metadata.py \
  --review-root <review-root> \
  --mineru-output <review-root>/mineru-outputs \
  --pdf-root <review-root>/source-paper/<your-subfolder> \
  --discover-from-pdf-root \
  --append-registry \
  --use-llm \
  --base-url "$OPENAI_BASE_URL" \
  --model "$REVIEW_METADATA_MODEL" \
  --reasoning-effort high
```

LLM extraction is constrained to the first-page blocks,
title/author/abstract candidates, and early Markdown context. Do not send full
papers unless explicitly needed.

Useful options:

```text
--base-url <openai-compatible-base-url>
--api-key <key>
--reasoning-effort high
--sleep-seconds 0.5
```

If old metadata files need the neutral `structured_tags` field for schema
compatibility:

```bash
python <review-root>/skills/review-metadata-prep/scripts/backfill_structured_tags.py \
  --review-root <review-root>
```

This only writes `not specified` placeholders. It does not classify papers.

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

Use `human_review` for audit status and notes. `structured_tags` stays neutral
unless a person explicitly edits and verifies the complete field with
`human_checked=true`. Do not automatically generate Tags or rely on legacy
`keywords`, `llm_tags`, `human_tags`, or category compatibility fields.

## Human Audit Dashboard

The current Web dashboard and API live outside this skill:

```text
<review-root>/frontend/src/features/library/
<review-root>/review_writer_api/domain_services/library.py
<review-root>/review_writer_api/routers/library.py
```

The dashboard is a review console. In hosted mode PostgreSQL owns user, project, audit and current-artifact state; immutable JSON metadata remains the scientific interchange artifact.

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
unverified structured tags are not neutral
missing source PDF
missing Markdown
missing metadata JSON
invalid JSON
```

Treat these as review warnings:

```text
missing journal
missing DOI
structured tag value is not specified
low confidence title
low confidence abstract
not human reviewed
```
