import os
import inspect
from sentence_transformers import SentenceTransformer
from cv.cv_parser_llm import parse_cv_with_llm
from db.connection import get_connection
from db.cv_repository import (
    insert_cv,
    log_event,
    update_cv_status,
    get_job_description,
    save_questions,
    mark_email_processed,
    log_mail_received,
    has_applied_by_name,
    is_email_processed,
    mark_webhook_processed
)
from gmail.gmail_service import get_gmail_service
from gmail.gmail_reader import fetch_job_application_emails, fetch_webhook_submissions
from gmail.gmail_sender import (
    send_missing_cv_email,
    send_rejection_email,
    send_missing_job_id_matches_email,
    send_multiple_cv_error_email,
    send_additional_details_email,
    send_interview_email
)
from cv.pdf_utils import extract_text_from_pdf_bytes
from matching.text_cleaner import clean_text
from matching.similarity import match_cv_to_best_job, match_cv_to_specific_job, match_cv_to_other_jobs
from config import MATCH_THRESHOLD
from llm.question_generator import generate_interview_questions
from llm.form_evaluator import evaluate_candidate_form_with_llm
import time
from googleapiclient.errors import HttpError
import json
from datetime import datetime
import re
from utils.logger import debug_log, info_log, warning_log, error_log

# ---------------------------------------------------------
# SYSTEM LOGGING UTILITIES (Now with timestamps)
# ---------------------------------------------------------
def get_current_time():
    """Helper to get formatted current timestamp."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# def get_log_context():
#     """Helper to grab the exact file, function, and line number of the caller."""
#     frame = inspect.currentframe().f_back.f_back
#     filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
#     function_name = frame.f_code.co_name
#     line_number = frame.f_lineno
#     return filename, function_name, line_number

# def debug_log(message):
#     file, func, line = get_log_context()
#     print(f"[{get_current_time()}] [DEBUG] {file}::{func}:{line} - {message}")

# def info_log(message):
#     file, func, line = get_log_context()
#     print(f"[{get_current_time()}] [INFO] {file}::{func}:{line} - {message}")

# def warning_log(message):
#     file, func, line = get_log_context()
#     print(f"[{get_current_time()}] [WARNING] {file}::{func}:{line} - {message}")

# def error_log(message):
#     file, func, line = get_log_context()
#     print(f"[{get_current_time()}] [ERROR] {file}::{func}:{line} - {message}")

# ---------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------

def main():
    info_log("Starting main processing cycle")
    
    try:
        conn = get_connection()
        service = get_gmail_service()
        model = SentenceTransformer("all-MiniLM-L6-v2")
    except Exception as e:
        error_log(f"Failed to initialize services: {str(e)}")
        return
    
    # ---------------------------------------------------------
    # STEP 1: Process Form Submissions (The Local Webhook)
    # ---------------------------------------------------------
    try:
        submissions = fetch_webhook_submissions(service)
    except Exception as e:
        error_log(f"Failed to fetch webhook submissions: {str(e)}")
        submissions = []

    if submissions:
        info_log(f"Found {len(submissions)} form submissions to process.")
        
    for sub in submissions:
        candidate_cv_id = sub.get("candidate_cv_id")
        webhook_email_id = sub.get("webhook_msg_id")
        form_json_data = sub.get("form_data_json")
        
        cursor = conn.cursor()
        
        # 0. DATABASE CHECK
        try:
            cursor.execute("SELECT FORM_SUBMIT FROM processed_emails WHERE MSG_ID = %s", (candidate_cv_id,))
            status_row = cursor.fetchone()
            
            if status_row and status_row[0] == 'YES':
                warning_log(f"Form for Candidate CV {candidate_cv_id} already marked 'YES'. Skipping.")
                try:
                    service.users().messages().modify(userId='me', id=webhook_email_id, body={'removeLabelIds': ['UNREAD']}).execute()
                except Exception:
                    pass
                continue
        except Exception as e:
            error_log(f"Database error checking form submission status: {str(e)}")
            continue
        
        # 1. Fetch details
        try:
            cursor.execute("SELECT EMAIL, JOBTITLE, JOB_ID FROM cv_info WHERE ID = %s", (candidate_cv_id,))
            result = cursor.fetchone()
        except Exception as e:
            error_log(f"Database error fetching CV details for ID {candidate_cv_id}: {str(e)}")
            result = None
            
        if result:
            candidate_email, job_title, fetched_job_id = result
            
            # 2. Clean JSON
            try:
                parsed_dict = json.loads(form_json_data, strict=False)
                safe_json_data = json.dumps(parsed_dict, ensure_ascii=True)
            except Exception as parse_err:
                warning_log(f"Failed to pre-parse JSON, using raw string: {str(parse_err)}")
                safe_json_data = form_json_data

            # 3. Fetch JD and Evaluate with LLM
            try:
                jd_text = get_job_description(conn, fetched_job_id)
                info_log(f"Sending CV_ID {candidate_cv_id} form data to LLM for JD evaluation.")
                
                eval_result = evaluate_candidate_form_with_llm(safe_json_data, jd_text)
                is_shortlisted = str(eval_result.get("isShortlisted", "No")).strip().upper()
                eval_reason = eval_result.get("Reason", "No reason provided by LLM.")
                
                debug_log(f"LLM Decision: {is_shortlisted} | Reason: {eval_reason}")
                
            except Exception as eval_err:
                error_log(f"LLM Pipeline failed for {candidate_cv_id}: {str(eval_err)}")
                eval_reason = "LLM Evaluation Failure"
                is_shortlisted = "NO"

            # 4. Update Database (Force STATUS to remain 'SHORTLISTED' so the resume status isn't overwritten)
            try:
                cursor.execute(
                    "UPDATE cv_info SET STATUS = 'SHORTLISTED', ADDITIONAL_DETAILS = %s, EVALUATION_REASON = %s WHERE ID = %s", 
                    (safe_json_data, eval_reason, candidate_cv_id)
                )
                conn.commit()
                debug_log(f"Successfully saved form details and LLM evaluation for CV_ID: {candidate_cv_id}")
            except Exception as db_err:
                error_log(f"Failed to update database for CV_ID {candidate_cv_id}: {str(db_err)}")

            # 5. Dispatch Email based on LLM Decision & Set Current Flag
            assessment_sent_flag = 0  # Default to 0 before sending
            
            # 5. Dispatch Email based on LLM Decision & Set Final Statuses
            if is_shortlisted == "YES":
                try:
                    # Send assessment email
                    send_interview_email(service, candidate_email, job_title, candidate_cv_id, fetched_job_id)
                    
                    # Update the CURRENT_ROUND flag to 1
                    cursor.execute(
                        "UPDATE cv_info SET CURRENT_ROUND = 1 WHERE ID = %s", 
                        (candidate_cv_id,)
                    )
                    conn.commit()
                    
                    info_log(f"Assessment email successfully sent and CURRENT_ROUND updated to 1 for CV_ID: {candidate_cv_id}")
                    
                    # Log timestamp
                    conn_temp = get_connection()
                    log_event(conn_temp, candidate_cv_id, "assessment_mail_sent")
                    conn_temp.close()
                except Exception as mail_err:
                    error_log(f"Failed to send Assessment email to {candidate_email}: {str(mail_err)}")
            else:
                try:
                    # Send rejection email
                    send_rejection_email(service, candidate_email, [])
                    
                    # Explicitly mark FINAL_STATUS as REJECTED in the database
                    cursor.execute(
                        "UPDATE cv_info SET FINAL_STATUS = 'REJECTED' WHERE ID = %s", 
                        (candidate_cv_id,)
                    )
                    conn.commit()
                    
                    info_log(f"Rejection email sent and FINAL_STATUS updated to 'REJECTED' for CV_ID: {candidate_cv_id}")
                except Exception as mail_err:
                    error_log(f"Failed to send Rejection email to {candidate_email}: {str(mail_err)}")
        
        # 6. Mark webhook email as read in Gmail
        try:
            service.users().messages().modify(
                userId='me', 
                id=webhook_email_id, 
                body={'removeLabelIds': ['UNREAD']}
            ).execute()
        except Exception as e:
            warning_log(f"Failed to mark webhook email {webhook_email_id} as read in Gmail: {str(e)}")
            
        # 7. DATABASE LOG: Update processed_emails table
        try:
            cursor.execute(
                "UPDATE processed_emails SET FORM_SUBMIT = 'YES', FORM_TIMESTAMP = NOW() WHERE MSG_ID = %s",
                (candidate_cv_id,)
            )
            debug_log(f"Updated FORM_SUBMIT to 'YES' for CV_ID {candidate_cv_id} in processed_emails table.")
        except Exception as e:
            error_log(f"Failed to log form submission in timestamps table for {candidate_cv_id}: {str(e)}")
            
        conn.commit()
        cursor.close()

    # ---------------------------------------------------------
    # STEP 2: Original CV Processing Flow
    # ---------------------------------------------------------
    try:
        emails = fetch_job_application_emails(conn, service)
        if emails:
            info_log(f"Retrieved {len(emails)} unread job application emails")
    except Exception as e:
        error_log(f"Failed to fetch job application emails: {str(e)}")
        emails = []
    
    processed_count = 0
    rejected_count = 0
    shortlisted_count = 0

    for email_index, mail in enumerate(emails, start=1):
        try:
            cv_id = mail.get("msg_id")
            from_email = mail.get("from")
            job_id = mail.get("job_id")
            subject = mail.get("subject")
            
            debug_log(f"Processing CV Email {email_index}/{len(emails)} - From: {from_email}")

            mail_received_ts = None
            if mail.get("internalDate"):
                mail_received_ts = datetime.fromtimestamp(int(mail["internalDate"]) / 1000)
            
            # 1. Reject Multiple PDFs
            if mail.get("has_multiple_pdfs"):
                warning_log(f"Multiple PDFs detected from {from_email}. Rejecting application.")
                send_multiple_cv_error_email(service, from_email)
                mark_email_processed(conn, cv_id)
                continue

            # 2. Handle Missing PDF
            if not mail.get("pdf"):
                warning_log(f"No PDF attached from {from_email}. Sending missing CV request.")
                send_missing_cv_email(service, from_email, subject)
                mark_email_processed(conn, cv_id)
                continue

            # 3. Extract CV text
            try:
                raw_cv_text = extract_text_from_pdf_bytes(mail.get("pdf")) or ""
            except Exception as e:
                error_log(f"PDF extraction failed for {from_email}: {str(e)}")
                continue

            # 4. LLM parsing
            try:
                cv_data = parse_cv_with_llm(raw_cv_text)
                debug_log(f"LLM Parsing successful for {from_email}")
            except Exception as e:
                error_log(f"LLM parsing failed for {from_email}: {str(e)}")
                cv_data = {"name": None, "email": None, "skills": [], "total_experience": 0, "education": []}

            name = cv_data.get("name") or "Candidate"
            extracted_email = str(cv_data.get("email") or "").strip()
            
            if not extracted_email or "@" not in extracted_email or extracted_email.lower() in ["none", "null", "not found"]:
                email = from_email
                warning_log(f"LLM failed to extract valid email. Defaulting to sender: {email}")
            else:
                email = extracted_email

            email = email or "unknown@email.com"

            # 5. Spam Blocker (By Candidate Name & Job ID)
            if job_id and has_applied_by_name(conn, name, job_id):
                warning_log(f"Spam Prevention triggered: Candidate '{name}' already applied for {job_id}. Skipping.")
                mark_email_processed(conn, cv_id)
                continue

            # 6. Handle Missing Job ID
            skills_text = " ".join(cv_data.get("skills", []))
            cv_text = clean_text(raw_cv_text + " " + skills_text) or ""

            if not job_id:
                warning_log(f"No Job ID found for {email}. Analyzing CV for best matches...")
                matched_jobs = match_cv_to_other_jobs(model, cv_text, None, conn) or []
                
                try:
                    send_missing_job_id_matches_email(service, email, matched_jobs)
                except Exception as e:
                    error_log(f"Missing Job ID recommendation email failed for {email}: {str(e)}")
                
                mark_email_processed(conn, cv_id)
                continue

            # 7. Insert CV into CV_INFO 
            skills_list = cv_data.get("skills", [])
            skills_list = [str(s) for s in skills_list] if isinstance(skills_list, list) else []
            skills = ", ".join(skills_list)

            try:
                experience = float(cv_data.get("total_experience", 0))
            except Exception:
                experience = 0.0

            education = json.dumps(cv_data.get("education", []))

            try:
                insert_cv(
                    conn, cv_id, name, email, mail.get("pdf"), job_id,
                    skills, experience, education, mail.get("mail_body")
                )
                debug_log(f"Successfully inserted CV into database for {email}")
            except Exception as e:
                error_log(f"Database insertion failed for {email}: {str(e)}")
                continue
            
            try:
                conn_temp = get_connection()
                if mail_received_ts:
                    log_mail_received(conn_temp, cv_id, mail_received_ts)
                log_event(conn_temp, cv_id, "mail_read")
                conn_temp.close()
            except Exception as e:
                error_log(f"Failed to log initial timestamps for {cv_id}: {str(e)}")
            
            mark_email_processed(conn, cv_id)

            # 8. Match job safely
            try:
                title, description, score = match_cv_to_specific_job(
                    model, cv_text, job_id, conn, cv_data
                )
                debug_log(f"Matched {email} to {title} - Score: {score:.4f}")
            except Exception as e:
                error_log(f"Vector matching failed for {email}: {str(e)}")
                title, description, score = "Unknown", "", 0.0

            # 9. Dispatch Decision
            if score >= MATCH_THRESHOLD:
                info_log(f"SHORTLISTING candidate {email} with score {score:.2f} - Pending Additional Details")
                update_cv_status(conn, cv_id, score, title, "SHORTLISTED")

                try:
                    questions = generate_interview_questions(
                        cv_text=cv_text, jd_text=description, num_questions=5
                    )
                except Exception as e:
                    error_log(f"LLM question generation failed for {email}: {str(e)}")
                    questions = []

                save_questions(conn, cv_id, questions)

                try:
                    base_url = "https://docs.google.com/forms/d/e/1FAIpQLScEGRr1i6SKcgdQ0Sw3nu_i4Q8YuUhPQEwd-q5A6PceGuxo6A/viewform?usp=pp_url&entry.833441891="
                    dynamic_form_url = f"{base_url}{cv_id}" 
                    
                    send_additional_details_email(service, email, title, dynamic_form_url, cv_id)
                except Exception as e:
                    error_log(f"Additional details Google Form email failed for {email}: {str(e)}")
                
                try:
                    conn_temp = get_connection()
                    log_event(conn_temp, cv_id, "form_link_mail")
                    conn_temp.close()
                except Exception as e:
                    error_log(f"Failed to log form_link_mail timestamp for {email}: {str(e)}")

                shortlisted_count += 1

            else:
                info_log(f"REJECTING candidate {email} with score {score:.2f}")
                matched_jobs = match_cv_to_other_jobs(model, cv_text, job_id, conn) or []
                update_cv_status(conn, cv_id, score, title, "NOT SHORTLISTED")

                try:
                    send_rejection_email(service, email, matched_jobs)
                except Exception as e:
                    error_log(f"Rejection email failed for {email}: {str(e)}")
                
                rejected_count += 1

            processed_count += 1

        except Exception as e:
            error_log(f"Critical error processing email MSG_ID {mail.get('msg_id')}: {str(e)}")
            continue

    try:
        conn.commit()
        conn.close()
    except Exception as e:
        error_log(f"Database connection closure failed: {str(e)}")

    info_log(f"Cycle Complete | Processed: {processed_count} | Shortlisted: {shortlisted_count} | Rejected: {rejected_count}")
    
    
if __name__ == "__main__":
    info_log("Application successfully started - Entering 15-minute polling loop")
    iteration = 1
    
    while True:
        try:
            info_log(f"--- Starting Iteration {iteration} ---")
            main()
            info_log(f"Iteration {iteration} completed at {get_current_time()}. Sleeping for 900 seconds (15 minutes).")
            time.sleep(900)
            iteration += 1
        except Exception as e:
            error_log(f"FATAL CRASH in main loop (Type: {type(e).__name__}): {str(e)}")
            warning_log("Sleeping for 60 seconds before attempting recovery...")
            time.sleep(60)