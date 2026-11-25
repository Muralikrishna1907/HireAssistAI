import base64
from email.mime.text import MIMEText
# from app import generate_exam_link
from db.connection import get_connection
from utils.logger import debug_log, info_log, warning_log
from gmail.gmail_service import gmail_call

def send_missing_cv_email(service, to_email, subject, thread_id=None, in_reply_to=None):
    safe_subject = subject if subject else "Job Application"
    debug_log(f"Preparing missing-CV email to {to_email} (subject: {safe_subject}, thread: {thread_id})")
    
    body = (
        "Dear Candidate,\n\n"
        "We have recieved your Application. However, your application did not include a CV.\n"
        "Please reply to this mail with your CV attached.\n\n"
        "Best Regards,\nRecruitment Team"
    )
    # reply inside the candidate's thread, so their reply with the CV keeps the Job ID subject
    send_reply_in_thread(service, to_email, safe_subject, body, thread_id, in_reply_to)

def send_interview_email(service, to_email, job_title, exam_id, job_id):
    debug_log(f"Preparing assessment invitation to {to_email} - exam_id={exam_id}, job_id={job_id}, job_title={job_title}")
    # ADD THE IMPORT HERE:
    from app import generate_exam_link

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT INTERVIEW_TIME FROM JOB_INFO WHERE ID = %s", (job_id,))
    result = cursor.fetchone()

    interview_time = result[0] if result else 30
    if not result:
        warning_log(f"Job job_id={job_id} not found when reading INTERVIEW_TIME - using default 30 minutes")

    cursor.close()
    conn.close()

    # exam_link = "https://192.168.1.10:8000/"  # Replace with production URL
    exam_link = generate_exam_link(exam_id)
    support_email = "support@sbp.com"
    debug_log(f"Exam link generated for exam_id={exam_id} (duration {interview_time} min)")

    body = (
        "Dear Candidate,\n\n"

        f"Thank you for applying for the {job_title} position"
        + (f" (Job ID: {job_id})" if job_id else "") + ".\n\n"

        "Based on a review of your profile, you have been shortlisted for the next step of our "
        "selection process: an ONLINE ASSESSMENT.\n\n"

        "Please note: this is an invitation to take the online assessment only. It is not an offer "
        "or a final selection for the role. Candidates who clear the assessment will be contacted "
        "about the further interview rounds.\n\n"

        "Assessment Details:\n"
        # f"Exam Link : {exam_link}\n"
        # f"Exam ID   : {exam_id}\n"
        f"Exam Link : {exam_link}\n"
        f"Duration  : {interview_time} Minutes\n"
        "Validity  : Complete within 48 hours of receiving this email.\n\n"

        "Important Guidelines:\n"
        "- Camera access is mandatory throughout the assessment.\n"
        "- No external assistance, devices or tab switching is permitted.\n"
        "- Three (3) detected violations will result in automatic termination of the interview.\n\n"

        "By proceeding, you consent to monitoring for identity verification "
        "and assessment integrity.\n\n"

        "Please ensure a stable internet connection and a quiet environment.\n\n"

        f"For technical assistance, contact: {support_email}\n\n"

        "Best Regards,\n"
        "Recruitment Team"
    )
    _send(
        service,
        to_email,
        f"Shortlisted for Online Assessment – {job_title}" + (f" ({job_id})" if job_id else ""),
        body
    )
    info_log(f"Assessment invitation sent: exam_id={exam_id}, job_id={job_id}, to={to_email}")


def _send(service, to_email, subject, body):
    msg = MIMEText(body)

    # FIX: Capitalize 'To' and 'Subject' here as well!
    msg["To"] = to_email
    msg["Subject"] = subject
    
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    with gmail_call(f"Gmail send to {to_email} (subject: {subject})", level="INFO"):
        sent = service.users().messages().send(
            userId="me",
            body={"raw": raw}
        ).execute()
    info_log(f"Email sent: to={to_email}, subject='{subject}', message_id={(sent or {}).get('id')}, "
             f"thread_id={(sent or {}).get('threadId')}, body {len(body)} chars")
    
    
