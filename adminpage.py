import secrets
import string
import base64
import time
import uuid
from litellm import responses
import pandas as pd
from flask import Flask, render_template, session, redirect, request, flash, url_for, jsonify,Response
from pydash import result
# from torch.monitor import log_event
from db.cv_repository import log_event, ensure_violation_snapshot_column
from app import generate_exam_link
from db import connection
from db.connection import get_connection
from flask import send_file, abort, g, got_request_exception
from io import BytesIO
from utils.logger import debug_log, info_log, warning_log, error_log, exception_log, log_duration, short, log_app_urls
from db.data_access import get_candidate_attachment 
from llm.job_description_generator import generate_job_description
from werkzeug.security import generate_password_hash, check_password_hash
from email.mime.text import MIMEText
from gmail.gmail_service import get_gmail_service
from gmail.gmail_sender import _send
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, KeepTogether
from reportlab.platypus import Image as RLImage
from reportlab.lib.utils import ImageReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib import colors
from datetime import datetime, timedelta
import re
from openpyxl import Workbook, styles
import json
import os
from werkzeug.utils import secure_filename
from sentence_transformers import SentenceTransformer, util
import torch


model = SentenceTransformer("all-MiniLM-L6-v2")
LATEST_OUTPUT = {
    "preview_data": None,
    "excel_buffer": None,
    "job_description": "",
    "processed_candidates": None,  # NEW
    "file_name": "",               # NEW
    "batch_id": "",                # NEW
    "job_title": "",               # job title (existing job's title, or read from the new JD)
    "job_mode": "",                # EXISTING (picked from Jobs) or NEW (new JD -> simulated job)
    "existing_job_id": ""          # set when job_mode == EXISTING
}

# Abbreviations kept in capitals when formatting column headers for display
HEADER_ABBREVIATIONS = {"ID", "CV", "UG", "PG", "CTC", "LPA", "URL", "DOB", "PAN", "JD", "AI", "SAP", "HR", "NO"}


def display_header(col):
    """Turn a raw column name (e.g. MATCH_SCORE, candidate_name) into a title-case header."""
    words = str(col).replace("_", " ").split()
    return " ".join(w.upper() if w.upper() in HEADER_ABBREVIATIONS else w.capitalize() for w in words)

DEFAULT_MENU = [
    {
        "key": "dashboard",
        "label": "See Dashboard Preview",
        "url": "dashboard"
    },
    {
        "key": "reports",
        "label": "Reports",
        "url": "reports"
    },
    {
        "key": "excel_upload",
        "label": "Excel Upload",
        "url": "excel_upload"
    },
    {
        "key": "create_job",
        "label": "Create Job Description",
        "url": "create_job"
    },
    {
        "key": "view_jobs",
        "label": "View All Jobs",
        "url": "job_descriptions"
    },
    {
        "key": "manage_users",
        "label": "Manage Users",
        "url": "manage_users"
    }
]

ALLOWED_EXTENSIONS = {"xlsx", "xls"}

def allowed_file(filename):

    return (
        "." in filename and
        filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


# def fetch_dashboard_candidates():
#     debug_log("Starting fetch_dashboard_candidates")
#     conn = get_connection()
#     cursor = conn.cursor()

#     query = """
#     WITH SCORE_AGG AS (
#         SELECT
#             ID,
#             SUM(SCORE) AS TOTAL_SCORE,
#             COUNT(*) AS RESP_COUNT,
#             SUM(SCORE) / 5 AS GRADE
#         FROM RESPONSE_TABLE
#         GROUP BY ID
#     ),

#     LANG_CNT AS (
#         SELECT
#             ID,
#             PROG_LANG,
#             COUNT(*) AS CNT
#         FROM QUESTIONS_TABLE
#         GROUP BY ID, PROG_LANG
#     ),

#     LANG_PICK AS (
#         SELECT
#             ID,
#             PROG_LANG,
#             ROW_NUMBER() OVER (PARTITION BY ID ORDER BY CNT DESC) AS RN
#         FROM LANG_CNT
#     )

#     SELECT
#     cv.NAME,
#     cv.EMAIL,
#     cv.ID,
#     lp.PROG_LANG,
#     cv.STATUS,
#     cv.MATCH_SCORE,
#     ROUND((sa.GRADE / 5) * 100, 0) AS AVERAGE,
#     CASE
#         WHEN cv.FINAL_STATUS IS NOT NULL THEN cv.FINAL_STATUS
#         WHEN sa.GRADE < 50 THEN 'REJECTED'
#         WHEN sa.GRADE BETWEEN 51 AND 60 THEN 'PENDING'
#         WHEN sa.GRADE > 61 THEN 'SELECTED'
#     END AS FINAL_STATUS,
#     cv.FACE_IMAGE
# FROM CV_INFO cv
# LEFT JOIN SCORE_AGG sa ON sa.ID = cv.ID
# LEFT JOIN LANG_PICK lp ON lp.ID = cv.ID AND lp.RN = 1
# ORDER BY cv.ID DESC
#     """

#     cursor.execute(query)
#     rows = cursor.fetchall()
#     debug_log(f"Retrieved {len(rows)} candidates from database")

#     cursor.close()
#     conn.close()

#     result = [
#         {
#             "name": r[0],
#             "email": r[1],
#             "id": r[2],
#             "prog_lang": r[3],
#             "status": r[4],
#             "match_score": r[5],
#             "average": f"{int(r[6])}%" if r[6] is not None else "0%",
#             "final_status": r[7],
#             "has_face": r[8] is not None 
#         }
#         for r in rows
#     ]
#     debug_log(f"Returning {len(result)} formatted candidates")
#     return result

def fetch_dashboard_candidates():
    debug_log("Starting fetch_dashboard_candidates")
    conn = get_connection()
    cursor = conn.cursor()

    query = """
WITH SCORE_AGG AS (
    SELECT
        ID,
        SUM(SCORE) AS TOTAL_SCORE
    FROM RESPONSE_TABLE
    GROUP BY ID
),

QUESTION_COUNT AS (
    SELECT
        ID,
        COUNT(*) AS TOTAL_QUESTIONS
    FROM QUESTIONS_TABLE
    GROUP BY ID
)

SELECT
    cv.NAME,
    cv.EMAIL,
    cv.ID,
    cv.JOB_ID,

    COALESCE(cv.STATUS, 'REJECTED') AS STATUS,
    COALESCE(cv.MATCH_SCORE, 0) AS MATCH_SCORE,

    -- ✅ IF NO FACE IMAGE IS CAPTURED, THEY HAVEN'T TAKEN THE EXAM -> RETURN NULL
    CASE 
        WHEN cv.FACE_IMAGE IS NULL THEN NULL 
        ELSE COALESCE(ROUND((COALESCE(sa.TOTAL_SCORE, 0) * 100.0) / NULLIF(qc.TOTAL_QUESTIONS * 5, 0), 0), 0)
    END AS AVERAGE,

    -- ✅ EXACT DYNAMIC LOGIC FOR FINAL STATUS USING FACE_IMAGE
    CASE
        -- 1. If an admin explicitly overrode the status via the dashboard buttons
        WHEN cv.FINAL_STATUS IS NOT NULL AND cv.FINAL_STATUS != '' THEN cv.FINAL_STATUS
        
        -- 2. If the AI rejected their CV
        WHEN cv.STATUS = 'NOT SHORTLISTED' THEN 'REJECTED'
        
        -- 3. If Shortlisted but no face image is captured (exam not started)
        WHEN cv.STATUS = 'SHORTLISTED' AND cv.FACE_IMAGE IS NULL THEN 'NOT APPEARED'
        
        -- 4. If Shortlisted & Taken Exam (Score > 50)
        WHEN (COALESCE(sa.TOTAL_SCORE, 0) * 100.0) / NULLIF(qc.TOTAL_QUESTIONS * 5, 0) > 50 THEN 'SELECTED'
        
        -- 5. If Shortlisted & Taken Exam (Score between 40 and 50)
        WHEN (COALESCE(sa.TOTAL_SCORE, 0) * 100.0) / NULLIF(qc.TOTAL_QUESTIONS * 5, 0) >= 40 THEN 'PENDING'
        
        -- 6. If Shortlisted & Taken Exam (Score < 40)
        ELSE 'REJECTED'
    END AS FINAL_STATUS,

    -- only whether a photo exists (the image itself is loaded on demand by /face-preview)
    (cv.FACE_IMAGE IS NOT NULL) AS HAS_FACE,
    cv.CREATED_AT,
    ts.start_exam,
    ts.response_submit,
    cv.EXPERIENCE

FROM CV_INFO cv

LEFT JOIN SCORE_AGG sa 
    ON sa.ID = cv.ID

LEFT JOIN QUESTION_COUNT qc 
    ON qc.ID = cv.ID

LEFT JOIN CV_TIMESTAMPS ts 
    ON ts.ID = cv.ID

ORDER BY cv.ID DESC;
    """

    with log_duration("Dashboard candidates query", level="DEBUG", slow_ms=1500):
        cursor.execute(query)
        rows = cursor.fetchall()
    debug_log(f"Retrieved {len(rows)} candidates from database")

    cursor.close()
    conn.close()

    result = [
        {
            "name": r[0],
            "email": r[1],
            "id": r[2],
            "job_id": r[3], 
            "status": r[4],
            "match_score": r[5],
            
            # RENDER "N/A" IF THEY HAVEN'T TAKEN IT
            "average": f"{int(r[6])}%" if r[6] is not None else "N/A",
            
            "final_status": r[7],
            "has_face": bool(r[8]),
            
            "created_at": str(r[9]) if r[9] else None,
            "exam_start": str(r[10]) if r[10] else None,
            "exam_submit": str(r[11]) if r[11] else None,
            "experience": r[12] if r[12] else "Not Provided",
            # IST calendar days for the Dashboard trend chart
            "created_day": ist_day(r[9], "db"),        # CV_INFO.CREATED_AT: DB default
            "submit_day": ist_day(r[11], "app"),       # CV_TIMESTAMPS: written by the app
        }
        for r in rows
    ]

    debug_log(f"Returning {len(result)} formatted candidates")
    return result
import unicodedata

def normalize_text(text):
    if not text:
        return text
    text = unicodedata.normalize("NFKD", text)
    return text.encode("utf-8", "ignore").decode("utf-8")


app = Flask(__name__)

# The admin app always runs on this port.
ADMIN_APP_PORT = 8002

def create_fresh_users():
    users = [
        # ("AITEAM", "Admin@123", "super_admin"),
        # ("moksha", "moksha@123", "admin"),
        # ("ravali", "ravali@123", "admin"),
        # ("murali", "murali@123", "admin"),
        # ("siva", "siva@123", "admin"),
        # ("dibyajyoti", "dibyajyoti@123", "admin")
    ]

    conn = connection.get_connection()
    cursor = conn.cursor()

    for username, password, role in users:
        hashed = generate_password_hash(password)

        cursor.execute("""
            INSERT INTO ADMIN_USERS (USERNAME, PASSWORD_HASH, ROLE)
            VALUES (%s, %s, %s)
        """, (username, hashed, role))

    conn.commit()
    cursor.close()
    conn.close()

    info_log(f"Fresh users created successfully ({len(users)} users)")

app.secret_key = "secret"

# One pooled DB connection per request, returned to the pool when the request ends;
# open a connection in the background now so the first page doesn't wait for it.
app.teardown_appcontext(connection.release_request_connection)
connection.warm_up()


# ---------------------------------------------------------------------------
# Request logging (werkzeug's own per-request lines are turned down by utils.logger)
# ---------------------------------------------------------------------------
def _request_user():
    return session.get("username") or session.get("admin") or "anonymous"


def _is_quiet_path(path):
    return path.startswith("/static/") or path == "/favicon.ico"


@app.before_request
def _log_request_start():
    g._request_start = time.perf_counter()


@app.after_request
def _log_request_end(response):
    try:
        start = getattr(g, "_request_start", None)
        elapsed = (time.perf_counter() - start) * 1000 if start is not None else 0.0
        message = (f"{request.method} {request.path} -> {response.status_code} in {elapsed:.0f} ms "
                   f"(user={_request_user()})")
        status = response.status_code
        if _is_quiet_path(request.path):
            debug_log(message)
        elif status >= 500:
            error_log(message, exc_info=False)
        elif status >= 400:
            warning_log(message)
        else:
            info_log(message)
    except Exception:
        pass  # logging must never break a response
    return response


def _log_unhandled_exception(sender, exception, **extra):
    exception_log(f"Unhandled exception on {request.method} {request.path} "
                  f"(user={_request_user()}): {exception}")


got_request_exception.connect(_log_unhandled_exception, app)

# ---------------------------------------------------------------------------
# Dates and times: always shown as DD/MM/YYYY (and hh:mm AM/PM) in IST.
#   Stored times come from two clocks:
#   - "db"  : made by MySQL (NOW(), CURRENT_TIMESTAMP defaults, TIMESTAMP columns). Every
#             connection sets the session time zone to IST (db/connection.py), so these are IST.
#   - "app" : written by Python with datetime.now() -> this app's local zone
#   Both are converted to IST for display; stored data is not changed.
# ---------------------------------------------------------------------------
from zoneinfo import ZoneInfo
from datetime import timezone

IST = ZoneInfo("Asia/Kolkata")
DB_TIMEZONE = ZoneInfo(os.getenv("DB_TIMEZONE", "Asia/Kolkata"))   # zone of DB-made times (see db/connection.py)
DATE_FMT = "%d/%m/%Y"
DATETIME_FMT = "%d/%m/%Y %I:%M %p"


def to_ist(value, source="app"):
    """datetime (or 'YYYY-MM-DD HH:MM:SS' string / epoch seconds) -> aware datetime in IST, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).astimezone(IST)
    if not isinstance(value, datetime):
        text = str(value).strip()
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=DB_TIMEZONE) if source == "db" else value.astimezone()
    return value.astimezone(IST)


def fmt_ist_date(value, source="app"):
    dt = to_ist(value, source)
    return dt.strftime(DATE_FMT) if dt else None


def fmt_ist_datetime(value, source="app"):
    dt = to_ist(value, source)
    return dt.strftime(DATETIME_FMT) if dt else None


def fmt_ist_seconds(value, source="app"):
    """Like fmt_ist_datetime, with seconds: 23/11/2026 03:31:05 PM."""
    dt = to_ist(value, source)
    return dt.strftime("%d/%m/%Y %I:%M:%S %p") if dt else None


def fmt_ist_millis(value, source="db"):
    """With seconds and milliseconds (for proctoring violations): 23/11/2026 03:31:05.123 PM."""
    dt = to_ist(value, source)
    return dt.strftime("%d/%m/%Y %I:%M:%S") + f".{dt.microsecond // 1000:03d} " + dt.strftime("%p") if dt else None


def fmt_elapsed(value, start, source="db", start_source="app"):
    """Time from the exam start to a violation: '4 min 05.123 sec after start' (None if unknown)."""
    t, s = to_ist(value, source), to_ist(start, start_source)
    if not t or not s or t < s:
        return None
    ms = int((t - s).total_seconds() * 1000)
    mins, rest = divmod(ms, 60000)
    return f"{mins} min {rest // 1000:02d}.{rest % 1000:03d} sec after start"


def ist_day(value, source="app"):
    """Calendar day in IST as YYYY-MM-DD (for grouping/sorting), or None."""
    dt = to_ist(value, source)
    return dt.strftime("%Y-%m-%d") if dt else None


app.jinja_env.filters["ist_date"] = lambda v, source="app": fmt_ist_date(v, source) or ""
app.jinja_env.filters["ist_datetime"] = lambda v, source="app": fmt_ist_datetime(v, source) or ""

@app.route("/", methods=["GET", "POST"])
def login():
    debug_log(f"Login attempt - Method: {request.method}")

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        debug_log(f"Login attempt for username: {username}")
        

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT ID, PASSWORD_HASH, ROLE, MUST_CHANGE_PASSWORD
            FROM ADMIN_USERS
            WHERE USERNAME = %s
        """, (username,))

        user = cursor.fetchone()
        debug_log(f"User found: {user is not None}")

        cursor.close()
        conn.close()
        if user and check_password_hash(user[1], password):
            info_log(f"Login successful for '{username}' (user ID {user[0]}, role {user[2]})")

            session["admin"] = user[0]
            session["role"] = user[2]
            session["username"] = username

            if user[3] == 'Y':
                info_log(f"Forcing password change for user {user[0]}")
                return redirect(url_for("force_change_password"))

            return redirect(url_for("dashboard"))

        else:
            warning_log(f"Login failed - invalid credentials for username '{username}' "
                        f"({'unknown user' if not user else 'wrong password'})")
            return render_template("admin_login.html",
                                   error="Invalid username or password")

    return render_template("admin_login.html")

@app.route("/change-status", methods=["POST"])
def change_status():

    data = request.get_json()

    candidate_id = data.get("candidate_id")
    status = data.get("status")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE CV_INFO
        SET STATUS = %s
        WHERE ID = %s
    """, (status, candidate_id))

    conn.commit()
    info_log(f"CV status changed: cv_id={candidate_id}, status={status}, by={_request_user()} "
             f"(rows affected: {cursor.rowcount})")

    cursor.close()
    conn.close()

    return jsonify({"message": "Status updated successfully"})

@app.route("/face-preview/<candidate_id>")
def face_preview(candidate_id):
    # Candidate photos are personal data: only signed-in admins may view them.
    if "admin" not in session:
        warning_log(f"Face preview for {candidate_id} refused: not signed in")
        return "Unauthorized", 401

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT FACE_IMAGE
        FROM CV_INFO
        WHERE ID = %s
    """, (candidate_id,))

    row = cursor.fetchone()

    cursor.close()
    conn.close()

    if row and row[0]:
        debug_log(f"Face preview served for {candidate_id} ({len(row[0])} bytes)")
        # older photos are PNG, newer ones JPEG
        mimetype = "image/jpeg" if bytes(row[0][:3]) == b"\xff\xd8\xff" else "image/png"
        return Response(row[0], mimetype=mimetype)

    warning_log(f"No face image found for candidate {candidate_id}")
    return "No image found", 404


@app.route("/force-change-password", methods=["GET", "POST"])
def force_change_password():
    debug_log(f"Force change password - Method: {request.method}")

    if "admin" not in session:
        debug_log("No admin in session, redirecting to login")
        return redirect(url_for("login"))

    if request.method == "POST":
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password")
        debug_log(f"Password change attempt for user {session['admin']}")

        if new_password != confirm_password:
            warning_log(f"Password change for user {session['admin']} rejected: passwords do not match")
            return render_template("force_change_password.html",
                                   error="Passwords do not match")

        hashed = generate_password_hash(new_password)

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE ADMIN_USERS
            SET PASSWORD_HASH = %s, MUST_CHANGE_PASSWORD = 'N'
            WHERE ID = %s
        """, (hashed, session["admin"]))

        conn.commit()
        info_log(f"Password changed for user {session['admin']} ({session.get('username')})")
        
        cursor.close()
        conn.close()

        return redirect(url_for("dashboard"))

    return render_template("force_change_password.html")

@app.route("/reset-password/<int:user_id>")
def reset_password(user_id):
    debug_log(f"Reset password attempt for user {user_id} by {session.get('admin')}")

    if session.get("role") != "super_admin":
        warning_log(f"Password reset for user {user_id} denied: {_request_user()} is not a super admin")
        return "Unauthorized", 403

    # A super admin may also reset their own password: they get a temporary password
    # and must change it at their next login.

    characters = string.ascii_letters + string.digits + "!@#$%"
    temp_password = ''.join(secrets.choice(characters) for _ in range(10))
    hashed_password = generate_password_hash(temp_password)
    debug_log(f"Generated temporary password for user {user_id}")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE ADMIN_USERS
        SET PASSWORD_HASH = %s, MUST_CHANGE_PASSWORD = 'Y'
        WHERE ID = %s
    """, (hashed_password, user_id))

    conn.commit()
    info_log(f"Password reset for user {user_id} by {_request_user()} (temporary password issued, "
             f"rows affected: {cursor.rowcount})")
    
    cursor.close()
    conn.close()

    return render_template("temp_password.html", temp_password=temp_password)

ADMIN_ROLES = {"admin": "Admin", "super_admin": "Super Admin"}
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,50}$")


def _generate_temp_password(length=10):
    characters = string.ascii_letters + string.digits + "!@#$%"
    return ''.join(secrets.choice(characters) for _ in range(length))


def _is_super_admin(cursor):
    """Check the signed-in user's role in the database (not only the session copy)."""
    if "admin" not in session:
        return False
    cursor.execute("SELECT ROLE FROM ADMIN_USERS WHERE ID = %s", (session["admin"],))
    row = cursor.fetchone()
    return bool(row and row[0] == "super_admin")


@app.route("/create-admin-user", methods=["POST"])
def create_admin_user():
    """Super admins only: add an admin user with a temporary password (changed at first login)."""
    if "admin" not in session:
        warning_log("Create admin user refused: not signed in")
        return jsonify({"success": False, "message": "Please log in again."}), 401

    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    role = str(data.get("role") or "admin").strip()
    if not _USERNAME_RE.match(username):
        warning_log(f"Create admin user rejected: invalid username '{short(username, 60)}'")
        return jsonify({"success": False,
                        "message": "Username must be 3-50 characters: letters, numbers, dot, underscore or hyphen."}), 400
    if role not in ADMIN_ROLES:
        warning_log(f"Create admin user rejected: invalid role '{short(role, 40)}'")
        return jsonify({"success": False, "message": "Please choose a valid role."}), 400

    conn = get_connection()
    cursor = conn.cursor()
    try:
        if not _is_super_admin(cursor):
            warning_log(f"Create admin user '{username}' denied: {_request_user()} is not a super admin")
            return jsonify({"success": False, "message": "Only a super admin can create admin users."}), 403
        cursor.execute("SELECT ID FROM ADMIN_USERS WHERE LOWER(USERNAME) = LOWER(%s)", (username,))
        if cursor.fetchone():
            warning_log(f"Create admin user rejected: username '{username}' already taken")
            return jsonify({"success": False, "message": f"The username '{username}' is already taken."}), 409

        temp_password = _generate_temp_password()
        cursor.execute("""
            INSERT INTO ADMIN_USERS (USERNAME, PASSWORD_HASH, ROLE, IS_ACTIVE, MUST_CHANGE_PASSWORD)
            VALUES (%s, %s, %s, 'Y', 'Y')
        """, (username, generate_password_hash(temp_password), role))
        conn.commit()
        info_log(f"Admin user '{username}' ({role}) created by {_request_user()}")
    except Exception as e:
        conn.rollback()
        error_log(f"Creating admin user '{username}' ({role}) failed: {e}")
        return jsonify({"success": False, "message": "The user could not be created. Please try again."}), 500
    finally:
        cursor.close()
        conn.close()

    return jsonify({"success": True, "username": username, "role": ADMIN_ROLES[role],
                    "temp_password": temp_password,
                    "message": f"{ADMIN_ROLES[role]} '{username}' created."})


