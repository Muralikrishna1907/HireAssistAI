import secrets
import string
import base64
import inspect
import uuid
from litellm import responses
import pandas as pd
from flask import Flask, render_template, session, redirect, request, flash, url_for, jsonify,Response
from pydash import result
# from torch.monitor import log_event
from db.cv_repository import log_event
from app import generate_exam_link
from db import connection
from db.connection import get_connection
from flask import send_file, abort
from io import BytesIO
from db.data_access import get_candidate_attachment 
from llm.job_description_generator import generate_job_description
from werkzeug.security import generate_password_hash, check_password_hash
from email.mime.text import MIMEText
from gmail.gmail_service import get_gmail_service
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib import colors
from datetime import datetime
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
    "batch_id": ""                 # NEW
}

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


# Debug helper function
def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

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

    cv.FACE_IMAGE,
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
            "has_face": r[8] is not None,
            
            "created_at": str(r[9]) if r[9] else None,
            "exam_start": str(r[10]) if r[10] else None,
            "exam_submit": str(r[11]) if r[11] else None,
            "experience": r[12] if r[12] else "Not Provided"
        }
        for r in rows
    ]

    debug_log(f"Returning {len(result)} formatted candidates")
    return result
import unicodedata

def normalize_text(text):
    debug_log(f"Normalizing text: {text[:50] if text else 'None'}...")
    if not text:
        return text
    text = unicodedata.normalize("NFKD", text)
    return text.encode("utf-8", "ignore").decode("utf-8")


app = Flask(__name__)

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

    print("Fresh users created successfully!")

app.secret_key = "secret"

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
        print(user)
        debug_log(f"User found: {user is not None}")

        cursor.close()
        conn.close()
        debug_log(f"User ID: {user}")
        if user and check_password_hash(user[1], password):
            debug_log(f"Login successful for user ID: {user[0]}")
            
            session["admin"] = user[0]
            session["role"] = user[2]

            if user[3] == 'Y':
                debug_log(f"Forcing password change for user {user[0]}")
                return redirect(url_for("force_change_password"))

            return redirect(url_for("admin_home"))

        else:
            debug_log(f"Login failed - Invalid credentials for {username}")
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

    cursor.close()
    conn.close()

    return jsonify({"message": "Status updated successfully"})

@app.route("/face-preview/<candidate_id>")
def face_preview(candidate_id):

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
        return Response(row[0], mimetype="image/png")

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
            debug_log("Passwords do not match")
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
        debug_log(f"Password updated successfully for user {session['admin']}")
        
        cursor.close()
        conn.close()

        return redirect(url_for("admin_home"))

    return render_template("force_change_password.html")

@app.route("/reset-password/<int:user_id>")
def reset_password(user_id):
    debug_log(f"Reset password attempt for user {user_id} by {session.get('admin')}")

    if session.get("role") != "super_admin":
        debug_log("Unauthorized - Not super_admin")
        return "Unauthorized", 403

    if user_id == session["admin"]:
        debug_log("User attempted to reset own password")
        return "You cannot reset yourself", 403

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
    debug_log(f"Password reset complete for user {user_id}")
    
    cursor.close()
    conn.close()

    return render_template("temp_password.html", temp_password=temp_password)

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

        return jsonify({
            "success": False,
            "message": "Session expired"
        }), 401

    try:

        # -----------------------------------
        # GET REQUEST DATA
        # -----------------------------------

        data = request.get_json()

        print("MENU DATA :", data)
        print("SESSION :", session)

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

        # -----------------------------------
        # SAFETY CHECK
        # -----------------------------------

        if not username:

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

        print(f"Menu order saved for {username}")

        return jsonify({
            "success": True
        })

    except Exception as e:

        print("SAVE MENU ERROR :", str(e))

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

# @app.route("/admin-home")
# def admin_home():
#     debug_log(f"Accessing admin home for user {session.get('admin')}")
    
