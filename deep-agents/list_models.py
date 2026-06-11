import os
from google.ai import generativelanguage_v1beta as generativelanguage

def list_supported_models():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable is not set.")
        return
        
    print("Listing available models that support generateContent:")
    print("-" * 50)
    
    try:
        # Initialize client with API key
        client = generativelanguage.ModelServiceClient(
            client_options={"api_key": api_key}
        )
        
        count = 0
        for m in client.list_models():
            if "generateContent" in m.supported_generation_methods:
                print(f"Name: {m.name} | Description: {m.description}")
                count += 1
        print("-" * 50)
        print(f"Found {count} supported models.")
    except Exception as e:
        print(f"Error querying ModelService: {e}")

if __name__ == "__main__":
    list_supported_models()