@app.route("/delete-admin-user/<int:user_id>", methods=["POST"])
def delete_admin_user(user_id):
    """Super admins only: delete an admin user (not yourself, not the last super admin)."""
    if "admin" not in session:
        warning_log(f"Delete admin user {user_id} refused: not signed in")
        return jsonify({"success": False, "message": "Please log in again."}), 401
    if user_id == session["admin"]:
        warning_log(f"Delete admin user rejected: user {user_id} tried to delete their own account")
        return jsonify({"success": False, "message": "You cannot delete your own account."}), 400

    conn = get_connection()
    cursor = conn.cursor()
    try:
        if not _is_super_admin(cursor):
            warning_log(f"Delete admin user {user_id} denied: {_request_user()} is not a super admin")
            return jsonify({"success": False, "message": "Only a super admin can delete users."}), 403
        cursor.execute("SELECT USERNAME, ROLE FROM ADMIN_USERS WHERE ID = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            warning_log(f"Delete admin user {user_id}: user not found")
            return jsonify({"success": False, "message": "That user no longer exists."}), 404
        username, role = row
        if role == "super_admin":
            cursor.execute("SELECT COUNT(*) FROM ADMIN_USERS WHERE ROLE = 'super_admin'")
            if cursor.fetchone()[0] <= 1:
                warning_log(f"Delete admin user rejected: '{username}' (ID {user_id}) is the last super admin")
                return jsonify({"success": False, "message": "The last super admin cannot be deleted."}), 400
        cursor.execute("DELETE FROM ADMIN_USERS WHERE ID = %s", (user_id,))
        conn.commit()
        info_log(f"Admin user '{username}' (ID {user_id}, {role}) deleted by {_request_user()}")
    except Exception as e:
        conn.rollback()
        error_log(f"Deleting admin user {user_id} failed: {e}")
        return jsonify({"success": False, "message": "The user could not be deleted. Please try again."}), 500
    finally:
        cursor.close()
        conn.close()

    return jsonify({"success": True, "message": f"User '{username}' deleted."})


def fetch_admin_users():
    debug_log("Fetching all admin users...")
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT ID, USERNAME, ROLE
        FROM ADMIN_USERS
    """)

    rows = cursor.fetchall()
    debug_log(f"Retrieved {len(rows)} admin users")

    cursor.close()
    conn.close()

    return [
        {"id": r[0], "username": r[1], "role": r[2]}
        for r in rows
    ]

@app.route(
    '/save_menu_order',
    methods=['POST']
)
def save_menu_order():

    # -----------------------------------
    # LOGIN CHECK
    # -----------------------------------

    if 'admin' not in session:

        warning_log("Save menu order refused: session expired")
        return jsonify({
            "success": False,
            "message": "Session expired"
        }), 401

    try:

        # -----------------------------------
        # GET REQUEST DATA
        # -----------------------------------

        data = request.get_json()

        debug_log(f"Menu order received from user {session.get('admin')}: {short(data, 300)}")

        # -----------------------------------
        # GET USERNAME
        # -----------------------------------

        username = session.get("username")

        # -----------------------------------
        # FALLBACK IF USERNAME MISSING
        # -----------------------------------

        if not username:

            conn = get_connection()

            cursor = conn.cursor()

            cursor.execute("""

                SELECT USERNAME
                FROM ADMIN_USERS
                WHERE ID = %s

            """, (session['admin'],))

            user_row = cursor.fetchone()

            cursor.close()
            conn.close()

            if user_row:

                username = user_row[0]

                session['username'] = username
                debug_log(f"Username missing in session; resolved '{username}' from user ID {session['admin']}")

        # -----------------------------------
        # SAFETY CHECK
        # -----------------------------------

        if not username:

            warning_log(f"Save menu order rejected: no username for user ID {session.get('admin')}")
            return jsonify({
                "success": False,
                "message": "Username not found"
            }), 400

        # -----------------------------------
        # DB CONNECTION
        # -----------------------------------

        conn = get_connection()

        cursor = conn.cursor()

        # -----------------------------------
        # DELETE OLD MENU ORDER
        # -----------------------------------

        cursor.execute("""

            DELETE FROM ADMIN_MENU_ORDER
            WHERE USERNAME = %s

        """, (username,))

        # -----------------------------------
        # INSERT NEW ORDER
        # -----------------------------------

        for index, menu_key in enumerate(data):

            cursor.execute("""

                INSERT INTO ADMIN_MENU_ORDER
                (
                    USERNAME,
                    MENU_KEY,
                    DISPLAY_ORDER
                )
                VALUES (%s, %s, %s)

            """, (

                username,
                menu_key,
                index

            ))

        # -----------------------------------
        # COMMIT
        # -----------------------------------

        conn.commit()

        cursor.close()
        conn.close()

        info_log(f"Menu order saved for {username} ({len(data)} items)")

        return jsonify({
            "success": True
        })

    except Exception as e:

        error_log(f"Saving menu order failed for user {session.get('admin')}: {e}")

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


@app.route("/manage-users")
def manage_users():
    debug_log("Accessing manage users page")
    
    if "admin" not in session:
        debug_log("No admin in session, redirecting to login")
        return redirect(url_for("login"))

    users = fetch_admin_users()
    debug_log(f"Loaded {len(users)} users for management")
    return render_template("manage_users.html", users=users)

@app.route("/dashboard")
def dashboard():
    debug_log(f"Accessing dashboard for user {session.get('admin')}")
    
    if "admin" not in session:
        debug_log("No admin in session, redirecting to login")
        return redirect(url_for("login"))
        
    with log_duration("Dashboard data load", level="DEBUG", slow_ms=1500):
        candidates = fetch_dashboard_candidates()
        postings = build_postings(candidates)
    debug_log(f"Dashboard loaded with {len(candidates)} candidates, {len(postings)} postings")
    job_stats = {
        "total_postings": len(postings),
        "active_postings": sum(1 for p in postings if p["is_active"]),
        "candidates_applied": sum(p["applied"] for p in postings),
        "awaiting_decision": sum(p["awaiting"] for p in postings),
    }
    return render_template("admin_dashboard.html", candidates=candidates,
                           postings=postings, job_stats=job_stats)


def build_postings(candidates):
    """One entry per job posting, newest first, with its applicants and counts.

    A posting is active or inactive as set on the Jobs page (JOB_INFO.IS_ACTIVE).
    Candidates whose job has been deleted are left out.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        ensure_job_postings_column(cursor)
        cursor.execute("SELECT ID, JOB_TITLE, CREATED_AT, IS_ACTIVE, NUM_POSTINGS, ROUND_CONFIG "
                       "FROM JOB_INFO ORDER BY CREATED_AT DESC")
        jobs = cursor.fetchall()
        detail_statuses = fetch_detail_statuses(cursor)
        round_results = fetch_round_results(cursor)
        schedules = fetch_round_schedules(cursor)
    finally:
        cursor.close()
        conn.close()

    job_rounds = {str(j[0]): parse_round_config(j[5]) for j in jobs}
    postings, by_id = [], {}
    for job_id, title, created, is_active, num_postings, _config in jobs:
        posting = {
            "id": str(job_id),
            "title": title or "",
            "created": fmt_ist_date(created, "db"),
            "is_active": is_active is None or bool(is_active),
            "num_postings": num_postings or 1,
            "candidates": [],
        }
        postings.append(posting)
        by_id[posting["id"]] = posting

    for c in candidates:
        row = build_application(c, {}, detail_statuses.get(str(c.get("id"))))
        posting = by_id.get(row["job_id"])
        if posting is None:
            continue
        row["rounds"] = build_round_progress(row, job_rounds.get(row["job_id"], []),
                                             round_results.get(row["id"], {}))
        apply_overall_status(row, schedules.get(row["id"], {}))
        row["is_open"] = not row["overall_final"] or row["overall_tone"] == "PENDING"
        posting["candidates"].append(row)
    debug_log(f"Built {len(postings)} job postings from {len(jobs)} jobs and {len(candidates)} candidates")

    for p in postings:
        rows = p["candidates"]
        p["applied"] = len(rows)
        p["shortlisted"] = sum(1 for r in rows if r["cv_status"] == "SHORTLISTED")
        p["interviewed"] = sum(1 for r in rows if r["exam_submit"])
        # selected = cleared every round; rejected = at the assessment or in a later round
        p["selected"] = sum(1 for r in rows if r["overall_final"] and r["overall_tone"] == "SELECTED")
        p["rejected"] = sum(1 for r in rows if r["overall_final"] and r["overall_tone"] in ("REJECTED", "VIOLATED"))
        p["awaiting"] = sum(1 for r in rows if r["is_open"])
    return postings



# @app.route("/excel-upload", methods=["GET", "POST"])
# def excel_upload():
#     debug_log(f"Excel upload - Method: {request.method}")

#     if "admin" not in session:
#         debug_log("No admin in session, redirecting to login")
#         return redirect(url_for("login"))

#     if request.method == "POST":
#         file = request.files.get("file")
#         if not file:
#             flash("No file uploaded.", "error")
#             return redirect(url_for("excel_upload"))

#         # Process the uploaded Excel file
#         # ... (implementation details)

#     return render_template("excel_upload.html")


@app.route("/api/excel-candidates", methods=["GET"])
def api_excel_candidates():
    if "admin" not in session:
        warning_log("Excel candidates API refused: not signed in")
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    job_title = request.args.get("job_title", "").strip()
    batch_id = request.args.get("batch_id", "").strip()
    debug_log(f"Fetching excel candidates (job_title='{job_title}', batch_id='{batch_id}')")

    # ADDED JOB_DESCRIPTION to SELECT list
    query = """
        SELECT ec.ID, ec.BATCH_ID, ec.FILE_NAME, ec.CANDIDATE_RANK, ec.MATCH_SCORE,
               ec.JOB_TITLE, ec.DATE_OF_APPLICATION, ec.NAME, ec.EMAIL, ec.PHONE,
               ec.RAW_DATA, ec.UPLOADED_AT, ec.JOB_DESCRIPTION,
               bj.JOB_ID, COALESCE(ji.JOB_TITLE, sj.JOB_TITLE),
               CASE WHEN bj.JOB_ID IS NULL THEN NULL
                    WHEN sj.JOB_ID IS NULL THEN 'EXISTING'
                    WHEN sj.IS_SIMULATED = 1 THEN 'SIMULATED'
                    ELSE 'POSTED' END,
               inv.STATUS, inv.SENT_AT
        FROM EXCEL_CANDIDATES ec
        LEFT JOIN EXCEL_BATCH_JOBS bj ON bj.BATCH_ID = ec.BATCH_ID
        LEFT JOIN SIMULATED_JOBS sj ON sj.JOB_ID = bj.JOB_ID
        LEFT JOIN JOB_INFO ji ON ji.ID = bj.JOB_ID
        LEFT JOIN EXCEL_INVITATIONS inv ON inv.EXCEL_CANDIDATE_ID = ec.ID AND inv.JOB_ID = bj.JOB_ID
        WHERE 1=1
    """
    params = []

    if job_title:
        query += " AND ec.JOB_TITLE = %s"
        params.append(job_title)
    if batch_id:
        query += " AND ec.BATCH_ID = %s"
        params.append(batch_id)

    query += " ORDER BY ec.BATCH_ID ASC, ec.CANDIDATE_RANK ASC"

    try:
        conn = get_connection()
        cursor = conn.cursor()
        ensure_smart_shortlisting_tables(cursor)
        with log_duration("Excel candidates query", level="DEBUG", slow_ms=1500):
            cursor.execute(query, params)
            rows = cursor.fetchall()
        debug_log(f"Excel candidates query returned {len(rows)} rows")

        data = []
        for r in rows:
            uploaded_at_str = fmt_ist_datetime(r[11], "db") or ""   # UPLOADED_AT: DB default

            data.append({
                "id": r[0],
                "batch_id": str(r[1]),
                "file_name": str(r[2]),
                "rank": r[3],
                "score": float(r[4]) if r[4] else 0.0,
                "job_title": str(r[5]),
                "date": str(r[6]),
                "name": str(r[7]),
                "email": str(r[8]),
                "phone": str(r[9]),
                "raw_data": r[10],
                "uploaded_at": uploaded_at_str,
                "job_description": str(r[12]) if len(r) > 12 and r[12] else "No Job Description Saved", # ADDED THIS
                # Simulated job created for this batch on Save, and the invitation sent for it
                "job_id": r[13] or "",
                "sim_job_title": r[14] or "",
                "job_status": r[15] or "",          # EXISTING (picked job) / SIMULATED (new JD, not in Jobs yet) / POSTED
                "invite_status": r[16] or "",       # SENT / FAILED / ""
                "invited_at": fmt_ist_datetime(r[17], "db") or "",   # SENT_AT: DB NOW()
            })
        cursor.close()
        conn.close()
        return jsonify({"success": True, "data": data})
    except Exception as e:
        error_log(f"API error fetching excel candidates (job_title='{job_title}', batch_id='{batch_id}'): {e}")
        return jsonify({"success": False, "message": str(e)})


@app.route("/excel-upload", methods=["GET", "POST"])
def excel_upload():
    if "admin" not in session:
        return redirect(url_for("login"))

    success_message = None
    message_type = "success"
    job_description = LATEST_OUTPUT["job_description"]
    job_title = LATEST_OUTPUT.get("job_title", "")
    job_mode = LATEST_OUTPUT.get("job_mode", "")
    existing_job_id = LATEST_OUTPUT.get("existing_job_id", "")

    # Job ID preview for a new job description (generated from its title; final on save)
    next_job_id = ""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        ensure_smart_shortlisting_tables(cursor)
        if job_mode == "NEW" and (job_title or extract_jd_job_id(job_description)):
            next_job_id = resolve_new_job_id(cursor, job_description, job_title)[0]
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        error_log(f"Could not prepare the Job ID preview (job_mode={job_mode}, title='{job_title}'): {e}")

    # -----------------------------------
    # FETCH AVAILABLE JOBS FOR THE CREATION DROPDOWN
    # -----------------------------------
    available_jobs = []
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT ID, JOB_TITLE, JOB_DESCRIPTION FROM JOB_INFO ORDER BY CREATED_AT DESC")
        for row in cursor.fetchall():
            available_jobs.append({
                "id": str(row[0]),
                "title": str(row[1]),
                "description": str(row[2])
            })
        cursor.close()
        conn.close()
    except Exception as e:
        error_log(f"Failed to fetch existing jobs for dropdown: {e}")

    # -----------------------------------
    # FETCH DISTINCT FILTERS FOR THE PROCESSED DATA TAB
    # -----------------------------------
    distinct_jobs = []
    distinct_batches = []
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Unique job titles and batches in ONE round trip to the (remote) DB
        cursor.execute("""
            SELECT 'JOB' AS KIND, JOB_TITLE AS VAL, NULL AS LATEST
            FROM EXCEL_CANDIDATES WHERE JOB_TITLE IS NOT NULL AND JOB_TITLE != '' GROUP BY JOB_TITLE
            UNION ALL
            SELECT 'BATCH', BATCH_ID, MAX(UPLOADED_AT)
            FROM EXCEL_CANDIDATES WHERE BATCH_ID IS NOT NULL AND BATCH_ID != '' GROUP BY BATCH_ID
        """)
        filter_rows = cursor.fetchall()
        distinct_jobs = sorted((r[1] for r in filter_rows if r[0] == 'JOB'), key=lambda v: str(v).lower())
        batches = [r for r in filter_rows if r[0] == 'BATCH']
        # newest upload first (same order as before)
        batches.sort(key=lambda r: r[2] or datetime.min, reverse=True)
        distinct_batches = [r[1] for r in batches]
        
        cursor.close()
        conn.close()
    except Exception as e:
        error_log(f"Failed to fetch distinct filters: {e}")
    debug_log(f"Excel upload page data: {len(available_jobs)} jobs, {len(distinct_jobs)} job titles, "
              f"{len(distinct_batches)} batches")

    # -----------------------------------
    # FORM SUBMIT
    # -----------------------------------
    if request.method == "POST":
        try:
            job_description = request.form.get("job_description", "").strip()
            # Job ID / title come from the description: an existing job keeps its own ID,
            # a new description gets an auto-generated (simulated) Job ID on save.
            job_mode, existing_job_id, job_title = resolve_upload_job(
                request.form.get("existing_job_id", ""), job_description, available_jobs)

            if not job_description:
                warning_log("Excel upload rejected: no job description provided")
                return render_template("excel_upload.html", LATEST_OUTPUT=LATEST_OUTPUT, next_job_id=next_job_id, job_title=job_title, job_mode=job_mode, existing_job_id=existing_job_id, success_message="Please provide Job Description.", message_type="error", job_description=job_description, available_jobs=available_jobs, distinct_jobs=distinct_jobs, distinct_batches=distinct_batches)

            if "file" not in request.files or request.files["file"].filename == "":
                warning_log("Excel upload rejected: no file uploaded")
                return render_template("excel_upload.html", LATEST_OUTPUT=LATEST_OUTPUT, next_job_id=next_job_id, job_title=job_title, job_mode=job_mode, existing_job_id=existing_job_id, success_message="Please upload Excel file.", message_type="error", job_description=job_description, available_jobs=available_jobs, distinct_jobs=distinct_jobs, distinct_batches=distinct_batches)

            file = request.files["file"]

            if not allowed_file(file.filename):
                warning_log(f"Excel upload rejected: invalid file type '{file.filename}'")
                return render_template("excel_upload.html", LATEST_OUTPUT=LATEST_OUTPUT, next_job_id=next_job_id, job_title=job_title, job_mode=job_mode, existing_job_id=existing_job_id, success_message="Invalid file type. Please upload .xlsx or .xls file.", message_type="error", job_description=job_description, available_jobs=available_jobs, distinct_jobs=distinct_jobs, distinct_batches=distinct_batches)

            info_log(f"Excel upload started: file='{file.filename}', job_mode={job_mode}, "
                     f"existing_job_id={existing_job_id or '-'}, title='{job_title}', by={_request_user()}")
            excel_bytes = BytesIO(file.read())
            with log_duration(f"Excel processing of '{file.filename}'", slow_ms=30000):
                result = process_excel_file(excel_bytes, file.filename, job_description)

            df_preview = pd.DataFrame(result["preview_data"])
            table_html = df_preview.rename(columns=display_header).to_html(classes="dataframe", index=False)

            LATEST_OUTPUT["preview_data"] = result["preview_data"]
            LATEST_OUTPUT["excel_buffer"] = result["output_excel_buffer"].getvalue()
            LATEST_OUTPUT["job_description"] = job_description
            if job_mode == "NEW" and not job_title:
                job_title = most_common_excel_title(result["processed_candidates"])
            LATEST_OUTPUT["job_title"] = job_title
            LATEST_OUTPUT["job_mode"] = job_mode
            LATEST_OUTPUT["existing_job_id"] = existing_job_id
            LATEST_OUTPUT["processed_candidates"] = result["processed_candidates"]
            LATEST_OUTPUT["file_name"] = result["file_name"]
            LATEST_OUTPUT["batch_id"] = result["batch_id"]
            LATEST_OUTPUT["table_html"] = table_html

            success_message = f"Successfully processed {result['total_candidates']} candidates."
            message_type = "success"
            info_log(f"Excel uploaded: batch_id={result['batch_id']}, file='{result['file_name']}', "
                     f"rows={result['total_candidates']}, job_mode={job_mode}, title='{job_title}'")
        except Exception as e:
            error_log(f"Excel upload processing failed: {e}")
            success_message = f"Error: {str(e)}"
            message_type = "error"

    return render_template(
        "excel_upload.html",
        LATEST_OUTPUT=LATEST_OUTPUT,
        next_job_id=next_job_id,
        job_title=job_title,
        job_mode=job_mode,
        existing_job_id=existing_job_id,
        success_message=success_message,
        message_type=message_type,
        job_description=job_description,
        table_html=LATEST_OUTPUT.get("table_html", ""),
        available_jobs=available_jobs,
        distinct_jobs=distinct_jobs,
        distinct_batches=distinct_batches
    )
      
          
EXCEL_COLUMN_MAPPING = {

    "Name": "name",
    "Job Title": "job_title",
    "Candidate profile": "candidate_profile",
    "Key Skills": "skills",
    "Total Experience": "total_experience",
    "Resume Headline": "resume_headline",
    "Summary": "summary",
    "Under Graduation degree": "ug_degree",
    "Post graduation degree": "pg_degree",
    "Doctorate degree": "doctorate_degree",

    "Contact Details": "phone",
    "Current Location": "location",

    "Curr. Company name": "company",
    "Curr. Company Designation": "designation",

    "Annual Salary": "ctc",

    "Notice period/ Availability to join": "notice_period",

    "Ans(What is your expected CTC in Lacs per annum?)": "expected_ctc",

    "Ans(What is your current CTC in Lacs per annum?)": "current_ctc",

    "Ans(What is your notice period?)": "notice_period_answer"
}

def build_education(candidate):

    education_list = []

    if candidate.get("ug_degree"):

        education_list.append({
            "degree": candidate.get("ug_degree")
        })

    if candidate.get("pg_degree"):

        education_list.append({
            "degree": candidate.get("pg_degree")
        })

    if candidate.get("doctorate_degree"):

        education_list.append({
            "degree": candidate.get("doctorate_degree")
        })

    return education_list

def generate_raw_text(data):

    raw_text = f"""
Candidate Name: {data.get('name', '')}

Resume Headline:
{data.get('resume_headline', '')}

Professional Summary:
{data.get('summary', '')}

Job Title:
{data.get('job_title', '')}

Candidate Profile:
{data.get('candidate_profile', '')}

Phone:
{data.get('phone', '')}

Location:
{data.get('location', '')}

Current Company:
{data.get('company', '')}

Designation:
{data.get('designation', '')}

Technical Skills:
{', '.join(data.get('skills', []))}

Total Experience:
{data.get('total_experience', '')} Years

Current CTC:
{data.get('current_ctc', '')}

Expected CTC:
{data.get('expected_ctc', '')}

Notice Period:
{data.get('notice_period', '')}

Education:
"""

    for edu in data.get("education", []):

        raw_text += f"""

Degree: {edu.get('degree', '')}
"""

    return raw_text.strip()


def process_excel_file(file_bytes, filename, job_description):
    debug_log(f"Reading Dynamic Excel File: {filename}")

    with log_duration(f"Excel parse of '{filename}'", level="DEBUG", slow_ms=1500):
        df = pd.read_excel(file_bytes)
    df = df.fillna("")
    debug_log(f"Excel '{filename}' has {len(df)} rows and {len(df.columns)} columns; "
              f"scoring against JD ({len(job_description or '')} chars)")

    processed_candidates = []

    for index, row in df.iterrows():
        candidate_data = row.to_dict()
        
        raw_text_parts = []
        for col_name, val in candidate_data.items():
            str_val = str(val).strip()
            if str_val and str_val.lower() not in ["none", "nan", "null"]:
                raw_text_parts.append(f"{col_name}: {str_val}")
                
        raw_text = "\n".join(raw_text_parts)
        score = compare_resume_with_jd(raw_text, job_description)
        
        candidate_record = {
            **candidate_data,
            "match_score": score
        }
        processed_candidates.append(candidate_record)

    processed_candidates = sorted(
        processed_candidates,
        key=lambda x: x["match_score"],
        reverse=True
    )

    output_rows = []
    
    for rank, candidate in enumerate(processed_candidates, start=1):
        candidate["rank"] = rank
        
        row_data = {
            "RANK": rank,
            "MATCH_SCORE": candidate["match_score"]
        }
        
        for col in df.columns:
            row_data[col] = candidate.get(col, "")
            
        output_rows.append(row_data)

    output_df = pd.DataFrame(output_rows)
    excel_buffer = BytesIO()

    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        output_df.to_excel(writer, index=False, sheet_name="Candidate Ranking")

    excel_buffer.seek(0)
    
    # Generate the Batch ID but do NOT save to DB yet
    batch_id = f"BATCH_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    debug_log(f"Excel '{filename}' processed: {len(processed_candidates)} candidates ranked, batch_id={batch_id}, "
              f"top score={processed_candidates[0]['match_score'] if processed_candidates else 'n/a'}")

    return {
        "total_candidates": len(processed_candidates),
        "processed_candidates": processed_candidates,
        "output_excel_buffer": excel_buffer,
        "preview_data": output_rows,
        "batch_id": batch_id,
        "file_name": filename
    }
    
        
