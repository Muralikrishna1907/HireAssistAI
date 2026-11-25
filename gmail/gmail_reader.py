import base64
import re
import json
import os
from db.cv_repository import (
    is_email_processed,
    mark_email_processed,
    log_mail_received
)
from datetime import datetime
from db.connection import get_connection
from utils.logger import debug_log, info_log, error_log, warning_log, short
from gmail.gmail_service import gmail_call


# ✅ Extract JOB ID (Updated to check Subject and Body)
def extract_job_id(subject, email_body=""):
    pattern = r'(?:JOB\s*ID[:\s]*)?([A-Z]{2,5}\d{2,5})'

    # 1. Check Subject First
    if subject:
        match = re.search(pattern, subject.upper())
        if match:
            debug_log(f"Job ID {match.group(1)} found in subject: {short(subject, 120)}")
            return match.group(1)

    # 2. Check Email Body as a Fallback
    if email_body:
        match = re.search(pattern, email_body.upper())
        if match:
            debug_log(f"Job ID {match.group(1)} found in email body (not in subject: {short(subject, 120)})")
            return match.group(1)

    debug_log(f"No Job ID found in subject or body (subject: {short(subject, 120)})")
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
    debug_log("Checking Gmail inbox for job application emails")

    # ✅ UPDATED QUERY: Fetch any email with a PDF attachment, bypassing strict subject requirements
    with gmail_call("Gmail list inbox application messages"):
        results = service.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            # replies to our Smart Shortlisting invitations are applications too (even without a PDF,
            # so the candidate gets the "please attach your CV" reply)
            q='subject:"Job Application" OR subject:"Invitation to apply" OR (has:attachment filename:pdf)',
            maxResults=10
        ).execute()

    messages = results.get("messages", [])
    emails = []

    if not messages:
        debug_log("No matching application emails in inbox")
        return emails

    skipped = 0
    for msg_index, msg in enumerate(messages, start=1):
        msg_id = msg["id"]
        if is_email_processed(conn, msg_id):
            skipped += 1
            continue

        mail_read_time = datetime.now()
        debug_log(f"Fetching new email {msg_index}/{len(messages)} msg_id={msg_id} "
                  f"(thread {msg.get('threadId')}) at {mail_read_time}")

        with gmail_call(f"Gmail get message {msg_id}"):
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
        message_id_header = None

        for h in headers:
            if h["name"].lower() == "subject":
                subject = h["value"]
            elif h["name"].lower() == "from":
                from_email = h["value"]
            elif h["name"].lower() == "message-id":
                message_id_header = h["value"]

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

        debug_log(f"Email body ({len(email_body or '')} chars): {short(email_body)}")

        # ✅ Now extract Job ID using BOTH Subject and Body
        job_id = extract_job_id(subject, email_body)

        # ✅ PDF extraction (Updated to find all PDFs)
        def extract_pdfs(parts):
            found_pdfs = []
            for part in parts:
                filename = part.get("filename", "")
                body = part.get("body", {})

                if filename.lower().endswith(".pdf") and "attachmentId" in body:
                    with gmail_call(f"Gmail get attachment '{filename}' of message {msg_id}"):
                        att = service.users().messages().attachments().get(
                            userId="me",
                            messageId=msg_id,
                            id=body["attachmentId"]
                        ).execute()
                    found_pdfs.append(base64.urlsafe_b64decode(att["data"]))
                    debug_log(f"PDF attachment '{filename}' downloaded ({len(found_pdfs[-1])} bytes)")

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

        debug_log(f"Email msg_id={msg_id}: from={from_email}, subject={short(subject, 120)}, job_id={job_id}, "
                  f"pdf attachments={len(all_pdfs)}, thread_id={msg_data.get('threadId')}, "
                  f"parsed fields={[k for k, v in candidate_details.items() if v]}")
        if has_multiple_pdfs:
            warning_log(f"Email msg_id={msg_id} from {from_email} has {len(all_pdfs)} PDF attachments")

        emails.append({
            "msg_id": msg_id,
            "from": from_email,
            "subject": subject,
            "job_id": job_id,
            "mail_body": mail_body_json,
            "internalDate": msg_data["internalDate"],
            "pdf": pdf_bytes,
            "has_multiple_pdfs": has_multiple_pdfs,  # ✅ Added multiple PDFs flag
            "thread_id": msg_data.get("threadId"),
            "message_id_header": message_id_header,
        })

    info_log(f"Inbox check: {len(messages)} application emails found, {skipped} already processed, "
             f"{len(emails)} new fetched"
             + (f" (job ids: {[e['job_id'] for e in emails]})" if emails else ""))
    return emails


# ✅ OPTIONAL (if you want timestamp insert)
def insert_mail_read_timestamp(conn, msg_id, mail_read_time):
    cursor = conn.cursor()

    query = """
        INSERT INTO time_stamps (id, mail_read)
        VALUES (%s, %s)
    """

    cursor.execute(query, (msg_id, mail_read_time))
    debug_log(f"Mail read timestamp inserted for msg_id={msg_id} ({mail_read_time})")


def html_to_text(html):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"'))
    return "\n".join(line.strip() for line in text.splitlines())


def strip_quoted_reply(text):
    """Keep only what the candidate wrote, dropping the quoted earlier message."""
    kept = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if re.match(r"^On .+wrote:\s*$", stripped) or re.match(r"^-{2,}\s*Original Message\s*-{2,}", stripped, re.I) \
                or re.match(r"^From:\s.+", stripped) and kept:
            break
        if stripped.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def fetch_thread_replies(service, thread_id, after_ms=0):
    """Messages the candidate sent in a thread after `after_ms` (epoch ms), oldest first.

    Our own messages (label SENT) are skipped. Each item: msg_id, body (quoted text removed),
    internal_date (ms), message_id_header, subject.
    """
    replies = []
    with gmail_call(f"Gmail get thread {thread_id}"):
        thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    own, older = 0, 0
    for message in thread.get("messages", []):
        if "SENT" in message.get("labelIds", []):
            own += 1
            continue
        internal = int(message.get("internalDate", "0"))
        if internal <= after_ms:
            older += 1
            continue
        payload = message.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        body = extract_email_body(payload) or ""
        if "<" in body and ">" in body and re.search(r"(?i)</?(html|div|p|br|span|table)\b", body):
            body = html_to_text(body)
        replies.append({
            "msg_id": message["id"],
            "body": strip_quoted_reply(body),
            "internal_date": internal,
            "message_id_header": headers.get("message-id"),
            "subject": headers.get("subject"),
            "from": (re.search(r"<(.+?)>", headers.get("from", "")) or re.search(r"(\S+@\S+)", headers.get("from", "")) or [None, None])[1],
        })
    replies.sort(key=lambda r: r["internal_date"])
    debug_log(f"Thread {thread_id}: {len(thread.get('messages', []))} messages, {own} sent by us, "
              f"{older} older than {after_ms}, {len(replies)} new candidate replies "
              f"{[r['msg_id'] for r in replies]}")
    return replies
