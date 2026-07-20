# Deep Agents Project Workspace

This repository contains a FastAPI web application that uses the LangChain Deep Agents SDK to coordinate project-specific data-analysis workers. Projects and their source content are shared. Chat sessions, task runs, checkpoints, and generated artifacts are private to the authenticated user.

## Architecture

```text
Browser UI
  -> CDX (OIDC login/logout and JWT validation)
     -> x-fnma-jws-token header
        -> FastAPI REST and streaming APIs
           -> shared projects, skills, and source data
           -> user-owned chats, runs, checkpoints, and artifacts
           -> Deep Agents supervisor and project skill workers
```

CDX is the authentication boundary. The application trusts the header supplied by CDX, uses the JWT `sub` claim as the user identity, and uses the fixed `roles` claim for authorization. Only users with `PROJECT_ADMIN` may create, rename, import, replace, clear, or delete projects and update global settings.

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
    ├── auth.py
    ├── runtime_config.py
    ├── server.py
    ├── requirements.txt
    ├── static/
    └── tests/
```

Runtime databases and generated outputs are intentionally excluded from Git.

## Setup

Create the SDK-local virtual environment and install constrained dependencies:

```bash
python3 -m venv deep-agents-sdk/venv
./deep-agents-sdk/venv/bin/pip install -r deep-agents-sdk/requirements.txt
```

Copy the environment template:

```bash
cp .env.template .env
```

Configure the Portkey gateway, model allowlist, storage roots, and optional development login in `.env`.

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
| Create, rename, import, clear, or delete projects | No | Yes |
| View own sessions, messages, and task runs | Yes | Yes |
| View another user's sessions, messages, or artifacts | No | No |
| Download own generated artifacts | Yes | Yes |
| Update global settings | No | Yes |

Administrator status does not grant access to other users' private chats or artifacts.

## API overview

Authentication:

- `GET /api/auth/me`

Settings:

- `GET /api/settings`
- `PUT /api/settings` — admin only

Shared projects:

- `GET /api/projects`
- `GET /api/projects/{project_id}`
- `GET /api/projects/{project_id}/contents`
- `GET /api/projects/{project_id}/media/{path}`
- `POST /api/projects` — admin only
- `PUT /api/projects/{project_id}` — admin only
- `DELETE /api/projects/{project_id}` — admin only
- `DELETE /api/projects/{project_id}/contents` — admin only
- `GET /api/projects/{project_id}/isolation` — admin only

Admin imports:

- `POST /api/uploads/preview`
- `POST /api/projects/import`
- `POST /api/projects/{project_id}/contents/import`

Upload preview tokens are user-bound, short-lived, and single-use. ZIP paths, symlinks, entry counts, compressed upload size, and expanded size are validated. Project content is staged and swapped rather than destructively extracted in place.

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

Generated Python runs from a private per-user/project/session/run workspace, not from the shared project directory. Project data remains readable as `data/<filename>` through a workspace link. Generated files should be written to the `artifact_directory` returned by `get_project_context` (or `DEEP_AGENTS_RUN_ARTIFACT_DIR` inside Python). Relative generated files are automatically moved into the same run artifact directory before registration. The server audits the shared project tree and quarantines newly created project files as private artifacts; modifications or deletions of existing shared source files are logged and reported as execution errors.

## Tests

```bash
./deep-agents-sdk/venv/bin/python -m unittest discover -s deep-agents-sdk/tests -v
node --check deep-agents-sdk/static/app.js
```

The test suite covers the trusted-CDX claim contract, mock-CDX identity switching, header enforcement, admin authorization, shared project visibility, cross-user session isolation, per-user pruning, checkpoint namespacing, private artifact access and cleanup, private Python workspaces, recovery of misplaced project outputs, upload ownership, ZIP traversal rejection, and project-local skill discovery.

## Security boundary

Markdown responses are sanitized and the server sends a restrictive Content Security Policy. The production browser does not receive or persist the CDX header token. The mock-CDX development identity cookie is HTTP-only and restricted to localhost usage.

Generated Python execution is not sandboxed. It runs as the server's operating-system user. Deploy this version only for trusted internal users and trusted project administrators.
