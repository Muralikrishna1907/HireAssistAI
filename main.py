import os
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
    save_detail_request,
    get_pending_detail_requests,
    application_thread_status,
    is_job_active
)
from gmail.gmail_service import get_gmail_service
from gmail.gmail_reader import fetch_job_application_emails, fetch_thread_replies, html_to_text, strip_quoted_reply
from gmail.gmail_sender import (
    send_missing_cv_email,
    send_rejection_email,
    send_missing_job_id_matches_email,
    send_multiple_cv_error_email,
    send_missing_details_email,
    send_interview_email,
    send_job_closed_email
)
from llm.details_extractor import (
    REQUIRED_FIELDS,
    extract_candidate_details,
    missing_fields,
    details_for_evaluation
)
from cv.pdf_utils import extract_text_from_pdf_bytes
from matching.text_cleaner import clean_text
from matching.similarity import match_cv_to_best_job, match_cv_to_specific_job, match_cv_to_other_jobs
from config import MATCH_THRESHOLD
from llm.question_generator import generate_interview_questions
import time
from googleapiclient.errors import HttpError
import json
from datetime import datetime
import re
from utils.logger import (
    debug_log, info_log, warning_log, error_log, log_duration, short, get_current_time
)

POLL_INTERVAL_SECONDS = 900   # polling loop sleep (15 minutes)

# ---------------------------------------------------------
# CANDIDATE DETAILS BY EMAIL (replaces the Google Form)
# ---------------------------------------------------------
MAX_DETAIL_REQUESTS = 3   # how many times we ask a candidate for missing details


def send_assessment(conn, service, cv_id, details):
    """All details are in: save them and send the online assessment."""
    debug_log(f"Preparing assessment for CV_ID {cv_id}")
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT EMAIL, JOBTITLE, JOB_ID FROM cv_info WHERE ID = %s", (cv_id,))
        row = cursor.fetchone()
        if not row:
            error_log(f"CV_ID {cv_id} not found; cannot send the assessment.")
            return
        candidate_email, job_title, job_id = row

        cursor.execute("UPDATE cv_info SET STATUS = 'SHORTLISTED', ADDITIONAL_DETAILS = %s WHERE ID = %s",
                       (details_for_evaluation(details), cv_id))
        cursor.execute(
            "UPDATE processed_emails SET FORM_SUBMIT = 'YES', FORM_TIMESTAMP = NOW() WHERE MSG_ID = %s", (cv_id,))
        conn.commit()
        debug_log(f"Candidate details saved for CV_ID {cv_id} (job {job_id}); details marked as submitted")

        try:
            send_interview_email(service, candidate_email, job_title, cv_id, job_id)
            cursor.execute("UPDATE cv_info SET CURRENT_ROUND = 1 WHERE ID = %s", (cv_id,))
            conn.commit()
            conn_temp = get_connection()
            log_event(conn_temp, cv_id, "assessment_mail_sent")
            conn_temp.close()
            info_log(f"Assessment email sent to {candidate_email} for CV_ID {cv_id} (job {job_id} - {job_title}); "
                     f"CURRENT_ROUND set to 1")
        except Exception as mail_err:
            error_log(f"Failed to send Assessment email to {candidate_email} for CV_ID {cv_id} (job {job_id}): {str(mail_err)}")
    finally:
        cursor.close()