#     if not session.get("admin"):
#         debug_log("No admin in session, redirecting to login")
#         return redirect(url_for("login"))

#     return render_template("admin_home.html")

# DEFAULT MENU
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


@app.route('/admin-home')
def admin_home():
    # -----------------------------------
    # CHECK LOGIN & USERNAME (Existing Logic)
    # -----------------------------------
    if 'admin' not in session:
        return redirect(url_for('login'))

    username = session.get("username")
    if not username:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT USERNAME FROM ADMIN_USERS WHERE ID = %s", (session['admin'],))
        user_row = cursor.fetchone()
        if user_row:
            username = user_row[0]
            session['username'] = username

    if not username:
        session.clear()
        return redirect(url_for("login"))

    # -----------------------------------
    # FETCH ENHANCED ANALYTICS
    # -----------------------------------
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Menu Order
    cursor.execute("SELECT MENU_KEY FROM ADMIN_MENU_ORDER WHERE USERNAME = %s ORDER BY DISPLAY_ORDER", (username,))
    rows = cursor.fetchall()
    if not rows:
        menu_items = DEFAULT_MENU
    else:
        ordered_keys = [row[0] for row in rows]
        menu_map = {item["key"]: item for item in DEFAULT_MENU}
        menu_items = [menu_map[k] for k in ordered_keys if k in menu_map]
        existing_keys = [item["key"] for item in menu_items]
        for item in DEFAULT_MENU:
            if item["key"] not in existing_keys:
                menu_items.append(item)

    # 2. Base Stats
    cursor.execute("SELECT COUNT(*) FROM JOB_INFO")
    active_jobs = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM CV_INFO")
    total_candidates = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM CV_INFO WHERE FINAL_STATUS = 'PENDING'")
    pending_reviews = cursor.fetchone()[0]

    # 3. NEW: Pipeline Stats for Charts
    cursor.execute("SELECT COUNT(*) FROM CV_INFO WHERE FINAL_STATUS = 'SELECTED'")
    selected_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM CV_INFO WHERE FINAL_STATUS IN ('REJECTED', 'NOT APPEARED', 'NOT SHORTLISTED')")
    rejected_count = cursor.fetchone()[0]

    # 4. Recent Jobs with Application Count
    cursor.execute("""
        SELECT 
            j.ID, 
            j.JOB_TITLE, 
            j.CREATED_AT, 
            COUNT(cv.ID) AS APPLIED_COUNT
        FROM JOB_INFO j
        LEFT JOIN CV_INFO cv ON j.ID = cv.JOB_ID
        GROUP BY j.ID, j.JOB_TITLE, j.CREATED_AT
        ORDER BY j.CREATED_AT DESC 
        LIMIT 5
    """)
    recent_jobs_rows = cursor.fetchall()
    
    recent_jobs = []
    for r in recent_jobs_rows:
        recent_jobs.append({
            "id": r[0],
            "title": r[1],
            "created_at": r[2].strftime('%b %d, %Y') if r[2] else 'N/A',
            "applied_count": r[3]
        })

    cursor.close()
    conn.close()

    # Bundle stats
    stats = {
        "active_jobs": active_jobs,
        "total_candidates": total_candidates,
        "pending_reviews": pending_reviews,
        "selected_count": selected_count,
        "rejected_count": rejected_count
    }

    return render_template(
        'admin_home.html',
        menu_items=menu_items,
        stats=stats,
        recent_jobs=recent_jobs
    ) 
    
    
