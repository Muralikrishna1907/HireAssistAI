from datetime import datetime
from matching.text_cleaner import clean_text
from sentence_transformers import util
from sentence_transformers import SentenceTransformer
import pickle
import torch
import inspect
from datetime import datetime
import json

def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

model = SentenceTransformer("all-MiniLM-L6-v2")
debug_log("SentenceTransformer model loaded successfully")

def log_event(conn, cv_id, column_name):
    try:
        cursor = conn.cursor()

        cursor.execute(f"""
            INSERT INTO CV_TIMESTAMPS (ID, {column_name})
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE {column_name} = %s
        """, (cv_id, datetime.now(), datetime.now()))

        conn.commit()

        print(f"✅ {column_name} updated: {cv_id}")

    except Exception as e:
        print(f"[WARNING] Failed to update {column_name} for {cv_id}: {str(e)}")
        
def log_mail_received(conn, cv_id, received_time):
    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO CV_TIMESTAMPS (ID, mail_received)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE mail_received = %s
        """, (cv_id, received_time, received_time))

        conn.commit()

        print(f"✅ mail_received inserted/updated: {cv_id}")

    except Exception as e:
        print(f"[ERROR] mail_received failed: {str(e)}")


def insert_cv(conn, cv_id, name, email, pdf, job_id, skills, experience, education, mail_body):
    debug_log(f"Starting insert_cv - CV_ID: {cv_id}, Name: {name}, Email: {email}, Job_ID: {job_id}")

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

    debug_log(f"CV inserted successfully for CV_ID: {cv_id} with JOB_ID: {job_id}")

def update_cv_status(conn, cv_id, score, title, status):
    debug_log(f"Starting update_cv_status - CV_ID: {cv_id}, Score: {score}, Title: {title}, Status: {status}")

    cursor = conn.cursor()

    cursor.execute("""
        UPDATE CV_INFO
        SET MATCH_SCORE = %s, JOBTITLE=%s, STATUS = %s
        WHERE ID = %s
    """, (score, title, status, cv_id))

    debug_log(f"CV status updated successfully for CV_ID: {cv_id}")


def get_job_description(conn, job_id):
    debug_log(f"Starting get_job_description - Job_ID: {job_id}")

    cursor = conn.cursor()

    cursor.execute("""
        SELECT JOB_DESCRIPTION
        FROM JOB_INFO
        WHERE ID = %s
    """, (job_id,))

    row = cursor.fetchone()
    debug_log(f"Job description fetch completed - Found: {row is not None}")

    if not row:
        debug_log(f"ERROR: JD not found for Job_ID: {job_id}")
        raise ValueError("JD not found")

    debug_log(f"Job description retrieved successfully - Length: {len(row[0])} characters")
    return row[0]


def save_questions(conn, user_id, questions):
    debug_log(f"Starting save_questions - User_ID: {user_id}, Total Questions: {len(questions)}")

    cursor = conn.cursor()

    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    debug_log(f"Timestamp generated: {ts}")

    for idx, q in enumerate(questions, start=1):
        ques_id = f"Q_{ts}_{idx:02d}"
        debug_log(f"Inserting question {idx}/{len(questions)} - QUES_ID: {ques_id}, Language: {q.get('language', 'N/A')}")

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
    debug_log(f"All {len(questions)} questions saved successfully for User_ID: {user_id}")


def is_email_processed(conn, msg_id):
    debug_log(f"Checking if email is processed - MSG_ID: {msg_id}")

    cursor = conn.cursor()

    cursor.execute(
        "SELECT 1 FROM PROCESSED_EMAILS WHERE MSG_ID = %s",
        (msg_id,)
    )

    result = cursor.fetchone() is not None
    debug_log(f"Email processed check completed - Processed: {result}")

    return result


def mark_email_processed(conn, msg_id):
    debug_log(f"Starting mark_email_processed - MSG_ID: {msg_id}")

    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO PROCESSED_EMAILS (MSG_ID) VALUES (%s)",
        (msg_id,)
    )

    conn.commit()
    debug_log(f"Email marked as processed successfully - MSG_ID: {msg_id}")


def add_job_description(conn, ID, job_title, job_description):
    debug_log(f"Starting add_job_description - Job_ID: {ID}, Title: {job_title}, Description Length: {len(job_description)} characters")

    cursor = conn.cursor()

    cleaned_jd = clean_text(job_description)
    debug_log(f"Job description cleaned - Original Length: {len(job_description)}, Cleaned Length: {len(cleaned_jd)}")

    embedding = model.encode(cleaned_jd)
    debug_log(f"Embedding generated - Shape: {embedding.shape}")

    embedding_blob = pickle.dumps(embedding)
    debug_log(f"Embedding serialized - Blob Size: {len(embedding_blob)} bytes")

    cursor.execute("""
        INSERT INTO JOB_INFO (ID, JOB_TITLE, JOB_DESCRIPTION, JD_EMBEDDING)
        VALUES (%s, %s, %s, %s)
    """, (ID, job_title, job_description, embedding_blob))

    conn.commit()
    debug_log(f"Job description inserted successfully - Job_ID: {ID}")

    cursor.close()


def get_all_job_embeddings(conn):
    debug_log("Starting get_all_job_embeddings")

    cursor = conn.cursor()

    cursor.execute("""
        SELECT ID, JOB_TITLE, JD_EMBEDDING, JOB_DESCRIPTION
        FROM JOB_INFO
        WHERE JD_EMBEDDING IS NOT NULL
    """)

    rows = cursor.fetchall()
    debug_log(f"Retrieved {len(rows)} job records from database")

    cursor.close()

    jobs = []

    for job_id, title, blob, job_description in rows:
        debug_log(f"Processing job - Job_ID: {job_id}, Title: {title}")

        embedding = pickle.loads(blob)

        if not torch.is_tensor(embedding):
            embedding = torch.tensor(embedding)
            debug_log(f"Converted embedding to tensor - Shape: {embedding.shape}")

        jobs.append({
            "id": job_id,
            "title": title,
            "embedding": embedding,
            "description": job_description
        })

    debug_log(f"All job embeddings processed successfully - Total Jobs: {len(jobs)}")
    return jobs


def get_job_by_id(conn, job_id):
    debug_log(f"Starting get_job_by_id - Job_ID: {job_id}")

    cursor = conn.cursor()

    cursor.execute("""
        SELECT ID, JOB_TITLE, JD_EMBEDDING, JOB_DESCRIPTION
        FROM JOB_INFO
        WHERE ID = %s
        AND JD_EMBEDDING IS NOT NULL
    """, (job_id,))

    row = cursor.fetchone()
    debug_log(f"Job query completed - Found: {row is not None}")

    cursor.close()

    if not row:
        debug_log(f"Job not found for Job_ID: {job_id}")
        return None

    job_id, title, blob, description = row

    debug_log(f"Deserializing embedding for Job_ID: {job_id}")

    embedding = pickle.loads(blob)

    if not torch.is_tensor(embedding):
        embedding = torch.tensor(embedding)
        debug_log(f"Converted embedding to tensor - Shape: {embedding.shape}")

    debug_log(f"Job retrieved successfully - Job_ID: {job_id}, Title: {title}")

    return {
        "job_id": job_id,
        "title": title,
        "embedding": embedding,
        "description": description
    }


def generate_missing_job_embeddings(model, conn):
    debug_log("Starting generate_missing_job_embeddings")

    cursor = conn.cursor()

    cursor.execute("""
        SELECT ID, JOB_DESCRIPTION
        FROM JOB_INFO
        WHERE JD_EMBEDDING IS NULL
    """)

    rows = cursor.fetchall()
    debug_log(f"Found {len(rows)} records with missing embeddings")

    if not rows:
        debug_log("All job records already have embeddings - No action needed")
        cursor.close()
        return

    debug_log(f"Generating embeddings for {len(rows)} records...")

    for job_id, job_description in rows:
        debug_log(f"Processing Job_ID: {job_id}")

        if job_description:
            debug_log(f"Encoding job description for Job_ID: {job_id}")

            embedding = model.encode(job_description, convert_to_tensor=True)

            debug_log(f"Embedding generated - Shape: {embedding.shape}")

            embedding_bytes = pickle.dumps(embedding.cpu().tolist())

            debug_log(f"Embedding serialized - Size: {len(embedding_bytes)} bytes")

            cursor.execute("""
                UPDATE JOB_INFO
                SET JD_EMBEDDING = %s
                WHERE ID = %s
            """, (embedding_bytes, job_id))

            debug_log(f"Embedding updated successfully for Job_ID: {job_id}")

    conn.commit()
    debug_log(f"All embeddings generated and committed successfully - Total Records: {len(rows)}")

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
    
    return count > 0


def mark_webhook_processed(conn, msg_id):
    """Records the webhook email in processed_emails with form_submit = YES"""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO processed_emails (MSG_ID, FORM_SUBMIT, FORM_TIMESTAMP) 
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            """,
            (msg_id, "YES")
        )
        conn.commit()
    except Exception as e:
        print(f"[ERROR] Failed to record webhook in processed_emails: {str(e)}")
    finally:
        cursor.close()