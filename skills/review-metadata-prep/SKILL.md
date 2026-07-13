---
name: review-metadata-prep
description: Prepare a MinerU-parsed review-writing paper library for metadata review. Use when Codex needs to extract or validate required paper metadata and open-vocabulary structured tags from PDF/Markdown/content_list outputs.
---

# Review Metadata Prep

Use this skill to implement the writing-preparation stage for a review-writing agent.

The skill assumes PDFs have already been parsed by MinerU and that a `mineru-outputs/manifest.json` exists.

## Locate the knowledge base (LLM step, required before running the script)

`--pdf-root` points at the folder of local PDFs this run should build metadata for — the paper "knowledge base." Do not assume a fixed path. Before running `prepare_metadata.py`, locate this folder yourself:

1. If the user has named a specific folder, use it.
2. Otherwise search the server environment for directories containing `.pdf` files — common locations include `review-library/paper_pdf/`, the server home directory, a mounted drive, or a `source-paper/<topic>/` folder from a prior run. Reuse whichever knowledge base directory `review-topic-paper-discovery` was already pointed at via `--paper-dir` for this project, if one was used, so metadata prep covers the same papers discovery already registered.
3. Confirm the folder actually contains `.pdf` files before proceeding; if several plausible candidates exist, ask the user rather than guessing.
4. If the discovered folder has not been parsed by MinerU yet (no corresponding entries under `mineru-outputs/`), run `mineru-precise-parse-review-writer` on it first — this skill only builds metadata from already-parsed output, it does not parse PDFs itself.

## Workflow

1. Build paper metadata:

```bash
python3 <skill-root>/scripts/prepare_metadata.py \
  --review-root <review-root> \
  --mineru-output <review-root>/mineru-outputs \
  --pdf-root <path/to/knowledge-base/folder> \
  --discover-from-pdf-root \
  --append-registry
```

`--pdf-root` is the knowledge base folder located in the step above.

Use `--discover-from-pdf-root` when `manifest.json` only records the latest MinerU batch.
Use `--append-registry` when adding a new knowledge base folder to an existing library.

Add `--crossref-lookup` to look each paper up on Crossref by its rule-extracted title at build time and fill `journal`/`volume`/`pages`/`doi`/`year` immediately if the rule-based extraction left them empty — no separate backfill run needed for papers that get a match. This makes one network call per paper with an empty bibliographic field, so it adds latency; it is optional and safe to skip (use `crossref_backfill_metadata.py` afterward instead, see "Crossref Backfill" below). Not every paper will match (conference papers, preprints, and non-Crossref-indexed venues commonly return no match); this is expected.

2. Validate metadata:

```bash
python3 <skill-root>/scripts/validate_metadata.py \
  --review-root <review-root>
```

3. Launch the local review dashboard from the separate view module when human audit is needed:

```bash
python3 <review-root>/view/serve_review_dashboard.py \
  --review-root <review-root> \
  --host 127.0.0.1 \
  --port 8765
```

Open:

```text
http://127.0.0.1:8765/library
```

## LLM Mode

By default (without `--use-llm`), `prepare_metadata.py` extracts bibliographic fields with deterministic rules but leaves all seven structured tags as `not specified` — there is no hardcoded domain vocabulary to classify against, so rule-only tagging cannot produce meaningful labels for an arbitrary topic.

For real classification tags, use LLM mode. The LLM extracts required bibliographic fields and exactly seven structured tags:

```text
output
input
method
co_input
modifier
process_type
document_scope
```

`co_input` and `modifier` do not apply to most papers outside a small set of fields (e.g. chemistry) — expect `not specified` for these two on most papers, and do not treat a low fill rate on them as a quality problem.

Tags are open-vocabulary: the LLM writes a short natural-language label per category based on the paper's actual content (see `references/metadata_extraction_system.md`), not a value picked from a fixed list. Use `not specified` when a category does not apply.

To enable LLM enhancement, set:

```bash
export OPENAI_API_KEY=...
```

Then run:

```bash
python3 <skill-root>/scripts/prepare_metadata.py \
  --review-root <review-root> \
  --mineru-output <review-root>/mineru-outputs \
  --pdf-root <path/to/knowledge-base/folder> \
  --discover-from-pdf-root \
  --append-registry \
  --use-llm \
  --base-url https://naiccc.com \
  --model gpt-5.4 \
  --reasoning-effort high
```

LLM extraction is constrained to the first-page blocks, title/author/abstract candidates, and early Markdown context. Do not send full papers unless explicitly needed.

## Two-Pass Tagging for Large Libraries (auto-enforced above 30 papers)

LLM tagging cost scales with every registered paper, but only the 20-30 papers discovery selects are ever used downstream. For larger libraries, invert the order — tag after discovery, not before.

**This is enforced automatically:** when `--use-llm` is set and the run covers more than `--max-llm-papers` papers (default 30), `prepare_metadata.py` prints an `[auto-rule-mode]` notice and falls back to rule-only extraction by itself — no flag changes needed. Continue with the two-pass flow below. To deliberately LLM-tag a large library anyway, pass `--force-llm` (or raise `--max-llm-papers`; `0` disables the auto-switch).

```text
Pass 1 (free): run prepare_metadata.py WITHOUT --use-llm.
    Rule extraction fills title/abstract/bibliography; all structured tags stay "not specified".
Discovery: run review-topic-paper-discovery as usual.
    discover.py scores untagged papers on title/abstract/markdown directly
    (no structured tags required), producing the 20-30 shortlist plus a borderline list.
Pass 2 (paid, shortlist only): tag just the shortlisted papers:

python3 <skill-root>/scripts/llm_retag_metadata.py \
  --review-root <review-root> \
  --paper-ids-from <review-root>/review-projects/<project_id>/00_discovery/selected_discovery_results.json \
  --model <model> --base-url <base-url> --reasoning-effort high

Optionally re-run discovery afterward for a tag-informed final cut.
```

