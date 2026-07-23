# Deep Agents Project Workspace

This repository contains a FastAPI web application that uses the LangChain Deep Agents SDK to coordinate project-specific data-analysis workers. Projects and their source content are shared. Chat sessions, task runs, checkpoints, and generated artifacts are private to the authenticated user.

## Architecture

```text
React + TypeScript browser UI
  -> CDX (OIDC login/logout and JWT validation)
     -> x-fnma-jws-token header
        -> FastAPI REST and streaming APIs
           -> shared projects, skills, and source data
           -> user-owned chats, runs, checkpoints, and artifacts
           -> Deep Agents supervisor and project skill workers
```

CDX is the authentication boundary. The application trusts the header supplied by CDX, uses the JWT `sub` claim as the user identity, and uses the fixed `roles` claim for authorization. Only users with `PROJECT_ADMIN` may create, rename, import, edit, replace, clear, or delete projects and update global settings.

## Repository layout

```text
langchain-deepagents/
├── .env.template
├── projects/
│   ├── hpi-analytics/
│   │   ├── data/
│   │   └── skills/
│   └── nmdb-analytics/
│       ├── data/
│       └── skills/
└── deep-agents-sdk/
    ├── deep_agents_app/
    │   ├── api/routers/       # FastAPI HTTP boundaries
    │   ├── db/                # SQLite connection and schema migrations
    │   ├── domain/            # Shared domain models
    │   ├── repositories/      # SQL query modules
    │   ├── runtime/           # Agent graphs, streaming, and Python execution
    │   ├── schemas/           # Pydantic API contracts
    │   ├── security/          # CDX and development identities
    │   ├── services/          # Projects, sessions, uploads, and artifacts
    │   └── application.py     # FastAPI factory and middleware
    ├── frontend/              # React, TypeScript, Vite, and browser tests
    ├── server.py              # Stable `uvicorn server:app` adapter
    ├── requirements-dev.txt
    ├── requirements.txt
    ├── static/
    │   ├── styles.css         # Shared application theme
    │   └── dist/              # Ignored Vite production build
    └── tests/
```

Application logic lives under `deep_agents_app`; `server.py` deliberately contains only the stable Uvicorn import path. Runtime databases, generated outputs, frontend build output, caches, and local environment files are intentionally excluded from Git.

## Setup

Create the SDK-local virtual environment and install constrained Python dependencies:

```bash
python3 -m venv deep-agents-sdk/venv
./deep-agents-sdk/venv/bin/pip install -r deep-agents-sdk/requirements.txt
```

Install and build the React frontend:

```bash
cd deep-agents-sdk/frontend
npm ci
npm run build
cd ../..
```

The Vite output is generated under `deep-agents-sdk/static/dist/` and is not committed. FastAPI returns a clear `503` build instruction if those assets are missing.

Copy the environment template:

```bash
cp .env.template .env
```

Configure the Portkey gateway, model allowlist, storage roots, and optional development login in `.env`.

## Production frontend build

The React application is compiled into static, self-hosted files that FastAPI serves from `deep-agents-sdk/static/dist/`. Build and validate a production release from the repository root:

```bash
cd deep-agents-sdk/frontend
npm ci
npm run deadcode
npm run lint
npm run typecheck
npm test
npm run build
```

`npm run build` runs the TypeScript build followed by Vite. Vite empties and recreates `deep-agents-sdk/static/dist/`, including `index.html`, `.vite/manifest.json`, hashed JavaScript and CSS bundles, fonts, and PDF.js assets. Commit or package the entire directory; do not select individual files from it.

The frontend currently has no environment-specific `VITE_*` build variables, so one validated build can be promoted across deployment environments. Runtime configuration remains in the FastAPI environment. Do not put secrets into frontend build variables because any such value is embedded in browser-readable JavaScript.

### Checking production assets into Git

Checking in the generated assets is a reasonable option when the Docker image must be built without Node.js. Remove this line from `.gitignore`:

```gitignore
deep-agents-sdk/static/dist/
```

Then build and stage the generated output together with the React source changes:

```bash
cd deep-agents-sdk/frontend
npm ci
npm run build
cd ../..
git add deep-agents-sdk/frontend deep-agents-sdk/static/dist
git status
```

Treat `static/dist` as generated output: never edit it manually, always use the locked dependencies from `package-lock.json`, and use one pinned Node.js version for all release builds. A clean rebuild should leave no generated differences:

