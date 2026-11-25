# cv/cv_parser_llm.py

import requests
import json
import re

from utils.logger import debug_log, info_log, warning_log, error_log, log_duration, short, mask_email

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gpt-oss:120b-cloud"
# MODEL = "gemma3:4b"


def clean_llm_output(output: str) -> str:
    """Remove markdown/code block wrappers from LLM output"""
    if "```" in output:
        output = output.replace("```json", "").replace("```", "")
    return output.strip()


def normalize_parsed_data(parsed: dict) -> dict:
    """Ensure consistent structure"""
    
    # Phone normalization
    if parsed.get("phone") in ["", "N/A", "null"]:
        parsed["phone"] = None

    # Skills normalization
    if not isinstance(parsed.get("skills"), list):
        parsed["skills"] = []

    # Education normalization
    if not isinstance(parsed.get("education"), list):
        parsed["education"] = []

    # Clean each education entry
    clean_education = []
    for edu in parsed["education"]:
        if isinstance(edu, dict):
            clean_education.append({
                "degree": edu.get("degree"),
                "field": edu.get("field"),
                "institution": edu.get("institution"),
                "year": edu.get("year")
            })

    parsed["education"] = clean_education

    return parsed


def parse_cv_with_llm(cv_text: str):
    debug_log(f"Parsing CV with LLM - CV text length: {len(cv_text)} chars")
    if not cv_text.strip():
        warning_log("CV text is empty - LLM parsing will have nothing to work with")

    prompt = f"""
Extract structured data from the CV.

Return ONLY valid JSON.

FORMAT:
{{
  "name": "",
  "email": "",
  "phone": "",
  "skills": [],
  "total_experience": 0,
  "education": [
    {{
      "degree": "",
      "field": "",
      "institution": "",
      "year": ""
    }}
  ]
}}

RULES:
- total_experience MUST be a decimal number (example: 4.3, 5.5)
- education MUST be an array
- education MUST be structured into degree, field, institution, year
- Do NOT return paragraph for education
- Do NOT add explanation
- Do NOT add text outside JSON
- If missing → null

CV:
{cv_text[:4000]}
"""

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "temperature": 0
    }

    debug_log(f"CV parse request - Model: {MODEL}, Prompt length: {len(prompt)} chars")

    try:
        with log_duration(f"LLM CV parsing ({MODEL})"):
            response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        debug_log(f"Ollama response status: {response.status_code}")
        response.raise_for_status()

        output = response.json().get("response", "").strip()
        debug_log(f"LLM raw output: {short(output)}")
        if not output:
            warning_log("LLM returned an empty response for CV parsing")

        # Clean output
        output = clean_llm_output(output)

        parsed = json.loads(output)

        # Normalize
        parsed = normalize_parsed_data(parsed)

        info_log(f"CV parsed by LLM - name found: {bool(parsed.get('name'))}, "
                 f"email: {mask_email(parsed.get('email')) or 'not found'}, phone found: {bool(parsed.get('phone'))}, "
                 f"skills: {len(parsed.get('skills') or [])}, experience: {parsed.get('total_experience')}, "
                 f"education entries: {len(parsed.get('education') or [])}")
        return parsed

    except json.JSONDecodeError as e:
        warning_log(f"LLM CV output is not valid JSON ({e}); falling back to regex parser")
        return fallback_parser(cv_text)
    except requests.exceptions.RequestException as e:
        error_log(f"Ollama request for CV parsing failed ({MODEL}): {e}; falling back to regex parser")
        return fallback_parser(cv_text)
    except Exception as e:
        error_log(f"LLM CV parsing failed: {e}; falling back to regex parser")
        return fallback_parser(cv_text)


def fallback_parser(text):
    """Basic regex-based fallback parser"""

    # Email
    email = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)

    # Phone (India-friendly)
    phone = re.search(r"(\+91[\-\s]?)?[6-9]\d{9}", text)

    # Basic education detection
    education_matches = re.findall(
        r"(B\.?Tech|M\.?Tech|B\.?Sc|M\.?Sc|MBA|Bachelor|Master)[^\n]*",
        text,
        re.IGNORECASE
    )

    education = []
    for edu in education_matches:
        education.append({
            "degree": edu.strip(),
            "field": None,
            "institution": None,
            "year": None
        })

    result = {
        "name": text.split("\n")[0] if text else None,
        "email": email.group() if email else None,
        "phone": phone.group() if phone else None,
        "skills": [],
        "total_experience": 0,
        "education": education
    }
    warning_log(f"Regex fallback parser used - name found: {bool(result['name'])}, "
                f"email: {mask_email(result['email']) or 'not found'}, phone found: {bool(result['phone'])}, "
                f"education entries: {len(education)} (skills and experience are not extracted)")
    return result


# -------------------------------
# OPTIONAL: DB INSERT HELPER
# -------------------------------

def prepare_db_payload(parsed_data: dict):
    """Prepare data for CV_INFO table"""

    return {
        "NAME": parsed_data.get("name"),
        "EMAIL": parsed_data.get("email"),
        "PHONE": parsed_data.get("phone"),
        "SKILLS": json.dumps(parsed_data.get("skills", [])),
        "TOTAL_EXPERIENCE": parsed_data.get("total_experience"),
        "EDUCATION": json.dumps(parsed_data.get("education", []))
    }