# Review Writer portable configuration

The workflow separates versioned contracts from deployment configuration.
Stage IDs, stage directory names, handoff schemas, SHA-256 lineage, paper IDs,
and chemistry integrity thresholds are versioned contracts. They should not be
changed through user settings.

## Workspace and project configuration

- `REVIEW_WRITER_ROOT` optionally identifies the workspace when a script is
  launched outside the repository directory.
- `review-projects/<project-id>/project_config.json` records the review topic
  and selected taxonomy profile. It is created with the Discovery project and
  is included in workflow lineage.
- A topic with allene-specific signals selects `allene`; other new topics use
  `chemistry_general`. An existing recorded profile is never silently changed.
- `REVIEW_TAXONOMY_PROFILE` explicitly overrides automatic selection.
- `REVIEW_CLASSIFICATION_RULES` points to a custom taxonomy file. Relative
  paths resolve from the workspace root.

The shared metadata library can contain papers from multiple profiles. Metadata
validation therefore accepts the union of installed profiles unless an explicit
taxonomy override is configured. Retrieval always uses only the active project
profile.

## Rule packs

Blueprint records `rule_pack` and `rule_pack_path`. Section generation reads
that exact, traversal-safe directory; it no longer loads the allenation pack
unconditionally. Add topic-specific packs to
`skills/review-section-blueprint/references/rule_packs.json`. Topics without a
matching signal use the `general` pack.

## Provider configuration

The dashboard Settings page remains the preferred provider configuration path.
The runtime normalizes provider URLs, endpoints, wire APIs, and key precedence
through `review_writer_core/providers.py`; provider hostnames no longer select
behavior implicitly.

Optional advanced environment settings:

```dotenv
# Multipart field required by an image-only compatible endpoint.
IMAGE_OPENAI_FIELD=image[]

# Skip a known-unsupported landscape attempt for square-only image providers.
IMAGE_SUPPORTED_SIZES=1024x1024
```

## Runtime limits

```dotenv
# Maximum papers downloaded by one literature acquisition job.
REVIEW_MAX_LITERATURE_BATCH=30
```

Values are validated centrally and reject invalid or unsafe ranges.