```bash
cd deep-agents-sdk/frontend
npm ci
npm run build
git diff --exit-code -- ../static/dist
```

The production Docker build can then remain Python-only. It must copy `deep-agents-sdk/static/dist/` into the image at the same path and must not exclude it through `.dockerignore`. Node.js, npm, `node_modules`, TypeScript caches, test reports, and Playwright output are not required in the image.

An alternative is to build `static/dist` in a separate release pipeline and pass it to the Docker build as a versioned artifact. This avoids generated files in Git while still keeping Node.js out of the Docker build and runtime image.

### CDX authentication contract

In production, CDX implements the OIDC login and logout flows. CDX validates the JWT signature, issuer, audience, and lifetime, removes any client-supplied `x-fnma-jws-token`, and writes its validated token into that header before forwarding the request.

Every `/api/*` request requires `x-fnma-jws-token`. The application decodes this already-trusted token without repeating cryptographic or lifetime validation. It requires a nonempty `sub`, reads roles from `roles`, and uses the constant `cdx` identity namespace internally.

```json
{
  "sub": "stable-user-id",
  "roles": ["PROJECT_ADMIN"]
}
```

There are no `FNMA_JWT_*`, auth-mode, identity-namespace, public-key, shared-secret, issuer, audience, or algorithm settings in this application.

This trust model requires the application backend to be unreachable except through CDX. CDX must overwrite rather than preserve a client-provided token header, and the CDX-to-application connection must be trusted. If an alternate ingress can reach the backend, a caller could forge `sub` or `PROJECT_ADMIN` because claim validation intentionally belongs to CDX.

The browser never reads, stores, or supplies the CDX header. It makes ordinary same-origin requests and CDX adds the header while forwarding them. The development-only login described below uses a signed HTTP-only cookie instead and never creates a trusted CDX header.

### Runtime storage

SQLite databases and their WAL/SHM sidecars are stored together under:

```text
DEEP_AGENTS_DB_DIR=deep-agents-sdk/runtime/databases
```

Private run workspaces, temporary generated code, upload previews, and retained
artifacts are stored under:

```text
DEEP_AGENTS_GENERATED_DIR=deep-agents-sdk/runtime/generated
```

Relative paths resolve from the repository root. Absolute paths are recommended for deployment. The database directory must use a local filesystem when SQLite WAL mode is enabled. The server rejects storage paths that overlap `PROJECTS_DIR`, the public static directory, each other, or an unsafe broad directory.

The generated root has one consistent layout:

```text
generated/
├── work/<user>/<project>/<session>/<run>/
├── artifacts/<user>/<project>/<session>/<run>/
└── uploads/<user>/<preview-token>.zip
```

Run workspaces and temporary generated code are deleted after each run. Registered artifacts remain until their owning session or project is deleted or pruned. `DEEP_AGENTS_RUN_ARTIFACT_DIR` and `DEEP_AGENTS_PROJECT_DIR` are injected into generated Python processes for the active run; they are not deployment settings and must not be added to `.env`.

`MAX_CONCURRENT_RUNS_PER_USER` limits simultaneous tasks per authenticated user; the default is `3`.

## Local development

Enable the same-port development identity popup in `.env`:

```text
DEEP_AGENTS_DEV_LOGIN_ENABLED=true
```

Then run the single FastAPI application directly:

```bash
cd deep-agents-sdk
./venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 9010
```

