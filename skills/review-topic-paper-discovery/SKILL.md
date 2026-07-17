---
name: review-topic-paper-discovery
description: Start a review project from a user topic, expand keywords against the eight LLM allene classification tags, retrieve local candidates from the metadata library, and optionally enrich with the hosted SciAtlas knowledge-graph search; produce 20-30 candidate papers for human check.
---

# Review Topic Paper Discovery

Goal: from the user review topic, select `20-30` local candidate papers and
keep an external evidence pool from SciAtlas for the matrix stage.

## Hard Rules

```text
Use only the 8 LLM structured tag categories for local retrieval:
product
substrate
catalyst_or_method
organometallic_partner
ligand_or_chiral_source
leaving_group
reaction_type
document_scope
```

Use `/home/ps/review-writer/allene_classification_rules.py` as the tag
vocabulary and synonym source. Do not rank local papers by metadata abstract.

External retrieval (both run in parallel when requested):

```text
SciAtlas /v1/search    enabled by --sciatlas-search (KG-grounded)
Crossref title search  enabled by --web-search       (open metadata)
none                   default when no flag is passed
```

When both flags are set, results are merged per keyword and de-duplicated by
DOI / URL / normalized title. Each merged record carries `sources` (e.g.
`['sciatlas']`, `['crossref']`, or `['sciatlas','crossref']`) and `source` is
the joined label for quick reading.

## Run

Before invoking the script, Codex must resolve the Topic using
`references/keyword_expansion_prompt.md` and write the query plan to:

```text
review-projects/<project-id>/00_discovery/query_plan.draft.json
```

For every resolved abbreviation, record an LLM confidence score and reason.
Put ambiguous concepts in `unresolved_concepts` rather than guessing, then
review them before discovery. Proceed only if other resolved concepts or
validated keywords still define a meaningful search; stop and ask for
clarification when the plan contains unresolved concepts only.

Convert relative-year instructions to inclusive local limits in
`filters.year_from` and `filters.year_to` using the current calendar year.
Record organization requests such as "by catalyst type" in `group_by` as
`["catalyst_or_method"]`, not as generic retrieval keywords.

Invoke the local discovery boundary with the generated plan:

```bash
python skills/review-topic-paper-discovery/scripts/discover.py \
  --review-root <review-root> \
  --topic "<review topic>" \
  --project-id <project-id> \
  --query-plan review-projects/<project-id>/00_discovery/query_plan.draft.json
```

Add `--sciatlas-search`, `--web-search`, or both to that command when external
coverage is requested. For SciAtlas KG, configure the service and append its
search controls:

```bash
export SCIATLAS_API_BASE_URL=http://sciatlas.openkg.cn
export SCIATLAS_API_KEY=sciatlas_xxx     # required for /v1/search

python skills/review-topic-paper-discovery/scripts/discover.py \
  --review-root <review-root> \
  --topic "<review topic>" \
  --project-id <project-id> \
  --query-plan review-projects/<project-id>/00_discovery/query_plan.draft.json \
  --sciatlas-search \
  --sciatlas-limit 8 \
  --sciatlas-time-range 2015-2025 \
  --sciatlas-domain "organic chemistry"
```

`--sciatlas-time-range` is only a hint for the external SciAtlas search. Local
metadata is filtered independently and inclusively by `filters.year_from` and
`filters.year_to` from `query_plan.draft.json`. The external hint does not
replace or alter the local query-plan year bounds.

Direct script execution without `--query-plan` retains the deterministic
fallback for compatibility, but Codex and discovery agents must use the query
plan handoff. Every keyword category and `group_by` value must be one of the
eight structured tag categories above.

## External Source: SciAtlas

SciAtlas is a hosted scientific knowledge graph. The skill calls
`POST /v1/search` once per expanded keyword with these defaults:

```text
retrieval_mode  hybrid
top_keywords    0
max_titles      0
max_refs        0
bias_exploration low
ranking_profile  precision
```

Per-keyword time range / domain hints come from CLI flags. Returned papers are
normalized into the same shape as Crossref results so the dashboard can render
both: `title, authors, year, journal, doi, url, abstract, score (0..1),
raw_score, source="sciatlas"`.

Auth:

```text
Authorization: Bearer $SCIATLAS_API_KEY
X-API-Key:     $SCIATLAS_API_KEY
```

Health check before searching:

```bash
curl -s http://sciatlas.openkg.cn/healthz
```

If SciAtlas health or auth fails, the script records the failure in
`web_results_by_keyword.json.status` and continues with local-only retrieval.

## Required Output

Write under:

```text
review-projects/<project_id>/00_discovery/
```

Required files:

```text
topic_input.md
query_plan.draft.json
keyword_set.draft.json
local_results_by_keyword.json
web_results_by_keyword.json
combined_results_by_keyword.json
selected_discovery_results.json
discovery_report.md
human_check_state.json
```

`web_results_by_keyword.json.source` is `sciatlas`, `crossref`, `sciatlas+crossref`, or `none`. Per-result rows carry a `sources` array so you can see which sources contributed.
`selected_discovery_results.json` should contain `20-30` kept local papers
when enough matches exist. External (SciAtlas/Crossref) papers go into
`web_papers`; they are a topic-coverage check pool only. They never enter
the local `paper_id` registry and the matrix stage may cite them only as
references without assigning a `paper_id`. If fewer than 20 local papers
are found, record why in `discovery_report.md`.

## Human Check

Stop after discovery. The human checks `/discovery`, deletes irrelevant
keywords/papers, and confirms the candidate set. SciAtlas papers are visible
in the same "external" panel as Crossref papers; deletions take effect for
both sources.
