# React + TypeScript SPA Migration Design

Date: 2026-08-14
Status: Approved architecture; awaiting written-spec review
Scope: Replace the browser presentation layer while preserving URLs, API contracts, workflow behavior, and persisted data

## 1. Decision

Review Writer will use one React 19 + TypeScript + Vite single-page application for the authenticated portal and all workflow pages. Existing public URLs and FastAPI API contracts remain stable.

The migration changes only the browser presentation layer. It does not replace FastAPI, PostgreSQL, workflow persistence, scientific artifact files, background jobs, or skill scripts.

The target stack is intentionally small:

- React and TypeScript for components and typed user-interface logic;
- React Router for URL-to-workspace routing;
- TanStack Query for server-owned data, cache invalidation, polling, and request state;
- Zustand only for small client-owned state that must survive component boundaries;
- React Hook Form for settings and other validation-heavy forms;
- Vite for development and production builds;
- Vitest and Testing Library for component tests;
- Playwright for a small set of end-to-end workflow checks.

Redux, Next.js, GraphQL, micro-frontends, server-side rendering, and multiple UI component libraries are explicitly outside the design.

## 2. Goals

1. Preserve these canonical URLs:
   - `/`
   - `/library`
   - `/discovery`
   - `/planning?tab=matrix`
   - `/planning?tab=blueprint`
   - `/sections`
   - `/images?tab=review`
   - `/images?tab=redraw`
   - `/draft`
   - `/final`
   - `/settings`
2. Preserve legacy redirects from `/matrix`, `/blueprint`, `/figure-review`, and `/figures`.
3. Preserve every existing `/api/v1` request and response contract used by the current pages.
4. Preserve the `project` query parameter so copied links reopen the same project and workspace.
5. Make background-job state server-owned so page navigation and browser refresh do not lose progress.
6. Preserve SVG editing, Ketcher, PDF preview, uploads, downloads, human approval, and internationalization.
7. Preserve all PostgreSQL records and scientific artifacts; the migration requires no user-data conversion.
8. Produce one cohesive component system instead of maintaining separate page-specific chrome and duplicated event handlers.

## 3. Non-goals

- Rewriting scientific Python scripts or workflow algorithms;
- changing provider, MinerU, image-generation, or text-generation behavior;
- renaming stages or changing stage handoff semantics;
- changing authentication cookies or project ownership rules;
- moving scientific artifacts into a database;
- replacing same-origin Ketcher or PDF iframe behavior;
- changing public API response schemas as part of the UI migration.

## 4. Current State

The current browser layer consists of separate HTML documents under `view/assets/dashboard`, shared native JavaScript, page-local scripts, and shared CSS. FastAPI selects an HTML file for each canonical route. The repository has no Node or Vite build pipeline.

The backend already exposes native FastAPI routers for library, discovery, planning, sections, figures, drafts, final assembly, artifacts, and jobs. This API boundary is the stable seam for the migration.

The hosted portal also has native HTML, CSS, and JavaScript under `review_writer_api/web`. The React application will absorb this portal so login, project selection, settings, and workflow pages share one runtime and visual system.

## 5. Target Architecture

```text
Browser
  React + TypeScript SPA
    React Router
    TanStack Query
    minimal Zustand stores
    reusable workspace components
           |
           | same-origin /api/v1 requests and HttpOnly session cookie
           v
FastAPI
  authentication and authorization
  native workflow routers
  job and artifact services
           |
           +-- PostgreSQL: users, sessions, projects, provider settings,
           |               workflow state and artifact metadata
           +-- workspace files: PDF, JSON, Markdown, PNG, SVG, DOCX
           +-- scientific Python scripts and external providers
```

FastAPI serves the built SPA and remains the only production process. Node.js is required at build time, not at runtime.

## 6. Route Contract

All canonical browser routes return the same built `index.html`. React Router selects the correct workspace using `location.pathname` and `location.search`.

| Browser route | React workspace |
|---|---|
| `/` | Authentication or project portal |
| `/library` | Shared literature library |
| `/discovery` | Project creation and discovery |
| `/planning?tab=matrix` | Literature matrix |
| `/planning?tab=blueprint` | Outline and blueprint |
| `/sections` | Section drafting |
| `/images?tab=review` | Source figure review |
| `/images?tab=redraw` | AI redraw and manual editing |
| `/draft` | Draft editing and feedback loop |
| `/final` | Final assembly, audit, and export |
| `/settings` | Provider and application settings |

FastAPI performs authentication before returning protected SPA routes. Deep-link refreshes therefore work without weakening current access control.

Legacy paths continue to issue server-side 307 redirects:

- `/matrix` -> `/planning?tab=matrix`
- `/blueprint` -> `/planning?tab=blueprint`
- `/figure-review` -> `/images?tab=review`
- `/figures` -> `/images?tab=redraw`