def check_details_and_continue(conn, service, cv_id, details, reply_to, thread_id, message_id_header,
                               subject, requests_sent, last_checked_ms, name, job_title, job_id):
    """Send the assessment when all details are present; otherwise ask (in the same thread) for what is missing."""
    missing = missing_fields(details)
    debug_log(f"Details check for CV_ID {cv_id} (job {job_id}) - thread {thread_id}, "
              f"requests sent so far: {requests_sent}/{MAX_DETAIL_REQUESTS}")
    if not missing:
        save_detail_request(conn, cv_id, thread_id, message_id_header, subject, details, [],
                            requests_sent, "COMPLETE", last_checked_ms)
        info_log(f"Details check CV_ID {cv_id}: COMPLETE - all {len(REQUIRED_FIELDS)} details received; sending the assessment.")
        send_assessment(conn, service, cv_id, details)
        return

    if requests_sent >= MAX_DETAIL_REQUESTS:
        save_detail_request(conn, cv_id, thread_id, message_id_header, subject, details, missing,
                            requests_sent, "INCOMPLETE", last_checked_ms)
        warning_log(f"Details check CV_ID {cv_id}: INCOMPLETE - still missing {missing} after {requests_sent} requests; "
                    f"no more reminders will be sent.")
        return

    info_log(f"Details check CV_ID {cv_id}: missing {len(missing)} field(s) {missing}")
    try:
        send_missing_details_email(
            service, reply_to, details.get("full_name") or name, job_title, job_id,
            [REQUIRED_FIELDS[k] for k in missing],
            [(REQUIRED_FIELDS[k], details.get(k)) for k in REQUIRED_FIELDS if details.get(k)],
            subject, thread_id=thread_id, in_reply_to=message_id_header, reminder=requests_sent > 0)
        info_log(f"Missing-details {'reminder' if requests_sent > 0 else 'request'} #{requests_sent + 1} sent to "
                 f"{reply_to} for CV_ID {cv_id} (thread {thread_id}): {missing}")
        if requests_sent == 0:
            conn_temp = get_connection()
            log_event(conn_temp, cv_id, "form_link_mail")    # timestamp of the first details request
            conn_temp.close()
        requests_sent += 1
    except Exception as e:
        error_log(f"Missing-details email failed for CV_ID {cv_id} (thread {thread_id}, to {reply_to}): {str(e)}")
    save_detail_request(conn, cv_id, thread_id, message_id_header, subject, details, missing,
                        requests_sent, "PENDING", last_checked_ms)
    debug_log(f"Details request for CV_ID {cv_id} saved as PENDING ({requests_sent} request(s) sent)")


def process_detail_replies(conn, service):
    """Read candidates' replies to our details requests and continue their applications."""
    try:
        pending = get_pending_detail_requests(conn)
    except Exception as e:
        error_log(f"Could not load pending details requests: {str(e)}")
        return set()
    if pending:
        info_log(f"Checking {len(pending)} application(s) waiting for candidate details.")
    else:
        debug_log("No applications are waiting for candidate details.")

    for req in pending:
        try:
            replies = [r for r in fetch_thread_replies(service, req["thread_id"], req["last_checked_ms"])
                       if not is_email_processed(conn, r["msg_id"])]
        except Exception as e:
            error_log(f"Could not read thread {req['thread_id']} for CV_ID {req['cv_id']}: {str(e)}")
            continue
        if not replies:
            debug_log(f"No new replies for CV_ID {req['cv_id']} in thread {req['thread_id']} "
                      f"({req['requests_sent']} request(s) sent so far)")
            continue

        info_log(f"Found {len(replies)} new reply(ies) for CV_ID {req['cv_id']} (job {req['job_id']}) "
                 f"in thread {req['thread_id']}: {[r['msg_id'] for r in replies]}")
        for r in replies:
            mark_email_processed(conn, r["msg_id"])
            try:
                service.users().messages().modify(userId="me", id=r["msg_id"],
                                                  body={"removeLabelIds": ["UNREAD"]}).execute()
            except Exception as e:
                warning_log(f"Could not mark reply {r['msg_id']} as read for CV_ID {req['cv_id']}: {e}")

        text = "\n\n".join(r["body"] for r in replies if r["body"])
        debug_log(f"Reply text for CV_ID {req['cv_id']}: {len(text)} chars")
        with log_duration(f"Details extraction from replies for CV_ID {req['cv_id']}"):
            details = extract_candidate_details(text, "", known=req["details"])
        last = replies[-1]
        check_details_and_continue(
            conn, service, req["cv_id"], details,
            reply_to=last.get("from") or req["email"], thread_id=req["thread_id"],
            message_id_header=last.get("message_id_header") or req["message_id_header"],
            subject=req["subject"], requests_sent=req["requests_sent"],
            last_checked_ms=last["internal_date"], name=req["name"],
            job_title=req["job_title"], job_id=req["job_id"])



# ---------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------

