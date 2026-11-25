import socket
import time
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, g, got_request_exception
from db.connection import get_connection
from db.cv_repository import log_event, ensure_violation_snapshot_column, compress_snapshot
from llm.score_generator import score_with_llm,score_communication_with_llm
import os
from flask_cors import CORS
import base64
from datetime import datetime, timedelta
import uuid
from itsdangerous import URLSafeSerializer, BadSignature
from gmail.gmail_service import get_gmail_service
from gmail.gmail_sender import send_rejection_email, _send
from utils.logger import debug_log, info_log, warning_log, error_log, exception_log, log_duration, short, log_app_urls

SESSION_TIMEOUT = 120  # minutes

app = Flask(__name__)

# The candidate app always runs on this port; exam links sent by email point here too.
CANDIDATE_APP_PORT = 8001
app.secret_key = "super_secret_key_123"

# Share one pooled DB connection per request and return it to the pool afterwards
from db.connection import release_request_connection
app.teardown_appcontext(release_request_connection)
serializer = URLSafeSerializer(app.secret_key)
CORS(app)


# ===============================
# 🔷 REQUEST LOGGING
#   werkzeug's own per-request lines are turned down to WARNING by utils.logger,
#   so every request is logged here: METHOD path -> status in N ms [cv_id]
# ===============================
@app.before_request
def _log_request_start():
    g._request_started = time.perf_counter()


@app.after_request
def _log_request_end(response):
    try:
        started = g.get("_request_started")
        elapsed = f"{(time.perf_counter() - started) * 1000:.0f} ms" if started else "? ms"
        cv_id = session.get("cv_id")
        message = (f"{request.method} {request.path} -> {response.status_code} in {elapsed}"
                   + (f" [cv_id={cv_id}]" if cv_id else "")
                   + (f" [ip={request.remote_addr}]" if response.status_code >= 400 else ""))
        if request.path.startswith("/static/"):
            debug_log(message)
        elif response.status_code >= 500:
            error_log(message, exc_info=False)
        elif response.status_code >= 400:
            warning_log(message)
        else:
            info_log(message)
    except Exception as e:                      # logging must never break a response
        debug_log(f"Request logging failed: {e}")
    return response


def _log_unhandled_exception(sender, exception, **extra):
    exception_log(f"Unhandled exception in {request.method} {request.path} "
                  f"[cv_id={session.get('cv_id')}]: {exception}")


got_request_exception.connect(_log_unhandled_exception, app)


# ===============================
# 🔷 UTILITIES / DEBUG
# ===============================

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
    port = CANDIDATE_APP_PORT

    info_log(f"Exam link generated for cv_id={cv_id} (host {ip}:{port}, valid until "
             f"{datetime.fromtimestamp(expiry_time).strftime('%Y-%m-%d %H:%M')})")
    return f"https://{ip}:{port}/start_exam/{token}"

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't actually connect, just gets local interface
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    debug_log(f"Local IP for exam links: {ip}")
    return ip

# ===============================
# 🔷 DATABASE HELPERS
# ===============================
def save_answers_only(responses, cv_id):
    debug_log(f"Saving {len(responses)} responses for cv_id={cv_id}")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        for item in responses:
            answered_at = item.get("answered_at") if item.get("user_answer") else None
            debug_log(f"Response ques_id={item['id']}: {len(item.get('user_answer', '') or '')} chars")
            cursor.execute(
                """
                INSERT INTO RESPONSE_TABLE (ID, QUES_ID, RESPONSE, SCORE, ANSWERED_AT)
                VALUES (%s, %s, %s, NULL, %s)
                """,
                (cv_id, item["id"], item.get("user_answer", "").strip() or None, answered_at)
            )
        conn.commit()
        answered = sum(1 for item in responses if (item.get("user_answer") or "").strip())
        info_log(f"Answers submitted: cv_id={cv_id}, {len(responses)} responses saved ({answered} answered, "
                 f"{len(responses) - answered} blank)")
    except Exception as e:
        error_log(f"Saving answers failed for cv_id={cv_id} ({len(responses)} responses) - rolled back: {e}")
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
        warning_log(f"Candidate login failed: unknown cv_id={cv_id}")
        return False, "Invalid ID"

    is_logged_in, db_token, last_activity = row

    if is_logged_in:
        if last_activity and datetime.now() - last_activity > timedelta(minutes=SESSION_TIMEOUT):
            info_log(f"Stale session for cv_id={cv_id} (last activity {last_activity}, timeout "
                     f"{SESSION_TIMEOUT} min) - resetting it")
            cursor.execute("""
                UPDATE CV_INFO
                SET IS_LOGGED_IN = FALSE, SESSION_TOKEN = NULL
                WHERE ID = %s
            """, (cv_id,))
            conn.commit()
        else:
            warning_log(f"Candidate login rejected: session already active for cv_id={cv_id} "
                        f"(last activity {last_activity})")
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

    info_log(f"Candidate verified and logged in: cv_id={cv_id}, ip={request.remote_addr if request else None}")
    return True, token