def send_alternative_jobs_email(service, to_email, matched_jobs):
    debug_log(f"Preparing alternative-jobs email to {to_email} - {len(matched_jobs)} jobs: "
              f"{[(job['title'], job.get('score', 'N/A')) for job in matched_jobs]}")

    # matched_jobs = [{"title": "...", "score": 0.52}, ...]
    job_lines = ""
    for idx, job in enumerate(matched_jobs, start=1):
        job_lines += f"- {job['title']} \n"
    
    body = (
        "Dear Candidate,\n\n"
        "Thank you for your application.\n"
        "While the applied role was not a strong match,\n"
        "your profile matches the following roles:\n\n"
        f"{job_lines}\n"
        "If you are interested, please reply to this email.\n\n"
        "Best Regards\nRecruitment Team"
    )
    _send(service, to_email, "Other Opportunities Available", body)
    
    
def send_rejection_email(service, to_email, matched_jobs=None):
    debug_log(f"Preparing rejection email to {to_email} - suggested jobs: "
              f"{[job['id'] for job in matched_jobs] if matched_jobs else 'none'}")

    suggestion_text = ""
    if matched_jobs:
        job_lines = ""
        for idx, job in enumerate(matched_jobs, start=1):
            job_lines += f"{job['id']} - {job['title']}\n"
        suggestion_text = (
            "\nHowever, your profile matches the following roles:\n\n"
            f"{job_lines}\n"
            "If you are interested, please reply to this email.\n"
        )

    body = (
        "Dear Candidate,\n\n"
        "Thank you for your application.\n"
        "After reviewing your profile, we will not be moving forward with your application this time "
        "with the role you applied for.\n"
        f"{suggestion_text}\n"
        "We appreciate your interest in our company.\n\n"
        "Best Regards,\nRecruitment Team"
    )
    _send(service, to_email, "Application Status Update", body)


def send_invalid_job_id_email(service, to_email, subject, job_id):
    debug_log(f"Preparing invalid-Job-ID email to {to_email} - job_id={job_id}, subject: {subject}")

    safe_subject = subject if subject else "Job Application"

    body = (
        "Dear Candidate,\n\n"
        f"The Job ID '{job_id}' mentioned in your application "
        "does not exist in our system.\n\n"
        "Please verify the Job ID and reapply with a valid one.\n\n"
        "Best Regards,\nRecruitment Team"
    )
    _send(service, to_email, "Re: " + safe_subject, body)
    
def send_job_closed_email(service, to_email, job_id, matched_jobs=None):
    """The candidate applied for a job that an admin has made inactive."""
    debug_log(f"Preparing job-closed email to {to_email} - job_id={job_id}, suggested jobs: "
              f"{[job['id'] for job in matched_jobs] if matched_jobs else 'none'}")

    suggestion_text = ""
    if matched_jobs:
        job_lines = "".join(f"• {job['id']} - {job['title']}\n" for job in matched_jobs)
        suggestion_text = (
            "Based on your profile, you may be a good match for these open roles:\n\n"
            f"{job_lines}\n"
            "To apply, send a NEW email with your CV attached and the Job ID in the subject line.\n\n"
        )

    body = (
        "Dear Candidate,\n\n"
        f"Thank you for your interest. The position {job_id} is no longer accepting applications.\n\n"
        f"{suggestion_text}"
        "We will keep your resume on file for future opportunities.\n\n"
        "Best Regards,\nRecruitment Team"
    )
    _send(service, to_email, f"{job_id} - Position Closed", body)


def send_multiple_cv_error_email(service, to_email):
    debug_log(f"Preparing multiple-CV error email to {to_email}")
    
    body = (
        "Dear Sender,\n\n"
        "We received your email, but we detected multiple PDF attachments.\n\n"
        "To ensure each candidate is processed and evaluated accurately by our AI system, "
        "we require a strict 'One CV per Email' format.\n\n"
        "Please resend these applications as separate emails. Make sure to include the "
        "Job ID in the subject line or body of each email.\n\n"
        "Best Regards,\nRecruitment Team"
    )
    
    _send(service, to_email, "Action Required: Multiple CVs Detected", body)