@app.route("/clear-excel-upload-state")
def clear_excel_upload_state():
    LATEST_OUTPUT["preview_data"] = None
    LATEST_OUTPUT["excel_buffer"] = None
    LATEST_OUTPUT["job_description"] = ""
    LATEST_OUTPUT["processed_candidates"] = None
    LATEST_OUTPUT["file_name"] = ""
    LATEST_OUTPUT["batch_id"] = ""
    LATEST_OUTPUT["job_title"] = ""
    LATEST_OUTPUT["job_mode"] = ""
    LATEST_OUTPUT["existing_job_id"] = ""
    debug_log(f"Excel upload state cleared by {_request_user()}")

    return jsonify({"status": "cleared"})
    
    
    
@app.route("/preview-output")
def preview_output():

    if not LATEST_OUTPUT["preview_data"]:

        return redirect(
            url_for("excel_upload")
        )

    df = pd.DataFrame(

        LATEST_OUTPUT["preview_data"]
    )

    table_html = df.rename(columns=display_header).to_html(

        classes=(
            "table table-bordered "
            "table-striped"
        ),

        index=False
    )

    return render_template(

        "preview_output.html",

        table_html=table_html
    ) 
    
    
@app.route("/download-output")
def download_output():

    if not LATEST_OUTPUT["excel_buffer"]:

        warning_log("Download output requested but no processed Excel is available; redirecting to upload")
        return redirect(
            url_for("excel_upload")
        )

    excel_buffer = BytesIO(

        LATEST_OUTPUT["excel_buffer"]
    )

    excel_buffer.seek(0)
    info_log(f"Ranking output downloaded: batch_id={LATEST_OUTPUT.get('batch_id')}, "
             f"{len(LATEST_OUTPUT['excel_buffer'])} bytes, by={_request_user()}")

    return send_file(

        excel_buffer,

        as_attachment=True,

        download_name=(
            "candidate_ranking_output.xlsx"
        ),

        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
    )
    

@app.route("/confirm-save-excel", methods=["POST"])
def confirm_save_excel():
    if "admin" not in session:
        warning_log("Confirm save excel refused: not signed in")
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    candidates = LATEST_OUTPUT.get("processed_candidates")
    filename = LATEST_OUTPUT.get("file_name")
    batch_id = LATEST_OUTPUT.get("batch_id")
    job_desc = LATEST_OUTPUT.get("job_description", "") # FETCH JD FROM MEMORY
    batch_title = (LATEST_OUTPUT.get("job_title") or "").strip()
    job_mode = LATEST_OUTPUT.get("job_mode") or "NEW"
    existing_job_id = LATEST_OUTPUT.get("existing_job_id") or ""

    if not candidates:
        warning_log("Confirm save excel: no processed candidates in memory")
        return jsonify({"success": False, "message": "No data available to save. Please process an Excel file first."})

    if not batch_title:
        batch_title = most_common_excel_title(candidates) or "Untitled Role"
        debug_log(f"Batch title not set; using '{batch_title}' from the Excel rows")
    debug_log(f"Saving excel batch {batch_id}: {len(candidates)} candidates, job_mode={job_mode}, "
              f"existing_job_id={existing_job_id or '-'}, title='{batch_title}'")

    try:
        conn = get_connection()
        cursor = conn.cursor()
        ensure_smart_shortlisting_tables(cursor)

        created_by = str(session.get("username") or session.get("admin"))
        if job_mode == "EXISTING" and existing_job_id:
            # Picked an existing job: just link the batch to it
            batch_job_id, is_simulated = existing_job_id, False
        else:
            # New job description: Job ID from the description when it is free, otherwise
            # generated from the title; kept as a simulated job (separate from Jobs) until
            # invitations are sent
            batch_job_id, _id_source, id_note = resolve_new_job_id(cursor, job_desc, batch_title)
            is_simulated = True
            cursor.execute("""
                INSERT INTO SIMULATED_JOBS (JOB_ID, JOB_TITLE, JOB_DESCRIPTION, IS_SIMULATED, CREATED_BY, CREATED_AT)
                VALUES (%s, %s, %s, 1, %s, NOW())
            """, (batch_job_id, batch_title[:100], job_desc, created_by))
        cursor.execute("""
            INSERT INTO EXCEL_BATCH_JOBS (BATCH_ID, JOB_ID, CREATED_BY, CREATED_AT)
            VALUES (%s, %s, %s, NOW())
        """, (batch_id, batch_job_id, created_by))
        
        for candidate in candidates:
            rank = candidate.get("rank", 0)
            score = candidate.get("match_score", 0)
            
            job_title = str(candidate.get("Job Title", ""))[:255]
            name = str(candidate.get("Name", candidate.get("Candidate Name", "Unknown")))[:255]
            email = str(candidate.get("Email ID", candidate.get("Email", "Unknown")))[:255]
            phone = str(candidate.get("Phone Number", candidate.get("Contact Details", "")))[:50]
            
            raw_date = candidate.get("Date of application", "")
            if hasattr(raw_date, 'strftime'):
                doa = raw_date.strftime('%d/%m/%Y')   # DD/MM/YYYY
            else:
                doa = str(raw_date)[:50]
                
            raw_data_json = json.dumps(candidate, default=str)
            
            # INSERT QUERY UPDATED TO INCLUDE JOB_DESCRIPTION
            cursor.execute("""
                INSERT INTO EXCEL_CANDIDATES 
                (BATCH_ID, FILE_NAME, CANDIDATE_RANK, MATCH_SCORE, JOB_TITLE, DATE_OF_APPLICATION, NAME, EMAIL, PHONE, RAW_DATA, JOB_DESCRIPTION) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (batch_id, filename, rank, score, job_title, doa, name, email, phone, raw_data_json, job_desc))
            
        conn.commit()
        cursor.close()
        conn.close()

        LATEST_OUTPUT["processed_candidates"] = None
        info_log(f"Excel batch saved: batch_id={batch_id}, file='{filename}', rows={len(candidates)}, "
                 f"job_id={batch_job_id} ({'simulated' if is_simulated else 'existing'}), by={created_by}")

        if is_simulated:
            job_note = (f"New job description saved as simulated Job ID {batch_job_id} - {batch_title} ({id_note}) "
                        f"Send invitations from Processed Data to post this job.")
        else:
            job_note = f"Linked to existing Job ID {batch_job_id} - {batch_title}."
        return jsonify({"success": True, "job_id": batch_job_id, "is_simulated": bool(is_simulated),
                        "message": f"Successfully saved {len(candidates)} candidates to the database! "
                                   f"(Batch: {batch_id}) {job_note}"})
    except Exception as e:
        error_log(f"Saving excel batch {batch_id} ('{filename}', {len(candidates)} candidates) failed: {e}")
        try:
            conn.rollback()
        except Exception:
            warning_log(f"Rollback after failed excel batch save {batch_id} also failed")
        return jsonify({"success": False, "message": f"Database Error: {str(e)}"})


# ---------------------------------------------------------------------------
# Smart Shortlisting: jobs for Excel batches and candidate invitations
#   SIMULATED_JOBS     a job created from a NEW job description: JOB_ID (generated from the
#                      title, e.g. BTP002), title, description and the IS_SIMULATED flag.
#                      It stays out of JOB_INFO until invitations are sent; then it is copied
#                      into JOB_INFO (same as New Job Posting), IS_SIMULATED -> 0, POSTED_AT set.
#   EXCEL_BATCH_JOBS   links each saved Excel batch to its job (existing JOB_INFO job or simulated job).
#   EXCEL_INVITATIONS  one row per invited Excel candidate.
#   Job IDs keep the 2-5 letters + digits shape so the mail intake recognises them.
# ---------------------------------------------------------------------------
DEFAULT_SIM_ROUND_CONFIG = {
    "round_1": {"name": "Online Assessment", "pass": "50", "pending_min": "40", "pending_max": "49", "fail": "39"}
}
_SMART_TABLES_READY = False
_TITLE_LINE = re.compile(r"^[ \t*#-]*(?:job\s*title|job\s*role|position|role|designation)\s*[:\-\u2013]\s*(.+?)\s*$",
                         re.IGNORECASE | re.MULTILINE)
# words that say little about the role itself (ignored when building a Job ID prefix)
_GENERIC_TITLE_WORDS = {
    "sap", "senior", "sr", "junior", "jr", "lead", "principal", "associate", "trainee", "intern", "staff",
    "developer", "engineer", "consultant", "manager", "analyst", "specialist", "architect", "technical",
    "functional", "tester", "administrator", "admin", "executive", "officer", "head", "expert",
    "professional", "and", "of", "the", "for", "with", "in", "a", "an", "team", "member",
}


def _table_columns(cursor, table):
    cursor.execute("SHOW TABLES LIKE %s", (table.lower(),))
    if not cursor.fetchall():
        return None
    cursor.execute(f"SHOW COLUMNS FROM {table}")
    return {r[0].upper() for r in cursor.fetchall()}


_JOB_POSTINGS_READY = False


def ensure_job_postings_column(cursor):
    """Adds the JOB_INFO columns the Jobs page needs if they are missing:
    NUM_POSTINGS (number of open positions) and IS_ACTIVE (1 = accepting applications)."""
    global _JOB_POSTINGS_READY
    if _JOB_POSTINGS_READY:
        return
    for column, ddl in (("NUM_POSTINGS", "INT NOT NULL DEFAULT 1"),
                        ("IS_ACTIVE", "TINYINT(1) NOT NULL DEFAULT 1")):
        cursor.execute(f"SHOW COLUMNS FROM JOB_INFO LIKE '{column}'")
        if not cursor.fetchall():
            cursor.execute(f"ALTER TABLE JOB_INFO ADD COLUMN {column} {ddl}")
            info_log(f"Added column JOB_INFO.{column} ({ddl})")
    _JOB_POSTINGS_READY = True


def ensure_smart_shortlisting_tables(cursor):
    global _SMART_TABLES_READY
    if _SMART_TABLES_READY:
        return
    # An earlier layout of these two tables may exist; replace it only while it is still empty.
    for table, marker in (("SIMULATED_JOBS", "BATCH_ID"), ("EXCEL_BATCH_JOBS", "JOB_TITLE")):
        cols = _table_columns(cursor, table)
        if cols and marker in cols:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            if cursor.fetchone()[0] == 0:
                warning_log(f"{table} has an old empty layout; dropping it so it can be recreated")
                cursor.execute(f"DROP TABLE {table}")
            else:
                error_log(f"{table} has an old layout and contains data; manual migration needed")
                raise RuntimeError(f"{table} has an old layout and contains data; please migrate it manually.")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS SIMULATED_JOBS (
            JOB_ID VARCHAR(10) NOT NULL PRIMARY KEY,
            JOB_TITLE VARCHAR(100),
            JOB_DESCRIPTION LONGTEXT,
            IS_SIMULATED TINYINT(1) NOT NULL DEFAULT 1,
            CREATED_BY VARCHAR(100),
            CREATED_AT DATETIME NOT NULL,
            POSTED_AT DATETIME NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS EXCEL_BATCH_JOBS (
            BATCH_ID VARCHAR(50) NOT NULL PRIMARY KEY,
            JOB_ID VARCHAR(10) NOT NULL,
            CREATED_BY VARCHAR(100),
            CREATED_AT DATETIME NOT NULL,
            KEY IX_BATCH_JOB (JOB_ID)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS EXCEL_INVITATIONS (
            ID INT AUTO_INCREMENT PRIMARY KEY,
            EXCEL_CANDIDATE_ID INT NOT NULL,
            JOB_ID VARCHAR(10) NOT NULL,
            EMAIL VARCHAR(255),
            STATUS VARCHAR(20) NOT NULL,
            ERROR_MESSAGE TEXT,
            SENT_BY VARCHAR(100),
            SENT_AT DATETIME NOT NULL,
            UNIQUE KEY UQ_INVITE (EXCEL_CANDIDATE_ID, JOB_ID)
        )
    """)
    _SMART_TABLES_READY = True
    debug_log("Smart shortlisting tables verified (SIMULATED_JOBS, EXCEL_BATCH_JOBS, EXCEL_INVITATIONS)")


def job_id_prefix(cursor, title):
    """Letters for a Job ID, based on the job title.

    1. Reuse the prefix of existing jobs about the same thing (e.g. any "CAPM" job -> CAP).
    2. Otherwise an acronym in the title other than SAP (e.g. "SAP MM Consultant" -> MM),
       or the first three letters of its main word ("Java Developer" -> JAV).
    """
    words = re.findall(r"[A-Za-z]+", title or "")
    acronyms = [w for w in words if w.isupper() and 2 <= len(w) <= 5 and w != "SAP"]
    significant = [w for w in words if w.lower() not in _GENERIC_TITLE_WORDS]
    keyword = acronyms[0] if acronyms else (significant[0] if significant else (words[0] if words else ""))

    # only a specific keyword may borrow another job's prefix (not "SAP", "Developer", ...)
    if keyword and keyword.lower() not in _GENERIC_TITLE_WORDS:
        cursor.execute("SELECT ID, JOB_TITLE FROM JOB_INFO UNION SELECT JOB_ID, JOB_TITLE FROM SIMULATED_JOBS")
        counts = {}
        for jid, jt in cursor.fetchall():
            if re.search(r"\b" + re.escape(keyword) + r"\b", jt or "", re.IGNORECASE):
                m = re.fullmatch(r"([A-Z]{2,5})(\d{2,5})", str(jid or "").strip().upper())
                if m:
                    counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        if counts:
            return max(counts, key=counts.get)

    prefix = (acronyms[0] if acronyms else keyword[:3]).upper() if keyword else "JOB"
    return (prefix + "X")[:5] if len(prefix) < 2 else prefix[:5]


def generate_job_id(cursor, title):
    """Next free Job ID for a title, e.g. "SAP BTP Developer" -> BTP002 when BTP001 exists."""
    prefix = job_id_prefix(cursor, title)
    cursor.execute("""
        SELECT ID FROM JOB_INFO WHERE ID LIKE %s
        UNION
        SELECT JOB_ID FROM SIMULATED_JOBS WHERE JOB_ID LIKE %s
    """, (prefix + "%", prefix + "%"))
    used = 0
    for (jid,) in cursor.fetchall():
        tail = str(jid)[len(prefix):]
        if tail.isdigit():
            used = max(used, int(tail))
    return f"{prefix}{used + 1:03d}"


_JD_JOB_ID_LINE = re.compile(r"^[ \t*#-]*job\s*id\s*[:\-\u2013]\s*([A-Za-z0-9]+)", re.IGNORECASE | re.MULTILINE)
_VALID_JOB_ID = re.compile(r"[A-Z]{2,5}\d{2,5}")   # the shape the mail intake recognises


def extract_jd_job_id(job_description):
    """Job ID written in the description ("Job ID: BTP005"), upper-cased, or ""."""
    m = _JD_JOB_ID_LINE.search(str(job_description or ""))
    return m.group(1).upper()[:20] if m else ""


def job_id_in_use(cursor, job_id):
    """True when an existing job or a simulated job already uses this ID."""
    cursor.execute("""
        SELECT ID FROM JOB_INFO WHERE ID = %s
        UNION
        SELECT JOB_ID FROM SIMULATED_JOBS WHERE JOB_ID = %s
    """, (job_id, job_id))
    return bool(cursor.fetchall())


def resolve_new_job_id(cursor, job_description, title):
    """(job_id, source, note) for a NEW job description.

    The Job ID written in the description is used when it has the right shape and no
    existing or simulated job uses it; otherwise a new one is generated from the title.
    """
    jd_id = extract_jd_job_id(job_description)
    if jd_id:
        if not _VALID_JOB_ID.fullmatch(jd_id):
            reason = f"Job ID {jd_id} in the description is not in the expected format (letters then digits, e.g. BTP001)"
        elif job_id_in_use(cursor, jd_id):
            reason = f"Job ID {jd_id} in the description is already used by another job"
        else:
            debug_log(f"Job ID {jd_id} taken from the job description")
            return jd_id, "DESCRIPTION", f"Job ID {jd_id} taken from the job description."
        generated = generate_job_id(cursor, title)
        debug_log(f"{reason}; generated {generated} from title '{title}' instead")
        return generated, "GENERATED", f"{reason}, so {generated} was generated from the job title."
    generated = generate_job_id(cursor, title)
    debug_log(f"Job ID {generated} generated from title '{title}'")
    return generated, "GENERATED", f"{generated} generated from the job title."


@app.route("/smart-shortlisting/suggest-job-id", methods=["GET", "POST"])
def suggest_job_id():
    """Preview of the Job ID a new job description would get (from the description or its title)."""
    if "admin" not in session:
        return jsonify({"job_id": ""}), 401
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or request.args.get("title", "")).strip()[:100]
    description = str(data.get("description") or "")
    if not title and not extract_jd_job_id(description):
        return jsonify({"job_id": "", "source": "", "note": ""})
    conn = get_connection()
    cursor = conn.cursor()
    try:
        ensure_smart_shortlisting_tables(cursor)
        job_id, source, note = resolve_new_job_id(cursor, description, title)
        conn.commit()
    except Exception as e:
        error_log(f"Job ID suggestion failed (title='{title}'): {e}")
        job_id, source, note = "", "", ""
    finally:
        cursor.close()
        conn.close()
    return jsonify({"job_id": job_id, "source": source, "note": note})


def _norm_text(text):
    return " ".join(str(text or "").split())


def extract_job_title(job_description):
    """Read the job title from a job description ("Job Title: ..." line, else a short first line)."""
    jd = str(job_description or "")
    m = _TITLE_LINE.search(jd)
    if m:
        return m.group(1).strip()[:100]
    for line in jd.splitlines():
        t = line.strip().strip("*#").strip()
        if not t or t.lower().rstrip(":") in ("job description", "about the role", "jd", "description"):
            continue
        if len(t) <= 80 and ":" not in t and not t.endswith("."):
            return t[:100]
        break
    return ""


def most_common_excel_title(candidates):
    titles = [str(c.get("Job Title", "")).strip() for c in (candidates or []) if str(c.get("Job Title", "")).strip()]
    return max(set(titles), key=titles.count)[:100] if titles else ""


def resolve_upload_job(selected_job_id, job_description, available_jobs):
    """(mode, existing_job_id, title) for an upload.

    EXISTING only when a job was picked and the description was not changed;
    anything else is a NEW description (gets a simulated Job ID on save).
    """
    selected_job_id = str(selected_job_id or "").strip()
    job = next((j for j in available_jobs if j["id"] == selected_job_id), None) if selected_job_id else None
    if job and _norm_text(job["description"]) == _norm_text(job_description):
        return "EXISTING", job["id"], (job["title"] or extract_job_title(job_description))[:100]
    return "NEW", "", extract_job_title(job_description)


