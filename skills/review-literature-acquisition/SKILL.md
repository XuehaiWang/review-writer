---
name: review-literature-acquisition
description: Search Crossref from a review topic, rank journal-article candidates, resolve lawful open-access PDFs through a Crossref/Europe PMC/Semantic Scholar/optional Unpaywall fallback chain, validate downloads, and register them in review-library. Use when the user asks to find papers by topic, download accessible journal articles, or grow the shared Library from online sources.
---

# Review Literature Acquisition

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

Use this skill for the acquisition boundary before the normal review stages. It complements
`review-online-paper-discovery`: discovery builds a project-specific shortlist; acquisition
adds verified, legally accessible source PDFs to the shared Library.

## Workflow

1. Search by an English bibliographic topic. Keep the original wording in the UI when useful,
   but use precise English chemical/scientific terms for Crossref.
2. Apply explicit year bounds and a conservative result limit.
3. Review the ranked candidates. Do not treat a metadata match as a relevance decision.
4. Download only candidates the user selected.
5. Check the existing Library by DOI before making provider requests.
6. Resolve OA locations in this order: licensed Crossref PDF, Europe PMC, Semantic Scholar,
   then Unpaywall when an optional email is available.
7. Try the next provider when a returned location fails PDF validation. Never bypass a paywall,
   anti-bot challenge, login, or publisher access control.
8. Validate the response as a bounded PDF before moving it into the Library.
9. Register metadata with `human_review.status = not_reviewed`; the paper must still pass the
   normal Library audit and downstream parsing.

## Dashboard

Open `/library` and choose **Find & Download OA**. Enter:

- a topic;
- optional publication-year bounds;
- the maximum number of candidates;
- an optional email used only to enable the final Unpaywall fallback.

Select candidate rows, then choose **Download selected**. Search and download states are
persisted in `.review-writer/workflow.sqlite3` and executed through Prefect when the dashboard
is running in its normal Prefect-backed mode. When no automated OA source succeeds, use
**Open article page** and import a legally obtained PDF through the normal local-PDF input.

## Script

The reusable implementation is:

```text
scripts/literature_acquisition.py
```

For a command-line search:

```powershell
python <review-root>/skills/review-literature-acquisition/scripts/literature_acquisition.py search `
  --review-root <review-root> `
  --topic "enantioselective synthesis of axially chiral allenes" `
  --year-from 2015 --limit 20
```

For a selected download:

```powershell
python <review-root>/skills/review-literature-acquisition/scripts/literature_acquisition.py download `
  --review-root <review-root> `
  --candidate-json candidate.json
```

Add `--email researcher@example.org` only when Unpaywall should be included.
`UNPAYWALL_EMAIL` or `CROSSREF_MAILTO` may supply that optional email. Set
`SEMANTIC_SCHOLAR_API_KEY` to improve Semantic Scholar reliability under its rate limits.
Never put API keys or email addresses in committed files.

## Safety and correctness gates

- Accept only public HTTP(S) destinations; reject loopback, private, link-local, multicast,
  reserved, and otherwise non-public IP addresses on the initial request and redirects.
- Cap response size and stream to a temporary file.
- Require a PDF signature and non-trivial body. Reject HTML/error pages returned with HTTP 200.
- Deduplicate by normalized DOI before downloading, then by SHA-256 before registration.
- Write the final PDF and metadata atomically.
- Record failures as job results; never register a failed or partial download.
- Preserve provenance: provider, provider attempts, DOI, OA source URL, OA license, host type,
  version, response hash, and acquisition timestamp.

See [references/provider_contracts.md](references/provider_contracts.md) for provider fields
and failure semantics.
