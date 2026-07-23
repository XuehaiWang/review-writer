# Topic Decomposition Prompt

The LabKAG backend never calls an LLM to understand a topic. That reasoning
happens here, in you (the agent running this skill), before you ever call
`match-topic`. The backend only validates the plan you produce and executes a
deterministic graph/embedding match against it.

Given a free-text topic and a project's current taxonomy (fetch it first with
`taxonomy-get --project-id <id>`), resolve the topic into a structured
`TopicPlan` and save it to a local JSON file. That file is the
agent-to-backend boundary, passed to `match-topic --plan <path>`.

## Step 0: one upfront check — ambiguity and expected size together

Before decomposing the topic, do a first pass over it for two things at
once, and if either needs the user's input, ask **both in the same
question** rather than interrupting twice:

1. **Ambiguity scan.** Does the topic contain an abbreviation or term with
   more than one plausible meaning in the relevant domain (not just
   theoretically possible elsewhere — plausible *here*)? Check corpus
   evidence first (taxonomy `allowed_values`/`aliases`, and the sampled
   papers' extracted text) per the disambiguation rule below. Only surface
   this to the user if corpus evidence is genuinely inconclusive between two
   or more domain-plausible readings — do not ask about a term that's
   unambiguous or already corpus-grounded, and do not skip the corpus check
   and go straight to asking.
2. **Expected result size.** Infer from how the user phrased the request
   (e.g. "just the key papers on X" vs. "everything on X"); only ask if
   there's no signal either way. Roughly: a **handful** (a few, highly
   specific), a **moderate set** (a typical literature-review slice), or
   **broad** (cast a wide net).

If step 1 needs the user (ambiguous term) and step 2 has no signal either,
combine them into one question — e.g. "By APA did you mean X or Y, and
roughly how many papers are you expecting — a handful, or a broader set?" —
rather than asking twice. If only one of the two needs input, ask only that
one. If neither does, proceed straight to decomposition without asking
anything.

The expected size is not enforced as a hard cap — the backend never pads
results to hit a target count — but it calibrates how strict the plan and
the match-topic call should be:

| Expected size | `essential` marking | `--min-essential-signals` | `--no-borderline` | `--limit` |
|---|---|---|---|---|
| Handful (a few) | Mark more concepts essential; prefer the most specific applicable value | 2 (or higher if the taxonomy has 3+ relevant categories) | pass it (borderline off) | small, e.g. 10-20 |
| Moderate | Default marking per the rules below | 2 | omit (borderline included, reported separately) | omit or a moderate cap |
| Broad | Mark fewer concepts essential; keep only the concepts that are truly non-negotiable | 1-2 | omit | omit |

If the user gives no signal at all, default to "moderate" rather than
guessing broad or narrow. Never use the expected count to fabricate matches
or extend a `confirmed`/`borderline` list past what actually cleared the
corroboration bar — it only tunes strictness inputs *before* matching runs,
never the output afterward.