def build_invitation_email(name, job_id, job_title, job_description):
    jd = (job_description or "").strip()
    if len(jd) > 1500:
        jd = jd[:1500].rsplit(" ", 1)[0] + " ..."
    subject = f"{job_id} - Invitation to apply: {job_title}"
    body = (
        f"Dear {name or 'Candidate'},\n\n"
        f"We came across your profile and believe you could be a great fit for the {job_title} role.\n\n"
        f"Job ID: {job_id}\n\n"
        "If you are interested, please REPLY to this email (keep the Job ID in the subject line) and include:\n"
        "  1. Your latest CV as a single PDF attachment\n"
        "  2. Full name\n"
        "  3. Mobile number\n"
        "  4. Total experience (in years)\n"
        "  5. Current company\n"
        "  6. Current CTC (in Lacs per annum)\n"
        "  7. Expected CTC (in Lacs per annum)\n"
        "  8. Notice period\n"
        "  9. Current location\n"
        "  10. Preferred location\n"
        + (f"\nAbout the role:\n{jd}\n" if jd else "")
        + "\nBest Regards,\nRecruitment Team"
    )
    return subject, body


def describe_send_error(error):
    """Readable reason for a failed Gmail send, and whether it blocks all further sends
    (rate limit / sign-in problems) so the rest of the batch is not attempted."""
    text = str(error)
    status = getattr(getattr(error, "resp", None), "status", None)
    if status == 429 or "rateLimitExceeded" in text or "Rate Limit" in text or "rate limit" in text.lower():
        match = re.search(r"Retry after (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)", text)
        when = ""
        if match:
            try:
                retry_at = datetime.strptime(match.group(1).split(".")[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                when = " Try again after " + fmt_ist_datetime(retry_at) + " (IST)."
            except Exception:
                debug_log(f"Could not parse Gmail retry time '{match.group(1)}'")
                when = ""
        return ("Gmail has temporarily stopped this account from sending emails (sending limit reached)."
                + (when or " Please try again later."), True)
    if "invalid_grant" in text or status == 401 or "Token has been expired" in text:
        return ("The Gmail sign-in for the agent account has expired. Re-authorise Gmail (token.json) and try again.", True)
    if status == 403:
        return ("Gmail refused to send this email (permission or daily sending limit).", True)
    if status == 400:
        return ("Gmail rejected this email address or message.", False)
    return ("The email could not be sent.", False)


@app.route("/smart-shortlisting/send-invitations", methods=["POST"])
def send_candidate_invitations():
    """Email selected Excel candidates an invitation to apply; post a simulated job to Jobs afterwards."""
    if "admin" not in session:
        warning_log("Send invitations refused: not signed in")
        return jsonify({"success": False, "message": "Please log in again."}), 401

    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id") or "").strip()
    try:
        candidate_ids = sorted({int(x) for x in (data.get("candidate_ids") or [])})
    except (TypeError, ValueError):
        warning_log(f"Send invitations for {job_id} rejected: invalid candidate selection {short(data.get('candidate_ids'), 100)}")
        return jsonify({"success": False, "message": "Invalid candidate selection."}), 400
    try:
        num_questions = max(1, min(50, int(data.get("num_questions") or 5)))
        interview_time = max(5, min(240, int(data.get("interview_time") or 30)))
    except (TypeError, ValueError):
        warning_log(f"Send invitations for {job_id} rejected: non-numeric questions/interview time "
                    f"({short(data.get('num_questions'), 20)}, {short(data.get('interview_time'), 20)})")
        return jsonify({"success": False, "message": "Questions and interview time must be numbers."}), 400
    if not job_id or not candidate_ids:
        warning_log(f"Send invitations rejected: job_id='{job_id}', {len(candidate_ids)} candidates selected")
        return jsonify({"success": False, "message": "Select at least one candidate."}), 400

    sent_by = str(session.get("username") or session.get("admin"))
    info_log(f"Sending invitations for job {job_id}: {len(candidate_ids)} candidates selected by {sent_by} "
             f"(questions={num_questions}, time={interview_time} min)")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        ensure_smart_shortlisting_tables(cursor)
        placeholders = ",".join(["%s"] * len(candidate_ids))
        cursor.execute(f"""
            SELECT ec.ID, ec.NAME, ec.EMAIL, inv.STATUS, bj.JOB_ID, sj.JOB_TITLE, sj.JOB_DESCRIPTION, sj.IS_SIMULATED
            FROM EXCEL_CANDIDATES ec
            JOIN EXCEL_BATCH_JOBS bj ON bj.BATCH_ID = ec.BATCH_ID
            LEFT JOIN SIMULATED_JOBS sj ON sj.JOB_ID = bj.JOB_ID
            LEFT JOIN EXCEL_INVITATIONS inv ON inv.EXCEL_CANDIDATE_ID = ec.ID AND inv.JOB_ID = bj.JOB_ID
            WHERE ec.ID IN ({placeholders})
        """, candidate_ids)
        rows = cursor.fetchall()
        cursor.execute("SELECT JOB_TITLE, JOB_DESCRIPTION FROM JOB_INFO WHERE ID = %s", (job_id,))
        in_jobs = cursor.fetchone()
    except Exception as e:
        cursor.close()
        conn.close()
        error_log(f"Invitation lookup failed for job {job_id}, candidates {candidate_ids}: {e}")
        return jsonify({"success": False, "message": "Could not load the selected candidates."}), 500

    if len(rows) != len(candidate_ids) or any(r[4] != job_id for r in rows):
        cursor.close()
        conn.close()
        warning_log(f"Send invitations rejected: {len(rows)} of {len(candidate_ids)} candidates found, "
                    f"or some are not linked to job {job_id}")
        return jsonify({"success": False, "message": f"Some selected candidates are not linked to job {job_id}."}), 400

    is_simulated = any(r[7] == 1 for r in rows)      # simulated job not yet copied to Jobs
    if not in_jobs and not is_simulated:
        cursor.close()
        conn.close()
        warning_log(f"Send invitations rejected: job {job_id} no longer exists in Jobs")
        return jsonify({"success": False, "message": f"Job {job_id} no longer exists in Jobs."}), 404
    # Prefer the live job's title/description when the job is in Jobs
    job_title = (in_jobs[0] if in_jobs and in_jobs[0] else rows[0][5]) or job_id
    job_desc = rows[0][6] or (in_jobs[1] if in_jobs else "")

    try:
        with log_duration("Gmail service creation", level="DEBUG"):
            service = get_gmail_service()
    except Exception as e:
        cursor.close()
        conn.close()
        error_log(f"Gmail service unavailable; no invitations sent for job {job_id}: {e}", exc_info=False)
        return jsonify({"success": False, "message": "The email service is not available. No invitations were sent."}), 503

    sent, failed, skipped = [], [], []
    blocked_reason = None          # once Gmail blocks sending, the remaining mails would fail too
    email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    for cid, name, email, prior, *_ in rows:
        email = (email or "").strip()
        if prior == "SENT":
            skipped.append({"id": cid, "name": name, "reason": "already invited"})
            continue
        if not email_re.match(email):
            skipped.append({"id": cid, "name": name, "reason": "no valid email"})
            continue
        subject, body = build_invitation_email(name, job_id, job_title, job_desc)
        status, error = "SENT", None
        if blocked_reason:
            status, error = "FAILED", blocked_reason
            failed.append({"id": cid, "name": name, "reason": blocked_reason})
        else:
            try:
                _send(service, email, subject, body)   # timed/logged inside _send
                sent.append({"id": cid, "name": name})
            except Exception as e:
                reason, blocking = describe_send_error(e)
                status, error = "FAILED", (reason + " | " + str(e))[:1000]
                failed.append({"id": cid, "name": name, "reason": reason})
                warning_log(f"Invitation to {email} (candidate {cid}) failed: {reason}")
                if blocking:
                    blocked_reason = reason
                    warning_log(f"Gmail is blocking sends; remaining invitations for {job_id} will be marked FAILED")
        try:
            cursor.execute("""
                INSERT INTO EXCEL_INVITATIONS (EXCEL_CANDIDATE_ID, JOB_ID, EMAIL, STATUS, ERROR_MESSAGE, SENT_BY, SENT_AT)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE STATUS = VALUES(STATUS), ERROR_MESSAGE = VALUES(ERROR_MESSAGE),
                                        EMAIL = VALUES(EMAIL), SENT_BY = VALUES(SENT_BY), SENT_AT = VALUES(SENT_AT)
            """, (cid, job_id, email, status, error, sent_by))
            conn.commit()
        except Exception as e:
            conn.rollback()
            error_log(f"Could not record invitation for candidate {cid} (job {job_id}, status {status}): {e}")

    # After the mails are out, post a simulated job to Jobs (same as New Job Posting)
    posted_now = False
    post_error = None
    if sent and is_simulated and not in_jobs:
        try:
            cursor.execute("""
                INSERT INTO JOB_INFO
                    (ID, JOB_TITLE, JOB_DESCRIPTION, CREATED_AT, NUM_QUESTIONS, INTERVIEW_TIME, TOTAL_ROUNDS, ROUND_CONFIG)
                VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s)
            """, (job_id, job_title, f"\nJob Description:\n{job_desc}\n", num_questions, interview_time,
                  1, json.dumps(DEFAULT_SIM_ROUND_CONFIG)))
            # the job now lives in Jobs: clear the simulated flag
            cursor.execute("UPDATE SIMULATED_JOBS SET IS_SIMULATED = 0, POSTED_AT = NOW() WHERE JOB_ID = %s", (job_id,))
            conn.commit()
            posted_now = True
            info_log(f"Simulated job {job_id} ('{job_title}') posted to Jobs by {sent_by} "
                     f"(questions={num_questions}, time={interview_time} min)")
        except Exception as e:
            conn.rollback()
            post_error = str(e)
            error_log(f"Posting simulated job {job_id} failed: {e}")

    cursor.close()
    conn.close()

    info_log(f"Invitations for job {job_id}: sent={len(sent)}, failed={len(failed)}, skipped={len(skipped)}"
             + (f", blocked: {blocked_reason}" if blocked_reason else ""))

    parts = [f"{len(sent)} invitation(s) sent for {job_id} - {job_title}."]
    if skipped:
        parts.append(f"{len(skipped)} skipped (" + ", ".join(f"{x['name']}: {x['reason']}" for x in skipped[:5])
                     + (", ..." if len(skipped) > 5 else "") + ").")
    if failed:
        parts.append(f"{len(failed)} failed to send: " + ", ".join(x["name"] or str(x["id"]) for x in failed[:5])
                     + (", ..." if len(failed) > 5 else "") + ".")
        reasons = list(dict.fromkeys(x["reason"] for x in failed))
        parts.append("Reason: " + " ".join(reasons[:2]) + " Failed candidates can be selected and sent again.")
    if posted_now:
        parts.append(f"Simulated job {job_id} has been added to Jobs.")
    elif post_error:
        parts.append(f"The emails were sent, but job {job_id} could not be added to Jobs. Please create it manually.")

    return jsonify({
        "success": bool(sent) and not post_error,
        "message": " ".join(parts),
        "sent": len(sent), "skipped": len(skipped), "failed": len(failed),
        "posted": posted_now or bool(in_jobs),
    })


@app.route("/create-job", methods=["GET", "POST"])
def create_job():
    debug_log(f"Create job - Method: {request.method}")
    
    if "admin" not in session:
        debug_log("No admin in session, redirecting to login")
        return redirect(url_for("login"))

    if request.method == "POST":
        job_id = request.form.get("job_id")
        title = request.form.get("title")
        experience = request.form.get("experience")
        skills = request.form.get("skills")
        description = request.form.get("description")
        num_questions = request.form.get("num_questions") or 30
        interview_time = request.form.get("interview_time") or 30
        try:
            num_postings = max(1, int(request.form.get("num_postings") or 1))
        except ValueError:
            num_postings = 1

        # --- NEW: Extract Rounds and Thresholds ---
        total_rounds = int(request.form.get("total_rounds", 1))
        round_configs = {}
        
        for i in range(1, total_rounds + 1):
            if i == 1:
                # Round 1 is the default pass/pending/fail
                pass_val = request.form.get(f"pass_{i}")
                if pass_val:
                    round_configs[f"round_{i}"] = {
                        "name": request.form.get(f"round_name_1", "Round 1"),
                        "pass": pass_val,
                        "pending_min": request.form.get(f"pending_min_{i}"),
                        "pending_max": request.form.get(f"pending_max_{i}"),
                        "fail": request.form.get(f"fail_{i}")
                    }
            else:
                # Dynamic Rounds (Round 2+) have round_name and marks
                round_name = request.form.get(f"round_name_{i}")
                marks = request.form.get(f"marks_{i}")
                
                # Only add if the round data actually exists (handles deleted rounds)
                if round_name and marks:
                    round_configs[f"round_{i}"] = {
                        "name": round_name,
                        "marks": marks
                    }
            
        round_config_json = json.dumps(round_configs)
        
        debug_log(f"Creating job - ID: {job_id}, Title: {title}, Rounds: {total_rounds}, "
                  f"configured rounds: {list(round_configs)}")
        
        full_description = f"""

Job Description:
{description}
"""
        conn = get_connection()
        cursor = conn.cursor()
        try:
            ensure_job_postings_column(cursor)
            cursor.execute("SELECT ID FROM JOB_INFO WHERE ID = %s", (job_id,))
            existing = cursor.fetchone()

            if existing:
                warning_log(f"Create job rejected: Job ID {job_id} already exists")
                flash("Job ID already exists.", "error")
                return redirect(url_for("create_job"))
        
            # --- UPDATED SQL QUERY to include TOTAL_ROUNDS and ROUND_CONFIG ---
            cursor.execute("""
               INSERT INTO JOB_INFO 
                (ID, JOB_TITLE, JOB_DESCRIPTION, CREATED_AT, NUM_QUESTIONS, INTERVIEW_TIME, TOTAL_ROUNDS, ROUND_CONFIG, NUM_POSTINGS)
                VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s, %s)
            """, (job_id, title, full_description, num_questions, interview_time, total_rounds, round_config_json, num_postings))

            conn.commit()
            info_log(f"Job created: {job_id} ('{title}'), postings={num_postings}, rounds={total_rounds}, questions={num_questions}, "
                     f"time={interview_time} min, by={_request_user()}")
            flash("Job created successfully!", "success")
        except Exception as e:
            error_log(f"Error creating job {job_id} ('{title}'): {e}")
            conn.rollback()
            flash("Job ID already exists or database error.", "error")
            return redirect(url_for("create_job"))
        finally:
            cursor.close()
            conn.close()
            
        return redirect(url_for("create_job"))

    return render_template("create_job.html")


@app.route('/logout')
def logout():
    info_log(f"Logout: user {session.get('admin')} ({session.get('username') or 'anonymous'})")
    session.clear()
    return redirect(url_for('login'))

@app.route("/download/<candidate_id>")
def download_attachment(candidate_id):
    debug_log(f"Downloading attachment for candidate {candidate_id}")
    
    file_data = get_candidate_attachment(candidate_id)

    if not file_data:
        warning_log(f"CV download: no file found for candidate {candidate_id}")
        abort(404)

    file_bytes = bytes(file_data["data"])
    info_log(f"CV downloaded for candidate {candidate_id} ({len(file_bytes)} bytes) by {_request_user()}")

    return send_file(
        BytesIO(file_bytes),
        mimetype="application/pdf",       
        download_name=f"{candidate_id}.pdf",
        as_attachment=True
    )

@app.route("/generate-description", methods=["POST"])
def generate_description():
    debug_log("Generating job description from AI")

    data = request.get_json()
    id = data.get("id", "")
    title = data.get("title", "")
    experience = data.get("experience", "")
    skills = data.get("skills", "")
    interview_time = data.get("interview_time", "")
    num_questions = data.get("num_questions", "")
    location = data.get("location", "")
    employment_type = data.get("employment_type", "")
    budget_range = data.get("budget_range", "")
    certifications = data.get("certifications", "")
    num_postings = str(data.get("num_postings", "") or "").strip()
    prompt = data.get("prompt", "")
    
    
    debug_log(f"Job ID: {id}, Title: {title}, Skills: {skills}")
    interview_time = interview_time if interview_time else "30"
    num_questions = num_questions if num_questions else "30"

    interview_time_line = f"Interview Duration: {interview_time} minutes"
    num_questions_line = f"Number of Interview Questions: {num_questions}"
    certifications_line = f"Mandatory Certifications: {certifications}" if certifications else ""
    location_line = f"Location: {location}" if location else ""
    employment_type_line = f"Employment Type: {employment_type}" if employment_type else ""
    budget_range_line = f"Budget Range: {budget_range}" if budget_range else ""
    num_postings_line = f"Number of Open Positions: {num_postings}" if num_postings else ""

    try:
        combined_prompt = f"""
            Generate a complete professional job description using the details below.

            Job ID: {id}
            Job Title: {title}
            Experience Required: {experience}
            {num_postings_line}
            Required Skills: {skills}
            {num_questions_line}
            {interview_time_line}
            {location_line}
            {employment_type_line}
            {budget_range_line}
            {certifications_line}

            Company Overview:
            SBP Consultancy Private Limited is a dynamic professional services firm headquartered in Hyderabad, specializing in providing innovative business solutions, strategic advisory, and operational support to diverse industries. We are committed to delivering excellence, driving growth, and creating long-term value for our clients. Join our team to be part of a collaborative environment that fosters learning and professional development.

            Additional Context:
            {prompt if prompt else "No additional instructions provided."}
            """

        debug_log(f"Calling AI generation service (prompt {len(combined_prompt)} chars)...")
        with log_duration(f"LLM job description generation for {id or title}", slow_ms=60000):
            generated_text = generate_job_description(combined_prompt)
        info_log(f"AI job description generated for {id or '-'} ('{title}'): "
                 f"{len(str(generated_text or ''))} chars - {short(generated_text, 120)}")
        return jsonify({"generated_text": generated_text})

    except Exception as e:
        error_log(f"AI job description generation failed for {id or '-'} ('{title}'): {e}", exc_info=False)
        return jsonify({"generated_text": "AI generation failed."})

@app.route("/update-status", methods=["POST"])
def update_status():
    # Changing a decision emails the candidate, so only logged-in admins may do it
    if "admin" not in session:
        warning_log("Update status refused: session expired")
        return jsonify({"message": "Your session has expired. Please log in again."}), 401

    data = request.get_json(silent=True) or {}
    candidate_id = data.get("candidate_id")
    action = data.get("action")
    debug_log(f"Update status request - candidate: {candidate_id}, action: {action}")

    if not candidate_id or action not in ("SELECT", "REJECT"):
        warning_log(f"Update status rejected: invalid request (candidate={candidate_id}, action={short(action, 40)})")
        return jsonify({"message": "Invalid request."}), 400

    new_status = "SELECTED" if action == "SELECT" else "REJECTED"

    with log_duration("Gmail service creation", level="DEBUG"):
        service = get_gmail_service()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE CV_INFO
            SET FINAL_STATUS = %s
            WHERE ID = %s
        """, (new_status, candidate_id))

        # keep CURRENT_ROUND in step with the assessment decision: selected -> the job's next round
        # (e.g. Technical) starts; rejected -> the process ends at the assessment
        cursor.execute("""SELECT j.ROUND_CONFIG, c.CURRENT_ROUND FROM CV_INFO c
                          LEFT JOIN JOB_INFO j ON j.ID = c.JOB_ID WHERE c.ID = %s""", (candidate_id,))
        row = cursor.fetchone()
        if row:
            later = sorted(r["number"] for r in parse_round_config(row[0]) if r["number"] > 1)
            current = row[1] or 0
            if new_status == "SELECTED" and later and current <= 1:
                cursor.execute("UPDATE CV_INFO SET CURRENT_ROUND = %s WHERE ID = %s", (later[0], candidate_id))
                info_log(f"Candidate {candidate_id} moves to round {later[0]} after manual selection")
            elif new_status == "REJECTED" and current > 1:
                ensure_round_table(cursor)
                cursor.execute("SELECT COUNT(*) FROM CANDIDATE_ROUND_RESULTS WHERE CV_ID = %s", (candidate_id,))
                if not cursor.fetchone()[0]:      # only if no later round was decided yet
                    cursor.execute("UPDATE CV_INFO SET CURRENT_ROUND = 1 WHERE ID = %s", (candidate_id,))

        log_event(conn, candidate_id, "pending_approval")

        conn.commit()
        info_log(f"Candidate decision: cv_id={candidate_id}, decision={new_status}, by={_request_user()}")

    except Exception as e:
        error_log(f"Saving decision {new_status} for candidate {candidate_id} failed: {e}")
        conn.rollback()
        # don't email the candidate if the decision was not saved
        return jsonify({"message": "The decision could not be saved. Please try again."}), 500
    finally:
        cursor.close()
        conn.close()

    email = get_candidate_email(candidate_id)

    if not email:
            warning_log(f"No email found for candidate {candidate_id}; {new_status} saved but no email sent")
            return jsonify({"message": f"{new_status} updated, but no email was found to notify."})

    try:
            if action == "SELECT":
                debug_log(f"Sending congratulations email to {email} for {candidate_id}")
                send_congratulations_email(service, email, candidate_id)
            else:
                debug_log(f"Sending rejection email to {email} for {candidate_id}")
                send_rejection_email(service, email, candidate_id)
    except Exception as e:
            error_log(f"Failed to send {new_status} email to {email} for candidate {candidate_id}: {e}",
                      exc_info=False)
            return jsonify({"message": f"{new_status} updated, but email dispatch failed."})

    info_log(f"{new_status} email sent to {email} for candidate {candidate_id}")
    return jsonify({"message": f"{new_status} updated and email sent successfully!"})

def get_candidate_email(cv_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT EMAIL
        FROM CV_INFO
        WHERE ID = %s
    """, (cv_id,))

    row = cursor.fetchone()
    debug_log(f"Email for candidate {cv_id}: {row[0] if row else 'not found'}")

    cursor.close()
    conn.close()

    return row[0] if row else None

def _send(service, to_email, subject, body):
    debug_log(f"Sending email to {to_email} with subject: {subject} (body {len(body or '')} chars)")

    msg = MIMEText(body)
    msg["to"] = to_email
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    with log_duration(f"Gmail send to {to_email} ('{short(subject, 80)}')", level="DEBUG", slow_ms=10000):
        service.users().messages().send(
            userId="me",
            body={"raw": raw}
        ).execute()


def send_congratulations_email(service, to_email, candidate_id):
    body = (
        "Hello,\n\n"
        "Congratulations!\n\n"
        "We are pleased to inform you that you have successfully cleared the assessment.\n\n"
        "Our team will reach out to you shortly with the next steps in the interview process.\n\n"
        "Recruitment Team"
    )

    _send(
        service,
        to_email,
        "Congratulations! You Are Shortlisted",
        body
    )


def send_rejection_email(service, to_email, candidate_id):
    body = (
        "Hello,\n\n"
        "Thank you for taking the time to complete the assessment.\n\n"
        "After careful review, we regret to inform you that you have not been selected to proceed further.\n\n"
        "We sincerely appreciate your interest and wish you success in your future endeavors.\n\n"
        "Recruitment Team"
    )

    _send(
        service,
        to_email,
        "Assessment Result",
        body
    )

@app.route("/job_descriptions")
def job_descriptions():
    debug_log(f"Accessing job descriptions for user {session.get('admin')}")
 
    if "admin" not in session:
        debug_log("No admin in session, redirecting to login")
        return redirect(url_for("login"))
 
    conn = get_connection()
    cursor = conn.cursor()

    _query_start = time.perf_counter()
    ensure_job_postings_column(cursor)
    cursor.execute("""
    SELECT j.ID, j.JOB_TITLE, j.JOB_DESCRIPTION, j.NUM_QUESTIONS, j.INTERVIEW_TIME, j.CREATED_AT,
           j.TOTAL_ROUNDS, j.ROUND_CONFIG,
           (SELECT COUNT(*) FROM CV_INFO cv WHERE cv.JOB_ID = j.ID) AS APPLIED_COUNT,
           j.NUM_POSTINGS, j.IS_ACTIVE
    FROM JOB_INFO j
    ORDER BY j.CREATED_AT DESC
        """)

    rows = cursor.fetchall()
    _query_ms = (time.perf_counter() - _query_start) * 1000
    (warning_log if _query_ms > 1500 else debug_log)(
        f"Retrieved {len(rows)} job descriptions in {_query_ms:.0f} ms")
 
    cursor.close()
    conn.close()
 
    jobs = [
        {
            "id": row[0],
            "title": row[1],
            "description": row[2],        # ✅ new field
            "num_questions": row[3],
            "interview_time": row[4],
            "created_at": row[5],         # ✅ shifted index
            "total_rounds": row[6],
            "rounds": parse_round_config(row[7]),
            "applied_count": row[8] or 0,
            "num_postings": row[9] or 1,
            "is_active": row[10] is None or bool(row[10]),
            "numeric_id": int(row[0]) if str(row[0]).isdigit() else 0
        }
        for row in rows
    ]

    jobs = sorted(jobs, key=lambda x: x["numeric_id"])

    # JSON-safe copy of each job for the right-hand details panel
    jobs_data = {
        str(j["id"]): {
            "id": str(j["id"]),
            "title": j["title"] or "",
            "description": (j["description"] or "").strip(),
            "num_questions": j["num_questions"],
            "interview_time": j["interview_time"],
            "created": fmt_ist_datetime(j["created_at"], "db") or "N/A",
            "total_rounds": j["total_rounds"],
            "rounds": j["rounds"],
            "applied_count": j["applied_count"],
            "num_postings": j["num_postings"],
            "is_active": j["is_active"],
        }
        for j in jobs
    }

    return render_template("jd.html", jobs=jobs, jobs_data=jobs_data, now=datetime.now())


@app.route("/applications")
def applications():
    debug_log(f"Accessing applications for user {session.get('admin')}")

    if "admin" not in session:
        return redirect(url_for("login"))

    with log_duration("Applications page data load", level="DEBUG", slow_ms=1500):
        # Same candidate data (and final-status rules) as the Dashboard
        candidates = fetch_dashboard_candidates()

        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT ID, JOB_TITLE, ROUND_CONFIG FROM JOB_INFO")
            job_rows = cursor.fetchall()
            round_results = fetch_round_results(cursor)   # read-only: no commit needed (saves a round trip)
            schedules = fetch_round_schedules(cursor, base_url=public_base_url())
            detail_statuses = fetch_detail_statuses(cursor)
        finally:
            cursor.close()
            conn.close()
        job_titles = {str(r[0]): r[1] for r in job_rows}
        job_rounds = {str(r[0]): parse_round_config(r[2]) for r in job_rows}

        apps = []
        for c in candidates:
            a = build_application(c, job_titles, detail_statuses.get(str(c.get("id"))))
            a["rounds"] = build_round_progress(a, job_rounds.get(a["job_id"], []),
                                               round_results.get(a["id"], {}))
            cand_schedules = schedules.get(a["id"], {})
            for r in a["rounds"]["rounds"]:
                r["schedule"] = fill_scorecard_marks(cand_schedules.get(r["number"]), r["max_marks"])
            apply_overall_status(a, cand_schedules)
            apps.append(a)
    debug_log(f"Applications data: {len(job_rows)} jobs, {len(round_results)} candidates with round results, "
              f"{len(detail_statuses)} detail statuses")

    # Jobs for the filter dropdown: every job, plus any job id only seen on candidates
    job_ids = set(job_titles) | {a["job_id"] for a in apps if a["job_id"]}
    job_options = sorted(
        ({"id": jid, "title": job_titles.get(jid, "")} for jid in job_ids),
        key=lambda j: (0, int(j["id"])) if j["id"].isdigit() else (1, j["id"])
    )

    # round statuses ("Selected for Technical", "Rejected in HR", ...) for the status filter
    static_statuses = {"CV Screening", "Awaiting Details", "Details Incomplete", "Awaiting Interview",
                       "Interview In Progress", "Selected", "Pending", "Rejected", "Violated"}
    round_statuses = sorted({a["overall_status"] for a in apps} - static_statuses)

    debug_log(f"Applications page loaded with {len(apps)} candidates")
    return render_template(
        "applications.html",
        applications=apps,
        job_options=job_options,
        round_statuses=round_statuses,
        selected_job=request.args.get("job", ""),
    )


# ---------------------------------------------------------------------------
# Interview rounds
#   Round 1 is always the online assessment (scored automatically; its outcome is
#   the candidate's FINAL_STATUS and is changed through /update-status).
#   Rounds 2+ come from JOB_INFO.ROUND_CONFIG and are decided by an admin here.
#   Their results are stored in CANDIDATE_ROUND_RESULTS (created on first use).
# ---------------------------------------------------------------------------
_ROUND_TABLE_READY = False


def ensure_round_table(cursor):
    global _ROUND_TABLE_READY
    if _ROUND_TABLE_READY:
        return
    debug_log("Ensuring CANDIDATE_ROUND_RESULTS table exists")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS CANDIDATE_ROUND_RESULTS (
            ID INT AUTO_INCREMENT PRIMARY KEY,
            CV_ID VARCHAR(255) NOT NULL,
            ROUND_NO INT NOT NULL,
            ROUND_NAME VARCHAR(255),
            DECISION VARCHAR(20) NOT NULL,
            MARKS DECIMAL(10,2) NULL,
            REMARKS TEXT,
            DECIDED_BY VARCHAR(100),
            DECIDED_AT DATETIME NOT NULL,
            UNIQUE KEY UQ_CV_ROUND (CV_ID, ROUND_NO)
        )
    """)
    _ROUND_TABLE_READY = True


def fetch_round_results(cursor, cv_id=None):
    """{cv_id: {round_no: {...}}} for every recorded (non-assessment) round decision."""
    ensure_round_table(cursor)
    sql = """SELECT CV_ID, ROUND_NO, ROUND_NAME, DECISION, MARKS, REMARKS, DECIDED_BY, DECIDED_AT
             FROM CANDIDATE_ROUND_RESULTS"""
    params = ()
    if cv_id is not None:
        sql += " WHERE CV_ID = %s"
        params = (cv_id,)
    cursor.execute(sql, params)
    results = {}
    for r in cursor.fetchall():
        marks = r[4]
        if marks is not None:
            marks = float(marks)
            marks = int(marks) if marks.is_integer() else marks
        results.setdefault(str(r[0]), {})[int(r[1])] = {
            "round_no": int(r[1]),
            "round_name": r[2],
            "decision": r[3],
            "marks": marks,
            "remarks": r[5] or "",
            "decided_by": r[6] or "",
            "decided_at": fmt_ist_datetime(r[7], "app"),
        }
    return results


def build_round_progress(a, job_rounds, results):
    """Where a candidate stands across the job's rounds.

    Each round gets a state: passed / failed / current / upcoming / skipped.
    `can_decide` is True only when the current round is a non-assessment round.
    """
    first = next((r for r in job_rounds if r["number"] == 1), None)
    rounds = [{
        "number": 1,
        "name": (first or {}).get("name") or "Assessment",
        "is_assessment": True,
        "max_marks": None,
    }]
    for r in job_rounds:
        if r["number"] > 1:
            try:
                max_marks = float(r.get("marks")) if r.get("marks") not in (None, "") else None
            except (TypeError, ValueError):
                max_marks = None
            if max_marks is not None and max_marks.is_integer():
                max_marks = int(max_marks)
            rounds.append({"number": r["number"], "name": r["name"],
                           "is_assessment": False, "max_marks": max_marks})
    total = len(rounds)
    for i, r in enumerate(rounds, 1):
        r["position"] = i
        r["result"] = None
        r["state"] = "upcoming"

    current = None
    ended = False          # rejected somewhere -> later rounds are skipped
    summary = ""

    assessment = rounds[0]
    if a["cv_status"] != "SHORTLISTED":
        # not in the interview process (yet)
        summary = "Not shortlisted" if a["cv_status"] == "NOT SHORTLISTED" else "CV screening"
        ended = a["cv_status"] == "NOT SHORTLISTED"
        for r in rounds:
            r["state"] = "skipped" if ended else "upcoming"
    else:
        fs = a["final_status"]
        if not a["is_final"] or fs == "PENDING":
            assessment["state"] = "current"
            current = assessment
            assessment["note"] = ("Awaiting assessment decision" if a["is_final"]
                                  else a["stage"])
        elif fs == "SELECTED":
            assessment["state"] = "passed"
        else:
            assessment["state"] = "failed"
            ended = True
            summary = "Ended at " + assessment["name"]

        if assessment["state"] == "passed":
            for r in rounds[1:]:
                res = results.get(r["number"])
                if ended:
                    r["state"] = "skipped"
                elif res and res["decision"] == "PASSED":
                    r["state"] = "passed"
                    r["result"] = res
                elif res and res["decision"] == "REJECTED":
                    r["state"] = "failed"
                    r["result"] = res
                    ended = True
                    summary = "Rejected in " + r["name"]
                elif current is None:
                    r["state"] = "current"
                    current = r
            if not ended and current is None:
                summary = "All rounds cleared"
        elif ended:
            for r in rounds[1:]:
                r["state"] = "skipped"

    if current is not None:
        summary = "Round %d of %d" % (current["position"], total)

    # the most recent manual decision can be undone (only if nothing came after it)
    decided = [r for r in rounds[1:] if r["result"]]
    undoable = decided[-1]["number"] if decided and (current is None or current["number"] > decided[-1]["number"]) else None

    return {
        "rounds": rounds,
        "total": total,
        "current": current,
        "current_name": current["name"] if current else None,
        "summary": summary,
        "can_decide": bool(current and not current["is_assessment"]),
        "undoable_round": undoable,
    }


def apply_overall_status(a, cand_schedules=None):
    """Overall processing status across every round, plus the segments of the status bar.

    a["status_label"] / a["final_status"] stay the assessment result. Once the assessment is
    passed and the job has more rounds, the overall status follows those rounds:
    "Selected for Technical", "Technical Scheduled", "Rejected in HR", and "Selected"
    only after the last round is cleared.
    Sets overall_status, overall_tone (a colour key), overall_final and progress.
    """
    rp = a["rounds"]
    rounds = rp["rounds"]
    later = rounds[1:]
    fs = a["final_status"]
    cand_schedules = cand_schedules or {}

    label, final = a["status_label"], a["is_final"]
    tone = fs if final else a["stage"]
    if rounds[0]["state"] == "passed" and later:
        failed = next((r for r in later if r["state"] == "failed"), None)
        current = rp["current"]
        if failed:
            label, tone, final = "Rejected in " + failed["name"], "REJECTED", True
        elif current:
            sch = cand_schedules.get(current["number"])
            sch_status = sch.get("status") if sch else None
            if sch_status == "SUBMITTED":      # interviewer's scorecard is in, waiting for the admin
                label, tone = current["name"] + " Approval Pending", "APPROVAL"
            else:
                label = (current["name"] + " Scheduled" if sch_status == "SCHEDULED"
                         else "Selected for " + current["name"])
                tone = "ROUND"
            final = False
        else:
            label, tone, final = "Selected", "SELECTED", True

    # status bar: Applied, CV screening, then one segment per round
    cv_state = ("failed" if a["stage"] == "CV Rejected" else
                "done" if a["stage_step"] >= 2 else "current")
    segments = [{"label": "Applied", "state": "done"}, {"label": "CV screening", "state": cv_state}]
    for r in rounds:
        state = {"passed": "done", "failed": "failed", "current": "current"}.get(r["state"], "todo")
        if r["is_assessment"]:
            if state == "failed" and fs in ("VIOLATED", "NOT APPEARED"):
                state = fs.lower().replace(" ", "_")
            elif state == "current" and a["is_final"] and fs == "PENDING":
                state = "pending"
        elif state == "current" and tone == "APPROVAL":
            state = "pending"
        segments.append({"label": r["name"], "state": state})
    if final and label == "Selected":
        for s in segments:
            s["state"] = "cleared"
    state_text = {"done": "done", "cleared": "cleared", "current": "in progress", "failed": "rejected",
                  "pending": "approval pending" if tone == "APPROVAL" else "decision pending", "violated": "violated", "not_appeared": "not appeared",
                  "todo": "not reached"}
    for s in segments:
        s["title"] = s["label"] + ": " + state_text[s["state"]]

    # one score per round: the assessment %, then the marks of every decided interview round
    scores = []
    if a["exam_score"] not in (None, "", "N/A"):
        scores.append({"round": rounds[0]["name"], "score": a["exam_score"], "state": rounds[0]["state"]})
    for r in later:
        res = r.get("result")
        if res and res.get("marks") is not None:
            scores.append({"round": r["name"],
                           "score": f"{res['marks']}/{r['max_marks']}" if r["max_marks"] else str(res["marks"]),
                           "state": r["state"]})

    a.update(overall_status=label, overall_tone=tone, overall_final=final, progress=segments,
             round_scores=scores)
    return a


def fetch_assessment_answers(cursor, cv_id, max_chars=600):
    """The candidate's online-assessment questions with their answers and scores.

    Returns (questions, total score, max score, answered count); each answer is scored out of 5.
    """
    cursor.execute("""
        SELECT q.QUESTION, r.RESPONSE, r.SCORE
        FROM QUESTIONS_TABLE q
        LEFT JOIN RESPONSE_TABLE r ON r.QUES_ID = q.QUES_ID AND r.ID = q.ID
        WHERE q.ID = %s
        ORDER BY q.QUES_ID
    """, (cv_id,))
    questions = []
    total = 0.0
    for question, response, score in cursor.fetchall():
        answer = (response or "").strip()
        total += float(score or 0)
        questions.append({
            "question": question or "",
            "answer": answer[:max_chars] + ("…" if len(answer) > max_chars else ""),
            "answered": bool(answer),
            "score": _num(score),
        })
    answered = sum(q["answered"] for q in questions)
    return questions, total, len(questions) * 5, answered


def _num(value):
    """DB number -> int when whole, else float (None stays None)."""
    if value is None:
        return None
    value = float(value)
    return int(value) if value.is_integer() else round(value, 2)


@app.route("/round-details/<candidate_id>/<int:round_no>")
def round_details(candidate_id, round_no):
    """Round information plus how the candidate did in it (JSON for the Applications panel)."""
    if "admin" not in session:
        warning_log(f"Round details for {candidate_id} refused: not signed in")
        return jsonify({"message": "Please log in again."}), 401

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT c.JOB_ID, j.ROUND_CONFIG, j.NUM_QUESTIONS, j.INTERVIEW_TIME,
                   t.candidate_login, t.image_taken, t.start_exam, t.end_exam, t.response_submit,
                   c.FINAL_STATUS, (c.FACE_IMAGE IS NOT NULL)
            FROM CV_INFO c
            LEFT JOIN JOB_INFO j ON j.ID = c.JOB_ID
            LEFT JOIN CV_TIMESTAMPS t ON t.ID = c.ID
            WHERE c.ID = %s
        """, (candidate_id,))
        row = cursor.fetchone()
        if not row:
            warning_log(f"Round details: candidate {candidate_id} not found")
            return jsonify({"message": "Candidate not found."}), 404

        config = {r["number"]: r for r in parse_round_config(row[1])}
        cfg = config.get(round_no) or {}
        info = {"number": round_no, "name": cfg.get("name") or ("Assessment" if round_no == 1 else f"Round {round_no}")}

        if round_no != 1:
            # admin-decided round: configured marks + the recorded decision
            try:
                info["max_marks"] = _num(cfg.get("marks")) if cfg.get("marks") not in (None, "") else None
            except (TypeError, ValueError):
                info["max_marks"] = None
            result = fetch_round_results(cursor, candidate_id).get(candidate_id, {}).get(round_no)
            if result and result["marks"] is not None and info["max_marks"]:
                result["percent"] = round(result["marks"] * 100 / info["max_marks"])
            schedule = fill_scorecard_marks(fetch_round_schedules(cursor, candidate_id).get(candidate_id, {}).get(round_no),
                                            info.get("max_marks"))
            debug_log(f"Round details served: {candidate_id} round {round_no} ({'decided' if result else 'no decision'}, "
                      f"{schedule['status'].lower() if schedule else 'not scheduled'})")
            return jsonify({"round": info, "result": result, "schedule": schedule})

        # ---- Round 1: the online assessment ----
        info["num_questions"] = row[2]
        info["interview_time"] = row[3]
        info["criteria"] = {k: cfg.get(k) for k in ("pass", "pending_min", "pending_max", "fail")
                            if cfg.get(k) not in (None, "")}

        # Questions are generated before the exam: only reveal them once the candidate has attempted it
        attempted = bool(row[6] or row[7] or row[8])
        if not attempted:
            debug_log(f"Round details served: {candidate_id} assessment not attempted (questions hidden)")
            return jsonify({"round": info, "performance": {"taken": False}})

        questions, total, max_total, answered = fetch_assessment_answers(cursor, candidate_id)

        violations = []
        try:
            ensure_violation_snapshot_column(cursor)
            cursor.execute("""SELECT VIOLATION_ID, EVENT_TYPE, EVENT_TIME, SNAPSHOT IS NOT NULL
                              FROM EXAM_VIOLATIONS WHERE ID = %s ORDER BY VIOLATION_ID""", (candidate_id,))
            violations = [{"id": v[0], "type": v[1] or "Unknown",
                           "time": fmt_ist_millis(v[2], "db"), "elapsed": fmt_elapsed(v[2], row[6]),
                           "has_image": bool(v[3])}
                          for v in cursor.fetchall()]
        except Exception as e:
            warning_log(f"Round details: violations not available for {candidate_id}: {e}")

        started, ended, submitted = to_ist(row[6]), to_ist(row[7]), to_ist(row[8])
        finish = submitted or ended
        duration = None
        if started and finish and finish >= started:
            secs = int((finish - started).total_seconds())
            duration = f"{secs // 60} min {secs % 60:02d} sec"
        final = str(row[9] or "").upper()
        if final == "VIOLATED":
            how = "Terminated - violation limit reached"
        elif submitted:
            how = "Submitted by the candidate"
        elif ended:
            how = "Ended (time up / closed)"
        elif started:
            how = "In progress"
        else:
            how = "Not started"

        performance = {
            "taken": True,
            "score_percent": round(total * 100 / max_total) if max_total else None,
            "total_score": _num(total) if questions else None,
            "max_score": max_total,
            "answered": answered,
            "skipped": len(questions) - answered,
            "questions": questions,
            "login": fmt_ist_seconds(row[4]),
            "photo_verified": fmt_ist_seconds(row[5]),
            "started": fmt_ist_seconds(row[6]),
            "ended": fmt_ist_seconds(row[7]),
            "submitted": fmt_ist_seconds(row[8]),
            "duration": duration,
            "how_ended": how,
            "violations": violations,
        }
        debug_log(f"Round details served: {candidate_id} assessment ({len(questions)} questions, "
                  f"{answered} answered, {len(violations)} violations)")
        return jsonify({"round": info, "performance": performance})
    except Exception as e:
        error_log(f"Round details failed for {candidate_id} round {round_no}: {e}")
        return jsonify({"message": "Could not load the round details."}), 500
    finally:
        cursor.close()
        conn.close()


