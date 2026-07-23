# Keyword Expansion Prompt

Given a review topic and user-provided keywords, generate a concise keyword set for online literature search (Crossref/SciAtlas).

Rules:

- Keep the user's original keywords unless clearly irrelevant.
- Add synonyms, subtopics, and adjacent terminology a domain expert would search for, using your own domain knowledge of whatever field the topic belongs to.
- Do not create too many broad generic keywords.
- Prefer search-useful terms over prose phrases.
- Mark source as `user`, `agent`, or both.

Expected output shape:

```json
{
  "user_topic": "...",
  "user_keywords": ["..."],
  "agent_keywords": [
    {"keyword": "...", "reason": "..."}
  ],
  "merged_keywords": [
    {"keyword": "...", "source": ["user", "agent"], "keep": true}
  ]
}
```
