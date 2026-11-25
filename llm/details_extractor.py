"""
Candidate details required before the assessment (replaces the Google Form).

The details are read from the candidate's email (body and replies) and their CV.
Anything still missing is requested by email in the same thread.
"""
import json
import re

import requests

from utils.logger import debug_log, info_log, warning_log, error_log, log_duration, short

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gpt-oss:120b-cloud"

# key -> label shown to the candidate (same items as the invitation email)
REQUIRED_FIELDS = {
    "full_name": "Full name",
    "mobile_number": "Mobile number",
    "total_experience": "Total experience (in years)",
    "current_company": "Current company",
    "current_ctc": "Current CTC (in Lacs per annum)",
    "expected_ctc": "Expected CTC (in Lacs per annum)",
    "notice_period": "Notice period",
    "current_location": "Current location",
    "preferred_location": "Preferred location",
}

# extra labels candidates commonly use, for the "Label: value" fallback parser
_LABEL_ALIASES = {
    "full_name": ["full name", "name", "candidate name"],
    "mobile_number": ["mobile number", "mobile", "phone number", "phone", "contact number", "contact"],
    "total_experience": ["total experience", "experience", "total exp", "exp"],
    "current_company": ["current company", "current employer", "company", "current organization", "current organisation"],
    "current_ctc": ["current ctc", "ctc", "current salary"],
    "expected_ctc": ["expected ctc", "expected salary", "ectc"],
    "notice_period": ["notice period", "notice"],
    "current_location": ["current location", "location", "current city"],
    "preferred_location": ["preferred location", "preferred locations", "preferred city", "location preference"],
}

# values that mean "not given" ("NA" / "Fresher" are real answers and are kept)
_EMPTY_VALUES = {"", "none", "null", "not mentioned", "not provided", "unknown", "-", "--", "?", "tbd", "xxx"}


def _clean(value):
    """Normalise a value; returns None when it is effectively empty."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    text = " ".join(str(value).split()).strip(" .;,")
    if text.lower() in _EMPTY_VALUES:
        return None
    return text[:200]


# every alias -> key, longest first so "current ctc" wins over "ctc"
_ALIAS_LIST = sorted(((alias, key) for key, aliases in _LABEL_ALIASES.items() for alias in aliases),
                     key=lambda pair: len(pair[0]), reverse=True)


def parse_labeled_lines(text):
    """Fallback parser for replies written as label/value lines, with or without a separator:
    'Expected CTC: 12 LPA', 'Expected CTC - 12 LPA', 'expected ctc 12LPA'."""
    found = {}
    for line in str(text or "").splitlines():
        # drop bullets / numbering and the "(in Lacs per annum)" style hints from our template
        cleaned = re.sub(r"^\s*[\-\*\u2022]*\s*(\d+[\.\)]\s*)?", "", line)
        cleaned = re.sub(r"\((?:in [^)]*)\)", "", cleaned).strip()
        lower = cleaned.lower()
        # older invitations asked "Current CTC and expected CTC" on one line: "... : 12 / 15 LPA"
        combined = re.match(r"(?i)current\s+ctc\s*(?:and|&|/|,)\s*expected\s+ctc\s*[:\-–=]?\s*(.*)", cleaned)
        if combined:
            values = [v for v in (_clean(p) for p in re.split(r"\s*(?:/|,|&|\band\b|\bto\b)\s*", combined.group(1))) if v]
            if len(values) == 2:          # a single number is ambiguous: leave both to be asked again
                found.setdefault("current_ctc", values[0])
                found.setdefault("expected_ctc", values[1])
            continue
        for alias, key in _ALIAS_LIST:
            if not lower.startswith(alias):
                continue
            rest = cleaned[len(alias):]
            # the label must end here: followed by a separator, a space, or a digit (e.g. "ctc 10LPA")
            if rest and not re.match(r"[\s:\-\u2013=]|\d", rest):
                continue
            has_separator = bool(re.match(r"\s*[:\-\u2013=]", rest))
            value_text = re.sub(r"^[\s:\-\u2013=]+", "", rest)
            # without ":"/"-", only trust multi-word labels ("expected ctc 15LPA") or a number right
            # after the label ("experience 5 years"); avoids reading prose like "location is fine"
            if not has_separator and " " not in alias and not value_text[:1].isdigit():
                break
            value = _clean(value_text)
            if value and key not in found:
                found[key] = value
            break
    return found


def extract_details_with_llm(email_text, cv_text=""):
    """Ask the LLM for the required fields; returns {key: value or None}."""
    keys = list(REQUIRED_FIELDS)
    prompt = f"""