@app.route("/violation-snapshot/<int:violation_id>")
def violation_snapshot(violation_id):
    """Camera frame stored with one proctoring violation (signed-in admins only)."""
    if "admin" not in session:
        return "Unauthorized", 401
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT SNAPSHOT FROM EXAM_VIOLATIONS WHERE VIOLATION_ID = %s", (violation_id,))
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not row or not row[0]:
        return "No image found", 404
    return Response(bytes(row[0]), mimetype="image/jpeg")


@app.route("/round-decision", methods=["POST"])
def round_decision():
    """Record (or undo) an admin decision for a candidate's current non-assessment round."""
    if "admin" not in session:
        warning_log("Round decision refused: not signed in")
        return jsonify({"message": "Please log in again."}), 401

    data = request.get_json(silent=True) or {}
    cv_id = str(data.get("candidate_id") or "").strip()
    action = str(data.get("action") or "").upper()
    try:
        round_no = int(data.get("round_no"))
    except (TypeError, ValueError):
        warning_log(f"Round decision for {cv_id} rejected: invalid round {short(data.get('round_no'), 20)}")
        return jsonify({"message": "Invalid round."}), 400
    if not cv_id or action not in ("PASS", "REJECT", "UNDO"):
        warning_log(f"Round decision rejected: invalid request (cv_id='{cv_id}', action={short(action, 20)})")
        return jsonify({"message": "Invalid request."}), 400
    debug_log(f"Round decision request: cv_id={cv_id}, round={round_no}, action={action}")

    candidate = next((c for c in fetch_dashboard_candidates() if str(c.get("id")) == cv_id), None)
    if candidate is None:
        warning_log(f"Round decision: candidate {cv_id} not found")
        return jsonify({"message": "Candidate not found."}), 404

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT JOB_TITLE, ROUND_CONFIG FROM JOB_INFO WHERE ID = %s", (candidate.get("job_id"),))
        job = cursor.fetchone()
        job_rounds = parse_round_config(job[1]) if job else []
        a = build_application(candidate, {})
        progress = build_round_progress(a, job_rounds, fetch_round_results(cursor, cv_id).get(cv_id, {}))
        target = next((r for r in progress["rounds"] if r["number"] == round_no), None)
        if target is None or target["is_assessment"]:
            warning_log(f"Round decision for {cv_id} rejected: round {round_no} is "
                        f"{'not configured' if target is None else 'the assessment round'}")
            return jsonify({"message": "The assessment round is decided from the Assessment Decision section."}), 400

        who = candidate.get("name") or cv_id
        if action == "UNDO":
            if progress["undoable_round"] != round_no:
                warning_log(f"Round decision undo for {cv_id} rejected: round {round_no} is not the most recent "
                            f"decision (undoable: {progress['undoable_round']})")
                return jsonify({"message": "Only the most recent round decision can be undone."}), 409
            cursor.execute("DELETE FROM CANDIDATE_ROUND_RESULTS WHERE CV_ID = %s AND ROUND_NO = %s",
                           (cv_id, round_no))
            cursor.execute("UPDATE CV_INFO SET CURRENT_ROUND = %s WHERE ID = %s", (round_no, cv_id))
            # the round's interview opens again: an approved scorecard goes back to "awaiting approval";
            # without a scorecard the interviewer's link works again (if it has not expired)
            ensure_schedule_table(cursor)
            cursor.execute("""UPDATE ROUND_SCHEDULES
                              SET STATUS = CASE WHEN SCORES IS NULL THEN 'SCHEDULED' ELSE 'SUBMITTED' END
                              WHERE CV_ID = %s AND ROUND_NO = %s AND STATUS = 'COMPLETED'""", (cv_id, round_no))
            conn.commit()
            info_log(f"Round decision undone: cv_id={cv_id}, round={round_no} ('{target['name']}'), "
                     f"by={_request_user()}")
            return jsonify({"message": f"Decision for {target['name']} was undone for {who}."})

        if not progress["can_decide"] or progress["current"]["number"] != round_no:
            warning_log(f"Round decision for {cv_id} rejected: round {round_no} is not the current round "
                        f"(current: {progress['current']['number'] if progress['current'] else None})")
            return jsonify({"message": "This round is not the candidate's current round."}), 409

        marks = data.get("marks")
        if marks in (None, ""):
            marks = None
        else:
            try:
                marks = float(marks)
            except (TypeError, ValueError):
                warning_log(f"Round decision for {cv_id} rejected: marks not a number ({short(marks, 20)})")
                return jsonify({"message": "Marks must be a number."}), 400
            if marks < 0 or (target["max_marks"] is not None and marks > float(target["max_marks"])):
                warning_log(f"Round decision for {cv_id} rejected: marks {marks} outside 0-{target['max_marks']}")
                return jsonify({"message": f"Marks must be between 0 and {target['max_marks']}."}), 400
        remarks = str(data.get("remarks") or "").strip()[:2000]
        decision = "PASSED" if action == "PASS" else "REJECTED"
        decided_by = str(session.get("username") or session.get("admin"))

        next_round = record_round_result(cursor, cv_id, progress, target, decision, marks, remarks, decided_by)
        conn.commit()
        info_log(f"Round decision: cv_id={cv_id}, round={round_no} ('{target['name']}'), decision={decision}, "
                 f"marks={marks}, by={decided_by}"
                 + (f", next round={next_round['number']}" if decision == "PASSED" and next_round else ""))
    except Exception as e:
        conn.rollback()
        error_log(f"Round decision failed for {cv_id} (round {round_no}, action {action}): {e}")
        return jsonify({"message": "The round decision could not be saved. Please try again."}), 500
    finally:
        cursor.close()
        conn.close()

    if decision == "PASSED":
        msg = f"{who} passed {target['name']}" + (f" and moves to {next_round['name']}." if next_round else ". All rounds cleared.")
        # the candidate hears they cleared the round only now, after the admin's approval
        if send_round_cleared_email(a, target["name"], next_round, job[0] if job else ""):
            msg += " The candidate has been emailed."
        else:
            msg += " The email to the candidate could not be sent."
    else:
        msg = f"{who} was rejected in {target['name']}."
    return jsonify({"message": msg})


