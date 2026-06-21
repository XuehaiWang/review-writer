---
name: review-topic-paper-discovery
description: Start a review project from a user topic, extract/search keywords against the 8 LLM allene classification tags, and produce 20-30 candidate papers for human check.
---

# Review Topic Paper Discovery

Goal: from the user review topic, select `20-30` local candidate papers.

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

Use `/home/ps/review-writer/allene_classification_rules.py` as the tag vocabulary and synonym source.

Do not rank local papers by metadata abstract.

## Run

```bash
python /home/ps/review-writer/skills/review-topic-paper-discovery/scripts/discover.py \
  --review-root /home/ps/review-writer \
  --topic "<review topic>" \
  --keywords "<optional user keywords>" \
  --project-id <project_id>
```

If the user gives no keywords, Codex must extract concise keywords from the topic first.

`keyword_set.draft.json` must not introduce extra local-retrieval categories. Every keyword category should be one of the eight structured tag categories above. If a topic token does not fit cleanly, classify it as `reaction_type` and let human check remove it if needed.

## Required Output

Write under:

```text
review-projects/<project_id>/00_discovery/
```

Required files:

```text
topic_input.md
keyword_set.draft.json
combined_results_by_keyword.json
selected_discovery_results.json
discovery_report.md
human_check_state.json
```

`selected_discovery_results.json` should contain `20-30` kept local papers when enough matches exist. If fewer than 20 are found, record why in `discovery_report.md`.

## Human Check

Stop after discovery. The human checks `/discovery`, deletes irrelevant keywords/papers, and confirms the candidate set.
