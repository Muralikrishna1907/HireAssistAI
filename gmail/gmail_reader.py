import base64
import re
import json
import os
import inspect
from db.cv_repository import (
    is_email_processed,
    mark_email_processed,
    log_mail_received
)
from datetime import datetime
from db.connection import get_connection
from utils.logger import debug_log, info_log, error_log, warning_log


def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")


# ✅ Extract JOB ID (Updated to check Subject and Body)
def extract_job_id(subject, email_body=""):
    debug_log(f"Starting extract_job_id - Raw Subject: {subject}")

    pattern = r'(?:JOB\s*ID[:\s]*)?([A-Z]{2,5}\d{2,5})'

    # 1. Check Subject First
    if subject:
        match = re.search(pattern, subject.upper())
        if match:
            return match.group(1)

    # 2. Check Email Body as a Fallback
    if email_body:
        match = re.search(pattern, email_body.upper())
        if match:
            return match.group(1)

    return None


# ✅ Extract candidate details from body
def extract_candidate_details(email_body):
    def extract_field(pattern, text):
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1).strip() if match else None

    return {
        "total_experience": extract_field(r"Total Experience\s*[:\-]\s*(.+)", email_body),
        "current_role": extract_field(r"Current Role\s*[:\-]\s*(.+)", email_body),
        "current_company": extract_field(r"Current Company\s*[:\-]\s*(.+)", email_body),
        "expected_ctc": extract_field(r"Expected CTC\s*[:\-]\s*(.+)", email_body),
        "notice_period": extract_field(r"Notice Period\s*[:\-]\s*(.+)", email_body)
    }


def extract_email_body(payload):
    if not payload:
        return None

    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})

    # ✅ If direct body
    if mime_type == "text/plain" and "data" in body:
        return base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="ignore")

    if mime_type == "text/html" and "data" in body:
        return base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="ignore")

    # ✅ If nested parts → RECURSIVE
    parts = payload.get("parts", [])
    for part in parts:
        result = extract_email_body(part)
        if result:
            return result

    return None


# ✅ MAIN FUNCTION
def fetch_job_application_emails(conn, service):
    info_log("Starting fetch_job_application_emails")
    
    # ✅ UPDATED QUERY: Fetch any email with a PDF attachment, bypassing strict subject requirements
    results = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        q='subject:"Job Application" OR (has:attachment filename:pdf)',
        maxResults=10
    ).execute()

    messages = results.get("messages", [])
    emails = []

    if not messages:
        return emails

    for msg_index, msg in enumerate(messages, start=1):
        msg_id = msg["id"]
        debug_log(msg)
        if is_email_processed(conn, msg_id):
            continue

        mail_read_time = datetime.now()
        debug_log(msg_id)
        print(msg_id, mail_read_time)

        msg_data = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="full"
        ).execute()
        
        internal_ts = msg_data.get("internalDate")
        mail_received_time = datetime.fromtimestamp(int(internal_ts) / 1000)

        headers = msg_data["payload"].get("headers", [])

        subject = None
        from_email = None

        for h in headers:
            if h["name"].lower() == "subject":
                subject = h["value"]
            elif h["name"].lower() == "from":
                from_email = h["value"]

        # ✅ Clean email
        if from_email and "<" in from_email:
            match = re.search(r"<(.+?)>", from_email)
            from_email = match.group(1) if match else from_email

        payload = msg_data["payload"]

        # ✅ Extract BODY FIRST (So we can use it to search for the Job ID)
        email_body = extract_email_body(payload)

        # 🔹 fallback (if body not in parts)
        if not email_body:
            payload_body = msg_data["payload"].get("body", {})
            if "data" in payload_body:
                email_body = base64.urlsafe_b64decode(payload_body["data"]).decode("utf-8", errors="ignore")

        print("EXTRACTED BODY:", email_body)

        # ✅ Now extract Job ID using BOTH Subject and Body
        job_id = extract_job_id(subject, email_body)

        print("\nFrom   :", from_email)
        print("Subject:", subject)
        print("Job ID :", job_id)

        # ✅ PDF extraction (Updated to find all PDFs)
        def extract_pdfs(parts):
            found_pdfs = []
            for part in parts:
                filename = part.get("filename", "")
                body = part.get("body", {})

                if filename.lower().endswith(".pdf") and "attachmentId" in body:
                    att = service.users().messages().attachments().get(
                        userId="me",
                        messageId=msg_id,
                        id=body["attachmentId"]
                    ).execute()
                    found_pdfs.append(base64.urlsafe_b64decode(att["data"]))

                if "parts" in part:
                    found_pdfs.extend(extract_pdfs(part["parts"]))

            return found_pdfs

        all_pdfs = extract_pdfs(payload.get("parts", []))
        
        # ✅ Determine if multiple PDFs are present
        has_multiple_pdfs = len(all_pdfs) > 1
        pdf_bytes = all_pdfs[0] if all_pdfs else None

        # ✅ Extract structured fields
        candidate_details = extract_candidate_details(email_body) if email_body else {}

        # ✅ FINAL JSON (THIS IS WHAT YOU STORE)
        mail_body_json = {
            "raw_body": email_body,
            "parsed_fields": candidate_details
        }

        debug_log(f"Parsed body JSON: {mail_body_json}")

        emails.append({
            "msg_id": msg_id,
            "from": from_email,
            "subject": subject,
            "job_id": job_id,
            "mail_body": mail_body_json,
            "internalDate": msg_data["internalDate"],
            "pdf": pdf_bytes,
            "has_multiple_pdfs": has_multiple_pdfs  # ✅ Added multiple PDFs flag
        })

    return emails


