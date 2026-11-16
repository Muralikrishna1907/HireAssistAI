import socket
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from db.connection import get_connection
from db.cv_repository import log_event 
from llm.score_generator import score_with_llm,score_communication_with_llm
import os
from flask_cors import CORS
import inspect
import base64
from datetime import datetime, timedelta
import uuid
from itsdangerous import URLSafeSerializer, BadSignature
from gmail.gmail_service import get_gmail_service
from gmail.gmail_sender import send_rejection_email, _send

SESSION_TIMEOUT = 120  # minutes

app = Flask(__name__)
app.secret_key = "super_secret_key_123"
serializer = URLSafeSerializer(app.secret_key)
CORS(app)

# ===============================
# 🔷 UTILITIES / DEBUG
# ===============================
def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

def generate_exam_link(cv_id):
    expiry_time = (datetime.now() + timedelta(hours=48)).timestamp()

    token = serializer.dumps({
        "cv_id": cv_id,
        "exp": expiry_time
    }, salt="exam-link")

    # 🔥 STORE TOKEN IN DB
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE CV_INFO
        SET EXAM_TOKEN = %s,
            EXAM_LINK_USED = FALSE
        WHERE ID = %s
    """, (token, cv_id))

    conn.commit()
    cursor.close()
    conn.close()

    ip = get_local_ip()
    port = 8000

    return f"https://{ip}:{port}/start_exam/{token}"

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't actually connect, just gets local interface
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ip

# ===============================
# 🔷 DATABASE HELPERS
# ===============================
def save_answers_only(responses, cv_id):
    debug_log(f"Starting save_answers_only for CV_ID: {cv_id}")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        debug_log(f"Saving {len(responses)} responses to database")
        for item in responses:
            answered_at = item.get("answered_at") if item.get("user_answer") else None
            debug_log(f"Inserting response - QUES_ID: {item['id']}, Answer: {item.get('user_answer', '')[:50]}")
            cursor.execute(
                """
                INSERT INTO RESPONSE_TABLE (ID, QUES_ID, RESPONSE, SCORE, ANSWERED_AT)
                VALUES (%s, %s, %s, NULL, %s)
                """,
                (cv_id, item["id"], item.get("user_answer", "").strip() or None, answered_at)
            )
        conn.commit()
        debug_log(f"All responses saved successfully for CV_ID: {cv_id}")
    except Exception as e:
        debug_log(f"DB ERROR (insert answers): {str(e)}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
        
def create_or_validate_login(cv_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT IS_LOGGED_IN, SESSION_TOKEN, LAST_ACTIVITY
        FROM CV_INFO
        WHERE ID = %s
    """, (cv_id,))

    row = cursor.fetchone()

    if not row:
        return False, "Invalid ID"

    is_logged_in, db_token, last_activity = row

    if is_logged_in:
        if last_activity and datetime.now() - last_activity > timedelta(minutes=SESSION_TIMEOUT):
            cursor.execute("""
                UPDATE CV_INFO
                SET IS_LOGGED_IN = FALSE, SESSION_TOKEN = NULL
                WHERE ID = %s
            """, (cv_id,))
            conn.commit()
        else:
            return False, "⚠️ Session already active"

    token = str(uuid.uuid4())

    cursor.execute("""
        UPDATE CV_INFO
        SET IS_LOGGED_IN = TRUE,
            SESSION_TOKEN = %s,
            LAST_ACTIVITY = %s
        WHERE ID = %s
    """, (token, datetime.now(), cv_id))

    conn.commit()
    log_event(conn, cv_id, "candidate_login")
    cursor.close()
    conn.close()

    return True, token

