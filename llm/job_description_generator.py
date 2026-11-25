import requests
import re

from utils.logger import debug_log, info_log, warning_log, error_log, log_duration, short

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gpt-oss:120b-cloud"
# MODEL_NAME = "qwen3:8b"

def generate_job_description(prompt_text: str):
    debug_log(f"Starting job description generation - Input length: {len(prompt_text)} chars")
    if not prompt_text or not prompt_text.strip():
        warning_log("Job description input is empty - the LLM has nothing to base the JD on")
    
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
   
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False
    }
    debug_log(f"Job description request - Model: {MODEL_NAME}, Prompt length: {len(prompt)} chars, Timeout: 300s")

    with log_duration(f"LLM job description generation ({MODEL_NAME})"):
        response = requests.post(OLLAMA_URL, json=payload, timeout=300)
    debug_log(f"Ollama response status: {response.status_code}")

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        error_log(f"Ollama job description generation returned an HTTP error ({MODEL_NAME}): {e}")
        raise

    output = response.json()["response"]

    final_output = output.strip()
    debug_log(f"LLM output ({len(final_output)} chars): {short(final_output, 300)}")
    if not final_output:
        warning_log("LLM returned an empty job description")
    else:
        info_log(f"Job description generated - {len(final_output)} chars")

    return final_output