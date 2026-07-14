# Keyword Expansion Prompt

Given a review topic and user-provided keywords, generate a concise keyword set for literature discovery.

Rules:

- Keep the user's original keywords unless clearly irrelevant.
- Add synonyms and related terms drawn from the library's own observed vocabulary (`review-library/metadata/library_vocabulary.json`), not from a fixed external taxonomy — the library may cover any subject area.
- Do not create too many broad generic keywords.
- Prefer search-useful terms over prose phrases.
- Classify each keyword as one of:
  - `output`
  - `input`
  - `method`
  - `co_input`
  - `modifier`
  - `process_type`
  - `document_scope`
- If a keyword does not fit cleanly, classify it as `process_type` rather than inventing a new category.
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