def record_round_result(cursor, cv_id, progress, target, decision, marks, remarks, decided_by, scores=None):
    """Save a PASSED/REJECTED result for the candidate's current round; returns the next round (or None).

    Moves CV_INFO.CURRENT_ROUND on after a pass and closes the round's interview schedule
    (so its scorecard link stops working). The caller commits.
    """
    cursor.execute("""
        INSERT INTO CANDIDATE_ROUND_RESULTS
            (CV_ID, ROUND_NO, ROUND_NAME, DECISION, MARKS, REMARKS, DECIDED_BY, DECIDED_AT)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (cv_id, target["number"], target["name"], decision, marks, remarks, decided_by, datetime.now()))

    # keep CV_INFO.CURRENT_ROUND in step: move on to the next round after a pass
    next_round = next((r for r in progress["rounds"] if r["position"] == target["position"] + 1), None)
    if decision == "PASSED" and next_round:
        cursor.execute("UPDATE CV_INFO SET CURRENT_ROUND = %s WHERE ID = %s", (next_round["number"], cv_id))

    # close the round's interview: a scheduled one (scorecard link stops working) or one whose
    # scorecard was submitted and is now approved (its scores are kept)
    ensure_schedule_table(cursor)
    cursor.execute("""UPDATE ROUND_SCHEDULES SET STATUS = 'COMPLETED', SCORES = COALESCE(%s, SCORES),
                          SUBMITTED_AT = COALESCE(%s, SUBMITTED_AT)
                      WHERE CV_ID = %s AND ROUND_NO = %s AND STATUS IN ('SCHEDULED', 'SUBMITTED')""",
                   (json.dumps(scores) if scores else None, datetime.now() if scores else None,
                    cv_id, target["number"]))
    return next_round


def send_round_cleared_email(a, round_name, next_round, job_title):
    """Tell the candidate they cleared a round (sent when an admin passes / approves it). Returns True if sent."""
    if not a.get("email"):
        return False
    nxt = (f"Our team will contact you shortly about the next round ({next_round['name']})."
           if next_round else "Our team will contact you shortly about the next steps.")
    try:
        _send_mail(get_gmail_service(), a["email"], f"{round_name} Round Cleared - {job_title or 'Your Application'}",
                   f"Dear {a['name'] or 'Candidate'},\n\nCongratulations! You have cleared the {round_name} round "
                   f"for {job_title or 'the position'}.\n\n{nxt}\n\nRegards,\nRecruitment Team")
        info_log(f"Round cleared email sent to {a['email']} for {a['id']} ({round_name})")
        return True
    except Exception as e:
        error_log(f"Round cleared email to {a['email']} failed for {a['id']}: {e}", exc_info=False)
        return False


# ---------------------------------------------------------------------------
# Interview scheduling + interviewer scorecard (rounds 2+)
#   The admin schedules the candidate's current round from Applications; the candidate
#   and the interviewer are emailed. The interviewer rates the candidate on a scorecard
#   opened from a private link (no login); submitting it records the round result.
# ---------------------------------------------------------------------------
SCORECARD_CRITERIA = [
    {"key": "technical", "name": "Technical knowledge", "hint": "Core language, frameworks and concepts for the role"},
    {"key": "problem_solving", "name": "Problem solving", "hint": "Breaks down a problem and reasons toward a solution"},
    {"key": "coding", "name": "Coding", "hint": "Writes correct, readable code during the interview"},
    {"key": "communication", "name": "Communication", "hint": "Explains their thinking clearly"},
]
SCORECARD_LINK_DAYS = 7          # the scorecard link works until 7 days after the interview
INTERVIEW_MODES = {"ONLINE": "Online", "IN_PERSON": "In person"}
_SCHEDULE_TABLE_READY = False


def ensure_schedule_table(cursor):
    global _SCHEDULE_TABLE_READY
    if _SCHEDULE_TABLE_READY:
        return
    debug_log("Ensuring ROUND_SCHEDULES table exists")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ROUND_SCHEDULES (
            ID INT AUTO_INCREMENT PRIMARY KEY,
            CV_ID VARCHAR(255) NOT NULL,
            ROUND_NO INT NOT NULL,
            ROUND_NAME VARCHAR(255),
            SCHEDULED_AT DATETIME NOT NULL,
            DURATION_MIN INT NOT NULL DEFAULT 45,
            MODE VARCHAR(20) NOT NULL,
            LOCATION VARCHAR(500),
            INTERVIEWER_NAME VARCHAR(255) NOT NULL,
            INTERVIEWER_EMAIL VARCHAR(255) NOT NULL,
            NOTE TEXT,
            STATUS VARCHAR(20) NOT NULL,
            TOKEN VARCHAR(64) NOT NULL,
            TOKEN_EXPIRES DATETIME NOT NULL,
            SCORES TEXT,
            SUBMITTED_AT DATETIME NULL,
            CREATED_BY VARCHAR(100),
            UPDATED_AT DATETIME NOT NULL,
            UNIQUE KEY UQ_SCHEDULE_ROUND (CV_ID, ROUND_NO),
            UNIQUE KEY UQ_SCHEDULE_TOKEN (TOKEN)
        )
    """)
    _SCHEDULE_TABLE_READY = True


def fmt_schedule_time(start, duration):
    """Naive IST datetime + minutes -> 'Wed, 25 Nov 2026, 11:00 AM - 11:45 AM IST'."""
    end = start + timedelta(minutes=duration or 0)
    return f"{start.strftime('%a, %d %b %Y, %I:%M %p')} - {end.strftime('%I:%M %p')} IST"


def public_base_url():
    """Base URL that other machines can open (the admin may be browsing on localhost)."""
    base = request.host_url.rstrip("/")
    host = request.host.split(":")[0]
    if host in ("localhost", "127.0.0.1", "::1"):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            base = f"{request.scheme}://{s.getsockname()[0]}:{ADMIN_APP_PORT}"
        except OSError:
            pass
        finally:
            s.close()
    return base


def schedule_from_row(r, base_url=None):
    """ROUND_SCHEDULES row (in SCHEDULE_COLUMNS order) -> JSON-safe dict."""
    scores = None
    if r[13]:
        try:
            scores = json.loads(r[13])
        except (TypeError, ValueError):
            scores = None
    return {
        "round_no": int(r[1]),
        "when": fmt_schedule_time(r[3], r[4]),
        "date": r[3].strftime("%Y-%m-%d"),
        "time": r[3].strftime("%H:%M"),
        "duration": r[4],
        "mode": r[5],
        "mode_label": INTERVIEW_MODES.get(r[5], r[5]),
        "location": r[6] or "",
        "interviewer_name": r[7],
        "interviewer_email": r[8],
        "note": r[9] or "",
        "status": r[10],
        "scorecard_url": f"{base_url}/scorecard/{r[11]}" if base_url and r[10] == "SCHEDULED" else None,
        "link_expired": r[12] < datetime.now(),
        "scores": scores,
        "submitted_at": r[14].strftime(DATETIME_FMT) if r[14] else None,
    }


SCHEDULE_COLUMNS = ("CV_ID, ROUND_NO, ROUND_NAME, SCHEDULED_AT, DURATION_MIN, MODE, LOCATION, INTERVIEWER_NAME, "
                    "INTERVIEWER_EMAIL, NOTE, STATUS, TOKEN, TOKEN_EXPIRES, SCORES, SUBMITTED_AT")


def fetch_round_schedules(cursor, cv_id=None, base_url=None):
    """{cv_id: {round_no: schedule}} for every interview that is scheduled or done (not cancelled)."""
    ensure_schedule_table(cursor)
    sql = f"SELECT {SCHEDULE_COLUMNS} FROM ROUND_SCHEDULES WHERE STATUS <> 'CANCELLED'"
    params = ()
    if cv_id is not None:
        sql += " AND CV_ID = %s"
        params = (cv_id,)
    cursor.execute(sql, params)
    result = {}
    for r in cursor.fetchall():
        result.setdefault(str(r[0]), {})[int(r[1])] = schedule_from_row(r, base_url)
    return result


def fill_scorecard_marks(sched, max_marks):
    """Scorecards submitted before marks were saved with them: work the marks out from the ratings."""
    scores = (sched or {}).get("scores")
    if scores and scores.get("ratings") and scores.get("marks") is None:
        scores["marks"], _ = scorecard_marks(scores["ratings"], max_marks)
        scores["max_marks"] = max_marks
    return sched


def scorecard_marks(ratings, max_marks):
    """Sum of 1-5 ratings scaled to the round's marks (to 0.5); out of 5 x criteria when no marks are set."""
    raw = sum(ratings.values())
    top = 5 * len(SCORECARD_CRITERIA)
    if not max_marks:
        return raw, top
    marks = round(raw / top * float(max_marks) * 2) / 2
    return (int(marks) if float(marks).is_integer() else marks), max_marks


def _send_mail(service, to_email, subject, body, attachment=None):
    """Plain-text email, optionally with one (filename, bytes, subtype) attachment."""
    if attachment:
        from email.mime.multipart import MIMEMultipart
        from email.mime.application import MIMEApplication
        msg = MIMEMultipart()
        msg.attach(MIMEText(body))
        part = MIMEApplication(attachment[1], _subtype=attachment[2])
        part.add_header("Content-Disposition", "attachment", filename=attachment[0])
        msg.attach(part)
    else:
        msg = MIMEText(body)
    msg["to"] = to_email
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    with log_duration(f"Gmail send to {to_email} ('{short(subject, 80)}')", level="DEBUG", slow_ms=10000):
        service.users().messages().send(userId="me", body={"raw": raw}).execute()


def candidate_interview_email(kind, name, cv_id, job_title, job_id, round_name, sched):
    """(subject, body) for the candidate: kind = new / updated / cancelled."""
    greeting = f"Dear {name or 'Candidate'},\n\n"
    job = f"{job_title} (Job {job_id})" if job_title else f"Job {job_id}"
    if kind == "cancelled":
        return (f"{round_name} Interview Cancelled - {job_title or job_id}",
                greeting + f"Your {round_name} interview for {job}, scheduled for {sched['when']}, has been cancelled.\n\n"
                "We will contact you if it is rescheduled.\n\nRegards,\nRecruitment Team")
    where = (f"Meeting link: {sched['location']}" if sched["mode"] == "ONLINE"
             else f"Venue: {sched['location']}") if sched["location"] else ""
    intro = ("Your interview has been rescheduled. The new details are below.\n\n" if kind == "updated" else
             f"You are invited to the {round_name} interview for {job}.\n\n")
    body = (greeting + intro +
            f"Round: {round_name}\nDate & time: {sched['when']}\nMode: {sched['mode_label']}\n" +
            (where + "\n" if where else "") +
            (f"\n{sched['note']}\n" if sched["note"] else "") +
            "\nRegards,\nRecruitment Team")
    subject = (f"{round_name} Interview " + ("Rescheduled" if kind == "updated" else "Scheduled")
               + f" - {job_title or job_id}")
    return subject, body


def interviewer_email(kind, cand, round_name, sched, scorecard_url):
    """(subject, body) for the interviewer: kind = new / updated / cancelled."""
    who = cand["name"] or cand["id"]
    if kind == "cancelled":
        return (f"Cancelled: {round_name} interview with {who}",
                f"Hi {sched['interviewer_name']},\n\nThe {round_name} interview with {who} on {sched['when']} "
                "has been cancelled. The scorecard link no longer works.\n\nRecruitment Team")
    where = (f"Meeting link: {sched['location']}" if sched["mode"] == "ONLINE"
             else f"Venue: {sched['location']}") if sched["location"] else ""
    body = (f"Hi {sched['interviewer_name']},\n\n" +
            ("The interview below has been rescheduled.\n\n" if kind == "updated"
             else f"You are the interviewer for this candidate's {round_name} round.\n\n") +
            f"Candidate: {who} ({cand['id']})\nJob: {cand['job_title'] or ''} ({cand['job_id']})\n"
            f"Assessment: {cand['assessment']}\nDate & time: {sched['when']}\nMode: {sched['mode_label']}\n" +
            (where + "\n" if where else "") +
            ("\nThe candidate's CV is attached.\n" if cand.get("has_cv") else "") +
            f"\nAfter the interview, fill in the scorecard here:\n{scorecard_url}\n\n"
            f"The link works only for this candidate and round, and expires {SCORECARD_LINK_DAYS} days after the interview. "
            "No login is needed.\n\nRecruitment Team")
    subject = (("Rescheduled: " if kind == "updated" else "") +
               f"{round_name} interview: {who} - {sched['when'].split(' - ')[0]}")
    return subject, body


def _candidate_round_context(cursor, cv_id):
    """(candidate dict, progress) for a candidate, or (None, None)."""
    candidate = next((c for c in fetch_dashboard_candidates() if str(c.get("id")) == cv_id), None)
    if candidate is None:
        return None, None
    cursor.execute("SELECT JOB_TITLE, ROUND_CONFIG FROM JOB_INFO WHERE ID = %s", (candidate.get("job_id"),))
    job = cursor.fetchone()
    a = build_application(candidate, {str(candidate.get("job_id") or ""): job[0] if job else ""})
    progress = build_round_progress(a, parse_round_config(job[1]) if job else [],
                                    fetch_round_results(cursor, cv_id).get(cv_id, {}))
    return a, progress


