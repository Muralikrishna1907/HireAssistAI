import json
import requests
from utils.logger import info_log, debug_log, warning_log, error_log

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gpt-oss:120b-cloud"

def evaluate_candidate_form_with_llm(form_data_json: str, jd_text: str):
    """
    Evaluates candidate form details against the JD using the raw Ollama generate API.
    Returns a dict with 'isShortlisted' (Yes/No) and 'Reason'.
    """
    debug_log(f"Starting evaluate_candidate_form_with_llm - Form Length: {len(form_data_json)} chars, JD Length: {len(jd_text)} chars")

    prompt = f"""
    You are an expert technical HR recruiter. Evaluate this candidate's submitted form data against the Job Description.
    Specifically analyze if their Expected CTC, Total Experience, Preferred/Current Location, Employment Type, and Key Skills match the role's requirements.

    STRICT OUTPUT FORMAT:
    Return ONLY a valid JSON object. Do not add markdown blocks, explanations, or conversational text.

    {{
        "isShortlisted": "Yes" or "No",
        "Reason": "Provide a brief explanation based on CTC, experience, location, and skills."
    }}

    JOB DESCRIPTION:
    {jd_text}

    CANDIDATE FORM DATA:
    {form_data_json}
    """
    
    debug_log(f"Prompt constructed - Total length: {len(prompt)} characters")

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "temperature": 0.0,
        "format": "json" 
    }
    
    debug_log(f"Request payload prepared - Model: {MODEL}, Temperature: 0.0, Stream: False, Format: JSON")

    try:
        debug_log(f"Sending request to Ollama API - URL: {OLLAMA_URL}, Timeout: 300s")
        response = requests.post(OLLAMA_URL, json=payload, timeout=300)
        debug_log(f"Ollama API response received - Status Code: {response.status_code}")
        
        response.raise_for_status()

        output = response.json().get("response", "").strip()
        debug_log(f"RAW LLM OUTPUT:\n{output}")
        
        if not output:
            warning_log("LLM returned an empty string. Cannot parse JSON.")
            return {"isShortlisted": "No", "Reason": "System Error: Empty response from LLM."}

        if output.startswith('```json'):
            output = output.replace('```json', '').replace('```', '')
        elif output.startswith('```'):
            output = output.replace('```', '')
            
        parsed_result = json.loads(output)
        debug_log(f"Successfully parsed LLM JSON output: {parsed_result}")
        
        return parsed_result
        
    except requests.exceptions.RequestException as req_err:
        error_log(f"Ollama API network error: {str(req_err)}")
        return {"isShortlisted": "No", "Reason": f"System Error during LLM network call: {str(req_err)}"}
    except json.JSONDecodeError as je:
        error_log(f"Failed to parse LLM JSON: {str(je)}")
        return {"isShortlisted": "No", "Reason": "System Error: LLM returned invalid JSON format."}
    except Exception as e:
        error_log(f"LLM API Call failed: {str(e)}")
        return {"isShortlisted": "No", "Reason": f"System Error during LLM evaluation: {str(e)}"}