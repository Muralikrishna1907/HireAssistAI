from datetime import datetime
from matching.text_cleaner import clean_text
from sentence_transformers import util
from sentence_transformers import SentenceTransformer
import pickle
import torch
from datetime import datetime
import json
from utils.logger import debug_log, info_log, warning_log, error_log, log_duration, short, mask_email

with log_duration("Load SentenceTransformer model all-MiniLM-L6-v2"):
    model = SentenceTransformer("all-MiniLM-L6-v2")

def log_event(conn, cv_id, column_name):
    try:
        cursor = conn.cursor()

        cursor.execute(f"""
            INSERT INTO CV_TIMESTAMPS (ID, {column_name})
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE {column_name} = %s
        """, (cv_id, datetime.now(), datetime.now()))

        conn.commit()

        debug_log(f"CV timestamp {column_name} recorded for cv_id={cv_id}")

    except Exception as e:
        error_log(f"Failed to record CV timestamp {column_name} for cv_id={cv_id}: {e}")
        
def log_mail_received(conn, cv_id, received_time):
    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO CV_TIMESTAMPS (ID, mail_received)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE mail_received = %s
        """, (cv_id, received_time, received_time))

        conn.commit()

        debug_log(f"CV timestamp mail_received recorded for cv_id={cv_id} ({received_time})")

    except Exception as e:
        error_log(f"Failed to record mail_received timestamp for cv_id={cv_id}: {e}")


def insert_cv(conn, cv_id, name, email, pdf, job_id, skills, experience, education, mail_body):
    debug_log(f"Inserting CV cv_id={cv_id}, name={name}, email={mask_email(email)}, job_id={job_id}, "
              f"attachment={len(pdf) if pdf else 0} bytes, skills={short(skills, 80)}")

    cursor = conn.cursor()

    # 🔥 UPDATED QUERY (added SKILLS, EXPERIENCE, EDUCATION)
    cursor.execute("""
        INSERT INTO CV_INFO 
        (ID, NAME, EMAIL, ATTACHMENT, STATUS, JOB_ID, SKILLS, EXPERIENCE, EDUCATION, MAIL_BODY)
        VALUES (%s, %s, %s, %s, 'NEW', %s, %s, %s, %s, %s)
    """, (
        cv_id,
        name,
        email,
        pdf,                     # ✅ FIXED (was pdf_bytes ❌)
        job_id,
        skills,
        experience,
        education,
        json.dumps(mail_body)    # ✅ THIS IS THE KEY FIX
    ))
    cursor.execute("""
            INSERT INTO CV_TIMESTAMPS (ID)
            VALUES (%s)
        """, (cv_id,))
   

    conn.commit()

    log_event(conn, cv_id, "question_generated")

    info_log(f"CV inserted: cv_id={cv_id}, job_id={job_id}, status=NEW")

def update_cv_status(conn, cv_id, score, title, status):
    debug_log(f"Updating CV status cv_id={cv_id} -> status={status}, score={score}, title={title}")

    cursor = conn.cursor()

    cursor.execute("""
        UPDATE CV_INFO
        SET MATCH_SCORE = %s, JOBTITLE=%s, STATUS = %s
        WHERE ID = %s
    """, (score, title, status, cv_id))

    info_log(f"CV status updated (caller commits): cv_id={cv_id}, status={status}, score={score}, "
             f"title={title}, rows affected={cursor.rowcount}")


def get_job_description(conn, job_id):
    debug_log(f"Fetching job description for job_id={job_id}")

    cursor = conn.cursor()

    cursor.execute("""
        SELECT JOB_DESCRIPTION
        FROM JOB_INFO
        WHERE ID = %s
    """, (job_id,))

    row = cursor.fetchone()

    if not row:
        error_log(f"Job description not found for job_id={job_id}")
        raise ValueError("JD not found")

    debug_log(f"Job description found for job_id={job_id} - {len(row[0])} characters")
    return row[0]


def save_questions(conn, user_id, questions):
    debug_log(f"Saving {len(questions)} questions for cv_id={user_id}")

    cursor = conn.cursor()

    ts = datetime.now().strftime("%Y%m%d%H%M%S")

    for idx, q in enumerate(questions, start=1):
        ques_id = f"Q_{ts}_{idx:02d}"
        debug_log(f"Question {idx}/{len(questions)} ques_id={ques_id}, language={q.get('language', 'N/A')}")

        cursor.execute("""
            INSERT INTO QUESTIONS_TABLE (
                ID,
                QUES_ID,
                QUESTION,
                PROG_LANG
            )
            VALUES (%s, %s, %s, %s)
        """, (
            user_id,
            ques_id,
            q["question"],
            q["language"]
        ))

    conn.commit()
    log_event(conn, user_id, "question_generated")
    info_log(f"Questions saved: cv_id={user_id}, count={len(questions)}, batch={ts}")


def is_email_processed(conn, msg_id):
    cursor = conn.cursor()

    cursor.execute(
        "SELECT 1 FROM PROCESSED_EMAILS WHERE MSG_ID = %s",
        (msg_id,)
    )

    result = cursor.fetchone() is not None
    debug_log(f"Email msg_id={msg_id} already processed: {result}")

    return result


def mark_email_processed(conn, msg_id):
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO PROCESSED_EMAILS (MSG_ID) VALUES (%s)",
        (msg_id,)
    )

    conn.commit()
    info_log(f"Email marked processed: msg_id={msg_id}")


def add_job_description(conn, ID, job_title, job_description):
    debug_log(f"Adding job description job_id={ID}, title={job_title}, {len(job_description)} characters")

    cursor = conn.cursor()

    cleaned_jd = clean_text(job_description)
    debug_log(f"Job description cleaned - {len(job_description)} -> {len(cleaned_jd)} characters")

    with log_duration(f"JD embedding for job_id={ID}", level="DEBUG"):
        embedding = model.encode(cleaned_jd)

    embedding_blob = pickle.dumps(embedding)
    debug_log(f"JD embedding shape={embedding.shape}, serialized {len(embedding_blob)} bytes")

    cursor.execute("""
        INSERT INTO JOB_INFO (ID, JOB_TITLE, JOB_DESCRIPTION, JD_EMBEDDING)
        VALUES (%s, %s, %s, %s)
    """, (ID, job_title, job_description, embedding_blob))

    conn.commit()
    info_log(f"Job description added: job_id={ID}, title={job_title}")

    cursor.close()


# ---------------------------------------------------------------------------
# Proctoring evidence: one small JPEG per violation, stored in EXAM_VIOLATIONS.SNAPSHOT
# ---------------------------------------------------------------------------
_VIOLATION_SNAPSHOT_READY = False
SNAPSHOT_MAX_SIZE = (480, 360)     # pixels - enough to see who is in front of the camera
SNAPSHOT_QUALITY = 55              # JPEG quality -> typically 10-25 KB per image


def ensure_violation_snapshot_column(cursor):
    """Adds EXAM_VIOLATIONS.SNAPSHOT (MEDIUMBLOB) if it is missing."""
    global _VIOLATION_SNAPSHOT_READY
    if _VIOLATION_SNAPSHOT_READY:
        return
    cursor.execute("SHOW COLUMNS FROM EXAM_VIOLATIONS LIKE 'SNAPSHOT'")
    if not cursor.fetchall():
        cursor.execute("ALTER TABLE EXAM_VIOLATIONS ADD COLUMN SNAPSHOT MEDIUMBLOB NULL")
        info_log("Added column EXAM_VIOLATIONS.SNAPSHOT (MEDIUMBLOB)")
    # violation times keep milliseconds (TIMESTAMP(3)); older rows keep their whole-second value
    cursor.execute("SHOW COLUMNS FROM EXAM_VIOLATIONS LIKE 'EVENT_TIME'")
    col = cursor.fetchone()
    if col and str(col[1]).lower() == "timestamp":
        cursor.execute("ALTER TABLE EXAM_VIOLATIONS MODIFY EVENT_TIME TIMESTAMP(3) NULL DEFAULT NULL")
        info_log("Changed EXAM_VIOLATIONS.EVENT_TIME to TIMESTAMP(3) to keep milliseconds")
    _VIOLATION_SNAPSHOT_READY = True


def compress_snapshot(image_bytes):
    """Re-encodes a camera frame as a small JPEG (max 480x360, quality 55). Returns bytes or None."""
    if not image_bytes:
        return None
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(image_bytes))
        img = img.convert("RGB")
        img.thumbnail(SNAPSHOT_MAX_SIZE)
        out = BytesIO()
        img.save(out, format="JPEG", quality=SNAPSHOT_QUALITY, optimize=True)
        small = out.getvalue()
        debug_log(f"Violation snapshot compressed {len(image_bytes)} -> {len(small)} bytes ({img.width}x{img.height})")
        return small
    except Exception as e:
        warning_log(f"Violation snapshot could not be compressed ({e}); storing it as received")
        return image_bytes


def is_job_active(conn, job_id):
    """False only when the job exists and an admin made it Inactive on the Jobs page.
    (Unknown jobs, or a database without the IS_ACTIVE column yet, count as active.)"""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT IS_ACTIVE FROM JOB_INFO WHERE ID = %s", (job_id,))
        row = cursor.fetchone()
    except Exception as e:
        debug_log(f"Job status check skipped for job_id={job_id} (IS_ACTIVE not available: {e})")
        return True
    finally:
        cursor.close()
    active = not row or row[0] is None or bool(row[0])
    debug_log(f"Job {job_id} active: {active}")
    return active


def get_all_job_embeddings(conn):
    """Active jobs only - these are the jobs suggested to candidates."""
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT ID, JOB_TITLE, JD_EMBEDDING, JOB_DESCRIPTION
            FROM JOB_INFO
            WHERE JD_EMBEDDING IS NOT NULL AND COALESCE(IS_ACTIVE, 1) = 1
        """)
    except Exception as e:
        # database without the IS_ACTIVE column yet (added when the Jobs page is first opened)
        debug_log(f"IS_ACTIVE not available ({e}); loading all jobs")
        cursor.execute("""
            SELECT ID, JOB_TITLE, JD_EMBEDDING, JOB_DESCRIPTION
            FROM JOB_INFO
            WHERE JD_EMBEDDING IS NOT NULL
        """)

    rows = cursor.fetchall()
    debug_log(f"Fetched {len(rows)} jobs with embeddings")

    cursor.close()

    jobs = []

    for job_id, title, blob, job_description in rows:
        embedding = pickle.loads(blob)

        if not torch.is_tensor(embedding):
            embedding = torch.tensor(embedding)

        jobs.append({
            "id": job_id,
            "title": title,
            "embedding": embedding,
            "description": job_description
        })

    debug_log(f"Loaded {len(jobs)} job embeddings: {[j['id'] for j in jobs]}")
    return jobs