**If the user gives (or you can reasonably infer) a specific number** rather
than just handful/moderate/broad, pass it as `--target-count N` on the
`match-topic` call in addition to picking the row above. This does not
truncate anything — `confirmed`/`borderline` are still returned in full —
but the backend adds `summary.suggested_confirmed_count` and
`summary.suggested_borderline_count`, a **rank-based cutoff**, not a
hardcoded one: it finds the point in the match_score-ranked results (within
a window around N) where the score actually drops the most, i.e. where
papers above are genuinely more corroborated than papers below, rather than
just stopping at the Nth item or padding weaker results in to hit N. If the
corpus has fewer qualifying papers than N, the suggestion is simply "take
everything" — never invented or padded. When reporting to the user, lead
with the suggested split, but always state the tiers' true total counts too
(e.g. "12 confirmed, all recommended; 32 borderline, the top 18 recommended
by corroboration strength — full lists available on request") so the
target-count framing never hides how many results actually exist.

## Rules

- Every `concept.category` must be one of the taxonomy's declared category
  `key` values. Every `concept.value` must be one of that category's
  `allowed_values` (or a value covered by its `aliases`). Never invent a
  category or value that is not in the taxonomy — if the topic implies
  something the taxonomy does not cover, put it in `unresolved` instead of
  guessing or picking the closest existing value.
- Mark each concept `essential: true` only if the topic is not meaningfully
  about the paper without it. Mark it `essential: false` (supporting) if it
  narrows or contextualizes the topic but a paper missing it could still be
  relevant. Do not mark everything essential — the backend will reject a
  paper that only matches supporting concepts, so under-marking essential
  concepts silently loses recall, and over-marking it turns a broad review
  topic into a narrow one that returns almost nothing.
- Give every concept a calibrated `confidence` (0-1) and a short `reason`
  tied to the actual topic wording, not a generic justification.
- Compute `year_from`/`year_to` yourself from any relative-year phrase in the
  topic (e.g. "past five years" against the current calendar year, inclusive
  range). The backend independently re-derives this from the raw topic text
  and rejects the plan if your stated range disagrees — so compute it
  correctly, do not leave it to the backend to fix.
- If the topic contains an abbreviation or term ambiguous enough that mapping
  it to a taxonomy value would be a guess, add it to `unresolved` with a
  `reason` instead. A plan may still proceed if other concepts give it a
  meaningful search. A plan with no essential concepts at all must not be
  submitted — stop and ask the user to clarify the topic first.
- **Disambiguating an abbreviation or term with more than one plausible
  meaning, even within the relevant domain**: never resolve it from general
  world knowledge (a web search, your own training data) as the sole basis
  for an essential concept — that resolution has no connection to what this
  *specific* corpus's authors actually mean by it, and a wrong guess here
  silently mis-scopes the whole plan. Resolve in this order:
  1. **Corpus evidence first.** Check whether the taxonomy's `allowed_values`
     already suggest a specific expansion (e.g. a value or its `aliases`
     spelling out the abbreviation), or search the sampled papers'
     extracted text for the abbreviation or its candidate expansions being
     used directly. Corpus evidence beats any outside source, because it
     reflects this project's actual terminology.
  2. **If the corpus is inconclusive** and more than one meaning is
     plausible within the domain, do not silently pick one — even a
     well-cited, seemingly-authoritative outside source is still a guess
     about *this* corpus's intent. List the candidate meanings you found
     (with your confidence in each) and ask the user which one they mean,
     before building any concept from it. Only proceed without asking if
     exactly one meaning is plausible and corpus-grounded.
  3. Record the resolution (which meaning, and why) in the concept's
     `reason` field so the choice is auditable later, not just implicit in
     the resulting `value`.
- Prefer more specific, narrower matches to more general ones when the
  taxonomy offers both (e.g. a specific allowed value over a broad
  catch-all), since specificity is what makes the backend's corroboration
  scoring precise instead of noisy.

## Expected `topic_plan.json` shape

```json
{
  "topic": "Iron-catalyzed aerobic oxidation of primary alcohols in the past 5 years",
  "project_id": "proj_123",
  "concepts": [
    {
      "category": "catalyst_type",
      "value": "iron",
      "essential": true,
      "confidence": 0.95,
      "reason": "Topic explicitly names iron catalysis."
    },
    {
      "category": "reaction_type",
      "value": "aerobic_oxidation",
      "essential": true,
      "confidence": 0.9,
      "reason": "Topic explicitly names aerobic oxidation."
    },
    {
      "category": "substrate_class",
      "value": "primary_alcohol",
      "essential": false,
      "confidence": 0.8,
      "reason": "Narrows the topic but iron/aerobic-oxidation papers on other alcohol classes may still be relevant."
    }
  ],
  "unresolved": [],
  "year_from": 2021,
  "year_to": 2026
}
```

## Usage

```text
py -3.10 labkag-review-skill/scripts/labkag_api.py taxonomy-get --project-id <id>
```

Read the returned taxonomy, write `topic_plan.json` following the rules
above, then:

```text
py -3.10 labkag-review-skill/scripts/labkag_api.py match-topic \
  --project-id <id> --plan topic_plan.json --target-count 60
```

(`--target-count` is optional — only pass it when step 0 established a
specific number.)

The response separates `confirmed` and `borderline` papers. Never merge them
into one list when reporting results to the user — the tier distinction is
the whole point of the corroboration model: a `confirmed` result cleared a
two-signal bar, a `borderline` result only cleared one and needs a human (or
your own further judgment) before being treated as a real match.

Each `MatchedPaper` also carries `match_score` (corroboration strength: 2
points per distinct matched essential category, 1 per distinct matched
supporting category, +1 for co-occurring evidence) and `embedding_score`
(cosine similarity to the topic, when embeddings are enabled — a ranking
tiebreaker only, never a tier input). Both lists are already sorted by
`match_score` descending (embedding as tiebreaker), highest-corroborated
first. **Use this ranking, not a hardcoded threshold, to work toward an
expected result count**: if the user expects ~N papers and a tier has more
than N, the top-N by rank within that tier are the strongest candidates to
report or read first — but still report the tier's true total count and
never silently drop the rest or claim only N papers matched. If a tier has
fewer than N, say so plainly (the corpus doesn't support more at this
corroboration strength) rather than dipping into the next tier to pad the
number, or loosening `--min-essential-signals` without telling the user why.
