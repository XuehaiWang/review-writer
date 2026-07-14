---
name: review-topic-paper-discovery
description: Start a review project from a user topic, expand keywords, search local and web papers, and produce 20-30 candidate papers for human check.
---

# Review Topic Paper Discovery

Goal: from the user review topic, select `20-30` local candidate papers.

## Translate topic first

If the user's topic is not in English, translate it to English before running anything. All keyword inference, structured-tag matching, and web search (Crossref) in `discover.py` operate on English text — a non-English `--topic` will score near zero against local metadata and return no web results.

Pass the English translation as `--topic`. Keep the original topic wording available for the human-facing report/dashboard if useful, but the value passed to `discover.py` must be English.

## Project folder naming

Derive `--project-id` from the English-translated topic, slugified (lowercase, spaces/punctuation replaced with `-`). This becomes the project's folder name under `review-projects/<project_id>/`.

Prefer a short, readable slug (a handful of the topic's key words) over slugifying the entire topic sentence verbatim — use your own judgment to pick the words that best identify the project at a glance.

Before running, check whether `review-projects/<project_id>/` already exists:

```text
if review-projects/<project_id>/ does not exist:
    use <project_id> as-is
else:
    append _2, then _3, _4, ... until an unused folder name is found
    e.g. urban-heat-island-mitigation-strategies -> ..._2 -> ..._3
```

Never overwrite or reuse an existing project folder for a new topic.

## Keyword expansion (LLM step, required before running the script)

`discover.py` does not contain any hardcoded keyword-expansion rules — it has no built-in knowledge of any subject matter. Before running the script, the LLM must expand the (English) topic into a broader set of search keywords itself, using its own domain knowledge of whatever field the topic belongs to.

### Vocabulary-aware expansion (do this first)

Topic-side keywords and paper-side metadata are written at different times, so their phrasings drift ("silyl ethers oxidation" vs a paper tagged "silyl ethers"; "PMB ether" vs "p-methoxybenzyl"). Token-overlap scoring then misses on-topic papers. Close the gap by grounding the expansion in the library's actual vocabulary:

```bash
python3 <skill-root>/scripts/dump_library_vocabulary.py --review-root <review-root>
```

This writes `review-library/metadata/library_vocabulary.json` containing every distinct structured-tag value per category plus all paper titles. Read it before writing `agent_keywords.json`, and for every library phrasing that is semantically on-topic, add a keyword entry using the library's exact wording (reason: `"library vocabulary alias"`). Keep your own domain-knowledge expansions too — the library phrasings guarantee recall on papers already in the library; your expansions cover web search and future additions.

If the library has no metadata yet (fresh setup), skip this step and rely on domain-knowledge expansion alone.

Write the expansion to a JSON file as a flat list:

```json
[
  {"keyword": "<expanded keyword or phrase>", "category": "<one of the 8 categories below>", "reason": "<why this keyword is relevant to the topic>"}
]
```

Categories (fixed by the current metadata schema — see `review-metadata-prep`):

```text
output
input
method
co_input
modifier
process_type
document_scope
```

If the topic's subject matter does not map naturally onto these categories (e.g. a non-chemistry review), use your best judgment to assign the closest category, or `document_scope` as a neutral fallback — do not invent new category names, since scoring in `discover.py` only recognizes these eight.

Aim for 10-25 expanded keywords covering synonyms, subtopics, and adjacent terminology a domain expert would search for. Save the file, e.g. to `<review-root>/review-projects/<project_id>/00_discovery/agent_keywords.json`, and pass its path via `--agent-keywords`.

If `--agent-keywords` is omitted, discovery runs on `--keywords` alone (no expansion), which will likely under-populate the candidate set.

## Untagged papers and the two-pass flow (large libraries)

Papers whose structured tags are all `not specified` (rule-only or stub metadata) are NOT invisible to scoring: `discover.py` falls back to matching keywords against their title, abstract, and Markdown head with a weight comparable to tag matching. This enables the recommended large-library order — rule-only metadata first (free), discovery shortlist, then LLM-tag only the shortlist (see "Post-confirmation shortlist tagging" below and `review-metadata-prep` SKILL.md, "Two-Pass Tagging for Large Libraries").

## Post-confirmation shortlist tagging (automatic, agent-run — never ask the user to run anything)

After the human confirms the candidate set, check whether any selected (or promoted-from-borderline) papers still have all structured tags as `not specified`. If so, tag them now — automatically, as part of this skill's wrap-up, without asking the user to invoke anything:

```bash
python3 <review-root>/skills_versa/review-metadata-prep/scripts/llm_retag_metadata.py \
  --review-root <review-root> \
  --paper-ids-from <review-root>/review-projects/<project_id>/00_discovery/selected_discovery_results.json \
  --model <model> --base-url <base-url> --reasoning-effort high
```

`--paper-ids-from` restricts tagging to the confirmed `local_papers` (plus `borderline_papers`) — already-tagged papers are simply refreshed, unselected papers are never touched, so this is safe to run unconditionally after confirmation. If no API key is available, fill the shortlisted papers' tags yourself by reading each paper's front matter (agent-authored tags), as `review-metadata-prep` describes. Optionally re-run `discover.py` afterward for a tag-informed final cut; downstream stages (`review-literature-matrix-outline` onward) assume the selected papers are tagged.

## Classification rules (optional, safe to omit)

`--classification-rules` optionally points to a Python file defining a `rules` list of `(label, category, aliases)` tuples — synonyms for structured-tag values already present in the paper library's metadata (set during `review-metadata-prep`). This only widens keyword matching recall; it does not assign or validate tags itself.

There is no default file and none is required. If omitted, matching falls back to exact tag-value text, which works fine for most topics. Only create this file if you notice discovery missing local papers due to phrasing mismatches between search keywords and existing tag values (e.g. papers tagged with a formal term but searched with a colloquial synonym).

## Script location

```text
<skill-root>/scripts/discover.py
<skill-root>/scripts/sciatlas_client.py   (imported by discover.py; not run directly)
```

where `<skill-root>` is the directory containing this `SKILL.md` file.

## Run

```bash
python3 <skill-root>/scripts/discover.py \
  --topic "<review topic>" \
  --keywords "<optional user keywords>" \
  --project-id <project_id> \
  --paper-dir <path/to/paper/storage> \
  --agent-keywords <path/to/agent_keywords.json> \
  --sciatlas-search \
  --web-search
```

Always include at least one external source (`--sciatlas-search`, `--web-search`, or both). Without either, only local papers are searched and `web_papers` will be empty. See "External paper search" below for how the two sources combine.

Always include `--agent-keywords` pointing to the file produced in the keyword-expansion step above.

`--review-root` defaults to the current working directory. Output goes to `<review-root>/review-projects/<project_id>/00_discovery/` unless overridden with `--output-dir`.

## External paper search

`discover.py` supports two independent, combinable external sources for `web_papers` — use either, both, or neither:

```text
--sciatlas-search   hosted SciAtlas knowledge-graph search (/v1/search), hybrid retrieval
--web-search         Crossref bibliographic search
```

Both can run together: results are merged and deduplicated by DOI (falling back to URL, then title) via `merge_external_results()`. A paper found by both sources is kept once, with `sources` recording every source that returned it.

**SciAtlas** requires `SCIATLAS_API_KEY` (env var, `<review-root>/.env`, or `--sciatlas-api-key`). If the key is missing or the `/healthz` check fails, `discover.py` does not error — it records the reason in `web_results_by_keyword.json`'s `status` field (e.g. `missing_api_key`, `health_failed: ...`) and falls back to whatever other source is active. Useful flags:

```text
--sciatlas-limit        results per keyword (default 8)
--sciatlas-time-range    optional year range, e.g. "2018-2025"
--sciatlas-domain        optional domain hint, e.g. "organic chemistry" or "urban climate" -- helps SciAtlas's ranking, not required
--sciatlas-base-url      overrides SCIATLAS_API_BASE_URL
--sciatlas-timeout       HTTP timeout in seconds
```

**Crossref** (`--web-search`) needs no credentials and is a reasonable default when SciAtlas is not configured. `web_results_by_keyword.json`'s `source`/`sources`/`status` fields report exactly which source(s) actually contributed results for this run, so the human check can tell at a glance whether external search worked as intended.

### `--paper-dir`

Provide the path to the server's local paper storage directory. When given, `discover.py` will automatically scan that directory for PDF files and register any that are not yet in the library — before running keyword search and scoring.

**The LLM must determine this path from the server environment before running the command.** Look for directories containing `.pdf` files under common locations such as the server home directory, a mounted drive, or a path the user has mentioned. Do not hardcode a path — resolve it at runtime.

After auto-registration, stub metadata records are written with the PDF path filled in but bibliographic fields empty. Run `review-metadata-prep` after discovery to extract full metadata (title, authors, abstract, tags) via LLM.

To write outputs to a custom folder:

```bash
python3 <skill-root>/scripts/discover.py \
  --topic "<review topic>" \
  --project-id <project_id> \
  --paper-dir <path/to/paper/storage> \
  --agent-keywords <path/to/agent_keywords.json> \
  --web-search \
  --output-dir <path/to/output/folder>
```

If the user gives no `--keywords`, that is fine — the LLM-authored `--agent-keywords` file from the expansion step above is the primary source of search keywords.

## Required Output

Files written to the output directory:

```text
topic_input.md
keyword_set.draft.json
local_results_by_keyword.json
web_results_by_keyword.json
combined_results_by_keyword.json
selected_discovery_results.json
discovery_report.md
human_check_state.json
```

`selected_discovery_results.json` should contain `20-30` kept local papers when enough matches exist. If fewer than 20 are found, record why in `discovery_report.md`.

## Borderline papers (near-miss review band, required step)

The score threshold is not a cliff. Papers that fall below the selection cut but at or above a near-miss floor are written to `selected_discovery_results.json` under `borderline_papers` and listed in `discovery_report.md` under "Borderline Papers — review required". The script also prints a `[borderline]` line when any exist.

After every discovery run, the LLM must review this list before presenting the candidate set: read each borderline paper's title (and abstract from its metadata if the title is ambiguous), and promote clearly on-topic papers into `local_papers` — recording the promotion in the results file (e.g. `matched_keywords: ["agent_promoted_from_borderline"]`) so the change is auditable. Phrasing drift between topic keywords and paper metadata is the most common reason an on-topic paper lands here; a borderline entry is a request for judgment, not a rejection.

## Agent relevance check (required step, excludes false positives by default)

`discover.py` scores by keyword match, not topical understanding — a paper can clear the selection cut through a coincidental match (e.g. the keyword `"REALM"` matching the ordinary English word "realm" inside an unrelated paper) and land in `local_papers` looking exactly as confident as a genuine hit. Nothing in the scoring step judges whether a matched paper is actually about the review topic; that judgment must happen here, before the candidate set reaches the human.

After the borderline-promotion step above and before presenting `local_papers` to the human:

1. Read every paper's title in `local_papers` (and its metadata abstract if the title alone doesn't make relevance obvious).
2. For each paper the LLM judges is **not actually about the review topic** — regardless of which keyword(s) it matched or how high its score is — move it out of `local_papers` into a new `excluded_papers` array in `selected_discovery_results.json`. Each moved entry keeps its original fields plus `"excluded_reason"`: a short note naming why (e.g. `"matched only via 'REALM' colliding with the unrelated English word 'realm'; paper is about copper-catalyzed allene synthesis"`).
3. This is an exclude-by-default step, not a delete: excluded papers stay fully recorded in the file, nothing is discarded, and every exclusion must be auditable from its `excluded_reason`.
4. Add an "## Agent-Excluded Papers — confirm before finalizing" section to `discovery_report.md`, listing each excluded paper with its reason, mirroring the borderline-papers report format.
5. When handing the candidate set to the user for the human check below, explicitly list the excluded papers and their reasons, and ask whether the user wants any of them moved back into `local_papers` before confirming. Do not silently drop this list — surfacing it is mandatory even though the default action was exclusion.

This step needs no API key or script; it is the same kind of agent judgment call already used for borderline promotion, just applied in the opposite direction (removing false positives instead of recovering false negatives).

## Human Check

Stop after discovery. The human checks `/discovery`, deletes irrelevant keywords/papers, reviews any remaining borderline papers, and confirms the candidate set. Once confirmed, run the post-confirmation shortlist tagging step above automatically before handing off to the next stage.