def get_job_by_id(conn, job_id):
    cursor = conn.cursor()

    cursor.execute("""
        SELECT ID, JOB_TITLE, JD_EMBEDDING, JOB_DESCRIPTION
        FROM JOB_INFO
        WHERE ID = %s
        AND JD_EMBEDDING IS NOT NULL
    """, (job_id,))

    row = cursor.fetchone()

    cursor.close()

    if not row:
        warning_log(f"Job not found (or has no embedding) for job_id={job_id}")
        return None

    job_id, title, blob, description = row

    embedding = pickle.loads(blob)

    if not torch.is_tensor(embedding):
        embedding = torch.tensor(embedding)

    debug_log(f"Job loaded: job_id={job_id}, title={title}, embedding shape={tuple(embedding.shape)}")

    return {
        "job_id": job_id,
        "title": title,
        "embedding": embedding,
        "description": description
    }


def generate_missing_job_embeddings(model, conn):
    cursor = conn.cursor()

    cursor.execute("""
        SELECT ID, JOB_DESCRIPTION
        FROM JOB_INFO
        WHERE JD_EMBEDDING IS NULL
    """)

    rows = cursor.fetchall()

    if not rows:
        debug_log("All jobs already have embeddings - nothing to generate")
        cursor.close()
        return

    info_log(f"Generating missing JD embeddings for {len(rows)} jobs")
    skipped = 0

    for job_id, job_description in rows:
        if job_description:
            embedding = model.encode(job_description, convert_to_tensor=True)

            embedding_bytes = pickle.dumps(embedding.cpu().tolist())

            cursor.execute("""
                UPDATE JOB_INFO
                SET JD_EMBEDDING = %s
                WHERE ID = %s
            """, (embedding_bytes, job_id))

            debug_log(f"JD embedding generated for job_id={job_id} ({len(embedding_bytes)} bytes)")
        else:
            skipped += 1
            warning_log(f"Job job_id={job_id} has no description - embedding not generated")

    conn.commit()
    info_log(f"Missing JD embeddings committed: {len(rows) - skipped} generated, {skipped} skipped (empty description)")

    cursor.close()

