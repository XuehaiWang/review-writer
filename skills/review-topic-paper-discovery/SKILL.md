---
name: review-topic-paper-discovery
description: Start a review project from a user topic, build a categorized multi-theme query plan against the active eight-field taxonomy profile, retrieve the de-duplicated local candidate set from the metadata library, and optionally enrich it with external search; let a human choose which candidates enter the Matrix.
---

# Review Topic Paper Discovery

Goal: from the user review topic, retrieve all qualifying local candidate papers,
let the human choose which candidates enter the Matrix, and keep an external
evidence pool from SciAtlas for coverage checking.

Every newly retrieved paper is a candidate only: set `selected_for_matrix` to
`false` initially. A paper enters the Matrix only after the human explicitly
selects it in Discovery.

## Hard Rules

Query keywords may use the shared vocabulary categories: `product`,
`substrate`, `catalyst_or_method`, `organometallic_partner`,
`ligand_or_chiral_source`, `leaving_group`, `reaction_type` and
`document_scope`. Unknown scientific phrases use the neutral `unclassified`
retrieval route. These are query hints, not preassigned paper Tags.

Do not use stored Library Tags for admission or ranking, or require a user
to confirm project Tags in Discovery. Retrieve from title and source-addressable
parsed content. Formal, evidence-backed paper classification belongs to Matrix
after the user selects candidate papers.

Reuse the shared taxonomy loader for vocabulary/aliases and record the effective
taxonomy and normalization SHA-256 identities. Default to `general_academic`;
honor the project's selected profile and `REVIEW_CLASSIFICATION_RULES`.
Match short aliases as whole tokens, not substrings of ordinary words.

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

The default dashboard path uses `--auto-query-plan`. It parses explicit
topic phrases, date bounds and organization requests locally. It does not
ask a model to generate an outline or paper-classification partitions.

Definitions such as "long form (ABBR)" and simple list references such as
"class, their derivatives" are resolved locally. Only undefined abbreviations
use the small optional prompt in `references/keyword_expansion_prompt.md`.
Run literal local retrieval before that request. On timeout or an unusable
answer, retain the original terms and continue; do not guess expansions.
Billing/authorization rules for model access remain separate.

Automatic plans preserve `group_by` and the original `organization_intent`.
Formal partitions are deferred to the selected papers' Matrix evidence.
The hosted application subsequently uses its existing Library index for
paper-level lexical/vector fusion; the standalone script handles local
lexical and optional external retrieval.

```bash
python skills/review-topic-paper-discovery/scripts/discover.py \
  --review-root <review-root> \
  --topic "<review topic>" \
  --project-id <project-id> \
  --auto-query-plan
```

The script writes `00_discovery/query_plan.draft.json`. Existing explicitly
supplied `--query-plan <path>` files remain supported and validated. A cached
automatic plan is reused only for matching topic, keywords, taxonomy, prompt,
model, planner schema and calendar year. Failed optional expansions are not
cached as a permanent success.

Add `--sciatlas-search`, `--web-search`, or both to that command when external
coverage is requested. For SciAtlas KG, configure the service and append its
search controls:

```bash
export SCIATLAS_API_BASE_URL=https://sciatlas-proxy.example
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

Direct script execution without either plan flag retains deterministic
retrieval for compatibility. Every `group_by` value must be one
of the eight structured tag categories above; keyword categories may also use
the Discovery-only `unclassified` route.

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

Health check before searching (against the configured HTTPS endpoint):

```bash
curl -s "$SCIATLAS_API_BASE_URL/healthz"
```

The application no longer embeds a remote HTTP default because that would send
the API key in cleartext. Prefer an HTTPS reverse proxy. A legacy non-loopback
HTTP endpoint is accepted only with the explicit
`SCIATLAS_ALLOW_INSECURE_HTTP=true` opt-in.

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
`selected_discovery_results.json` contains every local paper explicitly kept by
the human reviewer; there is no fixed paper-count cap. External (SciAtlas/Crossref) papers go into
`web_papers`; they are a topic-coverage check pool only. They never enter
the local `paper_id` registry. A search result alone is not chapter evidence;
acquire, register, and explicitly select a paper through the normal Library
workflow before using its source passages in manuscript claims.

In the Web workflow, confirming Discovery does not automatically queue
`matrix.enrich` or require scientific fact cards. Continue to outline selection
and chapter planning using the selected-paper context.

## Human Check

Stop after discovery. The human reviews candidate papers, includes or excludes
them, and confirms which enter Matrix. Topic-related retrieval hints are not
scientific classifications. Do not silently select all newly retrieved papers.