def validate_active_session():
    if "cv_id" not in session or "session_token" not in session:
        return False

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT SESSION_TOKEN
        FROM CV_INFO
        WHERE ID = %s
    """, (session["cv_id"],))

    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return False

    return row[0] == session["session_token"]

def score_and_update(cv_id):
    debug_log(f"Starting score_and_update for CV_ID: {cv_id}")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        debug_log("Fetching responses for scoring")
        cursor.execute(
            """
            SELECT r.QUES_ID, r.RESPONSE, q.QUESTION
            FROM RESPONSE_TABLE r
            JOIN QUESTIONS_TABLE q ON r.ID = q.ID AND r.QUES_ID = q.QUES_ID
            WHERE r.ID = %s
            """,
            (cv_id,)
        )
        rows = cursor.fetchall()
        debug_log(f"Retrieved {len(rows)} responses to score")
        for ques_id, answer, question in rows:
            debug_log(f"Scoring QUES_ID: {ques_id}")
            # score = score_with_llm(question, answer or "")
            # Step 1: Content Score
            content_score = score_with_llm(question, answer or "")

            # Step 2: Communication Score (AFTER content score)
            communication_score = score_communication_with_llm(question, answer or "")

            debug_log(f"Scores for QUES_ID {ques_id} → Content: {content_score}, Communication: {communication_score}")

            # Step 3: Update DB
            cursor.execute(
                """
                UPDATE RESPONSE_TABLE
                SET SCORE = %s,
                    COMMUNICATION_SCORE = %s
                WHERE ID = %s AND QUES_ID = %s
                """,
                (content_score, communication_score, cv_id, ques_id)
            )
            
            debug_log(f"LLM Score for QUES_ID {ques_id}: {content_score}, Communication Score: {communication_score}")
            # cursor.execute(
            #     """
            #     UPDATE RESPONSE_TABLE
            #     SET SCORE = %s
            #     WHERE ID = %s AND QUES_ID = %s
            #     """,
            #     (content_score, cv_id, ques_id)
            # )
        conn.commit()
        debug_log(f"All scores updated successfully for CV_ID: {cv_id}")
    except Exception as e:
        debug_log(f"DB / LLM ERROR: {str(e)}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()

def update_final_status_from_score(cv_id):
    debug_log(f"Starting update_final_status_from_score for CV_ID: {cv_id}")
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT SUM(SCORE), COUNT(*)
        FROM RESPONSE_TABLE
        WHERE ID = %s
    """, (cv_id,))

    total_score, total_questions = cursor.fetchone()

    if total_score is None:
        total_score = 0

    if not total_questions:
        percentage = 0
    else:
        max_possible_score = total_questions * 5 
        percentage = (total_score / max_possible_score) * 100

    if percentage >= 50:
        status = "SELECTED"
    elif percentage >= 40:
        status = "PENDING"
    else:
        status = "REJECTED"

    debug_log(f"Calculated status for CV_ID {cv_id}: {status} (Percentage: {percentage}%)")

    cursor.execute("""
        UPDATE CV_INFO
        SET FINAL_STATUS = %s
        WHERE ID = %s
    """, (status, cv_id))

    conn.commit()

    # --- EMAIL INITIATION LOGIC WITH LOGGERS ---
    debug_log(f"Initiating email process for CV_ID {cv_id}...")
    cursor.execute("SELECT EMAIL FROM CV_INFO WHERE ID = %s", (cv_id,))
    candidate = cursor.fetchone()

    if candidate and candidate[0]:
        to_email = candidate[0]
        debug_log(f"Candidate email found: {to_email}. Authenticating Gmail service...")
        
        try:
            # Build the service using the function imported from gmail_service.py
            service = get_gmail_service()
            debug_log("Gmail service authenticated successfully.")

            if status == "REJECTED":
                debug_log(f"Attempting to send REJECTION email to {to_email}...")
                send_rejection_email(service, to_email)
                debug_log("Rejection email dispatch complete.")
                
            elif status == "SELECTED":
                debug_log(f"Attempting to send SELECTION email to {to_email}...")
                body = "Congratulations! You have passed the assessment and are selected for the next round."
                _send(service, to_email, "Assessment Results: Selected", body)
                debug_log("Selection email dispatch complete.")
                
            else:
                debug_log(f"Status is {status}. No automated email configured for this status.")
                
        except Exception as e:
            debug_log(f"ERROR during email process for {to_email}: {str(e)}")
    else:
        debug_log(f"WARNING: No email address found in DB for CV_ID {cv_id}. Email process aborted.")

    cursor.close()
    conn.close()
    debug_log(f"Finished update_final_status_from_score for CV_ID: {cv_id}")
    
    
def get_questions_from_db(cv_id):
    debug_log(f"Fetching questions for CV_ID: {cv_id}")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT QUES_ID, QUESTION
        FROM QUESTIONS_TABLE
        WHERE ID = %s
        ORDER BY QUES_ID
        """,
        (cv_id,)
    )
    questions = []
    seen = set()
    for qid, qtext in cursor.fetchall():
        if qid not in seen:
            seen.add(qid)
            questions.append({"id": qid, "text": qtext})
            debug_log(f"Added question - QUES_ID: {qid}, Text: {qtext[:50]}")
    debug_log(f"Total questions fetched: {len(questions)}")
    cursor.close()
    conn.close()
    return questions

