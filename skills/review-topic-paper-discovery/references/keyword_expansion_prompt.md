# Keyword Expansion Prompt

Given a review topic and user-provided keywords, generate a concise keyword set for literature discovery.

Rules:

- Keep the user's original keywords unless clearly irrelevant.
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
- Mark source as `user`, `agent`, or both.

Expected output shape:

```json
{
  "user_topic": "...",
  "user_keywords": ["..."],
  "agent_keywords": [
    {"keyword": "...", "category": "...", "reason": "..."}
  ],
  "merged_keywords": [
    {"keyword": "...", "category": "...", "source": ["user", "agent"], "keep": true}
  ]
}
```