`--paper-ids-from` reads the discovery results file and retags only its `local_papers` and `borderline_papers`. This drops tagging cost from O(library size) to O(selection size). The tag-everything-first order remains fine for small libraries and for libraries shared across many future projects (tags are reusable; shortlists are per-project).

## Relevance Pre-Filter (cost control)

The LLM tagging call is the most expensive part of this skill, and it scales linearly with every registered paper -- including deliberately-mixed-in noise papers used to test discovery's filtering, or unrelated papers that happen to share a folder with the real knowledge base. Add `--relevance-keywords` to skip the LLM call entirely for papers that are clearly off-topic, before paying for it:

```bash
python3 <skill-root>/scripts/prepare_metadata.py \
  --review-root <review-root> \
  --mineru-output <review-root>/mineru-outputs \
  --pdf-root <path/to/knowledge-base/folder> \
  --discover-from-pdf-root \
  --append-registry \
  --use-llm \
  --relevance-keywords <review-root>/review-projects/<project_id>/00_discovery/agent_keywords.json \
  --min-relevance-score 0.12 \
  --base-url https://naiccc.com \
  --model gpt-5.4 \
  --reasoning-effort high
```

`--relevance-keywords` points at a keyword file -- the `agent_keywords.json` written during `review-topic-paper-discovery` works directly, or any plain JSON list of keyword strings. Before the LLM call, each paper's rule-extracted title/abstract is scored against these keywords with a cheap token-overlap check (no LLM involved). Papers scoring below `--min-relevance-score` (default `0.12`) skip LLM tagging entirely -- their rule-based metadata (title, authors, year, journal/volume/pages/doi if found) is still written, but `structured_tags` stays `not specified` and `extraction.notes` records `llm_skipped_low_relevance: relevance_score=<score> < <threshold>` so the skip is visible and auditable, not silent.

This filter is deliberately crude (a rule-based heuristic, not a judgment call) so it only catches clear-cut off-topic papers. It will not skip a paper the LLM would plausibly have found relevant from its title/abstract alone. If a paper you expected to be tagged got skipped, check its `extraction.notes` for the score and either lower `--min-relevance-score` or rerun just that paper with `llm_retag_metadata.py --paper-id <id>` (which has no relevance filter, since it's meant for targeted, already-selected papers).

Omit `--relevance-keywords` entirely and every paper is sent to the LLM as before -- this is opt-in, not a default behavior change.

To refresh only the seven LLM tags on an existing library without rebuilding paper IDs or paths:

```bash
python3 <skill-root>/scripts/llm_retag_metadata.py \
  --review-root <review-root> \
  --model gpt-5.4 \
  --base-url https://naiccc.com \
  --reasoning-effort high \
  --api-key "$OPENAI_API_KEY"
```

For a full-library refresh, prefer the resumable batch runner. It processes three papers per round by default, skips already successful LLM-tagged papers, writes progress after every paper, and retries failures:

```bash
python3 <skill-root>/scripts/batch_llm_retag_metadata.py \
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
python3 <skill-root>/scripts/backfill_structured_tags.py \
  --review-root <review-root>
```

This only writes `not specified` placeholders for schema compatibility. It does not replace LLM tagging.

## Crossref Backfill

Rule-only extraction (and some preprints) often leave `journal`, `volume`, `pages`, `doi`, or `year` empty even though the paper is a real, Crossref-indexed publication. If `prepare_metadata.py` was run without `--crossref-lookup`, or the run happened before this paper's metadata existed, run this afterward to fill those gaps by looking each paper up on Crossref by its extracted title:

```bash
python3 <skill-root>/scripts/crossref_backfill_metadata.py \
  --review-root <review-root>
```

It only fills fields that are currently empty and never already `human_checked`; it never overwrites a value that's already present. A Crossref match is only accepted at title-similarity >= 0.6, and if the returned `volume` is identical to the returned `year` (a real Crossref data quirk seen on some preprint-server DOIs), `volume` is dropped rather than printed, since a citation with `volume == year` is more likely to confuse a reader than help them.

Useful options:

```text
--paper-id P001
--limit 5
--force       (re-check and overwrite non-human-checked fields even if already present)
--sleep-seconds 1.0
```

Outputs:

```text
review-library/metadata/crossref_backfill_report.json
review-library/metadata/crossref_backfill_report.md
```

Not every paper will get a match — conference papers, preprints, and non-Crossref-indexed venues commonly return `no_match`. This is expected and does not indicate a bug.

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
volume
pages
doi
abstract
structured_tags
source_paths
extraction
human_review
quality
```

`volume` and `pages` are review-warning fields, not blocking: many papers (preprints, some conference papers) genuinely have neither. Leave them `not specified` rather than inventing one. They exist so `review-draft-merge-polish` can build a properly formatted journal-style reference list (see that skill's Reference List Format section).

Every extracted field should carry:

```text
value
source
confidence
human_checked
```

Use `human_review` for audit status and notes. Local paper retrieval uses only the seven values inside `structured_tags`; do not generate or rely on legacy `keywords`, `llm_tags`, `human_tags`, or category compatibility fields.

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
missing any of the seven structured tag keys
missing source PDF
missing Markdown
missing metadata JSON
invalid JSON
```

Treat these as review warnings:

```text
missing journal
missing volume
missing pages
missing DOI
missing structured_tags
structured tag value is not specified
llm_skipped_low_relevance (expected when --relevance-keywords filtered this paper out; not a defect -- retag it directly if it turns out to be relevant)
low confidence title
low confidence abstract
not human reviewed
```