def has_applied_by_name(conn, candidate_name, job_id):
    """Checks if a candidate with the exact same name has already applied for this job."""
    if not candidate_name or candidate_name.lower() == "candidate":
        return False
        
    cursor = conn.cursor()
    # Using LOWER() to ensure case-insensitive matching
    cursor.execute("""
        SELECT COUNT(*) FROM CV_INFO 
        WHERE LOWER(NAME) = LOWER(%s) AND JOB_ID = %s
    """, (candidate_name.strip(), job_id))
    
    count = cursor.fetchone()[0]
    cursor.close()
    debug_log(f"Duplicate-application check: name={candidate_name.strip()}, job_id={job_id} -> {count} existing")
    
    return count > 0


# ---------------------------------------------------------------------------
# Candidate details requested by email (replaces the Google Form)
#   One row per shortlisted candidate whose details were checked:
#   STATUS PENDING   -> waiting for the candidate to reply with missing details
#          COMPLETE  -> all details received, candidate evaluated
#          INCOMPLETE-> still missing after the maximum number of requests
# ---------------------------------------------------------------------------
_DETAIL_TABLE_READY = False


def ensure_detail_request_table(conn):
    global _DETAIL_TABLE_READY
    if _DETAIL_TABLE_READY:
        return
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS CANDIDATE_DETAIL_REQUESTS (
            CV_ID VARCHAR(100) NOT NULL PRIMARY KEY,
            THREAD_ID VARCHAR(64),
            LAST_MESSAGE_ID_HEADER VARCHAR(512),
            SUBJECT VARCHAR(500),
            DETAILS LONGTEXT,
            MISSING_FIELDS TEXT,
            REQUESTS_SENT INT NOT NULL DEFAULT 0,
            STATUS VARCHAR(20) NOT NULL,
            LAST_CHECKED_MS BIGINT NOT NULL DEFAULT 0,
            CREATED_AT DATETIME NOT NULL,
            UPDATED_AT DATETIME NOT NULL,
            KEY IX_DETAIL_THREAD (THREAD_ID)
        )
    """)
    conn.commit()
    cursor.close()
    _DETAIL_TABLE_READY = True
    debug_log("CANDIDATE_DETAIL_REQUESTS table ensured")


def save_detail_request(conn, cv_id, thread_id, message_id_header, subject, details, missing,
                        requests_sent, status, last_checked_ms):
    ensure_detail_request_table(conn)
    cursor = conn.cursor()
    now = datetime.now()
    cursor.execute("""
        INSERT INTO CANDIDATE_DETAIL_REQUESTS
            (CV_ID, THREAD_ID, LAST_MESSAGE_ID_HEADER, SUBJECT, DETAILS, MISSING_FIELDS,
             REQUESTS_SENT, STATUS, LAST_CHECKED_MS, CREATED_AT, UPDATED_AT)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            THREAD_ID = VALUES(THREAD_ID), LAST_MESSAGE_ID_HEADER = VALUES(LAST_MESSAGE_ID_HEADER),
            SUBJECT = VALUES(SUBJECT), DETAILS = VALUES(DETAILS), MISSING_FIELDS = VALUES(MISSING_FIELDS),
            REQUESTS_SENT = VALUES(REQUESTS_SENT), STATUS = VALUES(STATUS),
            LAST_CHECKED_MS = VALUES(LAST_CHECKED_MS), UPDATED_AT = VALUES(UPDATED_AT)
    """, (cv_id, thread_id, (message_id_header or "")[:512], (subject or "")[:500], json.dumps(details),
          ",".join(missing), requests_sent, status, int(last_checked_ms or 0), now, now))
    conn.commit()
    cursor.close()
    info_log(f"Detail request saved: cv_id={cv_id}, status={status}, requests_sent={requests_sent}, "
             f"thread_id={thread_id}, missing={','.join(missing) or 'none'}")


def application_thread_status(conn, thread_id, msg_id):
    """For a message that is a reply inside an existing application's email thread:
    'PENDING' if that application is waiting for candidate details, otherwise 'KNOWN'.
    None when the message starts a new conversation (a possible new application).
    """
    if not thread_id or thread_id == msg_id:
        return None
    ensure_detail_request_table(conn)
    cursor = conn.cursor()
    cursor.execute("SELECT STATUS FROM CANDIDATE_DETAIL_REQUESTS WHERE THREAD_ID = %s", (thread_id,))
    row = cursor.fetchone()
    if row:
        cursor.close()
        debug_log(f"Thread {thread_id} (msg {msg_id}) belongs to a detail request with status {row[0]}")
        return "PENDING" if row[0] == "PENDING" else "KNOWN"
    # Gmail's thread id is the id of the thread's first message, i.e. the original CV email
    cursor.execute("SELECT 1 FROM CV_INFO WHERE ID = %s", (thread_id,))
    known = cursor.fetchone() is not None
    cursor.close()
    debug_log(f"Thread {thread_id} (msg {msg_id}) matches an existing CV: {known}")
    return "KNOWN" if known else None


def get_pending_detail_requests(conn):
    ensure_detail_request_table(conn)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT r.CV_ID, r.THREAD_ID, r.LAST_MESSAGE_ID_HEADER, r.SUBJECT, r.DETAILS, r.REQUESTS_SENT,
               r.LAST_CHECKED_MS, cv.EMAIL, cv.NAME, cv.JOBTITLE, cv.JOB_ID
        FROM CANDIDATE_DETAIL_REQUESTS r
        JOIN CV_INFO cv ON cv.ID = r.CV_ID
        WHERE r.STATUS = 'PENDING' AND r.THREAD_ID IS NOT NULL
    """)
    rows = cursor.fetchall()
    cursor.close()
    result = []
    for r in rows:
        try:
            details = json.loads(r[4]) if r[4] else {}
        except (TypeError, ValueError) as e:
            warning_log(f"Unreadable DETAILS JSON for detail request cv_id={r[0]}; using empty details: {e}")
            details = {}
        result.append({
            "cv_id": r[0], "thread_id": r[1], "message_id_header": r[2], "subject": r[3],
            "details": details, "requests_sent": r[5] or 0, "last_checked_ms": r[6] or 0,
            "email": r[7], "name": r[8], "job_title": r[9], "job_id": r[10],
        })
    debug_log(f"Pending detail requests: {len(result)}")
    return result
