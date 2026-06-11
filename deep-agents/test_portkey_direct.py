import os
import sys
from dotenv import load_dotenv
load_dotenv()

# Set up tool mock functions for the agent
def tool_read_file(path: str) -> str:
    print(f"-> Agent called read_file: {path}")
    return "Dummy file content"

def tool_write_file(path: str, content: str) -> str:
    print(f"-> Agent called write_file: {path}")
    return "File successfully written"

def tool_execute_python(code: str) -> str:
    print(f"-> Agent called execute_python with code:\n{code}")
    return "Execution output: California HPI growth is 79.40%"

def test_direct():
    portkey_api_key = os.environ.get("PORTKEY_API_KEY")
    provider_slug = os.environ.get("PORTKEY_PROVIDER_SLUG")
    
    print(f"PORTKEY_API_KEY: {portkey_api_key[:10]}..." if portkey_api_key else "PORTKEY_API_KEY: None")
    print(f"PORTKEY_PROVIDER_SLUG: {provider_slug}")
    
    if not portkey_api_key or not provider_slug:
        print("Error: Missing credentials in .env file.")
        return
        
    try:
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent
        from langchain_core.tools import tool
        from langchain_core.messages import HumanMessage
        
        print("Initializing ChatOpenAI with Portkey...")
        model_name = os.environ.get("MODEL", "gemini-2.5-flash")
        model_string = f"@{provider_slug}/{model_name}"
        print(f"Model string: {model_string}")
        
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
            model=model_string,
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
        
        print("Compiling react agent...")
        agent = create_react_agent(llm, tools, prompt="You are a helpful assistant. Use tools if needed.")
        
        prompt = "Calculate California HPI growth rate from Q1 2010 to Q1 2020."
        print(f"Invoking agent with prompt: '{prompt}'...")
        
        # We increase the timeout here
        result = agent.invoke({"messages": [HumanMessage(content=prompt)]}, config={"recursion_limit": 100})
        
        print("\n--- Agent Execution Complete ---")
        final_msg = result["messages"][-1]
        print("Response:")
        print(final_msg.content)
        
    except Exception as e:
        print("Error occurred during execution:")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_direct()