@app.route("/dashboard")
def dashboard():
    debug_log(f"Accessing dashboard for user {session.get('admin')}")
    
    if "admin" not in session:
        debug_log("No admin in session, redirecting to login")
        return redirect(url_for("login"))
        
    candidates = fetch_dashboard_candidates()
    debug_log(f"Dashboard loaded with {len(candidates)} candidates")
    return render_template("admin_dashboard.html", candidates=candidates)



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
        return jsonify({"success": False, "message": "Unauthorized"}), 401
        
    job_title = request.args.get("job_title", "").strip()
    batch_id = request.args.get("batch_id", "").strip()
    
    # ADDED JOB_DESCRIPTION to SELECT list
    query = """
        SELECT ID, BATCH_ID, FILE_NAME, CANDIDATE_RANK, MATCH_SCORE, 
               JOB_TITLE, DATE_OF_APPLICATION, NAME, EMAIL, PHONE, 
               RAW_DATA, UPLOADED_AT, JOB_DESCRIPTION
        FROM EXCEL_CANDIDATES 
        WHERE 1=1
    """
    params = []
    
    if job_title:
        query += " AND JOB_TITLE = %s"
        params.append(job_title)
    if batch_id:
        query += " AND BATCH_ID = %s"
        params.append(batch_id)
        
    query += " ORDER BY UPLOADED_AT DESC, CANDIDATE_RANK ASC"
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        data = []
        for r in rows:
            uploaded_at_val = r[11]
            if hasattr(uploaded_at_val, 'strftime'):
                uploaded_at_str = uploaded_at_val.strftime('%Y-%m-%d %H:%M')
            else:
                uploaded_at_str = str(uploaded_at_val) if uploaded_at_val else ""

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
                "job_description": str(r[12]) if len(r) > 12 and r[12] else "No Job Description Saved" # ADDED THIS
            })
        cursor.close()
        conn.close()
        return jsonify({"success": True, "data": data})
    except Exception as e:
        debug_log(f"API Error fetching excel candidates: {str(e)}")
        return jsonify({"success": False, "message": str(e)})


