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
- Add synonyms, substrate classes, catalyst or method classes, reaction types, product classes, organometallic partners, ligands/chiral sources, leaving groups, and document-scope terms.
- Do not create too many broad generic keywords.
- Prefer search-useful terms over prose phrases.
- Classify each keyword as one of:
  - `product`
  - `substrate`
  - `catalyst_or_method`
  - `organometallic_partner`
  - `ligand_or_chiral_source`
  - `leaving_group`
  - `reaction_type`
  - `document_scope`
- If a keyword does not fit cleanly, classify it as `reaction_type` rather than inventing a new category.
- Mark each keyword source as `user` or `agent`.
- Convert relative-year requests against the current calendar year and use an
  inclusive range. For example, in 2026, "past five years" means
  `filters.year_from` is `2022` and `filters.year_to` is `2026`.
- Represent a request to organize by catalyst type as
  `group_by: ["catalyst_or_method"]`. Do not add generic words such as
  `catalysts`, `organized`, or `review` as retrieval keywords.
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
