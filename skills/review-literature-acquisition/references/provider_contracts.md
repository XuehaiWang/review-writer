# Provider contracts

## Crossref

Use `GET https://api.crossref.org/works` with `query.bibliographic`, `rows`, and optional
publication-date filters. The result is metadata, not evidence that a paper is open access.
Keep the DOI, title, authors, container title, publication year, abstract, URL, license list,
reference count, and direct-link metadata when present.

Use a descriptive `User-Agent`. If `CROSSREF_MAILTO` or the dashboard email field is present,
include `mailto` and identify it in the user agent.

Use a Crossref direct PDF link automatically only when the same candidate also contains
explicit license metadata. Treat a bare full-text/TDM URL as insufficient evidence of OA.

## Europe PMC

Use the REST search endpoint with the normalized DOI and `resultType=core`. Require an exact
DOI match and `isOpenAccess`. Prefer a PDF entry from `fullTextUrlList`; when the OA record has
only a PMCID, use Europe PMC's official `?pdf=render` article endpoint.

Record the PMCID and provider license when present. Expect good coverage for biomedical and
chemical-biology literature, but do not treat a missing Europe PMC result as a failure of the
whole acquisition job.

## Semantic Scholar

Use the Academic Graph paper endpoint with `DOI:{doi}` and request `isOpenAccess` plus
`openAccessPdf`. Accept a URL only when `isOpenAccess` is true and `openAccessPdf.url` exists.

Set `SEMANTIC_SCHOLAR_API_KEY` when available. Treat 429 responses, timeouts, and provider
errors as a skipped fallback so another provider can continue.

## Unpaywall

Use `GET https://api.unpaywall.org/v2/{doi}?email={email}`. The email is required by the
provider but optional for this project. Skip Unpaywall when no email is supplied; do not block
the other providers. Prefer `best_oa_location.url_for_pdf`. Record `license`, `host_type`, and
`version`.

Treat these outcomes distinctly:

- `open_access_pdf`: a public PDF URL was returned;
- `no_open_access_pdf`: the DOI was resolved but no PDF URL is available;
- `not_found`: Unpaywall returned 404;
- `skipped`: no optional provider-identification email was supplied;
- `provider_error`: timeout, malformed response, or provider failure.

Do not use a publisher landing-page URL as though it were a PDF.

## Resolution order

Resolve and validate in this order:

1. existing Library DOI match;
2. licensed Crossref PDF;
3. Europe PMC;
4. Semantic Scholar;
5. Unpaywall when configured;
6. manual article-page access and normal local-PDF import.

Continue to the next location when a provider URL returns HTML, redirects to a login, exceeds
the size limit, or otherwise fails PDF validation.

## Download validation

The downloader accepts a final response only when:

- every requested and redirect URL is public HTTP(S);
- proxy fake-IP DNS answers are accepted only after a fixed DNS-over-HTTPS lookup confirms
  exclusively public origin addresses;
- the response stays below the configured byte limit;
- the body starts with the PDF signature after an optional UTF-8 BOM/leading whitespace;
- the response is not HTML;
- the saved body is non-trivial.

The validation proves that the acquired artifact is a PDF from the OA location associated
with the selected DOI. It does not prove scientific relevance or extraction quality; those
remain human-review and parsing responsibilities.
