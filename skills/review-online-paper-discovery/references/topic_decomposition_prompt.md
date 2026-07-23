# Topic Decomposition and Disambiguation Prompt

`discover.py` never calls an LLM to understand a topic. That reasoning
happens here, in you (the agent running this skill), before you write
`agent_keywords.json` or run `discover.py search`.

Do this before the keyword-expansion step in `SKILL.md`, not instead of it —
decomposition and disambiguation produce a clearer topic to expand into
keywords from; they don't replace the keyword-expansion output itself.

## Step 1: break the topic into concepts

Split the (English-translated) topic into its constituent concepts — the
distinct ideas a paper would need to touch on to be genuinely about this
topic, not just a flat keyword list. For each concept, note:

```json
{"concept": "<short phrase>", "essential": true, "confidence": 0.9, "reason": "<why this reading, tied to the actual topic wording>"}
```

- `essential: true` — the topic isn't meaningfully about the paper without
  this concept present.
- `essential: false` — narrows or contextualizes the topic, but a paper
  missing it could still be relevant.

Don't mark everything essential — a topic with every concept essential
returns almost nothing; a topic with nothing essential returns everything.

## Step 2: scan for ambiguous terms

Before finalizing concepts, check whether the topic contains an abbreviation
or term with more than one plausible meaning in the relevant domain (not
just theoretically possible elsewhere — plausible *here*). A wrong silent
guess here mis-scopes every keyword and every search that follows it.

**This skill has no local corpus to check yet** (that's what it's building)
— so unlike a tool that already has a knowledge base to consult, resolve
ambiguity with **search evidence**, not general/training-data knowledge
alone:

1. **Probe first.** For each plausible meaning, run:

   ```bash
   python3 <skill-root>/scripts/discover.py probe --query "<candidate expansion or term in context>" --web-search --limit 8
   ```

   (Add `--sciatlas-search` too if configured.) This writes nothing and
   needs no project — it just prints what the actual literature returns for
   that term, so you can see which meaning the publication record actually
   supports instead of guessing from general knowledge.

2. **Read the results, not just the score.** Do the titles/journals cluster
   around one meaning? Do they mix multiple unrelated meanings? Does a
   generic hit (e.g. "APA standards and cite system") turn up, confirming a
   second, unrelated meaning exists?

3. **Resolve or ask:**
   - If the probe results clearly support one meaning and the alternative(s)
     don't surface at all, resolve to it — record the resolution and which
     probe evidence supported it in the concept's `reason` field, so the
     choice is auditable later.
   - If probe evidence is genuinely mixed or inconclusive between two or
     more domain-plausible readings, do **not** silently pick one — even a
     well-cited, seemingly-authoritative meaning is still a guess about what
     *this user* means. List the candidate meanings you found (with what the
     probe showed for each) and ask the user which one they mean, before
     building any concept from it.
   - Never resolve an ambiguous term from general world knowledge or your
     own training data as the *sole* basis — the probe step exists precisely
     because that's an easy way to mis-scope the whole search silently.

4. If a term is ambiguous enough that resolving it would still be a guess
   even after probing, and the user hasn't clarified, drop it from the
   concept list rather than forcing a reading — the remaining concepts can
   still drive a meaningful search.

## Step 3: proceed to keyword expansion

Once concepts are decomposed and any ambiguity resolved (or dropped),
continue with `SKILL.md`'s "Keyword expansion" step: expand the
disambiguated concepts into the broader `agent_keywords.json` keyword list
as already described there. The concept list above is working context for
getting that expansion right — it is not itself the file `discover.py`
reads.