def send_missing_job_id_matches_email(service, to_email, matched_jobs):
    debug_log(f"Preparing missing-Job-ID email to {to_email} - matched jobs: "
              f"{[job['id'] for job in matched_jobs] if matched_jobs else 'none'}")
    
    if not matched_jobs:
        body = (
            "Dear Candidate,\n\n"
            "We received your resume, but you did not mention a Job ID in your application.\n"
            "After reviewing your profile against our current openings, we do not have a matching role at this time.\n\n"
            "We will keep your resume on file for future opportunities.\n\n"
            "Best Regards,\nRecruitment Team"
        )
        _send(service, to_email, "Action Required: Your Job Application", body)
        return

    job_lines = ""
    for job in matched_jobs:
        job_lines += f"• {job['id']} - {job['title']}\n"
    
    body = (
        "Dear Candidate,\n\n"
        "We received your resume, but you did not mention a specific Job ID in the subject line or body of your email.\n\n"
        "Based on an AI analysis of your profile, you are a strong match for the following open roles:\n\n"
        f"{job_lines}\n"
        "To apply for one of these roles, please DO NOT reply to this email. Instead:\n"
        "1. Compose a NEW email to us.\n"
        "2. Make sure your CV is attached.\n"
        "3. Type ONLY the Job ID (e.g., ABAP001) in the Subject line or Body of the email.\n\n"
        "Once we receive your new email, our system will automatically process your application.\n\n"
        "Best Regards,\nRecruitment Team"
    )
    
    _send(service, to_email, "Action Required: Missing Job ID in Application", body)
    

def send_reply_in_thread(service, to_email, subject, body, thread_id=None, in_reply_to=None):
    """Send an email as a reply in an existing Gmail thread (so the candidate replies there too)."""
    msg = MIMEText(body)
    msg["To"] = to_email
    msg["Subject"] = subject if subject.lower().startswith("re:") else "Re: " + subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    message = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    with gmail_call(f"Gmail send reply to {to_email} in thread {thread_id}", level="INFO"):
        sent = service.users().messages().send(userId="me", body=message).execute()
    info_log(f"Email sent: to={to_email}, subject='{msg['Subject']}', requested thread_id={thread_id}, "
             f"message_id={(sent or {}).get('id')}, thread_id={(sent or {}).get('threadId')}, "
             f"in_reply_to={'yes' if in_reply_to else 'no'}, body {len(body)} chars")
    return sent


def send_missing_details_email(service, to_email, name, job_title, job_id, missing_labels, provided,
                               subject, thread_id=None, in_reply_to=None, reminder=False):
    """Ask the candidate (in the same thread) for the details that are still missing."""
    debug_log(f"Preparing {'reminder' if reminder else 'first'} missing-details email to {to_email} - "
              f"job_id={job_id}, missing: {list(missing_labels)}, thread: {thread_id}")
    lines = "\n".join(f"{label}: " for label in missing_labels)
    have = "\n".join(f"  - {label}: {value}" for label, value in provided if value)
    if reminder:
        intro = "Thank you for your reply. A few details are still missing.\n\n"
    else:
        intro = (f"Thank you for applying for the {job_title} role"
                 + (f" (Job ID: {job_id})" if job_id else "") + ".\n\n"
                 "To proceed with your application, we need a few more details.\n\n")
    body = (
        f"Dear {name or 'Candidate'},\n\n"
        + intro
        + "Please REPLY to this email and fill in the following:\n\n"
        + lines + "\n\n"
        + 'If something does not apply to you (for example, you are a fresher), write "NA".\n'
        + (f"\nDetails we already have:\n{have}\n" if have else "")
        + "\nBest Regards,\nRecruitment Team"
    )
    return send_reply_in_thread(service, to_email, subject or f"Application for {job_title}", body,
                                thread_id, in_reply_to)