@app.route("/schedule-round", methods=["POST"])
def schedule_round():
    """Schedule, reschedule or cancel the interview for a candidate's current round; emails both sides."""
    if "admin" not in session:
        warning_log("Schedule round refused: not signed in")
        return jsonify({"message": "Please log in again."}), 401

    data = request.get_json(silent=True) or {}
    cv_id = str(data.get("candidate_id") or "").strip()
    action = str(data.get("action") or "SCHEDULE").upper()
    try:
        round_no = int(data.get("round_no"))
    except (TypeError, ValueError):
        return jsonify({"message": "Invalid round."}), 400
    if not cv_id or action not in ("SCHEDULE", "CANCEL"):
        return jsonify({"message": "Invalid request."}), 400
    debug_log(f"Schedule round request: cv_id={cv_id}, round={round_no}, action={action}")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        ensure_round_table(cursor)
        ensure_schedule_table(cursor)
        a, progress = _candidate_round_context(cursor, cv_id)
        if a is None:
            return jsonify({"message": "Candidate not found."}), 404
        cur = progress["current"]
        if not progress["can_decide"] or cur["number"] != round_no:
            warning_log(f"Schedule round for {cv_id} rejected: round {round_no} is not the current interview round")
            return jsonify({"message": "Only the candidate's current interview round can be scheduled."}), 409

        cursor.execute(f"SELECT {SCHEDULE_COLUMNS} FROM ROUND_SCHEDULES WHERE CV_ID = %s AND ROUND_NO = %s",
                       (cv_id, round_no))
        existing = cursor.fetchone()
        active = existing if existing and existing[10] == "SCHEDULED" else None
        if existing and existing[10] == "SUBMITTED":
            warning_log(f"Schedule round for {cv_id} rejected: scorecard for round {round_no} awaits approval")
            return jsonify({"message": "The interviewer has already submitted the scorecard. "
                                       "Approve or reject the recommendation first."}), 409

        if action == "CANCEL":
            if not active:
                return jsonify({"message": "There is no scheduled interview to cancel."}), 409
            sched = schedule_from_row(active)
            cursor.execute("""UPDATE ROUND_SCHEDULES SET STATUS = 'CANCELLED', UPDATED_AT = %s
                              WHERE CV_ID = %s AND ROUND_NO = %s""", (datetime.now(), cv_id, round_no))
            conn.commit()
            info_log(f"Interview cancelled: cv_id={cv_id}, round={round_no} ('{cur['name']}'), by={_request_user()}")
            kind = "cancelled"
        else:
            # ---- validate the form ----
            try:
                start = datetime.strptime(f"{data.get('date')} {data.get('time')}", "%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                return jsonify({"message": "Choose a valid date and time."}), 400
            if start < datetime.now() - timedelta(minutes=5):
                return jsonify({"message": "The interview time is in the past."}), 400
            try:
                duration = int(data.get("duration") or 45)
            except (TypeError, ValueError):
                duration = 45
            duration = max(10, min(duration, 480))
            mode = str(data.get("mode") or "ONLINE").upper()
            if mode not in INTERVIEW_MODES:
                mode = "ONLINE"
            location = str(data.get("location") or "").strip()[:500]
            iname = str(data.get("interviewer_name") or "").strip()[:255]
            iemail = str(data.get("interviewer_email") or "").strip()[:255]
            note = str(data.get("note") or "").strip()[:1000]
            if not iname:
                return jsonify({"message": "Enter the interviewer's name."}), 400
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", iemail):
                return jsonify({"message": "Enter a valid interviewer email."}), 400
            if not location:
                return jsonify({"message": "Enter the meeting link." if mode == "ONLINE" else "Enter the venue."}), 400

            token = secrets.token_urlsafe(24)
            expires = start + timedelta(days=SCORECARD_LINK_DAYS)
            now = datetime.now()
            kind = "updated" if active else "new"
            if existing:
                cursor.execute("""
                    UPDATE ROUND_SCHEDULES SET ROUND_NAME=%s, SCHEDULED_AT=%s, DURATION_MIN=%s, MODE=%s, LOCATION=%s,
                        INTERVIEWER_NAME=%s, INTERVIEWER_EMAIL=%s, NOTE=%s, STATUS='SCHEDULED', TOKEN=%s,
                        TOKEN_EXPIRES=%s, SCORES=NULL, SUBMITTED_AT=NULL, CREATED_BY=%s, UPDATED_AT=%s
                    WHERE CV_ID=%s AND ROUND_NO=%s
                """, (cur["name"], start, duration, mode, location, iname, iemail, note, token, expires,
                      _request_user(), now, cv_id, round_no))
            else:
                cursor.execute("""
                    INSERT INTO ROUND_SCHEDULES (CV_ID, ROUND_NO, ROUND_NAME, SCHEDULED_AT, DURATION_MIN, MODE, LOCATION,
                        INTERVIEWER_NAME, INTERVIEWER_EMAIL, NOTE, STATUS, TOKEN, TOKEN_EXPIRES, CREATED_BY, UPDATED_AT)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'SCHEDULED', %s, %s, %s, %s)
                """, (cv_id, round_no, cur["name"], start, duration, mode, location, iname, iemail, note,
                      token, expires, _request_user(), now))
            conn.commit()
            cursor.execute(f"SELECT {SCHEDULE_COLUMNS} FROM ROUND_SCHEDULES WHERE CV_ID = %s AND ROUND_NO = %s",
                           (cv_id, round_no))
            sched = schedule_from_row(cursor.fetchone(), public_base_url())
            info_log(f"Interview {'rescheduled' if kind == 'updated' else 'scheduled'}: cv_id={cv_id}, round={round_no} "
                     f"('{cur['name']}'), at={start:%Y-%m-%d %H:%M}, interviewer={iemail}, by={_request_user()}")
    except Exception as e:
        conn.rollback()
        error_log(f"Schedule round failed for {cv_id} (round {round_no}, {action}): {e}")
        return jsonify({"message": "The interview could not be saved. Please try again."}), 500
    finally:
        cursor.close()
        conn.close()

    # ---- emails (the schedule is already saved) ----
    assessment = f"{a['exam_score'] or 'N/A'} - {a['final_status'].title()}"   # exam_score is already like "78%"
    cand = {"id": cv_id, "name": a["name"], "job_title": a["job_title"], "job_id": a["job_id"], "assessment": assessment}
    attachment = None
    if kind != "cancelled":
        try:
            cv_file = get_candidate_attachment(cv_id)
            if cv_file and cv_file.get("data"):
                attachment = (f"{cv_id}.pdf", bytes(cv_file["data"]), "pdf")
                cand["has_cv"] = True
        except Exception as e:
            warning_log(f"CV not attached to interviewer email for {cv_id}: {e}")

    failed = []
    try:
        with log_duration("Gmail service creation", level="DEBUG"):
            service = get_gmail_service()
    except Exception as e:
        error_log(f"Gmail service unavailable for interview emails ({cv_id}): {e}", exc_info=False)
        service = None
    for who, to, (subject, body), att in (
            ("candidate", a["email"], candidate_interview_email(kind, a["name"], cv_id, a["job_title"], a["job_id"], cur["name"], sched), None),
            ("interviewer", sched["interviewer_email"], interviewer_email(kind, cand, cur["name"], sched, sched.get("scorecard_url")), attachment)):
        if not to or service is None:
            failed.append(who)
            continue
        try:
            _send_mail(service, to, subject, body, att)
            info_log(f"Interview email ({kind}) sent to {who} {to} for {cv_id}")
        except Exception as e:
            error_log(f"Interview email ({kind}) to {who} {to} failed for {cv_id}: {e}", exc_info=False)
            failed.append(who)

    done = {"new": "Interview scheduled", "updated": "Interview rescheduled", "cancelled": "Interview cancelled"}[kind]
    msg = done + (". Emails sent to the candidate and the interviewer." if not failed
                  else f", but the email to the {' and '.join(failed)} could not be sent.")
    return jsonify({"message": msg, "emails_failed": failed})


def _load_scorecard(cursor, token):
    """(schedule row, state) for a scorecard token; state = open / submitted / cancelled / expired / invalid."""
    ensure_schedule_table(cursor)
    cursor.execute(f"SELECT {SCHEDULE_COLUMNS} FROM ROUND_SCHEDULES WHERE TOKEN = %s", (token,))
    row = cursor.fetchone()
    if not row:
        return None, "invalid"
    if row[10] in ("SUBMITTED", "COMPLETED"):
        return row, "submitted"
    if row[10] == "CANCELLED":
        return row, "cancelled"
    if row[12] < datetime.now():
        return row, "expired"
    return row, "open"


@app.route("/scorecard/<token>", methods=["GET", "POST"])
def scorecard(token):
    """Interviewer scorecard, opened from the emailed link (no admin login)."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        ensure_round_table(cursor)
        row, state = _load_scorecard(cursor, token)
        if state == "invalid":
            warning_log("Scorecard opened with an unknown link")
            return render_template("scorecard.html", state="invalid"), 404
        cv_id, round_no = str(row[0]), int(row[1])
        sched = schedule_from_row(row)
        a, progress = _candidate_round_context(cursor, cv_id)
        if a is None:
            return render_template("scorecard.html", state="invalid"), 404
        target = next((r for r in progress["rounds"] if r["number"] == round_no), None)
        if state == "open" and (not progress["can_decide"] or progress["current"]["number"] != round_no):
            state = "closed"          # decided some other way (e.g. by an admin)
        ctx = dict(state=state, a=a, sched=sched, round=target or {"name": row[2], "max_marks": None},
                   progress=progress, criteria=SCORECARD_CRITERIA, token=token, errors=[], form={})
        if state == "open" and a.get("exam_start"):
            # the candidate's online-assessment answers, for the interviewer to probe in this round
            questions, total, max_total, answered = fetch_assessment_answers(cursor, cv_id, max_chars=4000)
            ctx["assessment"] = {"questions": questions, "answered": answered,
                                 "total": _num(total), "max_total": max_total,
                                 "percent": round(total * 100 / max_total) if max_total else None}

        if request.method == "GET" or state != "open":
            debug_log(f"Scorecard {request.method} for {cv_id} round {round_no}: {state}")
            return render_template("scorecard.html", **ctx)

        # ---- submit ----
        form = request.form
        ratings, errors = {}, []
        for c in SCORECARD_CRITERIA:
            try:
                v = int(form.get(c["key"], ""))
                if not 1 <= v <= 5:
                    raise ValueError
                ratings[c["key"]] = v
            except ValueError:
                errors.append(f"Rate {c['name']} from 1 to 5.")
        decision = form.get("recommendation")
        if decision not in ("PASSED", "REJECTED"):
            errors.append("Choose a recommendation.")
        remarks = (form.get("remarks") or "").strip()[:2000]
        if not remarks:
            errors.append("Add your comments on the candidate.")
        if errors:
            ctx.update(errors=errors, form=form)
            return render_template("scorecard.html", **ctx), 400

        marks, _ = scorecard_marks(ratings, target["max_marks"])
        scores = {"ratings": ratings,
                  "criteria": [{"key": c["key"], "name": c["name"], "rating": ratings[c["key"]]} for c in SCORECARD_CRITERIA],
                  "recommendation": decision, "marks": marks, "max_marks": target["max_marks"], "remarks": remarks}
        # the interviewer only recommends: the round is decided (and the candidate emailed)
        # when an admin approves it from Applications
        cursor.execute("""UPDATE ROUND_SCHEDULES SET STATUS = 'SUBMITTED', SCORES = %s, SUBMITTED_AT = %s
                          WHERE TOKEN = %s AND STATUS = 'SCHEDULED'""", (json.dumps(scores), datetime.now(), token))
        conn.commit()
        info_log(f"Scorecard submitted (awaiting admin approval): cv_id={cv_id}, round={round_no} ('{target['name']}'), "
                 f"recommendation={decision}, marks={marks}, interviewer={sched['interviewer_email']}")
    except Exception as e:
        conn.rollback()
        error_log(f"Scorecard failed for token ...{token[-6:]}: {e}")
        return render_template("scorecard.html", state="error"), 500
    finally:
        cursor.close()
        conn.close()

    ctx.update(state="done", decision=decision, marks=marks)
    return render_template("scorecard.html", **ctx)


@app.route("/scorecard/<token>/cv")
def scorecard_cv(token):
    """The candidate's CV for the interviewer (only while the scorecard link is valid)."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        row, state = _load_scorecard(cursor, token)
    finally:
        cursor.close()
        conn.close()
    if state not in ("open", "submitted"):
        abort(404)
    file_data = get_candidate_attachment(str(row[0]))
    if not file_data:
        abort(404)
    info_log(f"CV downloaded from scorecard for candidate {row[0]} (interviewer {row[8]})")
    return send_file(BytesIO(bytes(file_data["data"])), mimetype="application/pdf",
                     download_name=f"{row[0]}.pdf", as_attachment=False)


MAX_DETAIL_REQUESTS = 3   # same limit as the mail pipeline (main.py)


def fetch_detail_statuses(cursor):
    """{cv_id: {status, missing (labels), requests_sent}} from the email details requests.

    Empty when the mail pipeline has not created the table yet.
    """
    from llm.details_extractor import REQUIRED_FIELDS
    cursor.execute("SHOW TABLES LIKE 'candidate_detail_requests'")
    if not cursor.fetchall():
        debug_log("CANDIDATE_DETAIL_REQUESTS table not found; no detail statuses")
        return {}
    cursor.execute("SELECT CV_ID, STATUS, MISSING_FIELDS, REQUESTS_SENT FROM CANDIDATE_DETAIL_REQUESTS")
    statuses = {}
    for cv_id, status, missing, sent in cursor.fetchall():
        keys = [k for k in (missing or "").split(",") if k]
        statuses[str(cv_id)] = {
            "status": status,
            "missing": [REQUIRED_FIELDS.get(k, k) for k in keys],
            "requests_sent": sent or 0,
        }
    return statuses


def build_application(c, job_titles, detail=None):
    """Shape one candidate row for the Applications page, including its processing stage.

    `detail` is the candidate's email details request (see fetch_detail_statuses), if any.
    """
    cv_status = (c.get("status") or "").upper()
    exam_start = c.get("exam_start")
    exam_submit = c.get("exam_submit")
    final_status = (c.get("final_status") or "").upper()

    # Processing stage, in pipeline order:
    # 1 Applied -> 2 CV screened -> 3 Interview -> 4 Decision
    if cv_status == "NOT SHORTLISTED":
        stage, step = "CV Rejected", 2
    elif cv_status != "SHORTLISTED":
        stage, step = "CV Screening", 1
    elif not exam_start:
        # Shortlisted: first the candidate's details (asked by email), then the interview
        detail_status = (detail or {}).get("status")
        if detail_status == "PENDING":
            stage, step = "Awaiting Details", 2
        elif detail_status == "INCOMPLETE":
            stage, step = "Details Incomplete", 2
        else:
            stage, step = "Awaiting Interview", 2
    elif not exam_submit and final_status not in ("SELECTED", "REJECTED"):
        stage, step = "Interview In Progress", 3
    else:
        # submitted, or an admin already decided (e.g. the test ended without the final submit)
        stage, step = "Interview Completed", 4

    # MATCH_SCORE is stored as a 0-1 fraction (see MATCH_THRESHOLD in config.py);
    # multiply by 100 to show it as a whole-number percentage.
    match_score = c.get("match_score")
    try:
        if match_score is not None:
            match_score = round(float(match_score) * 100)
    except (TypeError, ValueError):
        match_score = None

    final_status = final_status or "PENDING"
    # Processing has ended once the interview is submitted, the CV was rejected,
    # or the candidate was flagged for a violation. The last stage then shows the decision.
    is_final = step == 4 or stage == "CV Rejected" or final_status == "VIOLATED"
    status_label = final_status.title() if is_final else stage

    job_id = str(c.get("job_id") or "")
    return {
        "id": str(c.get("id") or ""),
        "name": c.get("name") or "",
        "email": c.get("email") or "",
        "job_id": job_id,
        "job_title": job_titles.get(job_id, ""),
        "experience": c.get("experience") or "Not Provided",
        "applied_on": fmt_ist_datetime(c.get("created_at"), "db"),
        "cv_status": cv_status or "PENDING",
        "match_score": match_score,
        "exam_score": c.get("average") or "N/A",
        "exam_start": fmt_ist_datetime(exam_start, "app"),
        "exam_submit": fmt_ist_datetime(exam_submit, "app"),
        "final_status": final_status,
        "stage": stage,
        "stage_step": step,
        "is_final": is_final,
        "status_label": status_label,
        "has_face": bool(c.get("has_face")),
        "details_status": (detail or {}).get("status"),
        "details_missing": (detail or {}).get("missing", []),
        "details_requests": (detail or {}).get("requests_sent", 0),
        "details_max_requests": MAX_DETAIL_REQUESTS,
    }


def parse_round_config(raw):
    """Turn the stored ROUND_CONFIG JSON into an ordered list of rounds for display."""
    if not raw:
        return []
    try:
        config = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(config, dict):
        return []

    def round_no(key):
        num = str(key).replace("round_", "")
        return int(num) if num.isdigit() else 0

    rounds = []
    for key in sorted(config, key=round_no):
        r = config.get(key) or {}
        if isinstance(r, dict):
            rounds.append({
                "number": round_no(key),
                "name": r.get("name") or f"Round {round_no(key)}",
                "pass": r.get("pass"),
                "pending_min": r.get("pending_min"),
                "pending_max": r.get("pending_max"),
                "fail": r.get("fail"),
                "marks": r.get("marks"),
            })
    return rounds
 
@app.route("/delete-job", methods=["POST"])
def delete_job():
    debug_log(f"Delete job attempt by user {session.get('admin')}")
 
    if "admin" not in session:
        debug_log("No admin in session, redirecting to login")
        return redirect(url_for("login"))
 
    job_id = request.form.get("job_id")
    debug_log(f"Deleting job ID: {job_id}")
 
    conn = get_connection()
    cursor = conn.cursor()
 
    cursor.execute("DELETE FROM JOB_INFO WHERE ID = %s", (job_id,))
    conn.commit()
    if cursor.rowcount:
        info_log(f"Job deleted: {job_id} by {_request_user()}")
    else:
        warning_log(f"Delete job: {job_id} not found (nothing deleted)")
 
    cursor.close()
    conn.close()
 
    return redirect(url_for("job_descriptions"))

@app.route("/update-job-description", methods=["POST"])
def update_job_description():

    if "admin" not in session:
        warning_log("Update job description refused: not signed in")
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json()
    job_id = data.get("job_id")
    description = data.get("description")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE JOB_INFO
        SET JOB_DESCRIPTION = %s
        WHERE ID = %s
    """, (description, job_id))

    conn.commit()
    info_log(f"JD saved for job {job_id} ({len(str(description or ''))} chars, rows affected: {cursor.rowcount}) "
             f"by {_request_user()}")

    cursor.close()
    conn.close()

    return jsonify({"message": "Updated successfully"})


@app.route("/update-job-status", methods=["POST"])
def update_job_status():
    """Make a job Active (accepting applications) or Inactive, from the Jobs page."""
    if "admin" not in session:
        warning_log("Update job status refused: not signed in")
        return jsonify({"message": "Please log in again."}), 401

    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id") or "").strip()
    active = data.get("active")
    if not job_id or not isinstance(active, bool):
        warning_log(f"Update job status rejected: invalid request (job_id='{job_id}', active={active!r})")
        return jsonify({"message": "Invalid request."}), 400

    conn = get_connection()
    cursor = conn.cursor()
    try:
        ensure_job_postings_column(cursor)
        cursor.execute("SELECT ID FROM JOB_INFO WHERE ID = %s", (job_id,))
        if not cursor.fetchone():
            warning_log(f"Update job status: job {job_id} not found")
            return jsonify({"message": f"Job {job_id} was not found."}), 404
        cursor.execute("UPDATE JOB_INFO SET IS_ACTIVE = %s WHERE ID = %s", (1 if active else 0, job_id))
        conn.commit()
        info_log(f"Job {job_id} marked {'ACTIVE' if active else 'INACTIVE'} by {_request_user()}")
    except Exception as e:
        conn.rollback()
        error_log(f"Updating status of job {job_id} failed: {e}")
        return jsonify({"message": "The job status could not be saved. Please try again."}), 500
    finally:
        cursor.close()
        conn.close()

    return jsonify({"message": f"Job {job_id} is now {'active' if active else 'inactive'}.", "is_active": active})


@app.route("/update-job-rounds", methods=["POST"])
def update_job_rounds():
    """Save a job's interview rounds from the Jobs panel (same format as New Job Posting)."""
    if "admin" not in session:
        warning_log("Update job rounds refused: not signed in")
        return jsonify({"message": "Please log in again."}), 401

    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id") or "").strip()
    first = data.get("assessment") or {}
    extra = data.get("rounds") or []
    if not job_id or not isinstance(extra, list):
        warning_log(f"Update job rounds rejected: invalid request (job_id='{job_id}')")
        return jsonify({"message": "Invalid request."}), 400

    def number(value, low, high, label):
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a number.")
        if not low <= f <= high:
            raise ValueError(f"{label} must be between {low} and {high}.")
        return str(int(f)) if f.is_integer() else str(f)

    try:
        pass_pct = number(first.get("pass"), 0, 100, "Pass %")
        pending_min = number(first.get("pending_min"), 0, 100, "Pending minimum")
        pending_max = number(first.get("pending_max"), 0, 100, "Pending maximum")
        fail_pct = number(first.get("fail"), 0, 100, "Fail %")
        if not float(pending_min) <= float(pending_max) < float(pass_pct):
            raise ValueError("The pending range must be in order and below the Pass %.")
        if float(fail_pct) > float(pending_min):
            raise ValueError("Fail % must not be above the pending minimum.")
        config = {"round_1": {
            "name": (str(first.get("name") or "").strip() or "Round 1")[:100],
            "pass": pass_pct, "pending_min": pending_min, "pending_max": pending_max, "fail": fail_pct,
        }}
        for i, r in enumerate(extra, start=2):
            r = r if isinstance(r, dict) else {}
            name = str(r.get("name") or "").strip()[:100]
            if not name:
                raise ValueError(f"Round {i} needs a name.")
            config[f"round_{i}"] = {"name": name, "marks": number(r.get("marks"), 1, 1000, f"Round {i} marks")}
    except ValueError as e:
        warning_log(f"Update job rounds for {job_id} rejected: {e}")
        return jsonify({"message": str(e)}), 400

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT ID FROM JOB_INFO WHERE ID = %s", (job_id,))
        if not cursor.fetchone():
            warning_log(f"Update job rounds: job {job_id} not found")
            return jsonify({"message": f"Job {job_id} was not found."}), 404
        round_config_json = json.dumps(config)
        cursor.execute("UPDATE JOB_INFO SET TOTAL_ROUNDS = %s, ROUND_CONFIG = %s WHERE ID = %s",
                       (len(config), round_config_json, job_id))
        conn.commit()
        info_log(f"Rounds edited for job {job_id}: {len(config)} rounds "
                 f"({', '.join(r['name'] for r in config.values())}) by {_request_user()}")
    except Exception as e:
        conn.rollback()
        error_log(f"Saving rounds for {job_id} failed: {e}")
        return jsonify({"message": "The rounds could not be saved. Please try again."}), 500
    finally:
        cursor.close()
        conn.close()

    return jsonify({"message": "Interview rounds saved.", "total_rounds": len(config),
                    "rounds": parse_round_config(round_config_json)})

# @app.route("/candidate-report/<candidate_id>")
# def candidate_report(candidate_id):
#     debug_log(f"Generating report for candidate {candidate_id}")

#     conn = get_connection()
#     cursor = conn.cursor()

#     cursor.execute("""
#         SELECT NAME, EMAIL, ID, STATUS, MATCH_SCORE, FINAL_STATUS
#         FROM CV_INFO
#         WHERE ID = %s
#     """, (candidate_id,))
#     candidate = cursor.fetchone()
#     if not candidate:
#         return "Candidate not found", 404
#     debug_log(f"Candidate found: {candidate[0] if candidate else 'Not found'}")

#     cursor.execute("""
#         SELECT
#             q.QUESTION,
#             r.RESPONSE,
#             r.SCORE,
#             r.CREATED_AT
#             FROM QUESTIONS_TABLE q
#             JOIN RESPONSE_TABLE r
#             ON q.QUES_ID = r.QUES_ID
#             WHERE r.ID = %s
#             ORDER BY r.CREATED_AT
#     """, (candidate_id,))

#     responses = cursor.fetchall()
#     debug_log(f"Retrieved {len(responses)} responses for candidate")

#     cursor.close()

#     buffer = BytesIO()

#     doc = SimpleDocTemplate(buffer, pagesize=A4)
#     styles = getSampleStyleSheet()
#     elements = []

#     elements.append(Paragraph("Candidate Interview Report", styles['Title']))
#     elements.append(Spacer(1,20))

#     info = [
#         ["Name", candidate[0]],
#         ["Email", candidate[1]],
#         ["Candidate ID", candidate[2]],
#         ["Application Status", candidate[3]],
#         ["CV Score", str(candidate[4])],
#         ["Final Decision", candidate[5]]
#     ]

#     table = Table(info, colWidths=[2*inch,4*inch])
#     table.setStyle([
#         ('GRID',(0,0),(-1,-1),1,colors.grey)
#     ])

#     elements.append(table)
#     elements.append(Spacer(1,30))

#     elements.append(Paragraph("Interview Questions & Answers", styles['Heading2']))
#     elements.append(Spacer(1,10))

#     data = [["Question","Answer","Score","Answered Time"]]

#     for r in responses:
#         data.append([
#             Paragraph(str(r[0]), styles['BodyText']),
#             Paragraph(str(r[1]), styles['BodyText']),
#             Paragraph(str(r[2]), styles['BodyText']),
#             Paragraph(str(r[3]), styles['BodyText'])
#         ])

#     qa_table = Table(
#         data,
#         colWidths=[3*inch,3*inch,0.8*inch,1.2*inch],
#         repeatRows=1
#     )
#     qa_table.setStyle([
#         ('GRID',(0,0),(-1,-1),1,colors.black),
#         ('BACKGROUND',(0,0),(-1,0),colors.lightgrey),
#         ('VALIGN',(0,0),(-1,-1),'TOP'),
#     ])

#     elements.append(qa_table)

#     doc.build(elements)
#     debug_log("PDF report built successfully")

#     buffer.seek(0)

#     return send_file(
#         buffer,
#         as_attachment=True,
#         download_name=f"{candidate_id}_interview_report.pdf",
#         mimetype="application/pdf"
#     )
    
# Camera frames stored with each violation (EXAM_VIOLATIONS.SNAPSHOT, written by app.py)
MAX_REPORT_SNAPSHOTS = 12


def report_image(data, max_w, max_h):
    """A ReportLab image scaled to fit max_w x max_h (keeps the aspect ratio), or None if unreadable."""
    try:
        src = BytesIO(bytes(data)) if isinstance(data, (bytes, bytearray, memoryview)) else data
        w, h = ImageReader(src).getSize()
        if isinstance(src, BytesIO):
            src.seek(0)
        scale = min(max_w / w, max_h / h)
        return RLImage(src, width=w * scale, height=h * scale)
    except Exception as e:
        warning_log(f"Report image could not be read: {e}")
        return None


@app.route("/candidate-report/<candidate_id>")
def candidate_report(candidate_id):
    # The report contains personal data (photo, answers): signed-in admins only
    if "admin" not in session:
        warning_log(f"Candidate report for {candidate_id} refused: not signed in")
        return redirect(url_for("login"))

    debug_log(f"Generating report for candidate {candidate_id}")

    conn = get_connection()
    cursor = conn.cursor()

    # ------------------ GET CANDIDATE INFO ------------------

    cursor.execute("""
        SELECT 
            c.NAME,
            c.EMAIL,
            c.ID,
            c.STATUS,
            c.MATCH_SCORE,
            c.FINAL_STATUS,
            t.start_exam,
            t.response_submit,
            t.mail_received,
            t.mail_read,
            c.JOB_ID,
            c.JOBTITLE,
            c.FACE_IMAGE,
            t.candidate_login,
            t.image_taken,
            t.end_exam
        FROM CV_INFO c
        LEFT JOIN CV_TIMESTAMPS t 
            ON c.ID = t.ID
        WHERE c.ID = %s
    """, (candidate_id,))

    candidate = cursor.fetchone()
    debug_log(f"Report candidate lookup for {candidate_id}: "
              f"{'found (' + str(candidate[0]) + ', job ' + str(candidate[10]) + ')' if candidate else 'not found'}")

    if not candidate:
        cursor.close()
        conn.close()
        warning_log(f"Candidate report: candidate {candidate_id} not found")
        return "Candidate not found", 404

    # The report exists only after the test: submitted, ended (time up / terminated) or marked VIOLATED
    test_finished = bool(candidate[7] or candidate[15] or str(candidate[5] or "").upper() == "VIOLATED")
    if not test_finished:
        cursor.close()
        conn.close()
        warning_log(f"Candidate report for {candidate_id} refused: assessment not completed yet")
        return "The report is available after the candidate completes the assessment.", 404

    # ------------------ GET RESPONSES ------------------

    cursor.execute("""
        SELECT
            q.QUESTION,
            r.RESPONSE,
            r.SCORE,
            r.ANSWERED_AT
        FROM QUESTIONS_TABLE q
        JOIN RESPONSE_TABLE r
            ON q.QUES_ID = r.QUES_ID
        WHERE r.ID = %s
        ORDER BY r.QUES_ID
    """, (candidate_id,))

    responses = cursor.fetchall()

    debug_log(f"Responses fetched: {len(responses)}")

    # ------------------ GET VIOLATIONS ------------------

    ensure_violation_snapshot_column(cursor)
    cursor.execute("""
        SELECT EVENT_TYPE, EVENT_TIME, SNAPSHOT
        FROM EXAM_VIOLATIONS
        WHERE ID = %s
        ORDER BY VIOLATION_ID
    """, (candidate_id,))
    violation_rows = cursor.fetchall()
    violations = [(r[0], r[1]) for r in violation_rows]
    # evidence images: (jpeg bytes, violation type, time text with seconds and milliseconds)
    snapshots = [(r[2], str(r[0] or "Unknown"),
                  (fmt_ist_millis(r[1], "db") or "Time not recorded")
                  + (f"<br/>{fmt_elapsed(r[1], candidate[6])}" if fmt_elapsed(r[1], candidate[6]) else ""))
                 for r in violation_rows if r[2]]
    debug_log(f"Violations fetched for {candidate_id}: {len(violations)} recorded, {len(snapshots)} snapshot(s)")

    cursor.close()
    conn.close()

    # ------------------ CREATE PDF ------------------

    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=40,
        leftMargin=40,
        topMargin=40,
        bottomMargin=40
    )

    styles = getSampleStyleSheet()
    elements = []

    # ------------------ TITLE ------------------

    elements.append(Paragraph("Candidate Interview Report", styles['Title']))
    elements.append(Spacer(1, 20))

    # ------------------ CANDIDATE DETAILS TABLE ------------------

    info = [
        ["Name", candidate[0]],
        ["Email", candidate[1]],
        ["Candidate ID", candidate[2]],
        ["Application Status", candidate[3]],
        ["CV Score", str(candidate[4])],
        ["Job ID", candidate[10]],
        ["Job Title", candidate[11]],
        ["Final Decision", candidate[5]],

        ["Mail Received", fmt_ist_datetime(candidate[8]) or "N/A"],
        ["Mail Read", fmt_ist_datetime(candidate[9]) or "N/A"],
        ["Violations", str(len(violations))]
    ]

    info_table = Table(info, colWidths=[2 * inch, 4.5 * inch])

    info_table.setStyle([
        ('GRID', (0,0), (-1,-1), 1, colors.grey),
        ('BACKGROUND', (0,0), (0,-1), colors.lightgrey),
    ])

    elements.append(info_table)
    elements.append(Spacer(1, 20))

    # ------------------ ASSESSMENT LOGIN & SUBMISSION ------------------

    elements.append(Paragraph("Assessment Login &amp; Submission", styles['Heading2']))
    elements.append(Spacer(1, 6))

    started, submitted, ended = to_ist(candidate[6]), to_ist(candidate[7]), to_ist(candidate[15])
    finish = submitted or ended
    if started and finish and finish >= started:
        secs = int((finish - started).total_seconds())
        duration = f"{secs // 60} min {secs % 60:02d} sec"
    else:
        duration = "N/A"
    final = str(candidate[5] or "").upper()
    if final == "VIOLATED":
        how = "Terminated - violation limit reached"
    elif submitted:
        how = "Submitted by the candidate"
    else:
        how = "Ended (time up / closed)"

    login_info = [
        ["Candidate Login", fmt_ist_seconds(candidate[13]) or "N/A"],
        ["Photo Verified", fmt_ist_seconds(candidate[14]) or "N/A"],
        ["Exam Started", fmt_ist_seconds(candidate[6]) or "N/A"],
        ["Exam Ended", fmt_ist_seconds(candidate[15]) or "N/A"],
        ["Responses Submitted", fmt_ist_seconds(candidate[7]) or "N/A"],
        ["Time Taken", duration],
        ["How the Exam Ended", how],
    ]
    login_table = Table(login_info, colWidths=[2 * inch, 4.5 * inch])
    login_table.setStyle([
        ('GRID', (0,0), (-1,-1), 1, colors.grey),
        ('BACKGROUND', (0,0), (0,-1), colors.lightgrey),
    ] + ([('TEXTCOLOR', (1,6), (1,6), colors.HexColor('#b91c1c'))] if final == "VIOLATED" else []))
    elements.append(login_table)
    elements.append(Spacer(1, 20))

    # ------------------ FACE VERIFICATION PHOTO ------------------

    elements.append(Paragraph("Face Verification Photo", styles['Heading2']))
    elements.append(Spacer(1, 6))
    face = report_image(candidate[12], 2.6 * inch, 2.0 * inch) if candidate[12] else None
    elements.append(face if face else Paragraph("No photo captured (assessment not attempted).", styles['BodyText']))
    elements.append(Spacer(1, 20))

    # ------------------ PROCTORING VIOLATIONS ------------------

    elements.append(Paragraph("Proctoring Violations", styles['Heading2']))
    elements.append(Spacer(1, 6))
    if not violations:
        elements.append(Paragraph("No violations recorded.", styles['BodyText']))
    else:
        terminated = str(candidate[5] or "").upper() == "VIOLATED"
        elements.append(Paragraph(
            f"<b>{len(violations)}</b> violation(s) recorded during the assessment."
            + (" <font color='#b91c1c'><b>The assessment was terminated because the violation limit was reached.</b></font>"
               if terminated else ""),
            styles['BodyText']))
        elements.append(Spacer(1, 8))

        v_data = [["#", "Violation", "Time (with seconds & ms)", "After exam start"]]
        for i, (event_type, event_time) in enumerate(violations, start=1):
            elapsed = fmt_elapsed(event_time, candidate[6])
            v_data.append([str(i), Paragraph(str(event_type or "Unknown"), styles['BodyText']),
                           fmt_ist_millis(event_time, "db") or "Not recorded",
                           elapsed.replace(" after start", "") if elapsed else "—"])
        v_table = Table(v_data, colWidths=[0.35 * inch, 2.45 * inch, 2.2 * inch, 1.5 * inch], repeatRows=1)
        v_table.setStyle([
            ('GRID', (0,0), (-1,-1), 1, colors.grey),
            ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ])
        elements.append(v_table)

    # Evidence: camera frames captured when a violation was flagged
    if snapshots:
        shown = snapshots[-MAX_REPORT_SNAPSHOTS:]
        elements.append(Spacer(1, 14))
        elements.append(Paragraph("Violation Evidence (camera snapshots)", styles['Heading3']))
        if len(snapshots) > len(shown):
            elements.append(Paragraph(f"Showing the latest {len(shown)} of {len(snapshots)} snapshots.", styles['BodyText']))
        elements.append(Spacer(1, 6))
        cells = []
        for image_bytes, kind, taken in shown:
            img = report_image(image_bytes, 3.1 * inch, 2.3 * inch)
            if img:
                cells.append([img, Paragraph(f"<b>{kind}</b><br/>{taken}", styles['BodyText'])])
        rows = [cells[i:i + 2] for i in range(0, len(cells), 2)]
        for row in rows:
            row = row + [["", ""]] * (2 - len(row))
            grid = Table([[row[0][0], row[1][0]], [row[0][1], row[1][1]]],
                         colWidths=[3.25 * inch, 3.25 * inch])
            grid.setStyle([('VALIGN', (0,0), (-1,-1), 'TOP'), ('ALIGN', (0,0), (-1,0), 'LEFT'),
                           ('BOTTOMPADDING', (0,1), (-1,1), 10)])
            elements.append(KeepTogether(grid))

    elements.append(Spacer(1, 30))

    # ------------------ QUESTIONS SECTION ------------------

    elements.append(Paragraph("Interview Questions & Answers", styles['Heading2']))
    elements.append(Spacer(1, 10))

    if not responses:
        elements.append(Paragraph("No interview responses found.", styles['BodyText']))
    else:

        data = [["Question", "Answer", "Score", "Answered Time"]]

        for r in responses:
            # We explicitly define answered_time_str here before we use it in the Paragraph
            answered_time_str = fmt_ist_seconds(r[3]) or "Skipped / N/A"
            
            data.append([
                Paragraph(str(r[0]), styles['BodyText']),
                Paragraph(str(r[1]), styles['BodyText']),
                Paragraph(str(r[2]), styles['BodyText']),
                Paragraph(answered_time_str, styles['BodyText'])
            ])

        qa_table = Table(
            data,
            colWidths=[2.8 * inch, 2.8 * inch, 0.7 * inch, 1.2 * inch], 
            repeatRows=1
        )

        qa_table.setStyle([
            ('GRID', (0,0), (-1,-1), 1, colors.black),
            ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ])

        elements.append(qa_table)

    # ------------------ BUILD PDF ------------------

    with log_duration(f"PDF report build for {candidate_id}", level="DEBUG", slow_ms=1500):
        doc.build(elements)

    info_log(f"Candidate report downloaded: cv_id={candidate_id}, {len(responses)} responses, "
             f"{len(violations)} violation(s), {len(snapshots)} snapshot(s), "
             f"{buffer.getbuffer().nbytes} bytes, by={_request_user()}")

    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"{candidate_id}_interview_report.pdf",
        mimetype="application/pdf"
    )
