# Deep Agents HPI Workspace Application

This workspace contains an interactive web application that implements a multi-agent **Orchestrator-Worker** model (using the LangChain Deep Agents SDK). The app supports multiple projects, where each project owns its own data, skills, resources, and chat sessions.

The application features a FastAPI REST server that drives a supervisor coordinator agent and dynamically routes tasks to specialized workers that explore data, execute analysis scripts, and generate comparative charts.

---

## 1. Directory Structure

```text
langchain-deepagents/
├── README.md
├── .env.template
├── projects/
│   └── hpi-analytics/
│       ├── data/
│       │   ├── hpi_master.csv
│       │   └── hpi_dictionary.xlsx
│       └── skills/
│           └── hpi-analysis/
│               ├── SKILL.md
│               └── references/
├── examples/
├── deep-agents-sdk/
│   ├── requirements.txt
│   ├── server.py
│   ├── static/
│   └── venv/
```

---

## 2. Prerequisites

1. **Portkey API Gateway**: Setup a Portkey account to access the Gateway.
2. **Portkey LLM Integration**: Configure a Portkey provider slug that supports the models listed in `AVAILABLE_MODELS`.
3. **Projects**: The default `HPI Analytics` project is stored under `projects/hpi-analytics/`.

---

## 3. Setup and Installation

All setup and execution tasks must use the SDK-local virtual environment (`deep-agents-sdk/venv`):

1. **Activate/Create Virtual Environment**:
   If `deep-agents-sdk/venv` does not exist, initialize it:
   ```bash
   python3 -m venv deep-agents-sdk/venv
   ```

2. **Install Dependencies**:
   Install the required Python packages from the SDK package:
   ```bash
   ./deep-agents-sdk/venv/bin/pip install -r deep-agents-sdk/requirements.txt
   ```

---

## 4. Running the Web Application

1. **Configure Environment Variables**:
   Copy `.env.template` to `.env` in the **project root folder**, then customize the values for your environment:
   ```bash
   cp .env.template .env
   ```
   The `.env` file uses these keys:
   ```text
   PORTKEY_API_KEY=your_portkey_api_key_here
   PORTKEY_PROVIDER_SLUG=your_provider_slug_in_portkey
   AVAILABLE_MODELS=gpt-5.5,gpt-5.4,gpt-5.4-codex
   DEFAULT_MODEL=gpt-5.5
   MAX_SESSIONS_PER_PROJECT=5
   TEMPERATURE=1.0
   PROJECTS_DIR=projects
   ```
   Runtime Settings changes are stored in `deep-agents-sdk/checkpoints.db`; `.env` provides the model allowlist and initial defaults.

2. **Start the FastAPI Server**:
   Launch the uvicorn development server on port 9010:
   ```bash
   ./deep-agents-sdk/venv/bin/python -m uvicorn deep-agents-sdk.server:app --reload --port 9010
   ```

3. **Access the Interface**:
   Open your browser and navigate to:
   ```text
   http://localhost:9010
   ```

---

## 5. REST API Endpoints

- **`GET /`**: Serves the Chat UI frontend (`index.html`).
- **`GET /api/settings`**: Returns available models, the selected default model, and the project session limit.
- **`PUT /api/settings`**: Updates the default model and max sessions per project.
- **`GET /api/projects`**: Lists available projects.
- **`POST /api/projects`**: Creates an empty project.
- **`DELETE /api/projects/{project_id}`**: Deletes the project record and its folder from disk.
- **`GET /api/projects/{project_id}/contents`**: Lists project files.
- **`POST /api/uploads/preview`**: Uploads a ZIP and returns its contents for review before import.
- **`POST /api/projects/import`**: Creates a new project from a previewed ZIP.
- **`POST /api/projects/{project_id}/contents/import`**: Merges or replaces project content from a previewed ZIP.
- **`GET /api/projects/{project_id}/sessions`**: Lists chat sessions for a project.
- **`POST /api/projects/{project_id}/sessions`**: Creates a new chat session.
- **`GET /api/projects/{project_id}/sessions/{session_id}`**: Loads a chat session and its visible messages.
- **`POST /api/chat`**: Project-scoped chat handler interface.
  - **Request Body**:
    ```json
    {
      "project_id": "hpi-analytics",
      "session_id": "optional-existing-session-id",
      "message": "Calculate California's quarterly HPI growth rate from Q1 2010 to Q1 2020.",
    }
    ```
  - **Response Body**:
    ```json
    {
      "project_id": "hpi-analytics",
      "session_id": "session-id",
      "response": "Based on the HPI dataset, California (CA) experienced a growth of approximately 79.40%...",
    }
    ```
- **`GET /static/*`**: Serves static CSS, client JS, and generated charts under `/static/charts/`.
- **`GET /examples/*`**: Serves user-saved images from the custom output directory.

---

## 6. Dynamic Skill Matching (Option B)

This implementation uses **Universal Specialist Injection** to dynamically scale skill loading:
* **Scanning:** The supervisor agent (`server.py`) scans all subfolders under the selected project's `skills/` folder to retrieve descriptions of available skills from their `SKILL.md` frontmatter.
* **Semantic Routing:** The supervisor matches user requests semantically using these descriptions, making a tool call to `specialist_worker`.
* **On-Demand Loading:** The `specialist_worker` tool lazy-loads the requested skill's guidelines and schema dynamically to spin up a transient specialist worker, keeping the supervisor's context window clean and lightweight.
* **Project Isolation:** Agent graphs, skill registries, chat sessions, and generated chart paths are scoped to the selected project.