def is_exam_already_completed(cv_id):
    debug_log(f"Checking exam completion status for CV_ID: {cv_id}")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM RESPONSE_TABLE WHERE ID = %s", (cv_id,))
        count = cursor.fetchone()[0]
        debug_log(f"Exam completion check - Found {count} responses")
        return count > 0
    except Exception as e:
        debug_log(f"Error checking exam status: {str(e)}")
        return False
    finally:
        cursor.close()
        conn.close()

# ===============================
# 🔷 CORE FLOWS: HOME / START
# ===============================
@app.route("/")
def home():
    debug_log("Home page accessed")
    error_msg = session.pop("error_msg", None)
    session.pop("cv_id", None)
    session.pop("questions", None)
    session.pop("q_index", None)
    session.pop("responses", None)
    session.pop("violation_count", None)
    return render_template("home.html", error_msg=error_msg)

@app.route("/start", methods=["GET", "POST"])
def start():
    debug_log("Start route accessed")

    cv_id = session.get("cv_id") or request.form.get("person_id")

    # 🔥 Enforce expiry
    if "link_expiry" in session:
        if datetime.now().timestamp() > session["link_expiry"]:
            session.clear()
            session["error_msg"] = "⏳ This exam link has expired (48 hours only)"
            return redirect(url_for("home"))

    if not cv_id:
        return "CV ID required"

    # 🔥 Check already completed
    if is_exam_already_completed(cv_id):
        session.clear()
        session["error_msg"] = "⚠️ You have already completed the exam."
        return redirect(url_for("home"))

    # 🔥 Login (skip if token flow)
    if "session_token" not in session:
        success, result = create_or_validate_login(cv_id)

        if not success:
            session["error_msg"] = result
            return redirect(url_for("home"))

        session["session_token"] = result

    token = session.get("session_token")

    # 🔥 Normal flow
    questions = get_questions_from_db(cv_id)
    exam_time = get_exam_time_from_job(cv_id)

    if not questions:
        return f"No questions found for CV_ID {cv_id}"

    session["cv_id"] = cv_id
    session["questions"] = questions
    session["q_index"] = 0
    session["violation_count"] = 0
    session["responses"] = []
    session["already_saved"] = False
    session["exam_duration"] = exam_time

    conn = get_connection()
    log_event(conn, cv_id, "start_exam")
    conn.close()

    return redirect(url_for("start_interview"))

