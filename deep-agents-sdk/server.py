import os
import sys
import uuid
import subprocess
import sqlite3
from typing import List, Dict, Any
from dotenv import load_dotenv

# Load environment variables from .env at project root
load_dotenv()
if not os.environ.get("PORTKEY_API_KEY") and os.path.exists(".env.portkey"):
    load_dotenv(".env.portkey")

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# FastAPI setup
app = FastAPI(title="HPI Agent Skill Interface (Deep Agents SDK)")

# Ensure static and chart directories exist under deep-agents-sdk
os.makedirs("deep-agents-sdk/static/charts", exist_ok=True)

# Define models
class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"

class ChatResponse(BaseModel):
    response: str
    session_id: str

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
            
    prompt += "\nCore Objective: Analyze the user's request, formulate python code to explore the data or plot charts, run the code using the execute_python tool, and present the final answer to the user. Always use index_nsa unless seasonally adjusted (index_sa) is specifically requested. If the user asks for charts, save them to 'deep-agents-sdk/static/charts/<filename>.png' and return a standard markdown image link: ![Chart](/static/charts/<filename>.png). If the user explicitly requests to save the chart to another custom location (like the 'examples' directory), write the python code to save it there, and also return a standard markdown image link using the mounted path so that it renders in the chat log: e.g. ![Chart](/examples/<filename>.png)."
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
    temp_file = f"temp_run_{uuid.uuid4().hex}.py"
    try:
        with open(temp_file, "w") as f:
            f.write(code)
            
        # Execute script using the deep-agents-sdk local venv python binary
        python_bin = "./deep-agents-sdk/venv/bin/python"
        if not os.path.exists(python_bin):
            python_bin = "./venv/bin/python"
            
        if not os.path.exists(python_bin):
            return "Error: Local virtual environment python binary not found."
            
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

# Agent graph singleton using the Deep Agents SDK and Portkey SDK
_agent_graph = None

def get_agent_graph():
    global _agent_graph
    if _agent_graph is not None:
        return _agent_graph

    from langchain_openai import ChatOpenAI
    from langchain_core.tools import tool
    from portkey_ai import createHeaders, PORTKEY_GATEWAY_URL
    from deepagents import create_deep_agent
    from langgraph.checkpoint.sqlite import SqliteSaver

    portkey_api_key = os.environ.get("PORTKEY_API_KEY")
    provider_slug = os.environ.get("PORTKEY_PROVIDER_SLUG", "google-ai-studio")
    model_name = os.environ.get("MODEL", "gemini-2.5-flash")
    
    # Choose temperature based on the selected model
    temp_env = os.environ.get("TEMPERATURE")
    if temp_env is not None:
        try:
            temperature = float(temp_env)
        except ValueError:
            temperature = 0.0
    else:
        temperature = 1.0 if "gpt-5" in model_name else 0.0

    # Configure headers using Portkey SDK
    headers = createHeaders(
        api_key=portkey_api_key,
        provider=provider_slug
    )

    # Initialize standard ChatOpenAI wrapper routing via Portkey Gateway URL
    llm = ChatOpenAI(
        model=f"@{provider_slug}/{model_name}" if not model_name.startswith("@") else model_name,
        temperature=temperature,
        base_url=PORTKEY_GATEWAY_URL,
        default_headers=headers,
        api_key=portkey_api_key,  # Needs a dummy/valid key to pass initialization checks
    )

    # Register custom tools for HPI analysis
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

    # Sub-agent blueprints
    hpi_analyst_blueprint = {
        "name": "hpi_analyst",
        "description": "Perform data analysis, HPI growth rate calculations, or generate charts on FHFA House Price Index (HPI) data. Can execute Python code.",
        "system_prompt": get_system_prompt(),
        "tools": [read_file, write_file, execute_python]
    }

    report_writer_blueprint = {
        "name": "report_writer",
        "description": "Write comprehensive summaries, articles, or blog posts. Synthesizes data files saved in the workspace.",
        "system_prompt": "You are a professional copywriter. Your goal is to read raw data, calculations, or files from the filesystem and format them into structured, beautiful reports or summaries.",
        "tools": [read_file, write_file]
    }

    # Initialize SQLite database with WAL and busy timeout for persistence
    db_path = "deep-agents-sdk/checkpoints.db"
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    checkpointer = SqliteSaver(conn)

    # Create the central coordinator (supervisor) agent using Deep Agents SDK
    _agent_graph = create_deep_agent(
        model=llm,
        tools=[],  # The supervisor delegates all tools to specialists
        subagents=[hpi_analyst_blueprint, report_writer_blueprint],
        system_prompt="You are a supervisor coordinator. Analyze the user prompt. Create a plan and delegate sub-tasks to the 'hpi_analyst' for data retrieval/calculations/charts, and to the 'report_writer' to write reports or formatted summaries. Present the final output back to the user.",
        checkpointer=checkpointer
    )
    return _agent_graph

def message_content_to_text(content: Any) -> str:
    """Flatten message content to a plain string."""
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

# Invoke Agent Runner
def run_agent(session_id: str, prompt: str) -> str:
    portkey_key = os.environ.get("PORTKEY_API_KEY")
    if not portkey_key:
        return simulate_agent_response(prompt)

    try:
        agent = get_agent_graph()
        
        # Configure the thread session identifier for LangGraph persistence
        config = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": 100
        }
        
        # Invoke deep agent graph
        result = agent.invoke(
            {"messages": [{"role": "user", "content": prompt}]}, 
            config=config
        )
        
        # In LangGraph/DeepAgents state structure, final AI turn is the last message
        messages = result.get("messages", [])
        if messages:
            last_msg = messages[-1]
            # Handle messages regardless of dict vs LangChain message class format
            if hasattr(last_msg, "content"):
                response_text = message_content_to_text(last_msg.content)
            elif isinstance(last_msg, dict):
                response_text = message_content_to_text(last_msg.get("content", ""))
            else:
                response_text = str(last_msg)
                
            if response_text.strip():
                return response_text

        return (
            "The model returned an empty response for this request. "
            "Please try again or rephrase your prompt."
        )

    except Exception as e:
        return f"Error invoking agent runner: {str(e)}. Please check your environment configuration and PORTKEY_API_KEY."

def simulate_agent_response(prompt: str) -> str:
    """Provides high-quality mock responses for local testing when API keys are absent."""
    p_lower = prompt.lower()
    
    if "california" in p_lower and "growth" in p_lower:
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
        
    if "plot" in p_lower or "chart" in p_lower:
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
plt.savefig('deep-agents-sdk/static/charts/hpi_{chart_id}.png', dpi=150)
"""
        tool_execute_python(code)
        return f"Here is the housing price index comparison chart for CA, NY, and TX from 2015 to present:\n\n![HPI Comparison Chart](/static/charts/hpi_{chart_id}.png)"
        
    return f"I received your request: '{prompt}'. (LLM API key is not set. Please set the PORTKEY_API_KEY environment variable to enable full multi-step agent reasoning.)"

# Routes
@app.post("/api/chat", response_model=ChatResponse)
def api_chat(request: ChatRequest):
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    response_text = run_agent(request.session_id, request.message)
    return ChatResponse(response=response_text, session_id=request.session_id)

@app.get("/")
def get_index():
    return FileResponse("deep-agents-sdk/static/index.html")

# Serve static assets
app.mount("/static", StaticFiles(directory="deep-agents-sdk/static"), name="static")
app.mount("/examples", StaticFiles(directory="examples"), name="examples")
