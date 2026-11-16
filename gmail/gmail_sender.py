import base64
from email.mime.text import MIMEText
import inspect
# from app import generate_exam_link
from db.connection import get_connection
from utils.logger import debug_log, info_log, error_log

def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

def send_missing_cv_email(service, to_email, subject):
    debug_log(f"Starting send_missing_cv_email - To: {to_email}, Subject: {subject}")
    
    safe_subject = subject if subject else "Job Application"
    debug_log(f"Safe subject generated: {safe_subject}")
    
    body = (
        "Dear Candidate,\n\n"
        "We have recieved your Application. However, your application did not include a CV.\n"
        "Please reply to this mail with your CV attached.\n\n"
        "Best Regards,\nRecruitment Team"
    )
    debug_log(f"Email body prepared - Length: {len(body)} characters")
    
    _send(service, to_email, "Re: " + safe_subject, body)
    debug_log(f"Missing CV email sent successfully to {to_email}")

def send_interview_email(service, to_email, job_title, exam_id, job_id):
    info_log(f"Starting send_interview_email - To: {to_email}")
    # ADD THE IMPORT HERE:
    from app import generate_exam_link

    debug_log(f"Starting send_interview_email - To: {to_email}, Job Title: {job_title}, Exam ID: {exam_id}, Job ID: {job_id}")
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT INTERVIEW_TIME FROM JOB_INFO WHERE ID = %s", (job_id,))
    result = cursor.fetchone()

    interview_time = result[0] if result else 30

    cursor.close()
    conn.close()

    # exam_link = "https://192.168.1.10:8000/"  # Replace with production URL
    exam_link = generate_exam_link(exam_id)
    support_email = "support@sbp.com"
    debug_log(f"Email configuration - Exam Link: {exam_link}, Support Email: {support_email}")

    body = (
        "Dear Candidate,\n\n"

        f"You have been shortlisted for the role of {job_title}.\n\n"

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
    debug_log(f"Interview email body prepared - Length: {len(body)} characters")

    _send(
        service,
        to_email,
        f"Online Assessment Invitation – {job_title}",
        body
    )
    debug_log(f"Interview email sent successfully to {to_email} for job: {job_title}")


def _send(service, to_email, subject, body):
    debug_log(f"Starting _send - To: {to_email}, Subject: {subject[:40]}...")
    
    msg = MIMEText(body)
    debug_log(f"MIME message created - Content Type: {msg.get_content_type()}")
    
    # FIX: Capitalize 'To' and 'Subject' here as well!
    msg["To"] = to_email
    msg["Subject"] = subject
    
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    debug_log(f"Email encoded - Encoded size: {len(raw)} characters")
    
    service.users().messages().send(
        userId="me",
        body={"raw": raw}
    ).execute()
    debug_log(f"Email sent successfully via Gmail API to {to_email}")
    
    
def send_alternative_jobs_email(service, to_email, matched_jobs):
    debug_log(f"Starting send_alternative_jobs_email - To: {to_email}, Total Jobs: {len(matched_jobs)}")
    
    # matched_jobs = [{"title": "...", "score": 0.52}, ...]
    job_lines = ""
    for idx, job in enumerate(matched_jobs, start=1):
        job_lines += f"- {job['title']} \n"
        debug_log(f"Job {idx} added - Title: {job['title']}, Score: {job.get('score', 'N/A')}")
    
    body = (
        "Dear Candidate,\n\n"
        "Thank you for your application.\n"
        "While the applied role was not a strong match,\n"
        "your profile matches the following roles:\n\n"
        f"{job_lines}\n"
        "If you are interested, please reply to this email.\n\n"
        "Best Regards\nRecruitment Team"
    )
    debug_log(f"Alternative jobs email body prepared - Length: {len(body)} characters")
    
    _send(service, to_email, "Other Opportunities Available", body)
    debug_log(f"Alternative jobs email sent successfully to {to_email}")
    
    
def send_rejection_email(service, to_email, matched_jobs=None):
    debug_log(f"Starting send_rejection_email - To: {to_email}, Matched Jobs: {len(matched_jobs) if matched_jobs else 0}")
    
    suggestion_text = ""
    if matched_jobs:
        debug_log(f"Generating job suggestions - Count: {len(matched_jobs)}")
        job_lines = ""
        for idx, job in enumerate(matched_jobs, start=1):
            job_lines += f"{job['id']} - {job['title']}\n"
            debug_log(f"Job {idx} suggestion added - ID: {job['id']}, Title: {job['title']}")
        suggestion_text = (
            "\nHowever, your profile matches the following roles:\n\n"
            f"{job_lines}\n"
            "If you are interested, please reply to this email.\n"
        )
        debug_log(f"Suggestion text generated - Length: {len(suggestion_text)} characters")
    else:
        debug_log("No matched jobs provided - sending standard rejection email")
    
    body = (
        "Dear Candidate,\n\n"
        "Thank you for your application.\n"
        "After reviewing your profile, we will not be moving forward with your application this time "
        "with the role you applied for.\n"
        f"{suggestion_text}\n"
        "We appreciate your interest in our company.\n\n"
        "Best Regards,\nRecruitment Team"
    )
    debug_log(f"Rejection email body prepared - Length: {len(body)} characters")
    
    _send(service, to_email, "Application Status Update", body)
    debug_log(f"Rejection email sent successfully to {to_email}")


def send_invalid_job_id_email(service, to_email, subject, job_id):
    debug_log(f"Starting send_invalid_job_id_email - To: {to_email}, Job ID: {job_id}, Subject: {subject}")
    
    safe_subject = subject if subject else "Job Application"
    debug_log(f"Safe subject generated: {safe_subject}")

    body = (
        "Dear Candidate,\n\n"
        f"The Job ID '{job_id}' mentioned in your application "
        "does not exist in our system.\n\n"
        "Please verify the Job ID and reapply with a valid one.\n\n"
        "Best Regards,\nRecruitment Team"
    )
    debug_log(f"Invalid Job ID email body prepared - Length: {len(body)} characters")

    _send(service, to_email, "Re: " + safe_subject, body)
    debug_log(f"Invalid Job ID email sent successfully to {to_email}")
    
def send_multiple_cv_error_email(service, to_email):
    debug_log(f"Starting send_multiple_cv_error_email - To: {to_email}")
    
    body = (
        "Dear Sender,\n\n"
        "We received your email, but we detected multiple PDF attachments.\n\n"
        "To ensure each candidate is processed and evaluated accurately by our AI system, "
        "we require a strict 'One CV per Email' format.\n\n"
        "Please resend these applications as separate emails. Make sure to include the "
        "Job ID in the subject line or body of each email.\n\n"
        "Best Regards,\nRecruitment Team\nSBP Consultancy Private Limited"
    )
    
    _send(service, to_email, "Action Required: Multiple CVs Detected", body)
    debug_log(f"Multiple CV error email sent to {to_email}")


def send_missing_job_id_matches_email(service, to_email, matched_jobs):
    debug_log(f"Starting send_missing_job_id_matches_email - To: {to_email}, Matches: {len(matched_jobs)}")
    
    if not matched_jobs:
        body = (
            "Dear Candidate,\n\n"
            "We received your resume, but you did not mention a Job ID in your application.\n"
            "After reviewing your profile against our current openings, we do not have a matching role at this time.\n\n"
            "We will keep your resume on file for future opportunities.\n\n"
            "Best Regards,\nRecruitment Team\nSBP Consultancy Private Limited"
        )
        _send(service, to_email, "Action Required: SBP Consultancy Application", body)
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
        "Best Regards,\nRecruitment Team\nSBP Consultancy Private Limited"
    )
    
    _send(service, to_email, "Action Required: Missing Job ID in Application", body)
    debug_log(f"Missing Job ID matches email sent to {to_email}")
    

def send_additional_details_email(service, to_email, job_title, form_link, cv_id):
    debug_log(f"Starting send_additional_details_email - To: {to_email}")
    
    body = (
        f"Dear Candidate,\n\n"
        f"Thank you for applying for the {job_title} position.\n\n"
        "To proceed further with your application, we require some additional details.\n\n"
        f"Please fill out the following form:\n{form_link}\n\n"
        "IMPORTANT: Your application requires a unique ID to process correctly. "
        "The form link above should automatically pre-fill this for you. If it does not, "
        "please copy and paste your exact Application ID below:\n\n"
        "=========================================\n"
        f"   YOUR APPLICATION ID: {cv_id}\n"
        "=========================================\n\n"
        "Best Regards,\nRecruitment Team\nSBP Consultancy Private Limited"
    )
    
    _send(service, to_email, f"Action Required: Additional Details for {job_title}", body)
    debug_log(f"Additional details email sent to {to_email}")