@app.route("/start_exam/<token>")
def start_exam(token):
    try:
        data = serializer.loads(token, salt="exam-link")
    except BadSignature:
        return "❌ Invalid or tampered link", 403

    cv_id = data.get("cv_id")
    expiry_time = data.get("exp")

    # ✅ Expiry check
    if datetime.utcnow().timestamp() > expiry_time:
        return "⏳ Link expired", 403

    # 🔥 Get client details
    user_ip = request.remote_addr
    user_agent = request.headers.get("User-Agent")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT EXAM_LINK_USED, EXAM_TOKEN, LINK_IP, USER_AGENT
        FROM CV_INFO
        WHERE ID = %s
    """, (cv_id,))
    
    row = cursor.fetchone()

    if not row:
        return "❌ Invalid candidate", 403

    db_used, db_token, db_ip, db_agent = row

    # 🔐 1. Token validation
    if db_token != token:
        return "❌ Invalid or old link", 403

    # 🔐 2. Single-use check
    if db_used:
        return "⚠️ Link already used", 403

    # 🔐 3. FIRST ACCESS → LOCK DEVICE + IP
    if not db_ip and not db_agent:
        cursor.execute("""
            UPDATE CV_INFO
            SET LINK_IP = %s,
                USER_AGENT = %s,
                EXAM_LINK_USED = TRUE
            WHERE ID = %s
        """, (user_ip, user_agent, cv_id))
        conn.commit()

    else:
        # 🔐 4. ENFORCE SAME DEVICE + IP
        if db_ip != user_ip or db_agent != user_agent:
            return "🚫 Access denied: different device/network detected", 403

    cursor.close()
    conn.close()

    # ✅ Create session
    success, result = create_or_validate_login(cv_id)

    if not success:
        session["error_msg"] = result
        return redirect(url_for("home"))

    session["cv_id"] = cv_id
    session["link_expiry"] = expiry_time
    session["session_token"] = result

    return redirect(url_for("start"))

@app.route("/verify")
def verify():
    debug_log("Verify route accessed")
    return render_template("verify.html")

# ===============================
# 🔷 Report Violations
# ===============================

@app.route("/report_violation", methods=["POST"])
def report_violation():
    debug_log("Violation report received")
    data = request.get_json()
    violation_type = data.get("type")
    cv_id = session.get("cv_id")
    debug_log(f"Violation Type: {violation_type}, CV_ID: {cv_id}")
    
    if "cv_id" not in session:
        debug_log("No CV_ID in session")
        return jsonify({"status": "error"}), 400

    if "violation_count" not in session:
        session["violation_count"] = 0
        debug_log("Initialized violation_count in session")

    session["violation_count"] += 1
    count = session["violation_count"]
    debug_log(f"Violation count incremented to: {count}")

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Insert violation
        cursor.execute("""
            INSERT INTO EXAM_VIOLATIONS (ID, EVENT_TYPE)
            VALUES (%s, %s)
        """, (cv_id, violation_type))
        debug_log(f"Violation record inserted - Type: {violation_type}")

        # If 4 or more → terminate
        if count >= 4 and not session.get("already_saved", False):
            debug_log(f"Violation threshold reached (count: {count}) - terminating exam")
            save_answers_only(session["responses"], cv_id)

            cursor.execute("""
                UPDATE CV_INFO
                SET FINAL_STATUS = 'VIOLATED'
                WHERE ID = %s
            """, (cv_id,))

            session["already_saved"] = True
            debug_log("Exam marked as VIOLATED and answers saved")

        conn.commit()
        debug_log(f"Violation report processed successfully - Terminate: {count > 3}")

        return jsonify({
            "status": "success",
            "count": count,
            "terminate": count > 3
        })

    except Exception as e:
        debug_log(f"Violation error: {str(e)}")
        conn.rollback()
        return jsonify({"status": "error"}), 500

    finally:
        cursor.close()
        conn.close()


# ===============================
# 🔷 QUIZ
# ===============================
@app.route("/quiz", methods=["GET", "POST"])
def quiz():
    # 🔥 Enforce link expiry during exam
    if "link_expiry" in session:
        if datetime.now().timestamp() > session["link_expiry"]:
            session.clear()
            return redirect(url_for("home"))
        
    if not validate_active_session():
        session.clear()
        return redirect(url_for("home"))
    
    # 🔐 Enforce device + IP during exam (FIXED FOR MANUAL LOGIN)
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT LINK_IP, USER_AGENT
        FROM CV_INFO
        WHERE ID = %s
    """, (session["cv_id"],))

    row = cursor.fetchone()
    cursor.close()
    conn.close()

    # 🔥 FIX: Only kick the user out if they actually logged in via a secure link (IP is not NULL)
    if row and row[0] and row[1]:
        db_ip, db_agent = row
        if db_ip != request.remote_addr or db_agent != request.headers.get("User-Agent"):
            session.clear()
            return redirect(url_for("home"))
    
    # --- Update Last Activity ---
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE CV_INFO
        SET LAST_ACTIVITY = %s
        WHERE ID = %s
    """, (datetime.now(), session["cv_id"]))

    conn.commit()
    cursor.close()
    conn.close()
    
    debug_log(f"Quiz route accessed - Method: {request.method}")
    if "cv_id" not in session or "questions" not in session:
        debug_log("Invalid session -> home")
        return redirect(url_for("home"))
    
    session.setdefault("q_index", 0)
    session.setdefault("responses", [])
    session.setdefault("already_saved", False)
    
    if "exam_end_time" not in session:
        exam_time = session.get("exam_duration", 30)
        print("USING EXAM TIME:", exam_time)
        session["exam_end_time"] = (
            datetime.now() + timedelta(minutes=exam_time)
        ).timestamp()
        debug_log(f"Exam timer started for {exam_time} minutes")
        
    if datetime.now().timestamp() > session["exam_end_time"]:
        debug_log("Time expired")
        if not session["already_saved"]:
            save_answers_only(session["responses"], session["cv_id"])
            score_and_update(session["cv_id"])
            session["already_saved"] = True
        return redirect(url_for("thank_you"))
    
    if session["already_saved"] and session["q_index"] > 0:
        debug_log("Already submitted -> thankyou")
        return redirect(url_for("thank_you"))
    
    questions = session["questions"]
    index = session["q_index"]
    
    if index >= len(questions):
        debug_log("All questions done")
        if not session["already_saved"]:
            save_answers_only(session["responses"], session["cv_id"])
            score_and_update(session["cv_id"])
            update_final_status_from_score(session["cv_id"])
            session["already_saved"] = True
            
            conn = get_connection()
            log_event(conn, session["cv_id"], "response_submit")
            log_event(conn, session["cv_id"], "end_exam")
            conn.close()
            
        return redirect(url_for("thank_you"))
    
    current_q = questions[index]
    
    if request.method == "POST":
        user_answer = request.form.get("answer", "")
        current_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session["responses"].append({
            "id": current_q["id"],
            "question": current_q["text"],
            "user_answer": user_answer,
            "answered_at": current_timestamp
        })
        session["q_index"] += 1
        
        if session["q_index"] >= len(questions):
            if not session["already_saved"]:
                save_answers_only(session["responses"], session["cv_id"])
                score_and_update(session["cv_id"])
                update_final_status_from_score(session["cv_id"])
                session["already_saved"] = True
                
                conn = get_connection()
                log_event(conn, session["cv_id"], "response_submit")
                log_event(conn, session["cv_id"], "end_exam")
                conn.close()
                
                # 🔥 release session
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE CV_INFO
                    SET IS_LOGGED_IN = FALSE,
                        SESSION_TOKEN = NULL
                    WHERE ID = %s
                """, (session["cv_id"],))
                conn.commit()
                cursor.close()
                conn.close()
            return jsonify({"finished": True})
        
        next_q = questions[session["q_index"]]
        return jsonify({"finished": False, "question": next_q["text"], "index": session["q_index"] + 1, "total": len(questions)})
    
    return render_template("index 2.html", exam_end_time=session["exam_end_time"], question=current_q, index=index + 1, total=len(questions))


