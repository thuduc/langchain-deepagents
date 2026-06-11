# Deep Agents HPI Workspace Application

This application wraps the data-centric `hpi_analysis` skill in an interactive web application. It features a FastAPI REST server that drives a LangChain/LangGraph agent and serves a premium glassmorphic Chat UI. The agent dynamically writes, executes, and visualizes analyses on FHFA Home Price Index (HPI) data.

---

## 1. Directory Structure

```text
deep-agents/
├── README.md                  # This documentation file
├── implementation_plan.md     # Design and architecture details
├── requirements.txt           # Python dependency specifications
├── server.py                  # FastAPI server and ReAct agent graph
├── list_models.py             # Diagnostic model utility
└── static/                    # Frontend UI web assets
    ├── index.html             # UI layout structure
    ├── styles.css             # Glassmorphism dark-mode styles
    ├── app.js                 # API communication & markdown parser
    └── charts/                # Dynamically generated HPI charts (served publicly)
```

---

## 2. Prerequisites
1. **Portkey API Gateway**: Set up a Portkey account and get your API key.
2. **Google AI Studio Integration**: Integrate Google AI Studio with Portkey and obtain the provider slug.
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
   Install the required Python packages:
   ```bash
   ./venv/bin/pip install -r deep-agents/requirements.txt
   ```

---

## 4. Running the Web Application

1. **Configure Environment Variables**:
   Create a `.env` file in the **project root folder** with the following keys:
   ```text
   PORTKEY_API_KEY=your_portkey_api_key_here
   PORTKEY_PROVIDER_SLUG=your_google_provider_slug_in_portkey
   ```

2. **Start the FastAPI Server**:
   Launch the uvicorn development server on port 9000:
   ```bash
   ./venv/bin/python -m uvicorn deep-agents.server:app --reload --port 9000
   ```

3. **Access the Interface**:
   Open your browser and navigate to:
   ```text
   http://localhost:9000
   ```

---

## 5. REST API Endpoints

- **`GET /`**: Serves the Chat UI frontend (`index.html`).
- **`POST /api/chat`**: Chat handler interface.
  - **Request Body**:
    ```json
    {
      "message": "Calculate California's HPI growth between 2010 and 2020.",
      "session_id": "optional-custom-session-id"
    }
    ```
  - **Response Body**:
    ```json
    {
      "response": "The HPI growth was...",
      "session_id": "optional-custom-session-id"
    }
    ```
- **`GET /static/*`**: Serves static CSS, client JS, and generated charts under `/static/charts/`.

---

## 6. Example Prompts to Try in the UI

*   *"Calculate California's quarterly HPI growth rate from Q1 2010 to Q1 2020."*
*   *"What are the top 5 states with the highest HPI growth between Q1 2020 and Q1 2025?"*
*   *"Generate a line chart comparing the quarterly HPI trend from 2015 to 2025 for California, New York, and Texas. Save it as a PNG."*
