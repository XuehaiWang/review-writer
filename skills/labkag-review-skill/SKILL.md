---
name: labkag-review-skill
description: "API wrapper skill for LabKAG. Use when an agent needs to call the LabKAG HTTP API through the bundled CLI script for health checks, upload, extraction, ingestion, literature query, evidence search, knowledge inspection, taxonomy management, or topic-based paper discovery. Two separately-invocable workflows: paper storage building (ingest + taxonomy + tagging) and paper discovery/recommendation (topic match against an already-built project)."
---

# LabKAG API Wrapper

Use this skill when you need to call LabKAG as an external service.
Do not modify the LabKAG repository.

This skill folder lives inside the LabKAG project itself
(`labkag-review-skill/`) — it is self-contained and should not be confused
with any other `labkag-skill`-named folder elsewhere on disk. All paths
below are relative to this project's root.

## Two Separate Workflows

This skill does two genuinely different jobs, and they are meant to be
invoked independently, not as one fused pipeline:

1. **[Paper storage building](#workflow-1-paper-storage-building)** — ingest
   a corpus, build or rebuild its taxonomy, tag every paper, report the
   build's actual performance. Run this once per project (and again
   whenever the corpus grows or the taxonomy needs revisiting).
2. **[Paper discovery / recommendation](#workflow-2-paper-discovery--recommendation)**
   — given an already-built project, decompose a topic and find matching
   papers. Run this as many times as needed against the same project,
   for as many different topics as the user asks about.

Workflow 2 depends on workflow 1 having run at least once (`taxonomy-get`
must return something) but not the other way around — never start ingesting
a whole new corpus just because the user asked a topic question; check
whether the project is already built first.

## Call Pattern

Prefer the bundled script:

```text
py -3.10 labkag-review-skill/scripts/labkag_api.py <command> [args]
```

Default base URL:

```text
http://127.0.0.1:8001
```

Override with `--base-url` or `LABKAG_BASE_URL`.

## Commands

- `health`
- `upload --file <pdf>`
- `extract --file-id <id> [--project-id <id>] [--extract-level basic|detailed] [--return-chunks] [--use-backup]`
- `ingest --paper-extraction <json-file> [--project-id <id>] [--confirm]`
- `query --question <text> [--project-id <id>] [--paper-id <id>] [--top-k N]`
- `search --query <text> [--project-id <id>] [--paper-id <id>] [--top-k N]`
- `knowledge --paper-id <id> [--project-id <id>] [--include-evidence / --no-include-evidence]`
- `papers-list --project-id <id>`
- `taxonomy-get --project-id <id>`
- `taxonomy-set --project-id <id> --taxonomy <json-file> [--confirm]`
- `match-topic --project-id <id> --plan <json-file> [--min-essential-signals N] [--no-borderline] [--limit N]`
- `batch-extract --input-dir <folder> --extractions-dir <folder> --project-id <id> [--extract-level basic|detailed] [--mineru-output-dir <dir>] [--limit N] [--force]`
- `batch-ingest --extractions-dir <folder> --project-id <id> [--limit N] [--force]`

## Minimal Rules

- Call `health` before other actions when the service state is unknown.
- Treat non-2xx responses as service failures and inspect the returned JSON body.
- `extract` requires the configured LLM provider. `--use-backup` reads the PyMuPDF
  fallback (`data/parsed_backup/`) instead of MinerU output for that file.
- `ingest` writes to Neo4j only when `--confirm` is set.
- `search` and `query` should prefer embedding when the service is configured for it.

## Workflow 1: Paper Storage Building

**Goal**: get a corpus of PDFs into a project with a taxonomy that actually
fits it, and every paper tagged against that taxonomy. Run this once to
build a project, and again whenever the corpus grows or the taxonomy needs
revisiting — it never needs a topic to run.

### 1a. Ingest the corpus

`batch-extract` and `batch-ingest` process many PDFs at once. Both are
resumable: they write a manifest after every paper and skip
already-succeeded ones on re-run (`--force` reprocesses anyway).

`batch-extract` uploads and extracts every PDF under `--input-dir`, caching
each successful `paper_extraction` JSON in `--extractions-dir` **on this
machine** — not assumed to share a filesystem with wherever the backend
runs, since this skill may be calling a remote `--base-url`. Pass
`--mineru-output-dir` to reuse a pre-parsed MinerU batch (see
`mineru_batch_parse.py` in the LabKAG repo) instead of re-parsing.

`batch-ingest` reads every `paper_extraction` JSON cached by a prior
`batch-extract` run from `--extractions-dir` and ingests each one via
`POST /v1/papers/ingest?confirm=true`. Ingest-time dedup (by DOI, then
title) means re-running `batch-ingest` on the same corpus never creates
duplicate Paper nodes.

```text
batch-extract --input-dir <pdfs> --extractions-dir <cache> --project-id <id>
batch-ingest  --extractions-dir <cache> --project-id <id>
```

Run `batch-extract` again any time to pick up newly added PDFs — it only
processes what's not already in its manifest.

### 1b. Build (or rebuild) the taxonomy

`taxonomy-get --project-id <id>`. If it returns nothing, follow
`references/taxonomy_bootstrap_prompt.md` to propose one, then
`taxonomy-set` it. **Do not skip its two ask-before-building checks** — this
is the "ask for review size and resolve ambiguity before building" step:

- Step 2: ask the user roughly how many papers a *typical* query against
  this project should return (handful / moderate / broad). One-time input
  that sizes the taxonomy's category count and value granularity — not
  re-asked per topic (that's a separate, per-topic question in workflow 2).
- Step 4: after reading a first ~10-paper sample, check in with the user
  once more before finalizing categories — propose candidate classification
  axes (catalyst? reaction type? substrate? application?) and surface any
  term the sample left ambiguous, so the taxonomy ends up organized around
  the axis the user actually wants to search by, not just whatever framing
  you inferred alone.

If a taxonomy already exists and this is a revision, `taxonomy-set` reports
`affected_papers_count` for breaking edits without applying them — read that
count before resubmitting with `--confirm` (see error-code remediation
below).

### 1c. Tag every paper

```text
python scripts/backfill_taxonomy_tags.py --project-id <id>
```

Retags every paper whose `taxonomy_version` doesn't match the taxonomy's
current version (i.e. everything after a first bootstrap, or everything
touched by a breaking edit). Resumable and checkpointed per paper — safe to
re-run if it's interrupted partway through.

### 1d. Report the build's actual performance

**Before considering storage building done, report back to the user**, not
just a silent "done":

- The backfill script's own summary line (`Retagged: N  Failed: N`
  `Skipped/total scanned: N`) — state it plainly, including any failures.
- If validation tooling is available for this project, run it and report
  warnings/blocking issues, not just a pass/fail count.
- Name anything that looks like a data-quality gap worth flagging (e.g. a
  cluster of papers that never resolved a required field, or a taxonomy
  category that ended up with very low coverage across the corpus) — this
  is the point where such gaps are cheapest to catch, before anyone runs a
  topic query against this project.

Only after reporting this should you offer to move into workflow 2 (paper
discovery) — as a next step to offer, not something to auto-continue into
without the user weighing in.

## Workflow 2: Paper Discovery / Recommendation

**Precondition**: the project must already be built (workflow 1 has run at
least once — `taxonomy-get --project-id <id>` returns a taxonomy). If it
doesn't, stop and go do workflow 1 first; do not attempt to decompose a
topic against a project with no taxonomy.

**The LabKAG backend never calls an LLM at query time.** Retrieval — vector
search, graph traversal, corroboration scoring — is deterministic. Any step
that requires understanding a topic happens here, in you, before you call
the backend. The backend only validates what you send it and executes the
match.

This workflow can run as many times as needed against the same built
project, for as many different topics as the user asks about — it never
touches the taxonomy or re-ingests anything.

1. Follow `references/topic_decomposition_prompt.md` to turn the topic into
   a `topic_plan.json` file, using the project's existing taxonomy
   (`taxonomy-get` to read it first). That doc's step 0 has you resolve any
   topic-specific ambiguity (a term with more than one plausible meaning —
   corpus-evidence-first, only asking the user if genuinely inconclusive)
   *and* ask the expected result size for this specific topic (handful /
   moderate / broad, or a specific number) in one combined check — it tunes
   `essential` marking and the
   `--min-essential-signals`/`--no-borderline`/`--limit` flags for the
   match-topic call in step 2, never the actual match results. If the user
   gave a specific number, also pass it as `--target-count N`.
2. `match-topic --project-id <id> --plan topic_plan.json [--target-count N]`.
   `--target-count` never truncates or pads — `confirmed`/`borderline` are
   always returned in full — it adds `summary.suggested_confirmed_count`/
   `suggested_borderline_count`, a rank-based cutoff (by `match_score`,
   corroboration strength) found at the steepest score drop near N, not
   just at the Nth item. Every `MatchedPaper` also carries `match_score` and
   `embedding_score`; both tiers are pre-sorted by `match_score` descending
   (embedding only breaks ties), so you can always work toward a target size
   from the ranking instead of guessing a threshold.
3. Report `confirmed` and `borderline` results separately — never merge
   them. A `confirmed` result cleared a two-signal corroboration bar; a
   `borderline` result cleared only one and needs further judgment before
   being treated as a real match. Never pad a small `confirmed` set with
   `borderline` results to hit a target count — say how many were found and
   why, instead (use the suggested-cutoff numbers from step 2 if you have
   them, but always state each tier's true total too). Relay each result's
   `reasons` entries to the user
   **verbatim** rather than re-deriving your own explanation — the backend
   already produces a human-readable reason per match (e.g. citing shared
   evidence for a co-occurrence hit), and re-paraphrasing risks
   misrepresenting *why* something was confirmed. A small or empty
   `confirmed` list is an expected, correct outcome when the corpus
   genuinely lacks papers combining the topic's essential concepts together
   — do not treat it as a bug to work around by loosening
   `--min-essential-signals` without telling the user why.

### When results fall short of the target

If `--target-count N` was given and `confirmed_count + borderline_count < N`
(the backend's cutoff suggestion will be "take everything" —
`suggested_confirmed_count == confirmed_count` and
`suggested_borderline_count == borderline_count`), that is a genuine
shortfall, not a bug, and not something to silently work around. Report the
gap plainly (actual vs. target) and let the **user** choose how to proceed —
each option below trades something real, so it is their call, not yours to
make on their behalf:

1. **Accept the actual count.** The corpus doesn't support more papers at
   this corroboration strength — this is a correct, complete answer, not a
   partial one.
2. **Loosen the match.** Lower `--min-essential-signals`, or move a concept
   from `essential` to supporting in the plan. Trades precision for recall —
   more papers qualify, but some will be weaker matches. Say explicitly
   which knob you'd change and what you'd expect it to do before doing it.
3. **Broaden the topic itself.** Drop a narrowing concept from the plan
   (e.g. a specific substrate class) so the *topic* covers more ground. This
   changes what's actually being searched for, not just how strictly it's
   matched — confirm the reworded topic with the user in their own words
   before resubmitting, don't just silently widen the plan.
4. **Check tagging quality before concluding the corpus lacks papers.** A
   shortfall can also mean relevant entities exist but weren't tagged
   (e.g. titles are obviously on-topic but the taxonomy tag never landed on
   any of their entities). If several `excluded` or thin-`borderline`
   papers look like they should plausibly match on a skim of their titles,
   consider a `backfill_taxonomy_tags.py --force` retag pass or refining the
   taxonomy's `aliases` for the relevant category before treating the
   shortfall as a real corpus limit.
5. **Expand the corpus.** Out of scope for a single topic query, but worth
   naming if it's plausibly the actual cause (e.g. the corpus predates the
   topic's year range, or the relevant papers were never ingested).

Never pick one of these unilaterally to hit the number — present the
shortfall and the options, then act on what the user actually chooses.

`taxonomy_bootstrap_prompt.md` and `topic_decomposition_prompt.md` are the
LLM-to-backend contract boundary for workflows 1 and 2 respectively. Do not
skip their verification rules (never invent a taxonomy category/value that
doesn't exist, never submit a plan with zero essential concepts, compute the
year filter correctly yourself) — the backend enforces them, and a plan that
violates them is rejected rather than silently corrected.

### Error-code remediation

Spans both workflows — some of these are workflow 1 (taxonomy) concerns,
others workflow 2 (match) concerns:

- `TAXONOMY_NOT_CONFIGURED` — no taxonomy exists for this project yet. This
  means workflow 1 hasn't run for it; go do that (its `1b`) before
  attempting any topic decomposition.
- `TOPIC_UNRESOLVED` — the submitted plan had zero essential concepts. Read
  the `unresolved` list in the error detail and either resolve it using the
  project's taxonomy or ask the user to clarify the topic. Never resubmit
  with a guessed concept.
- `GRAPH_QUERY_FAILED` — a backend/Neo4j failure, not a problem with your
  plan. Treat it like any other 5xx service failure.
- `POST /v1/projects/{id}/taxonomy` can return `status: "needs_review"`
  (not an error) with `applied: false` and `affected_papers_count` — this
  means the edit is breaking (removes/renames a value already in use). Read
  the count, decide whether to proceed, then resubmit with `--confirm` if
  so. Never pass `--confirm` reflexively on every call — it should follow a
  deliberate read of `affected_papers_count`.

### Interop with review-writer's discovery format

`review-online-paper-discovery` (the review-writer skill that used to own
this whole file set) now only searches Crossref/SciAtlas and downloads PDFs
into `review-library/paper_pdf/` — it no longer scores or shortlists papers
already in the local library. This script (`export_discovery_format.py`) is
therefore the **sole** producer of `review-projects/<project_id>/00_discovery/
selected_discovery_results.json` and its siblings, for any downstream stage
that expects them (e.g. `review-literature-matrix-outline`, which reads
`selected_discovery_results.json` + `topic_input.md` and opens
`review-library/metadata/papers/<paper_id>.metadata.json` per candidate).
Export a saved `match-topic` response into that exact format:

```text
py -3.10 labkag-review-skill/scripts/export_discovery_format.py \
  --match-result <saved match-topic response.json> \
  --review-root <review-writer-style root, has review-library/> \
  --discovery-project-id <project-id under review-projects/> \
  --group-by method
```

This writes all 9 files a review-writer discovery stage's downstream consumers expect
(`topic_input.md`, `query_plan.draft.json`, `keyword_set.draft.json`,
`local_results_by_keyword.json`, `web_results_by_keyword.json`,
`combined_results_by_keyword.json`, `selected_discovery_results.json`,
`discovery_report.md`, `human_check_state.json`), so any consumer built
against that format runs unmodified regardless of which tool produced the
candidates.

Read `export_discovery_format.py`'s module docstring before trusting the
output blindly — it documents the real limitations, not just the happy path:
- **Paper-ID join is by normalized title**, since `MatchedPaper` carries no
  DOI. Any paper whose title can't be matched (exact-normalized or
  token-overlap ≥0.6) to a `review-library/registry/papers.jsonl` entry is
  **excluded from the output and printed**, never guessed. Check the printed
  unresolved list every run.
- **`best_score` is a rescaled approximation** (`match_score / 4.0`, clamped
  to 1), a corroboration-strength count, not a keyword-overlap score — fine
  for sorting/display, not for literal comparison against any other tool's score.
- **`filter_stats`'s year-exclusion counts are not separable** from LabKAG's
  combined `excluded_count`, so they're left at 0 rather than fabricated.
- The taxonomy-category → structured-tag-category mapping
  (`DEFAULT_CATEGORY_MAP`) is a small, editable dict targeting review-writer's
  current domain-agnostic 7-category schema (`output`/`input`/`method`/
  `co_input`/`modifier`/`process_type`/`document_scope`, defined in
  `discover.py`'s `STRUCTURED_TAG_KEYS`); categories with no natural match
  fall back to `document_scope` as a neutral, low-signal category.

## Output Handling

- Print or return the raw JSON response when possible.
- If the API returns a structured error body, surface the `status`, first error `code`, and message.
- Do not infer success from HTTP status alone; inspect the JSON payload.