@app.route("/force_submit", methods=["POST"])
def force_submit():
    if not validate_active_session():
        return jsonify({"status": "invalid_session"}), 403
    debug_log("Force submit triggered")
    cv_id = session.get("cv_id")
    if not cv_id:
        return jsonify({"status": "error"}), 400
    if not session.get("already_saved", False):
        responses = session.get("responses", [])
        if responses:
            save_answers_only(responses, cv_id)
            score_and_update(cv_id)
            update_final_status_from_score(cv_id)

            conn = get_connection()
            log_event(conn, cv_id, "response_submit")
            log_event(conn, cv_id, "end_exam")   # 🔥 ADD THIS LINE
            conn.close()
        session["already_saved"] = True
        # 🔥 release session
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE CV_INFO
            SET IS_LOGGED_IN = FALSE,
                SESSION_TOKEN = NULL
            WHERE ID = %s
        """, (session["cv_id"],))
        conn.commit()
        cursor.close()
        conn.close()
    return jsonify({"status": "submitted"})

@app.route("/time_up", methods=["POST"])
def time_up():
    if not validate_active_session():
        return jsonify({"status": "invalid_session"}), 403
    debug_log("Time up notification received")
    cv_id = session.get("cv_id")
    if not cv_id:
        return jsonify({"status": "error"}), 400
    if not session.get("already_saved", False):
        save_answers_only(session["responses"], cv_id)
        score_and_update(cv_id)
        update_final_status_from_score(cv_id)
        
        conn = get_connection()
        log_event(conn, cv_id, "response_submit")
        log_event(conn, cv_id, "end_exam")
        conn.close()
        
        session["already_saved"] = True
        # 🔥 release session
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE CV_INFO
            SET IS_LOGGED_IN = FALSE,
                SESSION_TOKEN = NULL
            WHERE ID = %s
        """, (session["cv_id"],))
        conn.commit()
        cursor.close()
        conn.close()
    return jsonify({"status": "time_up"})

@app.route("/terminate_exam", methods=["POST"])
def terminate_exam():
    if not validate_active_session():
        return jsonify({"status": "invalid_session"}), 403
    debug_log("Exam termination request received")
    cv_id = session.get("cv_id")
    if not cv_id:
        return jsonify({"status": "error"}), 400
    if not session.get("already_saved", False):
        save_answers_only(session["responses"], cv_id)
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""UPDATE CV_INFO SET FINAL_STATUS = 'VIOLATED' WHERE ID = %s""", (cv_id,))
        conn.commit()
        cursor.close()
        conn.close()
        conn = get_connection()
        log_event(conn, cv_id, "end_exam")
        conn.close()
        session["already_saved"] = True
        # 🔥 release session
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE CV_INFO
            SET IS_LOGGED_IN = FALSE,
                SESSION_TOKEN = NULL
            WHERE ID = %s
        """, (session["cv_id"],))
        conn.commit()
        cursor.close()
        conn.close()
    return jsonify({"status": "terminated"})