Any existing query string, including `project`, is preserved.

## 7. Frontend Module Boundaries

The frontend will be created under `frontend/` with these primary boundaries:

```text
frontend/src/
  app/             application bootstrap, providers, router, error boundary
  api/             typed request client, schemas, endpoint modules
  components/      reusable visual primitives and shared workflow chrome
  features/
    auth/
    projects/
    settings/
    library/
    discovery/
    planning/
    sections/
    images/
    draft/
    final/
  integrations/
    ketcher/
    svg-editor/
    pdf-preview/
  state/           minimal client-owned Zustand stores
  i18n/            English and Chinese messages
  styles/          tokens, layout, and component styles
```

Feature modules may call only their typed API module and shared components. They must not construct backend filesystem paths or import Python-generated artifacts directly.

## 8. State Ownership

State will be assigned to one owner to prevent duplication.

### Server-owned state

TanStack Query owns and refreshes:

- current user and session;
- project list and project stage summaries;
- library papers and metadata;
- matrix, blueprint, sections, figures, draft, and final artifacts;
- provider-setting status;
- job status, progress, retries, errors, and generated output availability.

Mutations invalidate precise query keys. The UI must not assume success before the server confirms it.

### URL-owned state

React Router and URL search parameters own:

- active workspace;
- active combined-workspace tab;
- current `project` identifier;
- shareable filters or selected artifact identifiers when appropriate.

The URL remains the source of truth for project selection. Navigating or refreshing reconstructs the same workspace.

### Client-owned state

Zustand is limited to:

- language and non-sensitive visual preferences;
- temporary panel sizes and open/closed state;
- unsaved editor session state;
- transient selection inside SVG/Ketcher integrations.

API keys, workflow completion, approval, and job status must never be stored in Zustand or browser storage.

## 9. Jobs and Live Progress

Long-running operations use the existing job APIs. The first React version uses adaptive TanStack Query polling because it works with current infrastructure and survives reconnects without introducing a second real-time protocol.

- queued, running, and retrying jobs poll rapidly;
- completed or failed jobs stop polling;
- background tabs use a slower interval;
- returning to a route immediately refetches server state;
- route unmounting never cancels the server job unless the user explicitly requests cancellation;
- figure-list badges derive from server job state, not component memory.

SSE may be added later only if polling load becomes measurable. WebSocket and SSE will not be introduced during this migration.

## 10. Editors and Same-origin Integrations

Ketcher, PDF preview, and the full-image SVG editor remain same-origin integrations.

- Ketcher continues to load from `/assets/ketcher/` in a controlled iframe or adapter component.
- PDF preview continues to use authenticated same-origin artifact endpoints.
- SVG editing is wrapped in a React integration boundary that owns lifecycle and message handling but does not rewrite the established editor engine during the first migration.
- Editor save responses invalidate figure and artifact queries immediately so the saved result appears without leaving and reopening the page.
- Unsaved changes trigger route-navigation confirmation.

The applicable Content Security Policy and `X-Frame-Options: SAMEORIGIN` exceptions remain enforced by FastAPI.

## 11. Internationalization and Visual System

Existing English and Chinese labels become structured message dictionaries. Language selection persists as a non-sensitive preference and updates the whole React tree without page reload.

The visual system uses project-owned components and CSS variables rather than adding a large third-party UI kit. Shared components include:

- application shell and stage navigation;
- project selector and delete confirmation;
- workspace tabs and stage action footer;
- buttons, form controls, dialogs, status badges, progress indicators;
- three-column review workspace;
- empty, loading, stale, error, and permission states.

This preserves the current visual identity and avoids shipping two overlapping design systems.

## 12. Error Handling

- A top-level React error boundary prevents one feature crash from blanking the entire application.
- The API client normalizes FastAPI errors into typed errors carrying status, request ID, and actionable detail.
- Authentication failures route to the login view without exposing protected state.
- Network failures retain the last successful read-only data and provide an explicit retry action.
- Mutation errors do not discard unsaved user input.
- Background job errors remain visible after navigation and refresh.
- Unsupported or malformed artifact data renders a bounded error panel rather than causing an unhandled exception.

## 13. Build and Deployment

Vite produces hashed assets and one `index.html`. Docker uses a multi-stage build:

1. a Node build stage installs locked frontend dependencies and runs tests/build;
2. the Python runtime stage installs current Python dependencies;
3. the built SPA is copied into the FastAPI image;
4. production runs only FastAPI/Uvicorn.

Caching policy:

- `index.html`: `no-store` or `no-cache`;
- hashed JavaScript, CSS, fonts, and images: long-lived immutable cache;
- API responses: existing API-specific behavior.

