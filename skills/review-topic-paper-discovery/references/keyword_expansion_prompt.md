# Query Plan and Keyword Expansion Prompt

Given a review topic and user-provided keywords, resolve the Topic into a
structured literature-discovery query plan. Write the result to
`review-projects/<project-id>/00_discovery/query_plan.draft.json`; this file is
the LLM-to-script boundary passed to `discover.py --query-plan <path>`.

Rules:

- Keep the user's original keywords unless clearly irrelevant.
- Separate chemistry concepts from writing instructions.
- Resolve abbreviations only when the Topic and chemistry context support the
  expansion. Every resolved abbreviation must include a calibrated
  `confidence` from `0` to `1` and a short reason. Put ambiguous concepts in
  `unresolved_concepts` instead of guessing.
- Add search-useful synonyms and only the categories supported by the topic. Do
  not add a document-scope term merely because the user asks the system to
  write a review; add `review article` only when review articles are explicitly
  requested as source documents.
- Do not create too many broad generic keywords.
- Prefer search-useful terms over prose phrases.
- Resolve anaphoric list items such as "their derivatives" to the immediately
  preceding scientific class. When that derivative class is important to the
  requested grouping, add a small number of representative named subclasses
  supported by the domain context instead of retaining the vague pronoun.
- Include common nomenclature variants for explicitly requested grouping
  classes (for example positional names versus class names), while respecting
  the keyword limit.
- Classify each keyword as one of:
  - `product`
  - `substrate`
  - `catalyst_or_method`
  - `organometallic_partner`
  - `ligand_or_chiral_source`
  - `leaving_group`
  - `reaction_type`
  - `document_scope`
  - `unclassified` (Discovery routing only; it is not stored as a ninth metadata tag)
- If a keyword does not fit cleanly, classify it as `unclassified`. The local
  retriever will search it across all eight structured metadata fields and the
  parsed source text. Never force an unknown concept into `reaction_type`.
- Mark each keyword source as `user` or `agent`.
- Convert relative-year requests against the current calendar year and use an
  inclusive range. For example, in 2026, "past five years" means
  `filters.year_from` is `2022` and `filters.year_to` is `2026`.
- When the topic has no publication-year restriction, return `"filters": {}`.
  Do not emit `null`, an empty string, or an invented year bound.
- Preserve every explicit organization request in `group_by`: substrate,
  product, catalyst/method, reaction type, ligand/chiral source, leaving group,
  and document scope map to their corresponding metadata category. For
  example, "categorized by the substrates" becomes
  `group_by: ["substrate"]`. Do not add generic words such as `substrates`,
  `methods`, `organized`, or `review` as standalone retrieval keywords.
- In every `resolved_concepts` item, the canonical expanded field name is
  exactly `expanded_name` (not `normalized`).
- Review `unresolved_concepts` before running discovery. A plan may proceed
  when other resolved concepts or validated keywords provide a meaningful
  search, but an unresolved-only plan must stop before invoking `discover.py`
  and ask the user for clarification. A plan with no meaningful keyword must
  also stop before invoking `discover.py`.

Expected `query_plan.draft.json` shape:

```json
{
  "schema_version": 1,
  "topic": "Review palladium-catalyzed APA reactions developed in the past five years, organized by catalyst type.",
  "resolved_concepts": [],
  "unresolved_concepts": [
    {
      "surface": "APA",
      "reason": "The Topic does not provide enough chemistry context to expand APA confidently."
    }
  ],
  "keywords": [
    {
      "keyword": "palladium catalysis",
      "category": "catalyst_or_method",
      "source": "user",
      "reason": "The Topic explicitly requests palladium-catalyzed chemistry."
    }
  ],
  "filters": {
    "year_from": 2022,
    "year_to": 2026
  },
  "group_by": ["catalyst_or_method"]
}
```