@app.route("/thankyou")
def thank_you():
    debug_log("Thank you page accessed")
    return render_template("thankyou.html")

# ===============================
# 🔷 IDENTITY / PHOTO / FILE
# ===============================
@app.route("/upload_aadhar", methods=["POST"])
def upload_aadhar():
    debug_log("Aadhar upload initiated")
    data = request.get_json()
    image_data = data.get("image")
    cv_id = session.get("cv_id")
    if not image_data:
        debug_log("No image data provided")
        return jsonify({"status": "error"}), 400
    try:
        image_data = image_data.split(",")[1]
        image_bytes = base64.b64decode(image_data)
        filename = f"aadhar_{cv_id}.png"
        os.makedirs("uploads", exist_ok=True)
        with open(os.path.join("uploads", filename), "wb") as f:
            f.write(image_bytes)
        return jsonify({"status": "success"})
    except Exception as e:
        debug_log(f"Error uploading aadhar: {str(e)}")
        return jsonify({"status": "error"}), 400

@app.route("/capture_photo", methods=["POST"])
def capture_photo():
    if not validate_active_session():
        return jsonify({"status": "invalid_session"}), 403
    debug_log("Photo capture endpoint hit")
    data = request.get_json()
    image_data = data.get("image")
    cv_id = session.get("cv_id")
    if not image_data or not cv_id:
        return jsonify({"status": "error"}), 400
    try:
        image_data = image_data.split(",")[1]
        image_bytes = base64.b64decode(image_data)
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE CV_INFO SET FACE_IMAGE = %s WHERE ID = %s", (image_bytes, cv_id))
        conn.commit()
        log_event(conn, cv_id, "image_taken")
        cursor.close()
        conn.close()
        session["photo_verified"] = True
        return jsonify({"status": "success"})
    except Exception as e:
        debug_log(f"Photo save error: {str(e)}")
        return jsonify({"status": "error"}), 500

@app.route("/start_interview")
def start_interview():
    if not validate_active_session():
        session.clear()
        return redirect(url_for("home"))
    debug_log("Start interview route accessed")
    if "cv_id" not in session:
        return redirect(url_for("home"))
    session["photo_verified"] = True
    return render_template("start_interview.html")

# ===============================
# 🔷 REVIEW
# ===============================
@app.route("/review")
def review():
    debug_log("Review route accessed")
    cv_id = session.get("cv_id")
    if not cv_id:
        return redirect(url_for("home"))
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT q.QUESTION, r.RESPONSE
            FROM RESPONSE_TABLE r
            JOIN QUESTIONS_TABLE q ON r.ID = q.ID AND r.QUES_ID = q.QUES_ID
            WHERE r.ID = %s
            ORDER BY r.QUES_ID
            """,
            (cv_id,)
        )
        data = cursor.fetchall()
        responses = [{"question": question, "answer": answer} for question, answer in data]
        return render_template("review.html", responses=responses)
    except Exception as e:
        debug_log(f"Review error: {str(e)}")
        return redirect(url_for("thank_you"))
    finally:
        cursor.close()
        conn.close()
@app.route("/logout")
def logout():
    if "cv_id" in session:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE CV_INFO
            SET IS_LOGGED_IN = FALSE,
                SESSION_TOKEN = NULL
            WHERE ID = %s
        """, (session["cv_id"],))

        conn.commit()
        cursor.close()
        conn.close()

    session.clear()
    return redirect(url_for("home"))

def get_exam_time_from_job(cv_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT j.INTERVIEW_TIME
        FROM CV_INFO c
        JOIN JOB_INFO j ON c.JOB_ID = j.ID
        WHERE c.ID = %s
    """, (cv_id,))

    row = cursor.fetchone()

    cursor.close()
    conn.close()

    if row and row[0]:
        return int(row[0])

    return 30

if __name__ == "__main__":
    debug_log("Starting Flask application on 0.0.0.0:8001")
    app.run(host="0.0.0.0", port=8000, ssl_context='adhoc', debug=True)