# ✅ OPTIONAL (if you want timestamp insert)
def insert_mail_read_timestamp(conn, msg_id, mail_read_time):
    cursor = conn.cursor()

    query = """
        INSERT INTO time_stamps (id, mail_read)
        VALUES (%s, %s)
    """

    cursor.execute(query, (msg_id, mail_read_time))
    

def fetch_webhook_submissions(service):
    info_log("Starting fetch_webhook_submissions via Gmail API")
    submissions = []
    
    try:
        # Fetching emails from Inbox
        results = service.users().messages().list(
            userId="me",
            labelIds=["INBOX"], 
            q="subject:SYSTEM_WEBHOOK_SUBMISSION",
            maxResults=10
        ).execute()

        messages = results.get('messages', [])
        info_log(f"Gmail API returned {len(messages)} potential webhook emails.")

        for msg in messages:
            msg_id = msg['id']
            debug_log(f"Fetching full payload for webhook email MSG_ID: {msg_id}")
            
            try:
                message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
                payload = message.get('payload', {})
                headers = payload.get('headers', [])
                
                # Extract Subject
                subject = next((header['value'] for header in headers if header['name'] == 'Subject'), "")
                debug_log(f"Read Subject: '{subject}' from MSG_ID: {msg_id}")
                
                if "SYSTEM_WEBHOOK_SUBMISSION:" not in subject:
                    warning_log(f"Subject '{subject}' does not match expected webhook format. Skipping.")
                    continue
                    
                candidate_cv_id = subject.split(":")[1].strip()
                debug_log(f"Successfully extracted Candidate CV ID: {candidate_cv_id}")
                
                # Extract Body
                parts = payload.get('parts', [])
                body_data = ""
                if parts:
                    for part in parts:
                        if part.get('mimeType') == 'text/plain':
                            body_data = part.get('body', {}).get('data', '')
                            break
                else:
                    body_data = payload.get('body', {}).get('data', '')

                if body_data:
                    form_json_data = base64.urlsafe_b64decode(body_data).decode('utf-8')
                    debug_log(f"Successfully decoded JSON payload for CV ID: {candidate_cv_id}")
                    
                    submissions.append({
                        "candidate_cv_id": candidate_cv_id,
                        "webhook_msg_id": msg_id,
                        "form_data_json": form_json_data
                    })
                else:
                    warning_log(f"No body data found in webhook email MSG_ID: {msg_id}")

            except Exception as e:
                error_log(f"Failed to parse individual webhook email {msg_id}: {str(e)}")

        info_log(f"Successfully prepared {len(submissions)} webhooks for main pipeline.")
        return submissions

    except Exception as e:
        error_log(f"Fatal error fetching webhooks from Gmail: {str(e)}")
        return []