def main():
    info_log("Starting main processing cycle")
    cycle_started = time.perf_counter()

    try:
        with log_duration("Database connection", level="DEBUG"):
            conn = get_connection()
        with log_duration("Gmail service initialisation", level="DEBUG"):
            service = get_gmail_service()
        with log_duration("SentenceTransformer model load (all-MiniLM-L6-v2)", level="DEBUG"):
            model = SentenceTransformer("all-MiniLM-L6-v2")
    except Exception as e:
        error_log(f"Failed to initialize services: {str(e)}")
        return
    
    # ---------------------------------------------------------
    # STEP 1: Candidates' replies with the details we asked for
    # ---------------------------------------------------------
    process_detail_replies(conn, service)

    # ---------------------------------------------------------
    # STEP 2: Original CV Processing Flow
    # ---------------------------------------------------------
    try:
        with log_duration("Fetching job application emails", level="DEBUG"):
            emails = fetch_job_application_emails(conn, service)
        if emails:
            info_log(f"Retrieved {len(emails)} unread job application emails")
        else:
            info_log("No new job application emails")
    except Exception as e:
        error_log(f"Failed to fetch job application emails: {str(e)}")
        emails = []
    
    processed_count = 0
    rejected_count = 0
    shortlisted_count = 0

    for email_index, mail in enumerate(emails, start=1):
        try:
            cv_id = mail.get("msg_id")
            # A reply inside an existing application's thread is never a new application
            # (e.g. "Re: Job Application BTP001" with the requested details).
            thread_state = application_thread_status(conn, mail.get("thread_id"), cv_id)
            if thread_state == "PENDING":
                debug_log(f"Message {cv_id} is a reply to a details request; handled in step 1.")
                continue
            if thread_state == "KNOWN":
                info_log(f"Message {cv_id} is a reply in an existing application thread; not a new application.")
                mark_email_processed(conn, cv_id)
                continue
            from_email = mail.get("from")
            job_id = mail.get("job_id")
            subject = mail.get("subject")

            info_log(f"Processing email {email_index}/{len(emails)} - MSG_ID: {cv_id}, From: {from_email}, "
                     f"Subject: '{short(subject, 120)}', Job ID: {job_id or 'none'}")

            mail_received_ts = None
            if mail.get("internalDate"):
                mail_received_ts = datetime.fromtimestamp(int(mail["internalDate"]) / 1000)
            
            # 1. Reject Multiple PDFs
            if mail.get("has_multiple_pdfs"):
                warning_log(f"Decision MSG_ID {cv_id}: MULTIPLE PDFs from {from_email} - rejecting and asking for a single CV.")
                send_multiple_cv_error_email(service, from_email)
                mark_email_processed(conn, cv_id)
                continue

            # 2. Handle Missing PDF
            if not mail.get("pdf"):
                warning_log(f"Decision MSG_ID {cv_id}: MISSING CV - no PDF attached from {from_email}; sending missing CV request.")
                send_missing_cv_email(service, from_email, subject,
                                      thread_id=mail.get("thread_id"), in_reply_to=mail.get("message_id_header"))
                mark_email_processed(conn, cv_id)
                continue

            # 3. Extract CV text
            try:
                with log_duration(f"PDF text extraction for MSG_ID {cv_id}", level="DEBUG"):
                    raw_cv_text = extract_text_from_pdf_bytes(mail.get("pdf")) or ""
                debug_log(f"CV text extracted for MSG_ID {cv_id}: {len(raw_cv_text)} chars")
            except Exception as e:
                error_log(f"PDF extraction failed for MSG_ID {cv_id} from {from_email}: {str(e)}")
                continue

            # 4. LLM parsing
            try:
                with log_duration(f"CV parsing (LLM) for MSG_ID {cv_id}"):
                    cv_data = parse_cv_with_llm(raw_cv_text)
                debug_log(f"LLM Parsing successful for MSG_ID {cv_id} - skills: {len(cv_data.get('skills') or [])}, "
                          f"experience: {cv_data.get('total_experience')}")
            except Exception as e:
                error_log(f"LLM parsing failed for MSG_ID {cv_id} from {from_email}: {str(e)}")
                cv_data = {"name": None, "email": None, "skills": [], "total_experience": 0, "education": []}

            name = cv_data.get("name") or "Candidate"
            extracted_email = str(cv_data.get("email") or "").strip()

            if not extracted_email or "@" not in extracted_email or extracted_email.lower() in ["none", "null", "not found"]:
                email = from_email
                warning_log(f"No valid email found in the CV for MSG_ID {cv_id}. Defaulting to sender: {email}")
            else:
                email = extracted_email

            email = email or "unknown@email.com"
            debug_log(f"Candidate for MSG_ID {cv_id}: name '{name}', email {email}")

            # 5. Spam Blocker (By Candidate Name & Job ID)
            if job_id and has_applied_by_name(conn, name, job_id):
                warning_log(f"Decision MSG_ID {cv_id}: DUPLICATE - candidate '{name}' already applied for {job_id}. Skipping.")
                mark_email_processed(conn, cv_id)
                continue

            # 6. Handle Missing Job ID
            skills_text = " ".join(cv_data.get("skills", []))
            cv_text = clean_text(raw_cv_text + " " + skills_text) or ""

            if not job_id:
                warning_log(f"Decision MSG_ID {cv_id}: NO JOB ID from {email} - analyzing CV for best matches...")
                with log_duration(f"Job matching (other jobs) for MSG_ID {cv_id}", level="DEBUG"):
                    matched_jobs = match_cv_to_other_jobs(model, cv_text, None, conn) or []

                try:
                    send_missing_job_id_matches_email(service, email, matched_jobs)
                    info_log(f"Missing Job ID email sent to {email} for MSG_ID {cv_id} with {len(matched_jobs)} suggested job(s)")
                except Exception as e:
                    error_log(f"Missing Job ID recommendation email failed for {email} (MSG_ID {cv_id}): {str(e)}")

                mark_email_processed(conn, cv_id)
                continue

            # 6b. Job made Inactive on the Jobs page: no new applications; suggest open roles instead
            if not is_job_active(conn, job_id):
                warning_log(f"Decision MSG_ID {cv_id}: JOB INACTIVE - {email} applied for {job_id}, which is not "
                            f"accepting applications. Not saved; sending position-closed email.")
                with log_duration(f"Job matching (other jobs) for MSG_ID {cv_id}", level="DEBUG"):
                    matched_jobs = match_cv_to_other_jobs(model, cv_text, job_id, conn) or []
                try:
                    send_job_closed_email(service, email, job_id, matched_jobs)
                    info_log(f"Position-closed email sent to {email} for MSG_ID {cv_id} (job {job_id}) "
                             f"with {len(matched_jobs)} suggested job(s)")
                except Exception as e:
                    error_log(f"Position-closed email failed for {email} (MSG_ID {cv_id}, job {job_id}): {str(e)}")
                mark_email_processed(conn, cv_id)
                continue

            # 7. Insert CV into CV_INFO
            skills_list = cv_data.get("skills", [])
            skills_list = [str(s) for s in skills_list] if isinstance(skills_list, list) else []
            skills = ", ".join(skills_list)

            try:
                experience = float(cv_data.get("total_experience", 0))
            except Exception:
                warning_log(f"Invalid total_experience '{cv_data.get('total_experience')}' for MSG_ID {cv_id}; using 0.0")
                experience = 0.0

            education = json.dumps(cv_data.get("education", []))

            try:
                insert_cv(
                    conn, cv_id, name, email, mail.get("pdf"), job_id,
                    skills, experience, education, mail.get("mail_body")
                )
                debug_log(f"Inserted CV {cv_id} into database for {email} (job {job_id}, {len(skills_list)} skills, "
                          f"{experience} yrs experience)")
            except Exception as e:
                error_log(f"Database insertion failed for MSG_ID {cv_id} ({email}, job {job_id}): {str(e)}")
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
                with log_duration(f"Job matching for MSG_ID {cv_id} (job {job_id})", level="DEBUG"):
                    title, description, score = match_cv_to_specific_job(
                        model, cv_text, job_id, conn, cv_data
                    )
                debug_log(f"Matched {email} to {title} - Score: {score:.4f}")
                if title is None:
                    warning_log(f"Job {job_id} for MSG_ID {cv_id} was not found; match score is 0.0")
            except Exception as e:
                error_log(f"Vector matching failed for MSG_ID {cv_id} ({email}, job {job_id}): {str(e)}")
                title, description, score = "Unknown", "", 0.0

            # 9. Dispatch Decision
            if score >= MATCH_THRESHOLD:
                info_log(f"Decision MSG_ID {cv_id}: SHORTLISTED {email} for job {job_id} ({title}) - score {score:.2f} "
                         f">= threshold {MATCH_THRESHOLD}; checking candidate details")
                update_cv_status(conn, cv_id, score, title, "SHORTLISTED")

                try:
                    with log_duration(f"Question generation for CV_ID {cv_id}"):
                        questions = generate_interview_questions(
                            cv_text=cv_text, jd_text=description, num_questions=5
                        )
                except Exception as e:
                    error_log(f"LLM question generation failed for CV_ID {cv_id} ({email}): {str(e)}")
                    questions = []

                save_questions(conn, cv_id, questions)
                debug_log(f"Saved {len(questions)} question(s) for CV_ID {cv_id}")
                if not questions:
                    warning_log(f"No interview questions saved for CV_ID {cv_id}")

                # Check the required details (email body + CV); ask in the same thread for anything missing
                try:
                    body_text = (mail.get("mail_body") or {}).get("raw_body") or ""
                    if re.search(r"(?i)</?(html|div|p|br|span|table)\b", body_text):
                        body_text = html_to_text(body_text)
                    # a reply to our invitation quotes the invitation (job description, "Location: ...");
                    # only what the candidate wrote counts
                    body_text = strip_quoted_reply(body_text)
                    # details the candidate gave earlier in the same thread (e.g. a first reply without the CV)
                    thread_id = mail.get("thread_id")
                    if thread_id and thread_id != cv_id:
                        try:
                            earlier = [r["body"] for r in fetch_thread_replies(service, thread_id, 0)
                                       if r["msg_id"] != cv_id and r["body"]
                                       and r["internal_date"] < int(mail.get("internalDate") or 0)]
                            if earlier:
                                debug_log(f"Including {len(earlier)} earlier message(s) from thread {thread_id} "
                                          f"in the details check for CV_ID {cv_id}")
                                body_text = "\n\n".join(earlier + [body_text])
                        except Exception as e:
                            warning_log(f"Could not read earlier messages in thread {thread_id} for CV_ID {cv_id}: {str(e)}")
                    debug_log(f"Details check input for CV_ID {cv_id}: email text {len(body_text)} chars, "
                              f"CV text {len(raw_cv_text)} chars")
                    with log_duration(f"Details extraction for CV_ID {cv_id}"):
                        details = extract_candidate_details(body_text, raw_cv_text)
                    if not details.get("full_name") and name and name != "Candidate":
                        details["full_name"] = name
                    if not details.get("mobile_number") and cv_data.get("phone"):
                        details["mobile_number"] = str(cv_data.get("phone"))
                    check_details_and_continue(
                        conn, service, cv_id, details,
                        reply_to=from_email or email, thread_id=mail.get("thread_id"),
                        message_id_header=mail.get("message_id_header"),
                        subject=subject or f"Application for {title}", requests_sent=0,
                        last_checked_ms=int(mail.get("internalDate") or 0), name=name,
                        job_title=title, job_id=job_id)
                except Exception as e:
                    error_log(f"Candidate details check failed for CV_ID {cv_id} ({email}): {str(e)}")

                shortlisted_count += 1

            else:
                info_log(f"Decision MSG_ID {cv_id}: REJECTED {email} for job {job_id} ({title}) - score {score:.2f} "
                         f"< threshold {MATCH_THRESHOLD}")
                with log_duration(f"Job matching (other jobs) for MSG_ID {cv_id}", level="DEBUG"):
                    matched_jobs = match_cv_to_other_jobs(model, cv_text, job_id, conn) or []
                update_cv_status(conn, cv_id, score, title, "NOT SHORTLISTED")

                try:
                    send_rejection_email(service, email, matched_jobs)
                    info_log(f"Rejection email sent to {email} for MSG_ID {cv_id} with {len(matched_jobs)} other job suggestion(s)")
                except Exception as e:
                    error_log(f"Rejection email failed for {email} (MSG_ID {cv_id}): {str(e)}")

                rejected_count += 1

            processed_count += 1

        except Exception as e:
            error_log(f"Critical error processing email MSG_ID {mail.get('msg_id')} from {mail.get('from')}: {str(e)}")
            continue

    try:
        conn.commit()
        conn.close()
        debug_log("Database connection committed and closed")
    except Exception as e:
        error_log(f"Database connection closure failed: {str(e)}")

    info_log(f"Cycle Complete in {time.perf_counter() - cycle_started:.1f}s | Emails: {len(emails)} | "
             f"Processed: {processed_count} | Shortlisted: {shortlisted_count} | Rejected: {rejected_count} | "
             f"Skipped/other: {len(emails) - processed_count}")


if __name__ == "__main__":
    info_log(f"Application successfully started - Entering {POLL_INTERVAL_SECONDS // 60}-minute polling loop "
             f"(match threshold {MATCH_THRESHOLD}, max details requests {MAX_DETAIL_REQUESTS})")
    iteration = 1

    while True:
        try:
            info_log(f"--- Starting Iteration {iteration} at {get_current_time()} ---")
            main()
            next_run = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + POLL_INTERVAL_SECONDS))
            info_log(f"Iteration {iteration} completed at {get_current_time()}. Sleeping for {POLL_INTERVAL_SECONDS} seconds "
                     f"({POLL_INTERVAL_SECONDS // 60} minutes); next run at {next_run}.")
            time.sleep(POLL_INTERVAL_SECONDS)
            iteration += 1
        except Exception as e:
            error_log(f"FATAL CRASH in main loop at iteration {iteration} (Type: {type(e).__name__}): {str(e)}")
            warning_log("Sleeping for 60 seconds before attempting recovery...")
            time.sleep(60)