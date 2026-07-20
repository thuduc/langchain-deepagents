# requirements to add support for projects to deep-agents-sdk

Current the deep-agents-sdk implementation supports skills under skills/ folder. These skills refer to data residing under data/ . We need to add support for multiple projects, each with its own skills, data, and resources. Here are the requirements for multi-project support:

- Add a new variable to `.env` (and also the default template `.env.template`) called `PROJECTS_DIR`. This will point to the parent folder of all projects. The default value is `projects`. The `SKILLS_DIR` is no longer used. If skills exist for a project, they need to exist under a `skills` folder of that project. Under the skills subfolder, there could be multiple subfolders, each with its own skill. Every skill needs to be named `SKILL.md`.
- Each project will have a project name, whose name will be used to create a directory for it under the projects folder as defined in PROJECTS_DIR variable
- Each project will have multiple child folders. Currently we only use 2 child folders: data and skills. But others could be added and used per project.
- Use a database table to maintain a list of projects
- Create a new project called "HPI Analytics" under the projects/ folder. Move the skills/hpi-analysis/SKILL.md to this project's skills child folder. Also move the data directory, currently under the project's root, to under this new project
- The UI should be updated to look similar to the OpenAI Codex app, where the left pane shows a list of current projects.
- Add the ability to add a new project as well as the following:
  - Allow user to upload a zip file containing data, skills, etc.
  - The content of this zip file should be extracted under the new project's folder residing under PROJECTS_DIR
  - Allow the user to view the content of the zip before importing
  - When importing uploaded content, allow both merge mode and replace mode
- Allow users to view the contents of the selected project from the left pane
- Allow users to remove and upload new content of selected project
- When a user deletes a project, remove both its database record and its project folder from disk
- To issue prompts, users must first select a project. Upon selection, the system must load all skills metadata of the selected project into the skills registry, as currently done in the current implementation. In other word, each project has its own skills registry. This is critical dynamic loading of skills per prompt for the selected project
- For each project, users could have multiple chat sessions. Save each chat session so users can go back and view or continue where they left off
- The above functionality is very similar to how the current OpenAI Codex app works, except that is our implementation, we allow users to upload contents of each project. Also the skills registry for each project must be dynamically loaded upon selection

## Multi-user and storage requirements

- CDX performs OIDC login/logout and cryptographically validates the JWT before overwriting the `x-fnma-jws-token` header. Every API request requires that header. The application decodes the trusted token and uses its `sub` claim in the fixed `cdx` identity namespace.
- Projects, their data, and their skills are shared and readable by every authenticated user.
- Sessions, prompts, task runs, checkpoints, and generated artifacts are private to their owning user.
- Only a trusted CDX token whose `roles` claim contains `PROJECT_ADMIN` may create, rename, import, replace, clear, or delete projects or change global settings.
- Session limits apply independently to each user and project.
- Generated artifacts are served through ownership-checked API endpoints, never through a public static mount.
- `DEEP_AGENTS_DB_DIR` controls the local directory containing SQLite databases and their WAL/SHM sidecars.
- `DEEP_AGENTS_GENERATED_DIR` is the only generated-output root. It contains private temporary workspaces, upload previews, and retained artifacts under user/project/session/run ownership paths. `DEEP_AGENTS_RUN_ARTIFACT_DIR` is injected into generated Python for the active run and is not a configurable environment setting.
- Generated Python executes from its private run workspace. Shared project `data/` is exposed there for reads; relative outputs are collected into the run artifact directory, and unexpected new files under the shared project are quarantined as private run artifacts.
- Existing unowned runtime state is discarded during the multi-user cutover; shared project directories are rediscovered from `PROJECTS_DIR`.
