---
name: review-online-paper-discovery
description: Start a review project from a user topic, search Crossref/SciAtlas for candidate papers, let a human confirm the shortlist, resolve a download source for each, then register the human's manually-downloaded PDFs into the shared library.
---

# Review Online Paper Discovery

Goal: from the user review topic, find candidate papers via online search (Crossref/SciAtlas), get human confirmation, resolve where each confirmed paper's PDF can be found, then — once the human has downloaded and placed the PDFs somewhere — register them into `review-library`.

Downloading is manual, not automated: auto-fetch was tried and removed, because it almost never succeeds against mainstream paywalled journals or Cloudflare-protected preprint servers (see "Phase 2" below for why). What this skill still does well is the two things that *aren't* the download itself — resolving where a PDF might be found, and correctly registering it once a human has it.

This skill only grows the shared PDF corpus — it does not score papers already in the local library (that's `labkag-review-skill`'s job) and it does not produce a review-quality candidate shortlist for one specific review (see "Boundary with LabKAG" below).

## Translate topic first

If the user's topic is not in English, translate it to English before running anything. Keyword inference and web search (Crossref/SciAtlas) in `discover.py` operate on English text — a non-English `--topic` will return poor results.

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

## Topic decomposition and disambiguation (LLM step, do this first)

Before expanding keywords, break the topic into its constituent concepts and check for ambiguous terms (an abbreviation or phrase with more than one plausible meaning in the relevant domain). See `references/topic_decomposition_prompt.md` for the full method — in short: decompose into concepts, and for anything ambiguous, run a quick `discover.py probe` search (see below) to see which meaning the actual literature supports before committing to it, rather than guessing from general knowledge. Ask the user only if probe evidence is genuinely inconclusive.

## Keyword expansion (LLM step, required before running the script)

`discover.py` does not contain any hardcoded keyword-expansion rules — it has no built-in knowledge of any subject matter. Before running the script, the LLM must expand the (English, disambiguated) topic into a broader set of search keywords itself, using its own domain knowledge of whatever field the topic belongs to. See `references/keyword_expansion_prompt.md` for the expected output shape.

Write the expansion to a JSON file as a flat list:

```json
[
  {"keyword": "<expanded keyword or phrase>", "reason": "<why this keyword is relevant to the topic>"}
]
```

Aim for 10-25 expanded keywords covering synonyms, subtopics, and adjacent terminology a domain expert would search for. Save the file, e.g. to `<review-root>/review-projects/<project_id>/00_discovery/agent_keywords.json`, and pass its path via `--agent-keywords`.

If `--agent-keywords` is omitted, discovery runs on `--keywords` alone (no expansion), which will likely under-populate the candidate set.

## Script location

```text
<skill-root>/scripts/discover.py
<skill-root>/scripts/sciatlas_client.py   (imported by discover.py; not run directly)
```

where `<skill-root>` is the directory containing this `SKILL.md` file.

## Three-phase flow (plus an optional pre-flight probe)

0. **`probe`** (optional, before Phase 1) — a lightweight, project-agnostic search for one term, used to disambiguate an ambiguous topic term (see "Topic decomposition and disambiguation" above). Writes nothing, needs no `--project-id`.
1. **`search`** — query Crossref/SciAtlas, aggregate and score results into candidates, write a report, and stop for human review/confirmation.
2. **`list-for-download`** (automatic, agent-run after confirmation) — resolve a PDF source for every confirmed, non-excluded candidate and write a report. Nothing is fetched or written into any paper folder; this is information for a human to act on.
3. **`register-pdfs`** (agent-run once PDFs exist) — after the human has manually downloaded PDFs and placed them in a folder of their choosing, scan that folder and register each one against its candidate metadata into `review-library`.

## Phase 0: probe (disambiguation)

```bash
python3 <skill-root>/scripts/discover.py probe \
  --query "<candidate meaning or term in context>" \
  --web-search --limit 8
```

At least one of `--sciatlas-search`/`--web-search` is required, same as `search`. Prints the top results' year/journal/title to stdout — nothing is written to disk. Use it once per candidate meaning of an ambiguous term; compare what comes back.

## Phase 1: search

```bash
python3 <skill-root>/scripts/discover.py search \
  --topic "<review topic>" \
  --keywords "<optional user keywords>" \
  --project-id <project_id> \
  --agent-keywords <path/to/agent_keywords.json> \
  --sciatlas-search \
  --web-search
```

At least one of `--sciatlas-search`/`--web-search` is required — with neither, the command exits with an error rather than silently writing an empty report.

`--review-root` defaults to the current working directory. Output goes to `<review-root>/review-projects/<project_id>/00_discovery/` unless overridden with `--output-dir`.

## External paper search

`discover.py` supports two independent, combinable external sources — use either or both:

```text
--sciatlas-search   hosted SciAtlas knowledge-graph search (/v1/search), hybrid retrieval
--web-search         Crossref bibliographic search
```

Both can run together: results are merged and deduplicated by DOI (falling back to URL, then title) via `merge_external_results()`. A paper found by both sources is kept once, with `sources` recording every source that returned it. Each result carries a `pdf_url` field when the source itself exposed a direct PDF link (Crossref's `link` array with a `pdf` content-type, or SciAtlas's own `pdf_url`) — kept separate from `url` (the landing page), since Phase 2's download step needs to know specifically "this is downloadable," not just "this is *a* link."

**SciAtlas** requires `SCIATLAS_API_KEY` (env var, `<review-root>/.env`, or `--sciatlas-api-key`). If the key is missing or the `/healthz` check fails, `discover.py` does not error — it records the reason in `online_search_results_by_keyword.json`'s `status` field (e.g. `missing_api_key`, `health_failed: ...`) and falls back to whatever other source is active. Useful flags:

```text
--sciatlas-limit        results per keyword (default 8)
--sciatlas-time-range    optional year range, e.g. "2018-2025"
--sciatlas-domain        optional domain hint, e.g. "organic chemistry" or "urban climate" -- helps SciAtlas's ranking, not required
--sciatlas-base-url      overrides SCIATLAS_API_BASE_URL
--sciatlas-timeout       HTTP timeout in seconds
```

**Crossref** (`--web-search`) needs no credentials and is a reasonable default when SciAtlas is not configured.

## Year filtering

If the topic text contains an explicit recency phrase ("past 5 years", "近5年"), `discover.py` parses it automatically and applies it as a real Crossref date filter (`from-pub-date`/`until-pub-date`), not just a scoring bonus. Override or set this explicitly with `--year-from`/`--year-to` (each an integer year; omit or pass `0` for unbounded). `online_search_report.md` states the effective year range applied.

## Required Output (Phase 1)

```text
online_search_topic.md
online_search_keywords.json
online_search_results_by_keyword.json
online_search_candidates.json
online_search_report.md
online_search_human_check_state.json
```

## Borderline candidates (near-miss review band, required step)

The score threshold is not a cliff. Candidates that fall below the confident-candidate cut but at or above a borderline floor are written to `online_search_candidates.json` under `borderline_candidates` and listed in `online_search_report.md` under "Borderline Candidates — review required". The script prints a `[borderline]` line when any exist.

After every search run, the LLM must review this list before presenting the candidate set: read each borderline candidate's title (and abstract if available), and promote clearly on-topic candidates into `candidates` — recording the promotion (e.g. adding `"agent_promoted_from_borderline"` to `matched_keywords`) so the change is auditable. Phrasing drift between topic keywords and the paper's own title/abstract is the most common reason an on-topic paper lands here; a borderline entry is a request for judgment, not a rejection.

## Agent relevance check (required step, excludes false positives by default)

`discover.py` scores by keyword/text match, not topical understanding — a candidate can clear the selection cut through a coincidental match and land in `candidates` looking exactly as confident as a genuine hit. Nothing in the scoring step judges whether a matched paper is actually about the review topic; that judgment must happen here, before the candidate set reaches the human.

After the borderline-promotion step above and before presenting `candidates` to the human:

1. Read every candidate's title in `candidates` (and its abstract, if the title alone doesn't make relevance obvious).
2. For each candidate the LLM judges is **not actually about the review topic** — regardless of which keyword(s) it matched or how high its score is — move it out of `candidates` into a new `excluded_candidates` array in `online_search_candidates.json`. Each moved entry keeps its original fields plus `"excluded_reason"`: a short note naming why.
3. This is an exclude-by-default step, not a delete: excluded candidates stay fully recorded in the file, nothing is discarded, and every exclusion must be auditable from its `excluded_reason`. `download` respects `excluded_candidates` — anything listed there is never downloaded, even if it was left in `candidates` too.
4. Add an "## Agent-Excluded Candidates — confirm before finalizing" section to `online_search_report.md`, listing each excluded candidate with its reason.
5. When handing the candidate set to the user for the human check below, explicitly list the excluded candidates and their reasons, and ask whether the user wants any of them moved back into `candidates` before confirming.

## Human Check