You extract a job applicant's details from their email and CV.

Return ONLY a JSON object with exactly these keys: {json.dumps(keys)}.
- Use the value the candidate gave. Keep units as written (e.g. "6 LPA", "30 days", "4.5 years").
- If the candidate says something does not apply (e.g. "NA", "Fresher", "Immediate"), keep that text.
- If a detail is not mentioned anywhere, use null. Do NOT guess or invent values.
- full_name and mobile_number may come from the CV; the other details usually come from the email.

EMAIL:
{str(email_text or '')[:4000]}

CV:
{str(cv_text or '')[:4000]}
"""
    payload = {"model": MODEL, "prompt": prompt, "stream": False, "temperature": 0.0, "format": "json"}
    debug_log(f"Details extraction request - Model: {MODEL}, Prompt length: {len(prompt)} chars, "
              f"Email text: {len(str(email_text or ''))} chars, CV text: {len(str(cv_text or ''))} chars")
    try:
        with log_duration(f"LLM details extraction ({MODEL})"):
            response = requests.post(OLLAMA_URL, json=payload, timeout=300)
        debug_log(f"Ollama response status: {response.status_code}")
        response.raise_for_status()
        output = response.json().get("response", "").strip()
        output = output.replace("```json", "").replace("```", "").strip()
        debug_log(f"LLM details output: {short(output, 300)}")
        if not output:
            warning_log("LLM returned an empty response for details extraction - treating all fields as missing")
        data = json.loads(output) if output else {}
        result = {k: _clean(data.get(k)) for k in keys}
        debug_log(f"Fields found by LLM: {[k for k in keys if result[k]] or 'none'}")
        return result
    except json.JSONDecodeError as e:
        warning_log(f"LLM details output is not valid JSON ({e}) - treating all fields as missing from the LLM")
        return {k: None for k in keys}
    except Exception as e:
        error_log(f"Details extraction with LLM failed ({MODEL}): {e}")
        return {k: None for k in keys}


def extract_candidate_details(email_text, cv_text="", known=None):
    """Merge already-known details with what this email/CV provides (newer non-empty values win)."""
    details = {k: _clean((known or {}).get(k)) for k in REQUIRED_FIELDS}
    debug_log(f"Extracting candidate details - Email text: {len(str(email_text or ''))} chars, "
              f"CV text: {len(str(cv_text or ''))} chars, already known: {[k for k in details if details[k]] or 'none'}")
    llm = extract_details_with_llm(email_text, cv_text)
    labeled = parse_labeled_lines(email_text)
    from_labels, from_llm = [], []
    for key in REQUIRED_FIELDS:
        new_value = labeled.get(key) or llm.get(key)
        if new_value:
            details[key] = new_value
            (from_labels if labeled.get(key) else from_llm).append(key)
    debug_log(f"Details sources - from 'Label: value' lines: {from_labels or 'none'}, from LLM: {from_llm or 'none'}")
    still_missing = [k for k in REQUIRED_FIELDS if not details.get(k)]
    info_log(f"Candidate details extracted - {len(REQUIRED_FIELDS) - len(still_missing)}/{len(REQUIRED_FIELDS)} present"
             + (f", missing: {still_missing}" if still_missing else ""))
    return details


def missing_fields(details):
    missing = [k for k in REQUIRED_FIELDS if not _clean((details or {}).get(k))]
    debug_log(f"Missing required fields: {missing or 'none'}")
    return missing


def details_for_evaluation(details):
    """Details as readable JSON (labels as keys), saved as the candidate's ADDITIONAL_DETAILS."""
    return json.dumps({REQUIRED_FIELDS[k]: details.get(k) for k in REQUIRED_FIELDS}, ensure_ascii=True)
