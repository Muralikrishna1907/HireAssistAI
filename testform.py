import json
import requests
import inspect
from datetime import datetime

# --- MOCK LOGGING FOR LOCAL TEST ---
def get_current_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def debug_log(message):
    print(f"[{get_current_time()}] [DEBUG] {message}")

def warning_log(message):
    print(f"[{get_current_time()}] [WARNING] {message}")

def error_log(message):
    print(f"[{get_current_time()}] [ERROR] {message}")

# --- API CONFIGURATION ---
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gpt-oss:120b-cloud"

# --- MAIN FUNCTION ---
def evaluate_candidate_form_with_llm(form_data_json: str, jd_text: str):
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

        # Clean markdown formatting if present
        if output.startswith('```json'):
            output = output.replace('```json', '').replace('```', '')
        elif output.startswith('```'):
            output = output.replace('```', '')
            
        parsed_result = json.loads(output)
        debug_log("Successfully parsed LLM JSON output")
        
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

# --- TEST EXECUTION ---
if __name__ == "__main__":
    test_form_data = """{"ID": "1a1d0677b0ac2588", "Name": "Mani Kumar", "Role": "ABCD", "Gender": "Male", "Industry": "ABCD", "Department": "ABCD", "Key Skills": "SAP CAPM, SAP BTP, SAP Cloud SDK, OData services, Node.js", "Email ID": "mmkrishna190796@gmail.com", "Pin Code": "500085", "Expected CTC": "15", "Phone Number": "9876543210", "Date of Birth": "1996-01-01", "Current CTC": "10", "Employment Type": "Full Time", "Current Location": "Hyderabad", "Home Town/City": "Hyderabad", "Marital Status": "Single", "PG specialization": "", "Permanent Address": "Hyderabad", "UG Specialization": "Computers", "PG graduation year": "", "Doctorate degree": "", "Preferred Locations": "Hyderabad", "Curr. Company name": "ABCD", "UG Graduation year": "2023", "Post graduation degree": "", "Work permit for USA": "No", "Doctorate specialization": "", "Total Experience (Years)": "6", "Doctorate graduation year": "", "Alternate mobile number": "", "Under Graduation degree": "Bachelors of Technology", "Curr. Company Designation": "ABCD", "PG university/institute name": "", "UG University/institute Name": "JNTU Hyderabad", "Doctorate university/institute name": "", "Notice period / Availability to join": "Immediate"}"""

    test_jd_text = """Job Description:
    Job ID: BTP001
    Job Title: SAP BTP Developer
    Experience Required: 5 years

    Company Overview:
    SBP Consultancy Private Limited is a dynamic professional services firm headquartered in Hyderabad, specializing in providing innovative business solutions, strategic advisory, and operational support to diverse industries. We are committed to delivering excellence, driving growth, and creating long-term value for our clients. Join our team to be part of a collaborative environment that fosters learning and professional development.

    Job Summary:
    The SAP BTP Developer will design, develop, and deploy cloud-native applications on the SAP Business Technology Platform. The role involves collaborating with functional consultants and architects to translate business requirements into scalable technical solutions, ensuring high performance, security, and adherence to best practices.

    Required Skills:
    - SAP CAPM
    - SAP BTP
    - SAP Cloud SDK
    - OData services
    - Node.js

    Key Responsibilities:
    - Develop and extend SAP BTP applications using CAPM, Cloud SDK, and related technologies.
    - Design and implement OData services and REST APIs to integrate SAP solutions with external systems.
    - Collaborate with cross‑functional teams to gather requirements, provide technical estimates, and deliver high‑quality deliverables.
    - Perform unit testing, code reviews, and support deployment pipelines in a CI/CD environment.
    - Troubleshoot and resolve performance, security, and functional issues in production environments.

    Preferred Qualifications:
    - Bachelor’s degree in Computer Science, Information Technology, or a related field.
    - Minimum of 5 years of hands‑on experience developing solutions on SAP Business Technology Platform.
    - Proven experience with SAP Fiori, SAP UI5, and modern JavaScript frameworks.
    - Familiarity with version control systems (e.g., Git) and automated testing tools.

    Interview Time: 5 minutes
    Number of Questions: 2
    Location: Hyderabad
    Employment Type: Full Time
    Budget Range: 12-18"""

    print("--- STARTING LOCAL EVALUATION TEST ---\n")
    final_decision = evaluate_candidate_form_with_llm(test_form_data, test_jd_text)
    
    print("\n--- FINAL OUTPUT DICTIONARY ---")
    print(json.dumps(final_decision, indent=4))