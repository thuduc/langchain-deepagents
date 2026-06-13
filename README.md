# Deep Agents HPI Workspace Application

This workspace contains an interactive web application that implements a multi-agent **Orchestrator-Worker** model (using the LangChain Deep Agents SDK) to analyze US Federal Housing Finance Agency (FHFA) House Price Index (HPI) data.

The application features a FastAPI REST server that drives a supervisor coordinator agent and dynamically routes tasks to specialized workers that explore data, execute analysis scripts, and generate comparative charts.

---

## 1. Directory Structure

```text
langchain-deepagents/
├── README.md                  # This root documentation file
├── .env-template              # Template for configuring environment variables
├── data/                      # Contains raw CSV/Excel housing datasets
├── skills/                    # Repository of specialist agent skills (e.g. hpi_analysis)
│   └── hpi_analysis/
│       ├── SKILL.md           # Instructions/Rules for the HPI Specialist Worker
│       └── references/        # Schema definition & metadata files
├── examples/                  # Destination for user-saved HPI charts
├── deep-agents-sdk/           # The active SDK-based implementation
│   ├── requirements.txt       # Python dependencies for the SDK server
│   ├── server.py              # FastAPI server, supervisor, & specialist node tool
│   └── static/                # Gemini-style Web UI (HTML, CSS, JS)
└── venv/                      # Local Python virtual environment
```

---

## 2. Prerequisites

1. **Portkey API Gateway**: Setup a Portkey account to access the Gateway.
2. **Google AI Studio Integration**: Integrate Google AI Studio (Gemini 2.5 Flash) with Portkey and obtain the provider slug.
3. **Datasets**: Ensure that `data/hpi_master.csv` and `data/hpi_dictionary.xlsx` exist in the `data/` folder at the root of the repository.

---

## 3. Setup and Installation

All setup and execution tasks must be run inside the project's local virtual environment (`venv`):

1. **Activate/Create Virtual Environment**:
   If the `venv` directory does not exist, initialize it:
   ```bash
   python3 -m venv venv
   ```

2. **Install Dependencies**:
   Install the required Python packages from the SDK package:
   ```bash
   ./venv/bin/pip install -r deep-agents-sdk/requirements.txt
   ```

---

## 4. Running the Web Application

1. **Configure Environment Variables**:
   Create a `.env.portkey` file (or `.env` file) in the **project root folder** with the following keys:
   ```text
   PORTKEY_API_KEY=your_portkey_api_key_here
   PORTKEY_PROVIDER_SLUG=your_google_provider_slug_in_portkey
   MODEL=gemini-2.5-flash
   TEMPERATURE=0.0
   SKILLS_DIR=skills
   ```

2. **Start the FastAPI Server**:
   Launch the uvicorn development server on port 9010:
   ```bash
   ./venv/bin/python -m uvicorn deep-agents-sdk.server:app --reload --port 9010
   ```

3. **Access the Interface**:
   Open your browser and navigate to:
   ```text
   http://localhost:9010
   ```

---

## 5. REST API Endpoints

- **`GET /`**: Serves the Chat UI frontend (`index.html`).
- **`POST /api/chat`**: Chat handler interface.
  - **Request Body**:
    ```json
    {
      "message": "Calculate California's quarterly HPI growth rate from Q1 2010 to Q1 2020.",
      "session_id": "optional-custom-session-id"
    }
    ```
  - **Response Body**:
    ```json
    {
      "response": "Based on the HPI dataset, California (CA) experienced a growth of approximately 79.40%...",
      "session_id": "optional-custom-session-id"
    }
    ```
- **`GET /static/*`**: Serves static CSS, client JS, and generated charts under `/static/charts/`.
- **`GET /examples/*`**: Serves user-saved images from the custom output directory.

---

## 6. Dynamic Skill Matching (Option B)

This implementation uses **Universal Specialist Injection** to dynamically scale skill loading:
* **Scanning:** The supervisor agent (`server.py`) scans all subfolders under `SKILLS_DIR` at startup/invocation to retrieve descriptions of available skills from their `SKILL.md` frontmatter.
* **Semantic Routing:** The supervisor matches user requests semantically using these descriptions, making a tool call to `specialist_worker`.
* **On-Demand Loading:** The `specialist_worker` tool lazy-loads the requested skill's guidelines and schema dynamically to spin up a transient specialist worker, keeping the supervisor's context window clean and lightweight.
