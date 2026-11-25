import requests
import re

from utils.logger import debug_log, info_log, warning_log, error_log, log_duration, short

OLLAMA_URL = "http://localhost:11434/api/generate"

MODEL = "gpt-oss:120b-cloud"
# MODEL = "gemma3:4b"

def detect_programming_language(questions):
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

    debug_log(f"Language keyword scores: {score}")

    if not score:
        debug_log("No language keywords found in the questions - using 'General'")
        return "General"

    prog_lang = max(score, key=score.get)

    debug_log(f"Detected Programming Language: {prog_lang}")

    return prog_lang

def generate_interview_questions(cv_text: str, jd_text: str, num_questions: int):
    debug_log(f"Starting question generation - CV length: {len(cv_text)} chars, JD length: {len(jd_text)} chars, "
              f"Questions requested: {num_questions}")
    if not jd_text:
        warning_log("Job description text is empty - questions will be based on the CV only")

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

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "temperature": 0   # 🔥 makes output more consistent
    }
    debug_log(f"Question generation request - Model: {MODEL}, Prompt length: {len(prompt)} chars, Timeout: 300s")

    with log_duration(f"LLM question generation ({MODEL})"):
        response = requests.post(OLLAMA_URL, json=payload, timeout=300)
    debug_log(f"Ollama response status: {response.status_code}")

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        error_log(f"Ollama question generation returned an HTTP error ({MODEL}): {e}")
        raise

    output = response.json().get("response", "")
    debug_log(f"LLM output ({len(output)} chars): {short(output, 300)}")
    if not output.strip():
        warning_log("LLM returned an empty response for question generation")

    questions = parse_questions(output)

    # 🔥 ADD THESE LINES HERE
    prog_lang = detect_programming_language(questions)

    for q in questions:
        q["language"] = prog_lang

    if len(questions) != num_questions:
        warning_log(f"Expected {num_questions} question(s) but parsed {len(questions)} from the LLM output")
    info_log(f"Generated {len(questions)} interview question(s) - Language: {prog_lang}")
    return questions
    
    return questions

def parse_questions(output: str):
    questions = []

    # split lines
    lines = [line.strip() for line in output.splitlines() if line.strip()]

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

    debug_log(f"Parsed {len(questions)} question(s) from {len(lines)} non-empty line(s)")

    return questions