def validate_active_session():
    if "cv_id" not in session or "session_token" not in session:
        debug_log(f"No active candidate session (cv_id in session: {'cv_id' in session})")
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
        warning_log(f"Session check failed: cv_id={session['cv_id']} not found")
        return False

    if row[0] != session["session_token"]:
        warning_log(f"Session check failed for cv_id={session['cv_id']}: session token does not match the DB "
                    f"(logged out, timed out or logged in elsewhere)")
    return row[0] == session["session_token"]

def score_and_update(cv_id):
    info_log(f"Scoring triggered for cv_id={cv_id}")
    started = time.perf_counter()
    conn = get_connection()
    cursor = conn.cursor()
    try:
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
        debug_log(f"Scoring {len(rows)} responses for cv_id={cv_id}")
        scores = []
        for ques_id, answer, question in rows:
            # score = score_with_llm(question, answer or "")
            # Step 1: Content Score
            content_score = score_with_llm(question, answer or "")

            # Step 2: Communication Score (AFTER content score)
            communication_score = score_communication_with_llm(question, answer or "")

            scores.append(content_score)

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
            
            debug_log(f"cv_id={cv_id} ques_id={ques_id}: content score={content_score}, "
                      f"communication score={communication_score}")
            # cursor.execute(
            #     """
            #     UPDATE RESPONSE_TABLE
            #     SET SCORE = %s
            #     WHERE ID = %s AND QUES_ID = %s
            #     """,
            #     (content_score, cv_id, ques_id)
            # )
        conn.commit()
        info_log(f"Scoring completed for cv_id={cv_id}: {len(rows)} responses scored in "
                 f"{(time.perf_counter() - started) * 1000:.0f} ms, content scores={scores}")
    except Exception as e:
        error_log(f"Scoring failed for cv_id={cv_id} - scores rolled back: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()

def update_final_status_from_score(cv_id):
    debug_log(f"Calculating final status for cv_id={cv_id}")
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

    info_log(f"Final status for cv_id={cv_id}: {status} (score {total_score} over {total_questions} questions "
             f"= {percentage:.1f}%)")

    cursor.execute("""
        UPDATE CV_INFO
        SET FINAL_STATUS = %s
        WHERE ID = %s
    """, (status, cv_id))

    conn.commit()

    # --- EMAIL INITIATION LOGIC WITH LOGGERS ---
    cursor.execute("SELECT EMAIL FROM CV_INFO WHERE ID = %s", (cv_id,))
    candidate = cursor.fetchone()

    if candidate and candidate[0]:
        to_email = candidate[0]
        
        try:
            # Build the service using the function imported from gmail_service.py
            service = get_gmail_service()

            if status == "REJECTED":
                send_rejection_email(service, to_email)
                info_log(f"Assessment rejection email sent: cv_id={cv_id}, to={to_email}")

            elif status == "SELECTED":
                body = "Congratulations! You have passed the assessment and are selected for the next round."
                _send(service, to_email, "Assessment Results: Selected", body)
                info_log(f"Assessment selection email sent: cv_id={cv_id}, to={to_email}")

            else:
                info_log(f"No result email for cv_id={cv_id}: status {status} has no automated email")

        except Exception as e:
            error_log(f"Result email ({status}) failed for cv_id={cv_id}, to={to_email}: {e}")
    else:
        warning_log(f"No email address in DB for cv_id={cv_id} - result email ({status}) not sent")

    cursor.close()
    conn.close()
    
    
def get_questions_from_db(cv_id):
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
    debug_log(f"Fetched {len(questions)} questions for cv_id={cv_id}: {[q['id'] for q in questions]}")
    cursor.close()
    conn.close()
    return questions

def is_exam_already_completed(cv_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM RESPONSE_TABLE WHERE ID = %s", (cv_id,))
        count = cursor.fetchone()[0]
        debug_log(f"Exam completion check for cv_id={cv_id}: {count} saved responses")
        return count > 0
    except Exception as e:
        error_log(f"Exam completion check failed for cv_id={cv_id} - treating as not completed: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

# ===============================
# 🔷 CORE FLOWS: HOME / START
# ===============================
@app.route("/")
def home():
    error_msg = session.pop("error_msg", None)
    if error_msg:
        debug_log(f"Home page showing error: {error_msg}")
    # Start from a clean session. Leftovers from an earlier exam in this browser (old session token,
    # timer, link expiry...) made the next login fail once and only work on the second try.
    if session:
        debug_log(f"Clearing leftover session data on the login page: {sorted(session.keys())}")
    session.clear()
    return render_template("home.html", error_msg=error_msg)

@app.route("/start", methods=["GET", "POST"])
def start():
    form_cv_id = (request.form.get("person_id") or "").strip()
    if request.method == "POST" and form_cv_id:
        # Manual login from the home page is always a fresh login: never reuse another
        # (possibly finished) exam's session token, timer or CV ID that is still in the browser.
        if session:
            debug_log(f"Fresh login for cv_id={form_cv_id} - clearing earlier session data "
                      f"(cv_id in session: {session.get('cv_id')})")
        session.clear()
    cv_id = session.get("cv_id") or form_cv_id
    debug_log(f"Start requested: cv_id={cv_id} (from {'session' if session.get('cv_id') else 'form'})")

    # 🔥 Enforce expiry
    if "link_expiry" in session:
        if datetime.now().timestamp() > session["link_expiry"]:
            warning_log(f"Exam link expired for cv_id={cv_id} - start blocked")
            session.clear()
            session["error_msg"] = "⏳ This exam link has expired (48 hours only)"
            return redirect(url_for("home"))

    if not cv_id:
        warning_log("Start requested without a CV ID")
        return "CV ID required"

    # 🔥 Check already completed
    if is_exam_already_completed(cv_id):
        warning_log(f"Start blocked: cv_id={cv_id} has already completed the exam")
        session.clear()
        session["error_msg"] = "⚠️ You have already completed the exam."
        return redirect(url_for("home"))

    # 🔥 Login (skip if token flow)
    if "session_token" not in session:
        success, result = create_or_validate_login(cv_id)

        if not success:
            warning_log(f"Candidate verification failed for cv_id={cv_id}: {result}")
            session["error_msg"] = result
            return redirect(url_for("home"))

        session["session_token"] = result

    token = session.get("session_token")

    # 🔥 Normal flow
    questions = get_questions_from_db(cv_id)
    exam_time = get_exam_time_from_job(cv_id)

    if not questions:
        warning_log(f"Start blocked: no questions found for cv_id={cv_id}")
        return f"No questions found for CV_ID {cv_id}"

    session["cv_id"] = cv_id
    session["questions"] = questions
    session["q_index"] = 0
    session["violation_count"] = 0
    session["responses"] = []
    session["already_saved"] = False
    session["exam_duration"] = exam_time
    # the timer starts when the first question opens; drop any timer from an earlier exam
    session.pop("exam_end_time", None)
    session.pop("photo_verified", None)

    conn = get_connection()
    log_event(conn, cv_id, "start_exam")
    conn.close()

    info_log(f"Interview started: cv_id={cv_id}, {len(questions)} questions, duration {exam_time} min")
    return redirect(url_for("start_interview"))

@app.route("/start_exam/<token>")
def start_exam(token):
    info_log(f"Exam link opened from ip={request.remote_addr}")
    try:
        data = serializer.loads(token, salt="exam-link")
    except BadSignature:
        warning_log(f"Invalid or tampered exam link opened from ip={request.remote_addr}")
        return "❌ Invalid or tampered link", 403

    cv_id = data.get("cv_id")
    expiry_time = data.get("exp")

    # ✅ Expiry check
    if datetime.utcnow().timestamp() > expiry_time:
        warning_log(f"Expired exam link opened: cv_id={cv_id}, expired at "
                    f"{datetime.fromtimestamp(expiry_time).strftime('%Y-%m-%d %H:%M')}, ip={request.remote_addr}")
        return "⏳ Link expired", 403

    # 🔥 Get client details
    user_ip = request.remote_addr
    user_agent = request.headers.get("User-Agent")
    debug_log(f"Exam link for cv_id={cv_id}: ip={user_ip}, user agent={short(user_agent, 120)}")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT EXAM_LINK_USED, EXAM_TOKEN, LINK_IP, USER_AGENT
        FROM CV_INFO
        WHERE ID = %s
    """, (cv_id,))
    
    row = cursor.fetchone()

    if not row:
        warning_log(f"Exam link for unknown cv_id={cv_id} (ip={user_ip})")
        return "❌ Invalid candidate", 403

    db_used, db_token, db_ip, db_agent = row

    # 🔐 1. Token validation
    if db_token != token:
        warning_log(f"Outdated exam link for cv_id={cv_id}: token is not the latest issued (ip={user_ip})")
        return "❌ Invalid or old link", 403

    # 🔐 2. Single-use check
    if db_used:
        warning_log(f"Exam link already used for cv_id={cv_id} (ip={user_ip})")
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
        info_log(f"Exam link first use for cv_id={cv_id}: locked to ip={user_ip} and this browser")

    else:
        # 🔐 4. ENFORCE SAME DEVICE + IP
        if db_ip != user_ip or db_agent != user_agent:
            warning_log(f"Exam link access denied for cv_id={cv_id}: different device/network "
                        f"(ip {user_ip} vs locked {db_ip}, same browser: {db_agent == user_agent})")
            return "🚫 Access denied: different device/network detected", 403

    cursor.close()
    conn.close()

    # ✅ Create session
    success, result = create_or_validate_login(cv_id)

    if not success:
        warning_log(f"Candidate verification failed after exam link for cv_id={cv_id}: {result}")
        session["error_msg"] = result
        return redirect(url_for("home"))

    session["cv_id"] = cv_id
    session["link_expiry"] = expiry_time
    session["session_token"] = result

    info_log(f"Exam link validated for cv_id={cv_id} - redirecting to start")
    return redirect(url_for("start"))

@app.route("/verify")
def verify():
    return render_template("verify.html")

# ===============================
# 🔷 Report Violations
# ===============================

# Camera frame captured when the AI proctoring flags something (2nd person, no face, ...).
# It is stored with the violation in the database (EXAM_VIOLATIONS.SNAPSHOT) as a small JPEG.
def decode_violation_snapshot(cv_id, violation_type, data_url):
    """data:image/... URL from the exam page -> compressed JPEG bytes, or None."""
    if not data_url or not str(data_url).startswith("data:image/"):
        return None
    try:
        image_bytes = base64.b64decode(data_url.split(",", 1)[1])
        if len(image_bytes) > 2 * 1024 * 1024:            # a 480x360 JPEG is ~20 KB; refuse anything huge
            warning_log(f"Violation snapshot for cv_id={cv_id} too large ({len(image_bytes)} bytes) - not saved")
            return None
        return compress_snapshot(image_bytes)
    except Exception as e:
        error_log(f"Reading violation snapshot failed for cv_id={cv_id} ({violation_type}): {e}")
        return None


@app.route("/report_violation", methods=["POST"])
def report_violation():
    data = request.get_json(silent=True) or {}
    violation_type = str(data.get("type") or "Unknown")[:100]
    cv_id = session.get("cv_id")

    if "cv_id" not in session:
        warning_log(f"Violation report ({violation_type}) without a candidate session - ignored")
        return jsonify({"status": "error"}), 400

    details = str(data.get("details") or "")[:200]
    snapshot = decode_violation_snapshot(cv_id, violation_type, data.get("snapshot"))
    if details or snapshot:
        warning_log(f"Proctoring evidence for cv_id={cv_id}: type={violation_type}"
                    + (f", details: {details}" if details else "")
                    + (f", snapshot {len(snapshot)} bytes (stored in EXAM_VIOLATIONS)" if snapshot else ""))

    if "violation_count" not in session:
        session["violation_count"] = 0

    session["violation_count"] += 1
    count = session["violation_count"]

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Insert violation (with its camera snapshot as evidence)
        ensure_violation_snapshot_column(cursor)
        cursor.execute("""
            INSERT INTO EXAM_VIOLATIONS (ID, EVENT_TYPE, EVENT_TIME, SNAPSHOT)
            VALUES (%s, %s, NOW(3), %s)
        """, (cv_id, violation_type, snapshot))
        warning_log(f"Violation recorded: cv_id={cv_id}, type={violation_type}, count={count}")

        # If 4 or more → terminate
        if count >= 4 and not session.get("already_saved", False):
            warning_log(f"Violation limit reached for cv_id={cv_id} ({count} violations) - terminating interview")
            save_answers_only(session["responses"], cv_id)

            cursor.execute("""
                UPDATE CV_INFO
                SET FINAL_STATUS = 'VIOLATED'
                WHERE ID = %s
            """, (cv_id,))

            session["already_saved"] = True
            warning_log(f"Interview terminated: cv_id={cv_id}, FINAL_STATUS=VIOLATED after {count} violations")

        conn.commit()

        return jsonify({
            "status": "success",
            "count": count,
            "terminate": count > 3
        })

    except Exception as e:
        error_log(f"Recording violation failed for cv_id={cv_id} (type={violation_type}, count={count}): {e}")
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
            warning_log(f"Exam link expired during exam for cv_id={session.get('cv_id')} - session cleared")
            session.clear()
            return redirect(url_for("home"))

    if not validate_active_session():
        warning_log(f"Quiz access without a valid session (cv_id={session.get('cv_id')}) - redirecting home")
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
            warning_log(f"Quiz access from a different device/network for cv_id={session['cv_id']} "
                        f"(ip {request.remote_addr} vs locked {db_ip}) - session cleared")
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
    
    if "cv_id" not in session or "questions" not in session:
        warning_log(f"Quiz session has no questions loaded (cv_id={session.get('cv_id')}) - redirecting home")
        return redirect(url_for("home"))
    
    session.setdefault("q_index", 0)
    session.setdefault("responses", [])
    session.setdefault("already_saved", False)
    
    if "exam_end_time" not in session:
        exam_time = session.get("exam_duration", 30)
        session["exam_end_time"] = (
            datetime.now() + timedelta(minutes=exam_time)
        ).timestamp()
        info_log(f"Exam timer started for cv_id={session['cv_id']}: {exam_time} minutes")

    if datetime.now().timestamp() > session["exam_end_time"]:
        info_log(f"Exam time expired for cv_id={session['cv_id']} - submitting "
                 f"{len(session['responses'])} answers (already saved: {session['already_saved']})")
        if not session["already_saved"]:
            save_answers_only(session["responses"], session["cv_id"])
            score_and_update(session["cv_id"])
            session["already_saved"] = True
        return redirect(url_for("thank_you"))
    
    if session["already_saved"] and session["q_index"] > 0:
        debug_log(f"cv_id={session['cv_id']} already submitted - redirecting to thank-you page")
        return redirect(url_for("thank_you"))
    
    questions = session["questions"]
    index = session["q_index"]
    
    if index >= len(questions):
        info_log(f"All {len(questions)} questions answered for cv_id={session['cv_id']} "
                 f"(already saved: {session['already_saved']})")
        if not session["already_saved"]:
            save_answers_only(session["responses"], session["cv_id"])
            score_and_update(session["cv_id"])
            update_final_status_from_score(session["cv_id"])
            session["already_saved"] = True
            
            conn = get_connection()
            log_event(conn, session["cv_id"], "response_submit")
            log_event(conn, session["cv_id"], "end_exam")
            conn.close()
            info_log(f"Interview completed: cv_id={session['cv_id']}")

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
        debug_log(f"Answer received for cv_id={session['cv_id']}: question {session['q_index']}/{len(questions)} "
                  f"(ques_id={current_q['id']}, {len(user_answer)} chars)")

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
                info_log(f"Interview completed: cv_id={session['cv_id']}, all {len(questions)} questions answered "
                         f"- session released")
            return jsonify({"finished": True})
        
        next_q = questions[session["q_index"]]
        return jsonify({"finished": False, "question": next_q["text"], "index": session["q_index"] + 1, "total": len(questions)})
    
    return render_template("index 2.html", exam_end_time=session["exam_end_time"], question=current_q, index=index + 1, total=len(questions))


@app.route("/force_submit", methods=["POST"])
def force_submit():
    if not validate_active_session():
        warning_log(f"Force submit with invalid session (cv_id={session.get('cv_id')})")
        return jsonify({"status": "invalid_session"}), 403
    cv_id = session.get("cv_id")
    if not cv_id:
        return jsonify({"status": "error"}), 400
    info_log(f"Force submit for cv_id={cv_id}: {len(session.get('responses', []))} answers "
             f"(already saved: {session.get('already_saved', False)})")
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
        else:
            warning_log(f"Force submit for cv_id={cv_id} with no answers - nothing saved or scored")
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
        info_log(f"Interview completed (force submit): cv_id={cv_id} - session released")
    return jsonify({"status": "submitted"})

@app.route("/time_up", methods=["POST"])
def time_up():
    if not validate_active_session():
        warning_log(f"Time-up with invalid session (cv_id={session.get('cv_id')})")
        return jsonify({"status": "invalid_session"}), 403
    cv_id = session.get("cv_id")
    if not cv_id:
        return jsonify({"status": "error"}), 400
    info_log(f"Time up for cv_id={cv_id}: {len(session.get('responses', []))} answers "
             f"(already saved: {session.get('already_saved', False)})")
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
        info_log(f"Interview completed (time up): cv_id={cv_id} - session released")
    return jsonify({"status": "time_up"})

@app.route("/terminate_exam", methods=["POST"])
def terminate_exam():
    if not validate_active_session():
        warning_log(f"Terminate request with invalid session (cv_id={session.get('cv_id')})")
        return jsonify({"status": "invalid_session"}), 403
    cv_id = session.get("cv_id")
    if not cv_id:
        return jsonify({"status": "error"}), 400
    warning_log(f"Exam termination requested for cv_id={cv_id} (violations in session: "
                f"{session.get('violation_count', 0)}, already saved: {session.get('already_saved', False)})")
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
        warning_log(f"Interview terminated: cv_id={cv_id}, FINAL_STATUS=VIOLATED - session released")
    return jsonify({"status": "terminated"})

@app.route("/thankyou")
def thank_you():
    return render_template("thankyou.html")

# ===============================
# 🔷 IDENTITY / PHOTO / FILE
# ===============================
@app.route("/upload_aadhar", methods=["POST"])
def upload_aadhar():
    data = request.get_json()
    image_data = data.get("image")
    cv_id = session.get("cv_id")
    if not image_data:
        warning_log(f"Aadhar upload without image data (cv_id={cv_id})")
        return jsonify({"status": "error"}), 400
    try:
        image_data = image_data.split(",")[1]
        image_bytes = base64.b64decode(image_data)
        filename = f"aadhar_{cv_id}.png"
        os.makedirs("uploads", exist_ok=True)
        with open(os.path.join("uploads", filename), "wb") as f:
            f.write(image_bytes)
        info_log(f"Aadhar image saved for cv_id={cv_id}: {filename} ({len(image_bytes)} bytes)")
        return jsonify({"status": "success"})
    except Exception as e:
        error_log(f"Aadhar upload failed for cv_id={cv_id}: {e}")
        return jsonify({"status": "error"}), 400

@app.route("/capture_photo", methods=["POST"])
def capture_photo():
    if not validate_active_session():
        warning_log(f"Photo capture with invalid session (cv_id={session.get('cv_id')})")
        return jsonify({"status": "invalid_session"}), 403
    data = request.get_json()
    image_data = data.get("image")
    cv_id = session.get("cv_id")
    if not image_data or not cv_id:
        warning_log(f"Photo capture missing data: cv_id={cv_id}, image provided: {bool(image_data)}")
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
        info_log(f"Face photo captured for cv_id={cv_id} ({len(image_bytes)} bytes)")
        return jsonify({"status": "success"})
    except Exception as e:
        error_log(f"Saving face photo failed for cv_id={cv_id}: {e}")
        return jsonify({"status": "error"}), 500

@app.route("/start_interview")
def start_interview():
    if not validate_active_session():
        warning_log(f"Start-interview page with invalid session (cv_id={session.get('cv_id')}) - redirecting home")
        session.clear()
        return redirect(url_for("home"))
    if "cv_id" not in session:
        return redirect(url_for("home"))
    debug_log(f"Start-interview page shown for cv_id={session['cv_id']}")
    session["photo_verified"] = True
    return render_template("start_interview.html")

# ===============================
# 🔷 REVIEW
# ===============================
@app.route("/review")
def review():
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
        debug_log(f"Review page for cv_id={cv_id}: {len(responses)} responses")
        return render_template("review.html", responses=responses)
    except Exception as e:
        error_log(f"Loading review page failed for cv_id={cv_id}: {e}")
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
        info_log(f"Candidate logged out: cv_id={session['cv_id']}")

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
        debug_log(f"Exam duration for cv_id={cv_id}: {int(row[0])} min")
        return int(row[0])

    warning_log(f"No INTERVIEW_TIME for cv_id={cv_id}'s job - using default 30 min")
    return 30

if __name__ == "__main__":
    info_log(f"Starting candidate assessment app on https://0.0.0.0:{CANDIDATE_APP_PORT} (ssl=adhoc, debug=True)")
    log_app_urls("Candidate assessment app", CANDIDATE_APP_PORT, ssl=True, debug=True)
    app.run(host="0.0.0.0", port=CANDIDATE_APP_PORT, ssl_context='adhoc', debug=True)