@app.route("/reports")
def reports():

    debug_log(f"Accessing reports page by user {session.get('admin')}")

    if "admin" not in session:
        return redirect(url_for("login"))

    return render_template("reports.html")

@app.route("/get_reports")
def get_reports():

    debug_log("Fetching reports with filters")

    job = request.args.get('job')
    candidate = request.args.get('candidate')
    status = request.args.get('status')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT 
            cv.ID,
            cv.JOB_ID,
            cv.NAME,
            cv.EMAIL,
            cv.MAIL_BODY, 
            COALESCE(SUM(r.SCORE), 0) AS SCORE,
            COALESCE(cv.FINAL_STATUS, 'PENDING') AS STATUS,
            cv.EXAM_LINK_USED,
            MAX(r.CREATED_AT) AS CREATED_AT
        FROM CV_INFO cv
        LEFT JOIN RESPONSE_TABLE r ON cv.ID = r.ID
        WHERE 1=1
    """

    params = []

    # -------- Filters --------

    if job:
        query += " AND cv.JOB_ID = %s"
        params.append(job)

    if candidate:
        query += " AND cv.NAME = %s"
        params.append(candidate)

    if status:
        query += " AND cv.FINAL_STATUS = %s"
        params.append(status)

    if start_date:
        query += " AND DATE(r.CREATED_AT) >= %s"
        params.append(start_date)

    if end_date:
        query += " AND DATE(r.CREATED_AT) <= %s"
        params.append(end_date)

    query += """
        GROUP BY cv.ID, cv.JOB_ID, cv.NAME, cv.EMAIL, cv.FINAL_STATUS, cv.EXAM_LINK_USED
        ORDER BY CREATED_AT DESC
    """

    debug_log(f"Reports query: {short(query, 1000)}")
    debug_log(f"Params: {params}")

    _query_start = time.perf_counter()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    _query_ms = (time.perf_counter() - _query_start) * 1000
    (warning_log if _query_ms > 1500 else debug_log)(
        f"Reports query returned {len(rows)} rows in {_query_ms:.0f} ms")

    cursor.close()
    conn.close()

    # -------- Format response --------

    result = []
    for r in rows:
        result.append({
            "cv_id": r[0],  # ✅ NEW
            "job_id": r[1],
            "candidate": r[2],
            "email": r[3],
            "notice_period": extract_notice_period(r[4]),
            "score": int(r[5]),
            "status": r[6],
            "link_used": bool(r[7])  # ✅ NEW
        })

    debug_log(f"Returning {len(result)} records")

    return jsonify(result)

@app.route("/get_filters")
def get_filters():

    conn = get_connection()
    cursor = conn.cursor()

    # Get Job IDs
    cursor.execute("SELECT DISTINCT ID FROM JOB_INFO")
    jobs = [row[0] for row in cursor.fetchall()]

    # Get Candidates
    cursor.execute("SELECT DISTINCT NAME FROM CV_INFO")
    candidates = [row[0] for row in cursor.fetchall()]

    cursor.close()
    conn.close()
    debug_log(f"Report filters: {len(jobs)} jobs, {len(candidates)} candidate names")

    return jsonify({
        "jobs": jobs,
        "candidates": candidates
    })
    
def extract_notice_period(mail_body):
    try:
        data = json.loads(mail_body)
        return data.get("parsed_fields", {}).get("notice_period")
    except:
        return None

from openpyxl import Workbook

@app.route("/admin/regenerate_link/<cv_id>")
def regenerate_link(cv_id):
    # Issuing exam links is an admin action.
    if "admin" not in session:
        warning_log(f"Regenerate link for {cv_id} refused: not signed in")
        return jsonify({"status": "error", "message": "Please log in again."}), 401

    conn = get_connection()
    cursor = conn.cursor()

    # 🔥 Reset everything safely
    cursor.execute("""
        UPDATE CV_INFO
        SET EXAM_LINK_USED = FALSE,
            EXAM_TOKEN = NULL,
            LINK_IP = NULL,
            USER_AGENT = NULL
        WHERE ID = %s
    """, (cv_id,))

    conn.commit()
    reset_rows = cursor.rowcount
    cursor.close()
    conn.close()

    # 🔥 Generate new link
    link = generate_exam_link(cv_id)
    # (the link itself carries a token, so it is not logged)
    info_log(f"Interview link regenerated for cv_id={cv_id} by {_request_user()} (rows reset: {reset_rows})")

    return jsonify({
        "status": "success",
        "new_link": link
    })

@app.route("/admin/link_status/<cv_id>")
def link_status(cv_id):
    """Account and exam-link usage for one candidate (used by the Applications panel)."""
    if "admin" not in session:
        warning_log(f"Link status for {cv_id} refused: not signed in")
        return jsonify({"status": "error", "message": "Please log in again."}), 401

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT cv.EXAM_LINK_USED, cv.EXAM_TOKEN
            FROM CV_INFO cv
            WHERE cv.ID = %s
        """, (cv_id,))
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not row:
        warning_log(f"Link status: candidate {cv_id} not found")
        return jsonify({"status": "error", "message": "Candidate not found."}), 404

    used, token = row

    # The token carries its own expiry (set by generate_exam_link in app.py).
    expires_at, expired = None, False
    if token:
        try:
            from app import serializer
            exp = serializer.loads(token, salt="exam-link").get("exp")
            if exp:
                expires_at = fmt_ist_datetime(exp)
                expired = datetime.now().timestamp() > exp
        except Exception as e:
            warning_log(f"Could not read exam-link token expiry for {cv_id}: {type(e).__name__}")

    if not token:
        link_state = "Not issued"
    elif used:
        link_state = "Used"
    elif expired:
        link_state = "Expired"
    else:
        link_state = "Not opened yet"
    debug_log(f"Link status for {cv_id}: {link_state}, expires {expires_at or 'n/a'}")

    return jsonify({
        "status": "success",
        # Same rule as the Reports page "Account" column.
        "account_active": bool(used),
        "link_state": link_state,
        "expires_at": expires_at,
    })


@app.route("/export_excel")
def export_excel():

    debug_log("Exporting Excel with filters")

    job = request.args.get('job')
    candidate = request.args.get('candidate')
    status = request.args.get('status')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    ts = request.args.get("ts")

    conn = get_connection()
    cursor = conn.cursor()

    # =========================
    # ✅ BASE QUERY
    # =========================
    query = """
        SELECT 
            ts.ID,

            -- CV_INFO FIRST
            cv.NAME,
            cv.EMAIL,
            cv.STATUS,
            cv.MATCH_SCORE,
            cv.JOB_ID,
            cv.JOBTITLE,
            cv.FINAL_STATUS,
            cv.CREATED_AT,
            cv.EXPERIENCE,

            -- TIMESTAMPS
            ts.mail_read,
            ts.response_mail,
            ts.question_generated,
            ts.candidate_login,
            ts.image_taken,
            ts.start_exam,
            ts.end_exam,
            ts.response_submit,
            ts.pending_approval,
            ts.mail_received

        FROM CV_TIMESTAMPS ts
        LEFT JOIN CV_INFO cv 
            ON ts.ID = cv.ID
        LEFT JOIN RESPONSE_TABLE r 
            ON cv.ID = r.ID

        WHERE 1=1
    """

    params = []

    # =========================
    # ✅ APPLY FILTERS
    # =========================

    if job:
        query += " AND cv.JOB_ID = %s"
        params.append(job)

    if candidate:
        query += " AND cv.NAME = %s"
        params.append(candidate)

    if status:
        query += " AND cv.FINAL_STATUS = %s"
        params.append(status)

    if start_date:
        query += " AND DATE(r.CREATED_AT) >= %s"
        params.append(start_date)

    if end_date:
        query += " AND DATE(r.CREATED_AT) <= %s"
        params.append(end_date)

    query += """
        GROUP BY ts.ID
        ORDER BY cv.CREATED_AT DESC
    """

    debug_log(f"Export query: {short(query, 1000)}")
    debug_log(f"Params: {params}")

    _query_start = time.perf_counter()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    _query_ms = (time.perf_counter() - _query_start) * 1000
    (warning_log if _query_ms > 1500 else debug_log)(
        f"Export query returned {len(rows)} rows in {_query_ms:.0f} ms")

    # Headers
    headers = [desc[0] for desc in cursor.description]

    cursor.close()
    conn.close()

    # =========================
    # ✅ CREATE EXCEL
    # =========================
    wb = Workbook()
    ws = wb.active
    ws.title = "Filtered_Report"

    ws.append(headers)

    for row in rows:
        ws.append([
            (fmt_ist_datetime(col, "db" if headers[i].upper() == "CREATED_AT" else "app") if isinstance(col, datetime)
             else (str(col) if col is not None else ""))
            for i, col in enumerate(row)
        ])

    # =========================
    # SAVE FILE
    # =========================
    output = BytesIO()
    wb.save(output)
    output.seek(0)

# Generate filename
    if ts:
        filename = f"Interview_Report_{ts}.xlsx"
    else:
        filename = f"Interview_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    info_log(f"Report Excel exported: {filename}, {len(rows)} rows, filters job={job}, candidate={candidate}, "
             f"status={status}, dates={start_date}..{end_date}, by={_request_user()}")

    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    

def compare_resume_with_jd(raw_text, jd_text):

    # ---------------------------------------
    # Encode Resume
    # ---------------------------------------

    resume_embedding = model.encode(
        raw_text,
        convert_to_tensor=True
    )

    # ---------------------------------------
    # Encode JD
    # ---------------------------------------

    jd_embedding = model.encode(
        jd_text,
        convert_to_tensor=True
    )

    # ---------------------------------------
    # Similarity
    # ---------------------------------------

    similarity = util.cos_sim(
        resume_embedding,
        jd_embedding
    )

    score = round(
        float(similarity[0][0]) * 100,
        2
    )

    return score


if __name__ == "__main__":
    # create_fresh_users()
    info_log(f"Starting admin Flask application: host=0.0.0.0, port={ADMIN_APP_PORT}, ssl=adhoc, debug=True "
             f"(pid {os.getpid()}, reloader child: {os.environ.get('WERKZEUG_RUN_MAIN') == 'true'})")
    log_app_urls("Hire Assist AI admin app", ADMIN_APP_PORT, ssl=True, debug=True)
    app.run(host="0.0.0.0", port=ADMIN_APP_PORT, ssl_context='adhoc', debug=True)