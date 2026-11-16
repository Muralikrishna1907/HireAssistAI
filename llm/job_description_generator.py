import requests
import re
import inspect

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gpt-oss:120b-cloud"
# MODEL_NAME = "qwen3:8b"

def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

def generate_job_description(prompt_text: str):
    debug_log(f"Starting generate_job_description - Prompt length: {len(prompt_text)} characters")
    
    prompt = f"""
        You are a senior HR manager.

        Generate a professional job description

        STRICT OUTPUT FORMAT:
        Follow this structure exactly.

        Job ID:
        Job Title:
        Experience Required:

        Company Overview:

        Job Summary:

        Required Skills:
        - 
        - 
        - 

        Key Responsibilities:
        - 
        - 
        - 

        Preferred Qualifications:
        - At least 2 bullets are required
        - 

        Optional Sections (Include ONLY if provided in input):
        Interview Time:
        Number of Questions:
        Location:
        Employment Type:
        Budget Range:
        Certifications:

        IMPORTANT RULES:
        1. Always generate required sections.
        2. Use ONLY standard hyphen "-" for bullets.
        3. Do NOT use special characters like:
        • ■ ● ▪ ✓ ✔ ✦ or any Unicode symbols
        4. Do NOT use emojis.
        5. Use plain ASCII text only.
        6. If "Number of Questions" is provided, follow it strictly; else default to 5.
        
        🔥 SKILL EXPANSION RULE (NEW):
        7. If user provides ONLY 1 or 2 skills, expand them into at least 5 relevant and related skills.
        8. Skills must be commonly used together in real-world projects.
        9. Do NOT repeat skills.
        10. Do NOT include unrelated skills.
        11. Always include the original skill in the list.

        Example:
        Input: Python  
        Output:
        - Python
        - Flask
        - Django
        - REST APIs
        - SQL

        12. Return ONLY structured plain text.

        Input Details:
        {prompt_text}
    """
   
    debug_log(f"Full prompt constructed - Total length: {len(prompt)} characters")

    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False
    }
    debug_log(f"Request payload prepared - Model: {MODEL_NAME}, Stream: False")

    debug_log(f"Sending request to Ollama API - URL: {OLLAMA_URL}")
    response = requests.post(OLLAMA_URL, json=payload, timeout=300)
    debug_log(f"Ollama API response received - Status Code: {response.status_code}")
    
    response.raise_for_status()
    debug_log("Response validation passed - Status code indicates success")

    output = response.json()["response"]
    debug_log(f"Response JSON parsed successfully - Generated content length: {len(output)} characters")
    
    final_output = output.strip()
    debug_log(f"Output trimmed - Final length: {len(final_output)} characters")
    debug_log("Job description generation completed successfully")
    
    return final_output