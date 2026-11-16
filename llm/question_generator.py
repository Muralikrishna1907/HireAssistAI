import requests
import re
import inspect

OLLAMA_URL = "http://localhost:11434/api/generate"

MODEL = "gpt-oss:120b-cloud"
# MODEL = "gemma3:4b"

def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

def detect_programming_language(questions):
    debug_log("Starting detect_programming_language")

    language_keywords = {
        "Python": ["python", "django", "flask", "fastapi"],
        "SAP CAP": ["sap", "cap", "odata", "btp", "cds"],
        "JavaScript": ["javascript", "node", "express"],
        "Java": ["java", "spring"],
        "SQL": ["sql", "postgresql", "mysql"],
    }

    score = {}

    for q in questions:
        text = q["question"].lower()

        for lang, keywords in language_keywords.items():
            for kw in keywords:
                if kw in text:
                    score[lang] = score.get(lang, 0) + 1

    debug_log(f"Language scores: {score}")

    if not score:
        return "General"

    prog_lang = max(score, key=score.get)

    debug_log(f"Detected Programming Language: {prog_lang}")

    return prog_lang

def generate_interview_questions(cv_text: str, jd_text: str, num_questions: int):
    debug_log(f"Starting generate_interview_questions - CV Length: {len(cv_text)} characters, JD Length: {len(jd_text)} characters")

#     prompt = f"""

# You are an interviewer.

# Generate exactly {num_questions} interview questions.

# For each question use this format EXACTLY:

# Q1: Question text

# | Medium | Python

# Do not add any extra explanation.

# Candidate CV:

# {cv_text[:1500]}

# Job Description:

# {jd_text[:1000]}

# """

    prompt = f"""
    You are a senior technical interviewer.

    Generate interview questions based on BOTH:
    1. Job Description (PRIMARY)
    2. Candidate CV (SECONDARY)

    STRICT OUTPUT FORMAT:
    Return ONLY plain text.

    Questions:
    - 
    - 
    - 

    RULES:

    🔹 SOURCE DISTRIBUTION:
    - 70% questions MUST be based on Job Description (JD)
    - 30% questions MUST be based on Candidate CV
    - JD-based questions should focus on required skills and responsibilities
    - CV-based questions should focus on candidate’s experience and projects

    🔹 DIFFICULTY DISTRIBUTION:
    - 40% EASY questions (basic concepts, definitions)
    - 30% MEDIUM questions (applied concepts, coding logic)
    - 30% HARD questions (scenario-based, problem-solving, architecture)

    🔹 GENERAL RULES:
    1. Questions must be role-specific
    2. Do NOT generate scenario-based or situational questions
        (Avoid phrases like: "what will you do", "how would you", "in your experience")
    3. Reject any question containing:
        - "you"
        - "your"
        - "would"
        - "experience"
        - "scenario"
        Regenerate if any rule is violated.
    4. Each question MUST be under 20 words
    5. Use direct question format only (no descriptions)
    6. Do NOT repeat questions and Do NOT mention Difficulty level for each question like this: Easy, Medium, Hard and CV or JD based
        (Example: "Q1: What is polymorphism? | Easy | CV-based")
    7. Do NOT include answers
    8. Use ONLY standard hyphen "-" for bullets
    9.Do NOT use special characters or emojis • ■ ● ▪ ✓ ✔ ✦ or any Unicode symbols
    10. Maintain proper mix of theory and practical questions

    🔹 IMPORTANT:
    - Ensure proportional distribution is followed as closely as possible
    - If total questions = {num_questions}, adjust counts accordingly
    - Dont Generate more than or less than {num_questions} questions and generate exactly {num_questions} questions

    INPUT DATA:

    JOB DESCRIPTION:
    {jd_text}

    CANDIDATE CV:
    {cv_text}
    """
    debug_log(f"Prompt constructed - Total length: {len(prompt)} characters, CV excerpt: {len(cv_text[:1500])} chars, JD excerpt: {len(jd_text[:1000])} chars")

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "temperature": 0   # 🔥 makes output more consistent
    }
    debug_log(f"Request payload prepared - Model: {MODEL}, Temperature: 0, Stream: False")

    debug_log(f"Sending request to Ollama API - URL: {OLLAMA_URL}, Timeout: 300s")
    response = requests.post(OLLAMA_URL, json=payload, timeout=300)
    debug_log(f"Ollama API response received - Status Code: {response.status_code}")
    
    response.raise_for_status()
    debug_log("Response validation passed - Status code indicates success")

    output = response.json().get("response", "")
    debug_log(f"Response JSON parsed successfully - Generated content length: {len(output)} characters")
    
    # 🔥 ADD THIS LINE HERE
    debug_log(f"RAW LLM OUTPUT:\n{output}")
    
    debug_log("Passing output to parse_questions for parsing")
    questions = parse_questions(output)
    debug_log(f"Question parsing completed - Total questions extracted: {len(questions)}")

    # 🔥 ADD THESE LINES HERE
    prog_lang = detect_programming_language(questions)

    for q in questions:
        q["language"] = prog_lang

    return questions
    
    return questions

def parse_questions(output: str):
    debug_log(f"Starting parse_questions - Input length: {len(output)} characters")
    
    questions = []

    # split lines
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    debug_log(f"Output split into lines - Total lines after filtering: {len(lines)}")

    for line in lines:
        # match bullet questions like:
        # - question
        match = re.match(r"^-\s*(.+)", line)

        if match:
            question = match.group(1).strip()

            questions.append({
                "question": question,
                "difficulty": "Medium",   # default
                "language": "General"
            })

            debug_log(f"Extracted question: {question[:60]}...")

    debug_log(f"parse_questions completed - Total questions extracted: {len(questions)}")

    return questions
