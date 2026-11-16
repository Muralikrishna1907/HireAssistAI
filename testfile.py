import requests
import datetime 
import json

OLLAMA_URL = "http://localhost:11434/api/generate"
# MODEL_NAME = "gemma3:4b"   # change to your model
MODEL_NAME = "gpt-oss:120b-cloud"   # change to your model

prompt = """
Explain Python decorators with a simple example in 5 lines
"""

payload = {
    "model": MODEL_NAME,
    "prompt": prompt,
    "stream": False
}

try:
    response = requests.post(OLLAMA_URL, json=payload)

    print("Status Code:", response.status_code)

    response.raise_for_status()

    result = response.json()

    print("\n=== MODEL RESPONSE ===\n")
    print(result["response"])
    print(datetime.time)

except requests.exceptions.RequestException as e:
    print("Error:", e)