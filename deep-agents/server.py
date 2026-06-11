import os
import sys
from dotenv import load_dotenv

# Load environment variables from .env at project root
load_dotenv()
import subprocess
import uuid
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# FastAPI setup
app = FastAPI(title="HPI Agent Skill Interface")

# Ensure static directories exist
os.makedirs("deep-agents/static/charts", exist_ok=True)

# Define models
class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"

class ChatResponse(BaseModel):
    response: str
    session_id: str

# Message history storage (in-memory).
# Stores the full LangChain message trace per session (including tool-call and
# tool-result turns). Replaying text-only history strips the function-calling
# context, which makes Gemini prone to MALFORMED_FUNCTION_CALL empty responses.
session_history: Dict[str, List[Any]] = {}

# Load SKILL.md dynamically
def get_system_prompt() -> str:
    skill_path = "skills/hpi_analysis/SKILL.md"
    metadata_path = "skills/hpi_analysis/references/hpi_metadata.json"
    
    prompt = "You are an advanced agentic coding assistant for HPI (House Price Index) analysis.\n"
    
    if os.path.exists(skill_path):
        with open(skill_path, "r") as f:
            prompt += f"\n--- Skill Guidelines ---\n{f.read()}\n"
    if os.path.exists(metadata_path):
        with open(metadata_path, "r") as f:
            prompt += f"\n--- Dataset Metadata ---\n{f.read()}\n"
            
    prompt += "\nCore Objective: Analyze the user's request, formulate python code to explore the data or plot charts, run the code using the execute_python tool, and present the final answer to the user. Always use index_nsa unless seasonally adjusted (index_sa) is specifically requested. If the user asks for charts, save them to 'deep-agents/static/charts/<filename>.png' and return a standard markdown image link: ![Chart](/static/charts/<filename>.png)."
    return prompt

# Tools implementation
def tool_read_file(path: str) -> str:
    """Read contents of a file."""
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {str(e)}"

def tool_write_file(path: str, content: str) -> str:
    """Write content to a file."""
    try:
        # Prevent escaping parent folders for safety
        if ".." in path:
            return "Error: Cannot write outside the project structure."
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"File successfully written to {path}"
    except Exception as e:
        return f"Error writing file: {str(e)}"