@app.route("/excel-upload", methods=["GET", "POST"])
def excel_upload():
    if "admin" not in session:
        return redirect(url_for("login"))

    success_message = None
    message_type = "success"
    job_description = LATEST_OUTPUT["job_description"]

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
        debug_log(f"Failed to fetch existing jobs for dropdown: {str(e)}")

    # -----------------------------------
    # FETCH DISTINCT FILTERS FOR THE PROCESSED DATA TAB
    # -----------------------------------
    distinct_jobs = []
    distinct_batches = []
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Safely fetch unique jobs
        cursor.execute("SELECT DISTINCT JOB_TITLE FROM EXCEL_CANDIDATES WHERE JOB_TITLE IS NOT NULL AND JOB_TITLE != '' ORDER BY JOB_TITLE")
        distinct_jobs = [row[0] for row in cursor.fetchall()]
        
        # Safely fetch unique batches (using GROUP BY instead of DISTINCT to allow ordering by date)
        cursor.execute("SELECT BATCH_ID FROM EXCEL_CANDIDATES WHERE BATCH_ID IS NOT NULL AND BATCH_ID != '' GROUP BY BATCH_ID ORDER BY MAX(UPLOADED_AT) DESC")
        distinct_batches = [row[0] for row in cursor.fetchall()]
        
        cursor.close()
        conn.close()
    except Exception as e:
        debug_log(f"Failed to fetch distinct filters: {str(e)}")

    # -----------------------------------
    # FORM SUBMIT
    # -----------------------------------
    if request.method == "POST":
        try:
            job_description = request.form.get("job_description", "").strip()

            if not job_description:
                return render_template("excel_upload.html", LATEST_OUTPUT=LATEST_OUTPUT, success_message="Please provide Job Description.", message_type="error", job_description=job_description, available_jobs=available_jobs, distinct_jobs=distinct_jobs, distinct_batches=distinct_batches)

            if "file" not in request.files or request.files["file"].filename == "":
                return render_template("excel_upload.html", LATEST_OUTPUT=LATEST_OUTPUT, success_message="Please upload Excel file.", message_type="error", job_description=job_description, available_jobs=available_jobs, distinct_jobs=distinct_jobs, distinct_batches=distinct_batches)

            file = request.files["file"]

            if not allowed_file(file.filename):
                return render_template("excel_upload.html", LATEST_OUTPUT=LATEST_OUTPUT, success_message="Invalid file type. Please upload .xlsx or .xls file.", message_type="error", job_description=job_description, available_jobs=available_jobs, distinct_jobs=distinct_jobs, distinct_batches=distinct_batches)

            debug_log("Starting Excel Processing")
            excel_bytes = BytesIO(file.read())
            result = process_excel_file(excel_bytes, file.filename, job_description)

            df_preview = pd.DataFrame(result["preview_data"])
            table_html = df_preview.to_html(classes="dataframe", index=False)

            LATEST_OUTPUT["preview_data"] = result["preview_data"]
            LATEST_OUTPUT["excel_buffer"] = result["output_excel_buffer"].getvalue()
            LATEST_OUTPUT["job_description"] = job_description
            LATEST_OUTPUT["processed_candidates"] = result["processed_candidates"]
            LATEST_OUTPUT["file_name"] = result["file_name"]
            LATEST_OUTPUT["batch_id"] = result["batch_id"]
            LATEST_OUTPUT["table_html"] = table_html

            success_message = f"Successfully processed {result['total_candidates']} candidates."
            message_type = "success"
        except Exception as e:
            success_message = f"Error: {str(e)}"
            message_type = "error"

    return render_template(
        "excel_upload.html",
        LATEST_OUTPUT=LATEST_OUTPUT,
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

    df = pd.read_excel(file_bytes)
    df = df.fillna("")
    
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

    table_html = df.to_html(

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

        return redirect(
            url_for("excel_upload")
        )

    excel_buffer = BytesIO(

        LATEST_OUTPUT["excel_buffer"]
    )

    excel_buffer.seek(0)

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
        return jsonify({"success": False, "message": "Unauthorized"}), 401
        
    candidates = LATEST_OUTPUT.get("processed_candidates")
    filename = LATEST_OUTPUT.get("file_name")
    batch_id = LATEST_OUTPUT.get("batch_id")
    job_desc = LATEST_OUTPUT.get("job_description", "") # FETCH JD FROM MEMORY
    
    if not candidates:
        return jsonify({"success": False, "message": "No data available to save. Please process an Excel file first."})
        
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        for candidate in candidates:
            rank = candidate.get("rank", 0)
            score = candidate.get("match_score", 0)
            
            job_title = str(candidate.get("Job Title", ""))[:255]
            name = str(candidate.get("Name", candidate.get("Candidate Name", "Unknown")))[:255]
            email = str(candidate.get("Email ID", candidate.get("Email", "Unknown")))[:255]
            phone = str(candidate.get("Phone Number", candidate.get("Contact Details", "")))[:50]
            
            raw_date = candidate.get("Date of application", "")
            if hasattr(raw_date, 'strftime'):
                doa = raw_date.strftime('%Y-%m-%d')
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
        
        return jsonify({"success": True, "message": f"Successfully saved {len(candidates)} candidates to the database! (Batch: {batch_id})"})
    except Exception as e:
        return jsonify({"success": False, "message": f"Database Error: {str(e)}"})


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
        
        debug_log(f"Creating job - ID: {job_id}, Title: {title}, Rounds: {total_rounds}")
        
        full_description = f"""

Job Description:
{description}
"""
        conn = get_connection()
        cursor = conn.cursor()
        try: 
            cursor.execute("SELECT ID FROM JOB_INFO WHERE ID = %s", (job_id,))
            existing = cursor.fetchone()

            if existing:
                debug_log(f"Job ID {job_id} already exists")
                flash("Job ID already exists.", "error")
                return redirect(url_for("create_job"))
        
            # --- UPDATED SQL QUERY to include TOTAL_ROUNDS and ROUND_CONFIG ---
            cursor.execute("""
               INSERT INTO JOB_INFO 
                (ID, JOB_TITLE, JOB_DESCRIPTION, CREATED_AT, NUM_QUESTIONS, INTERVIEW_TIME, TOTAL_ROUNDS, ROUND_CONFIG)
                VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s)
            """, (job_id, title, full_description, num_questions, interview_time, total_rounds, round_config_json))
            
            conn.commit()
            debug_log(f"Job {job_id} created successfully with round configurations")
            flash("Job created successfully!", "success")
        except Exception as e:
            debug_log(f"Error creating job: {str(e)}")
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
    debug_log(f"User {session.get('admin')} logging out")
    session.clear()
    return redirect(url_for('login'))

@app.route("/download/<candidate_id>")
def download_attachment(candidate_id):
    debug_log(f"Downloading attachment for candidate {candidate_id}")
    
    file_data = get_candidate_attachment(candidate_id)

    if not file_data:
        debug_log(f"No file found for candidate {candidate_id}")
        abort(404)

    file_bytes = bytes(file_data["data"])
    debug_log(f"File retrieved, size: {len(file_bytes)} bytes")

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

    try:
        combined_prompt = f"""
            Generate a complete professional job description using the details below.

            Job ID: {id}
            Job Title: {title}
            Experience Required: {experience}
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

        debug_log("Calling AI generation service...")
        generated_text = generate_job_description(combined_prompt)
        debug_log("AI generation successful")
        return jsonify({"generated_text": generated_text})

    except Exception as e:
        debug_log(f"AI generation error: {str(e)}")
        return jsonify({"generated_text": "AI generation failed."})

@app.route("/update-status", methods=["POST"])
def update_status():
    debug_log("Updating candidate status...")
    
    service = get_gmail_service()
    data = request.get_json()
    candidate_id = data.get("candidate_id")
    action = data.get("action")
    debug_log(f"Candidate: {candidate_id}, Action: {action}")

    if action == "SELECT":
        new_status = "SELECTED"
    else:
        new_status = "REJECTED"
    
    debug_log(f"Setting status to: {new_status}")
        
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE CV_INFO
            SET FINAL_STATUS = %s
            WHERE ID = %s
        """, (new_status, candidate_id))
        
        log_event(conn, candidate_id, "pending_approval")

        conn.commit()
        debug_log(f"Status updated to {new_status} for candidate {candidate_id}")

    except Exception as e:
        debug_log(f"Error updating status: {str(e)}")
    finally:
        cursor.close()
        conn.close()

    email = get_candidate_email(candidate_id)
    debug_log(f"Retrieved email: {email}")

    if not email:
            debug_log(f"No email found for candidate {candidate_id}, skipping email dispatch.")
            return jsonify({"message": f"{new_status} updated, but no email was found to notify."})

    try:
            if action == "SELECT":
                debug_log("Sending congratulations email...")
                send_congratulations_email(service, email, candidate_id)
            else:
                debug_log("Sending rejection email...")
                send_rejection_email(service, email, candidate_id)
    except Exception as e:
            debug_log(f"Failed to send email via Gmail API: {str(e)}")
            return jsonify({"message": f"{new_status} updated, but email dispatch failed."})

    return jsonify({"message": f"{new_status} updated and email sent successfully!"})

def get_candidate_email(cv_id):
    debug_log(f"Fetching email for candidate {cv_id}")
    
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT EMAIL
        FROM CV_INFO
        WHERE ID = %s
    """, (cv_id,))

    row = cursor.fetchone()
    debug_log(f"Email found: {row[0] if row else 'Not found'}")

    cursor.close()
    conn.close()

    return row[0] if row else None

def _send(service, to_email, subject, body):
    debug_log(f"Sending email to {to_email} with subject: {subject}")
    
    msg = MIMEText(body)
    msg["to"] = to_email
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(
        userId="me",
        body={"raw": raw}
    ).execute()
    debug_log(f"Email sent successfully to {to_email}")


def send_congratulations_email(service, to_email, candidate_id):
    debug_log(f"Preparing congratulations email for {candidate_id}")
    
    body = (
        "Hello,\n\n"
        "Congratulations!\n\n"
        "We are pleased to inform you that you have successfully cleared the assessment.\n\n"
        f"Candidate ID: {candidate_id}\n"
        "Our team will reach out to you shortly with the next steps in the interview process.\n\n"
        "Recruitment Team"
    )

    _send(
        service,
        to_email,
        f"Congratulations! You Are Shortlisted – {candidate_id}",
        body
    )


def send_rejection_email(service, to_email, candidate_id):
    debug_log(f"Preparing rejection email for {candidate_id}")
    
    body = (
        "Hello,\n\n"
        "Thank you for taking the time to complete the assessment.\n\n"
        f"Candidate ID: {candidate_id}\n"
        "After careful review, we regret to inform you that you have not been selected to proceed further.\n\n"
        "We sincerely appreciate your interest and wish you success in your future endeavors.\n\n"
        "Recruitment Team"
    )

    _send(
        service,
        to_email,
        f"Assessment Result – {candidate_id}",
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
 
    cursor.execute("""
    SELECT ID, JOB_TITLE, JOB_DESCRIPTION, NUM_QUESTIONS, INTERVIEW_TIME, CREATED_AT
    FROM JOB_INFO
    ORDER BY CREATED_AT DESC
        """)
 
    rows = cursor.fetchall()
    debug_log(f"Retrieved {len(rows)} job descriptions")
 
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
            "numeric_id": int(row[0]) if str(row[0]).isdigit() else 0
        }
        for row in rows
    ]
 
    jobs = sorted(jobs, key=lambda x: x["numeric_id"])
    debug_log(f"Sorted {len(jobs)} jobs")
 
    return render_template("jd.html", jobs=jobs, now=datetime.now())
 
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
    debug_log(f"Job {job_id} deleted successfully")
 
    cursor.close()
    conn.close()
 
    return redirect(url_for("job_descriptions"))

@app.route("/update-job-description", methods=["POST"])
def update_job_description():

    if "admin" not in session:
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

    cursor.close()
    conn.close()

    return jsonify({"message": "Updated successfully"})

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
    
@app.route("/candidate-report/<candidate_id>")
def candidate_report(candidate_id):

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
            c.JOBTITLE
        FROM CV_INFO c
        LEFT JOIN CV_TIMESTAMPS t 
            ON c.ID = t.ID
        WHERE c.ID = %s
    """, (candidate_id,))

    candidate = cursor.fetchone()
    print("934 candidate:", candidate)

    if not candidate:
        cursor.close()
        conn.close()
        return "Candidate not found", 404

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

        ["Mail Received", str(candidate[8]) if candidate[8] else "N/A"],
        ["Mail Read", str(candidate[9]) if candidate[9] else "N/A"],
        ["Exam Started", str(candidate[6]) if candidate[6] else "N/A"],
        ["Exam Submitted", str(candidate[7]) if candidate[7] else "N/A"]
    ]

    info_table = Table(info, colWidths=[2 * inch, 4.5 * inch])

    info_table.setStyle([
        ('GRID', (0,0), (-1,-1), 1, colors.grey),
        ('BACKGROUND', (0,0), (0,-1), colors.lightgrey),
    ])

    elements.append(info_table)
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
            answered_time_str = str(r[3]) if r[3] else "Skipped / N/A"
            
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

    doc.build(elements)

    debug_log("PDF report generated successfully")

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

    debug_log(f"Executing query: {query}")
    debug_log(f"Params: {params}")

    cursor.execute(query, params)
    rows = cursor.fetchall()

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
    cursor.close()
    conn.close()

    # 🔥 Generate new link
    link = generate_exam_link(cv_id)

    return jsonify({
        "status": "success",
        "new_link": link
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

    debug_log(f"Export Query: {query}")
    debug_log(f"Params: {params}")

    cursor.execute(query, params)
    rows = cursor.fetchall()

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
            str(col) if col is not None else ""
            for col in row
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
    debug_log("Starting Flask application on 0.0.0.0:5010")
    app.run(host="0.0.0.0", port=5010, ssl_context='adhoc', debug=True)