The development workflow runs Vite with a same-origin-style proxy to FastAPI. Production uses only port 8770.

## 14. Migration Sequence

The repository will be changed in controlled vertical slices, but the accepted end state is complete React ownership of the browser interface.

### Slice 1: foundation

- create the Vite/React/TypeScript workspace;
- add typed API infrastructure, router, authentication gate, query provider, i18n, and shared shell;
- add FastAPI SPA fallback and Docker multi-stage build;
- preserve legacy pages behind a temporary rollback switch.

### Slice 2: portal, settings, library, and discovery

- migrate login, registration, project selection, settings, local PDF upload, MinerU progress, library review, project creation, and discovery review;
- verify user isolation and provider-setting persistence.

### Slice 3: planning and sections

- migrate Matrix and Blueprint as the two `/planning` tabs;
- migrate section generation, editing, progress, and handoff.

### Slice 4: image processing

- migrate source review and redraw as the two `/images` tabs;
- integrate per-figure job status, retries, batch approval, SVG editing, Ketcher, and human approval;
- preserve all image integrity and source-version gates.

### Slice 5: draft and final

- migrate draft editing, paragraph updates, rubric/loop controls, overview generation, final assembly, audit, and DOCX download;
- ensure Stage 8 edits remain the exact input to Stage 9.

### Slice 6: cutover and cleanup

- make React the default for all canonical routes;
- remove migrated page-local scripts and old HTML after parity tests pass;
- retain only necessary integration assets such as Ketcher;
- update deployment and contributor documentation.

Each slice must be releasable and reversible. Temporary coexistence is permitted only during migration; the final repository will not maintain two user interfaces.

## 15. Testing Strategy

### Unit and component tests

- route and query-parameter parsing;
- API response normalization;
- stage-action readiness and navigation;
- job badge and retry behavior;
- form validation and secret handling;
- editor save invalidation;
- English/Chinese rendering.

### Contract tests

- generate TypeScript-facing fixtures from current API schemas or maintain explicit typed schema tests;
- verify all existing `/api/v1` routes keep status codes and response fields required by the SPA;
- verify protected SPA routes require authentication.

### End-to-end tests

At minimum:

1. login, choose a project, navigate every canonical route, and refresh each deep link;
2. save provider settings and confirm the masked persisted state after navigation;
3. upload a PDF and observe its server-owned job state;
4. run one stage transition while preserving the `project` parameter;
5. start an image redraw, navigate away and back, and observe the same job state;
6. save an SVG edit and see the updated output immediately;
7. edit Stage 8 content and verify Stage 9 reads that exact content;
8. generate and download a `.docx` with the correct filename and MIME type.

### Existing backend tests

All current Python tests remain mandatory. Frontend migration must not change scientific outputs unless a separately approved backend change requires it.

## 16. Compatibility and Rollback

- No database migration is required for the UI cutover.
- No artifact path or file format changes are required.
- Existing API routes stay active.
- A deployment-time switch may temporarily select legacy HTML during migration slices.
- Rollback means serving the previous static pages while leaving backend data untouched.
- After final parity acceptance and a stable release window, the rollback switch and legacy pages are removed.

## 17. Acceptance Criteria

The migration is complete only when:

1. every canonical URL and legacy redirect behaves as specified;
2. every current user-visible operation is available in React;
3. browser refresh and back/forward navigation preserve project and tab context;
4. long-running job state survives route changes and refreshes;
5. Ketcher, SVG editing, PDF preview, upload, and DOCX download pass end-to-end tests;
6. Stage 8 manual edits are consumed by Stage 9;
7. existing PostgreSQL records and artifact files remain usable without conversion;
8. production runs as one FastAPI service on port 8770 with no Node runtime process;
9. no secrets are written to browser storage or bundled assets;
10. all Python and frontend test suites pass;
11. migrated legacy HTML and page-specific JavaScript are removed rather than maintained indefinitely.

## 18. Principal Risks and Controls

| Risk | Control |
|---|---|
| Feature loss in a large rewrite | Vertical slices, parity checklist, legacy rollback switch |
| API assumptions hidden in old JavaScript | Typed endpoint modules and contract tests before each feature cutover |
| Job state disappears on navigation | Server-owned job state with query polling and refetch-on-mount |
| Ketcher/SVG lifecycle regressions | Adapter boundaries and focused end-to-end tests |
| Browser refresh returns 404 | Authenticated FastAPI SPA fallback for every canonical route |
| Stale frontend after deployment | Non-cacheable `index.html` and hashed immutable assets |
| Technology redundancy | One query library, one small client store, one form library, no UI framework overlap |
| Migration never finishes | Remove each old page immediately after its parity gate passes; final cleanup is an acceptance requirement |