Open [http://localhost:9010](http://localhost:9010).

The UI and API are served by the same process and port. Whenever no `x-fnma-jws-token` header is present, the app displays a themed identity popup before loading the workspace. Enter a subject and choose whether it has `PROJECT_ADMIN`; a prior development selection is prefilled for convenience. The server signs the selection and stores it in an HTTP-only, same-site cookie for 12 hours. Its development signing key is persisted under `DEEP_AGENTS_DB_DIR`, so the cookie remains valid across server restarts. A real CDX header always takes precedence over this cookie and bypasses the popup.

After frontend changes, rebuild before refreshing the FastAPI-served application:

```bash
cd deep-agents-sdk/frontend
npm run build
```

For continuous same-port frontend development, run `npm run build -- --watch` in one terminal and Uvicorn in another. The browser still uses only `http://localhost:9010`; Vite writes updated assets for FastAPI to serve.

The development popup is deliberately controlled by `DEEP_AGENTS_DEV_LOGIN_ENABLED`. Keep it unset or `false` in production. Enabling it lets any caller reaching the application choose a subject and grant itself `PROJECT_ADMIN`; it is not a substitute for CDX.

Do not enable Uvicorn `--reload` while prompts are running. Agent tool calls create temporary Python files, and a source watcher can interpret those files as application changes, restart the process, and interrupt every active run. Stop active prompts before using reload mode for application development.

In production, run the application as an internal backend and route CDX to it. For example, from the repository root:

```bash
cd deep-agents-sdk
./venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 9010
```

Choose the internal host and port required by the deployment, but do not expose that listener around CDX. Use a single uvicorn worker while the application uses local SQLite and in-process agent graph caches.

## Authorization policy

| Resource/action | Authenticated user | `PROJECT_ADMIN` |
|---|---:|---:|
| List and view shared projects | Yes | Yes |
| View shared project contents/media | Yes | Yes |
| Create, rename, import, edit, clear, or delete projects | No | Yes |
| View own sessions, messages, and task runs | Yes | Yes |
| View another user's sessions, messages, or artifacts | No | No |
| Download own generated artifacts | Yes | Yes |
| Update global settings | No | Yes |

Administrator status does not grant access to other users' private chats or artifacts.

## API overview

Authentication:

- `GET /api/auth/config` — public bootstrap state for CDX or the development popup
- `POST /api/auth/development-login` — available only when development login is enabled
- `GET /api/auth/me`

Settings:

- `GET /api/settings`
- `PUT /api/settings` — admin only

Shared projects:

- `GET /api/projects`
- `GET /api/projects/{project_id}`
- `GET /api/projects/{project_id}/contents` — compatibility listing for project contents
- `GET /api/projects/{project_id}/directory?path=...` — lazy, immediate-child directory listing
- `GET /api/projects/{project_id}/file-search?query=...` — project filename/path search
- `GET /api/projects/{project_id}/file-preview?path=...` — safe format-aware preview metadata/content
- `GET /api/projects/{project_id}/file-inline?path=...` — inline image, PDF, audio, or video bytes
- `GET /api/projects/{project_id}/file-download?path=...` — protected project-file download
- `GET /api/projects/{project_id}/entry-info?path=...` — file/folder size and delete-impact summary
- `GET /api/projects/{project_id}/content-summary` — current content counts and size
- `GET /api/projects/{project_id}/export` — download visible project content as a ZIP
- `GET /api/projects/{project_id}/media/{path}`
- `POST /api/projects` — admin only
- `PUT /api/projects/{project_id}` — admin only
- `POST /api/projects/{project_id}/folders` — add a folder, admin only
- `POST /api/projects/{project_id}/files?parent_path=...` — add one file, admin only
- `PUT /api/projects/{project_id}/files?path=...` — atomically replace one file, admin only
- `DELETE /api/projects/{project_id}/entries?path=...` — delete one file or folder tree, admin only
- `DELETE /api/projects/{project_id}` — admin only
- `DELETE /api/projects/{project_id}/contents` — admin only
- `GET /api/projects/{project_id}/isolation` — admin only

Admin imports:

- `POST /api/uploads/preview`
- `POST /api/projects/import`
- `POST /api/projects/{project_id}/contents/import`

Upload preview tokens are user-bound, short-lived, and single-use. ZIP paths, symlinks, entry counts, compressed upload size, and expanded size are validated. Project content is staged and swapped rather than destructively extracted in place.

The project **Import** action keeps existing content by default and overwrites only paths supplied by the ZIP. When the project already contains files or custom folders, an explicit **Replace all current project content** checkbox is shown; selecting it clears the staged project copy before importing the ZIP. The **Export** action is available to authenticated users and creates a temporary ZIP containing visible files and folders while excluding hidden paths and symlinks. The temporary server archive is deleted after the download response completes.

The **Project Contents** action opens a Finder-style file explorer that loads one folder at a time, supports filename/path search, and keeps large projects responsive. It previews Markdown, source code, UTF-8 text, CSV/TSV data, common image/audio/video formats, and PDFs (including page navigation and zoom). Unsupported formats remain downloadable.

Authenticated users can browse, preview, and download shared project content. A `PROJECT_ADMIN` also gets contextual actions to create folders, add files, replace files, and delete files or folder trees. Replacement preserves the existing path and requires the same extension. Deletions show the number and size of affected files and require explicit confirmation. The top-level `data/` and `skills/` directories are protected from deletion. Individual uploads are limited to 250 MB, and names and paths reject traversal, hidden components, symlinks, and non-portable reserved characters.

Content mutations are intentionally serialized per project and are rejected while that project has an active task run. Files are staged and published atomically, then the project's `content_revision` is incremented and the mutation is written to the audit log in the same database transaction. A failure before the database commit restores the prior filesystem state. This is a deliberately simple consistency model for a single project administrator; it avoids unnecessary optimistic-locking UI while preventing prompts from reading a half-edited project.

Private chats and runs:

- `GET /api/projects/{project_id}/sessions`
- `POST /api/projects/{project_id}/sessions`
- `GET /api/projects/{project_id}/sessions/{session_id}`
- `DELETE /api/projects/{project_id}/sessions/{session_id}`
- `GET /api/projects/{project_id}/sessions/{session_id}/runs`
- `POST /api/chat`
- `POST /api/chat/stream`

Private generated files:

- `GET /api/artifacts/{artifact_id}`

Session and artifact lookups always include the authenticated internal user ID. Unknown and non-owned private resource IDs both return `404`.

## Project skills

Each project keeps skills under `skills/<skill-name>/SKILL.md`. On project selection, the server scans that project's skill metadata. The project agent graph is shared, while each run uses a user/project/session checkpoint thread and a user-specific artifact directory.

Every successful `data/` or `skills/` edit increments `content_revision`. Data-only changes are visible to the next prompt through the shared project path and do not rebuild the agent graph. Changes under `skills/` are validated (including `SKILL.md` frontmatter and directory/name agreement) before publication, invalidate the cached project agent, and cause skills to be loaded lazily before the next prompt. Existing running prompts are never hot-reloaded because project edits are blocked until all project runs finish.

Generated Python runs from a private per-user/project/session/run workspace, not from the shared project directory. Project data remains readable as `data/<filename>` through a workspace link. Generated files should be written to the `artifact_directory` returned by `get_project_context` (or `DEEP_AGENTS_RUN_ARTIFACT_DIR` inside Python). Relative generated files are automatically moved into the same run artifact directory before registration. The server audits the shared project tree and quarantines newly created project files as private artifacts; modifications or deletions of existing shared source files are logged and reported as execution errors.

## Tests

```bash
./deep-agents-sdk/venv/bin/pip install -r deep-agents-sdk/requirements-dev.txt
./deep-agents-sdk/venv/bin/ruff check deep-agents-sdk/deep_agents_app deep-agents-sdk/server.py deep-agents-sdk/tests --select E4,E7,E9,F
./deep-agents-sdk/venv/bin/vulture deep-agents-sdk/deep_agents_app deep-agents-sdk/server.py deep-agents-sdk/tests --min-confidence 80
./deep-agents-sdk/venv/bin/python -m unittest discover -s deep-agents-sdk/tests -v
cd deep-agents-sdk/frontend
npm run deadcode
npm run lint
npm run typecheck
npm test
npm run build
npm run test:e2e
```

The Playwright suite copies project sources into a disposable workspace and uses separate temporary databases and generated-output directories. Its edit tests therefore cannot mutate development projects, sessions, or artifacts, even when a test fails.

Optional browser-level tests use Playwright after the production frontend has been built:

```bash
cd deep-agents-sdk/frontend
npx playwright install chromium
npm run test:e2e
```

The suites cover the trusted-CDX claim contract, mock-CDX identity switching, header enforcement, admin authorization, shared project visibility, cross-user session isolation, per-user pruning, checkpoint namespacing, private artifact access and cleanup, private Python workspaces, recovery of misplaced project outputs, upload ownership, ZIP traversal rejection, project-local skill discovery, Markdown rendering, prompt metadata, and concurrent run-state isolation.

## Security boundary

Markdown responses are sanitized and the server sends a restrictive Content Security Policy. React, Marked, DOMPurify, KaTeX, and Highlight.js are compiled into self-hosted frontend assets; runtime script CDNs are not allowed. The production browser does not receive or persist the CDX header token. The mock-CDX development identity cookie is HTTP-only and restricted to development usage.

Generated Python execution is not sandboxed. It runs as the server's operating-system user. Deploy this version only for trusted internal users and trusted project administrators.