Stop after Phase 1. The human checks `/discovery`, deletes irrelevant keywords/candidates, reviews any remaining borderline candidates, and confirms the candidate set (writes `online_search_human_check_state.json`'s `status: "confirmed"`). Once confirmed, run Phase 2 (`list-for-download`) automatically, then stop again — Phase 3 (`register-pdfs`) needs the human to have actually downloaded files first, so it cannot be run in the same turn.

## Phase 2: resolve a PDF source for each confirmed candidate

```bash
python3 <skill-root>/scripts/discover.py list-for-download \
  --review-root <review-root> \
  --project-id <project_id>
```

Refuses to run (exits with an error) unless `online_search_human_check_state.json`'s `status` is `"confirmed"` — pass `--allow-unconfirmed` only if the human check is being deliberately skipped.

For each confirmed, non-excluded candidate (after skipping ones already in the library): use the candidate's own `pdf_url` if present (no network call); otherwise, if it has a DOI, query the free [Unpaywall](https://unpaywall.org/) API for an open-access location. No OA copy found is reported as `no_pdf_source_found`, not an error — most papers found via Crossref will not have one, and that's expected, not a failure. **Nothing is fetched.** The report lists each resolved paper's URL and a suggested filename (a slug from its title) for the human to use, purely as a convenience — `register-pdfs` also matches by title, so renaming is fine.

**Expect most candidates to end up `no_pdf_source_found`, and expect the ones that do resolve to still usually need manual download.** Two real, observed limitations, not implementation bugs:

- **Mainstream paywalled journals have low OA/preprint deposit rates.** For a typical synthetic-chemistry topic (ACS/Wiley/RSC journals), it is normal for the large majority of confirmed candidates to have no Unpaywall-known open-access copy at all.
- **Preprint servers behind Cloudflare (e.g. ChemRxiv) block scripted downloads even when Unpaywall correctly reports an open, CC-licensed PDF URL for them.** The URL resolves fine, but the file-serving domain returns a JS bot-challenge page to any scripted request instead of the PDF — this is why automated fetching was removed rather than patched (working around it would be bot-detection bypass). The resolved URL is still exactly what a human needs: open it in an ordinary browser (which passes the challenge normally) and save the PDF in seconds.

Useful flags: `--limit N` (cap how many candidates this run attempts), `--unpaywall-base-url`/`--unpaywall-timeout`, `--delay` (politeness delay between lookups).

## Required Output (Phase 2)

```text
online_search_manual_download_list.json
online_search_manual_download_list.md
```

## Phase 3: register manually-downloaded PDFs

Run once the human has downloaded some or all of the resolved papers and placed the PDFs in a folder of their choosing — anywhere on disk, a Downloads folder, another drive, wherever. Nothing about this skill assumes PDFs live under `review-library/`; that would fight how people actually acquire papers.

```bash
python3 <skill-root>/scripts/discover.py register-pdfs \
  --review-root <review-root> \
  --project-id <project_id> \
  --paper-pdf-dir <folder the human put the PDFs in> \
  --mineru-output-dir <folder with this batch's MinerU markdown/extracted output>
```

`--paper-pdf-dir` is required, with no default — always ask the human where they put the files rather than assuming `review-library/paper_pdf/`. `--mineru-output-dir` defaults to `<review-root>/review-library/mineru-outputs`: unlike the raw PDFs, MinerU's parsed output is derived/processed data that belongs inside the review-library workspace, so a predictable default is appropriate there even though it isn't for the PDFs themselves.

For each PDF in `--paper-pdf-dir` not already in the registry:

1. **Match to a candidate**, in order: (a) filename — does the file's name match a `suggested_filename` from Phase 2's list, resolved back to that candidate's metadata; (b) title, as a fallback for renamed files — compute the PDF's MinerU slug from its own filename, check whether `--mineru-output-dir` already has a parsed markdown for that slug, and if so match its extracted title (`# heading`) against every candidate's title (exact-normalized, then token-overlap ≥0.6). A file that matches neither way is reported as **unmatched** and left unregistered — never guessed.
2. **Register**: write a stub `review-library/metadata/papers/<paper_id>.metadata.json` (seeded with the matched candidate's title/authors/year/journal/doi/abstract, `structured_tags` left `"not specified"`, `source_paths.markdown` set if the title-match path found one) and a `review-library/registry/papers.jsonl` entry.

Re-running `register-pdfs` is safe and expected — it's meant to be run again each time more PDFs land in the folder (`registered_pdf_paths()` skips files already registered by absolute path). Running it *after* MinerU parsing has completed for this batch (rather than before) is what lets the title-match fallback actually find markdown on the first pass, instead of leaving `source_paths.markdown` empty for anything not matched by filename.

## Required Output (Phase 3)

```text
online_search_register_report.json
```

## Boundary with LabKAG

This skill only grows the review-library PDF corpus (registered via `register-pdfs`, wherever the human chose to keep the actual files) and writes `online_search_*` files under `00_discovery/`. It does **not** produce `selected_discovery_results.json`, `combined_results_by_keyword.json`, or `human_check_state.json` — the canonical filenames `review-literature-matrix-outline` reads. Those are produced separately, later, by `labkag-review-skill`'s workflow 1 (ingest + taxonomy + tag) and workflow 2 (`match-topic` + `export_discovery_format.py`), once the corpus this skill grew has been ingested into LabKAG. Running this skill is a corpus-acquisition prerequisite for that step, not a substitute for it.