def tool_execute_python(code: str) -> str:
    """Execute Python code in the local virtual environment."""
    # Write code to a temp file
    temp_file = f"temp_run_{uuid.uuid4().hex}.py"
    try:
        with open(temp_file, "w") as f:
            f.write(code)
            
        # Execute script using project's venv python binary
        python_bin = "./venv/bin/python"
        if not os.path.exists(python_bin):
            return "Error: Local virtual environment './venv' not found. Please initialize it."
            
        result = subprocess.run(
            [python_bin, temp_file],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        output = ""
        if result.stdout:
            output += f"Output:\n{result.stdout}\n"
        if result.stderr:
            output += f"Errors:\n{result.stderr}\n"
            
        if not output:
            output = "Execution completed successfully with no output."
            
        return output
    except subprocess.TimeoutExpired:
        return "Error: Code execution timed out after 30 seconds."
    except Exception as e:
        return f"Error executing code: {str(e)}"
    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass

# Agent executor singleton (building a new ChatGoogleGenerativeAI per request
# opens a fresh gRPC channel each time and never closes it)
_agent_executor = None

def get_agent_executor():
    global _agent_executor
    if _agent_executor is not None:
        return _agent_executor

    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent
    from langchain_core.tools import tool

    portkey_api_key = os.environ.get("PORTKEY_API_KEY")
    provider_slug = os.environ.get("PORTKEY_PROVIDER_SLUG", "google-ai-studio")

    model_name = os.environ.get("MODEL", "gemini-2.5-flash")
    
    # Dynamically select temperature (gpt-5.5 requires 1.0, standard models default to 0.0)
    temp_env = os.environ.get("TEMPERATURE")
    if temp_env is not None:
        try:
            temperature = float(temp_env)
        except ValueError:
            temperature = 0.0
    else:
        temperature = 1.0 if "gpt-5" in model_name else 0.0

    llm = ChatOpenAI(
        model=f"@{provider_slug}/{model_name}",
        temperature=temperature,
        base_url="https://api.portkey.ai/v1",
        api_key=portkey_api_key,
    )

    @tool
    def read_file(path: str) -> str:
        """Read a file in the project folder."""
        return tool_read_file(path)

    @tool
    def write_file(path: str, content: str) -> str:
        """Write content to a file."""
        return tool_write_file(path, content)

    @tool
    def execute_python(code: str) -> str:
        """Execute python code inside the virtual environment."""
        return tool_execute_python(code)

    tools = [read_file, write_file, execute_python]
    _agent_executor = create_react_agent(llm, tools, prompt=get_system_prompt())
    return _agent_executor

def message_content_to_text(content: Any) -> str:
    """Flatten message content to a plain string.

    Gemini responses with multiple parts come back as a list of strings or
    {'type': 'text', ...} dicts, which would fail the `response: str` schema.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "\n\n".join(p for p in parts if p.strip())
    return str(content)

# Core Agent Executor (runs standard tool-calling loop using Google Generative AI if key is set, otherwise falls back gracefully)
def run_agent(session_id: str, prompt: str) -> str:
    # Check if Portkey API key exists
    portkey_key = os.environ.get("PORTKEY_API_KEY")
    if not portkey_key:
        # Mock/Simulated agent response for dry-run/testing if no API key is available
        return simulate_agent_response(prompt)

    try:
        from langchain_core.messages import HumanMessage

        agent = get_agent_executor()
        history = session_history.get(session_id, [])
        input_msgs = history + [HumanMessage(content=prompt)]

        # Gemini sporadically ends a turn with finish_reason=MALFORMED_FUNCTION_CALL
        # and empty content; retry once before giving up.
        for _attempt in range(2):
            result = agent.invoke({"messages": input_msgs}, config={"recursion_limit": 100})
            response_text = message_content_to_text(result["messages"][-1].content)
            if response_text.strip():
                # Persist the full trace only on success so an empty/failed turn
                # never poisons the replayed history for the rest of the session.
                session_history[session_id] = result["messages"]
                return response_text

        return (
            "The model returned an empty response for this request. "
            "Please try again or rephrase your prompt."
        )

    except Exception as e:
        return f"Error invoking agent runner: {str(e)}. Please check your environment configuration and PORTKEY_API_KEY."

def simulate_agent_response(prompt: str) -> str:
    """Provides high-quality mock data/plot responses for local manual API testing when LLM API keys are absent."""
    p_lower = prompt.lower()
    
    # California growth query
    if "california" in p_lower and "growth" in p_lower:
        # Execute direct python test code to extract actual data
        code = """
import pandas as pd
df = pd.read_csv('data/hpi_master.csv', dtype={'note': str, 'place_id': str}, low_memory=False)
ca = df[(df['level'] == 'State') & (df['place_id'] == 'CA') & (df['hpi_type'] == 'traditional') & (df['frequency'] == 'quarterly')]
v1 = ca[(ca['yr'] == 2010) & (ca['period'] == 1)]['index_nsa'].values[0]
v2 = ca[(ca['yr'] == 2020) & (ca['period'] == 1)]['index_nsa'].values[0]
print(f"VALS: {v1}, {v2}, {((v2-v1)/v1)*100:.2f}%")
"""
        out = tool_execute_python(code)
        return f"Based on the HPI dataset, California (CA) experienced a growth of approximately **79.40%** between Q1 2010 and Q1 2020.\n\n*Details of calculation (computed via virtual environment python):*\n```\n{out}\n```"
        
    # Plotting request
    if "plot" in p_lower or "chart" in p_lower:
        # Generate chart using execute_python
        chart_id = uuid.uuid4().hex[:6]
        code = f"""
import pandas as pd
import matplotlib.pyplot as plt
df = pd.read_csv('data/hpi_master.csv', dtype={{'note': str, 'place_id': str}}, low_memory=False)
plt.figure(figsize=(8, 4))
plt.grid(True, linestyle='--', alpha=0.5)
for s in ['CA', 'NY', 'TX']:
    sdf = df[(df['level'] == 'State') & (df['place_id'] == s) & (df['frequency'] == 'quarterly') & (df['yr'] >= 2015)].sort_values(['yr', 'period'])
    plt.plot(sdf['yr'] + (sdf['period']-1)/4.0, sdf['index_nsa'], label=s, linewidth=2)
plt.title("HPI Comparison (2015 - Present)")
plt.legend()
plt.tight_layout()
plt.savefig('deep-agents/static/charts/hpi_{chart_id}.png', dpi=150)
"""
        tool_execute_python(code)
        return f"Here is the housing price index comparison chart for CA, NY, and TX from 2015 to present:\n\n![HPI Comparison Chart](/static/charts/hpi_{chart_id}.png)"
        
    return f"I received your request: '{prompt}'. (LLM API key is not set. Please set the PORTKEY_API_KEY and PORTKEY_PROVIDER_SLUG environment variables to enable full multi-step agent reasoning.)"

# Routes
@app.post("/api/chat", response_model=ChatResponse)
def api_chat(request: ChatRequest):
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    response_text = run_agent(request.session_id, request.message)
    return ChatResponse(response=response_text, session_id=request.session_id)

@app.get("/")
def get_index():
    return FileResponse("deep-agents/static/index.html")

# Serve static assets
app.mount("/static", StaticFiles(directory="deep-agents/static"), name="static")
