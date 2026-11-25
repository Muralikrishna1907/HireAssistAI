"""
Demo data for the interview-rounds feature (Online Assessment -> Technical -> HR).

Creates one demo job (ID "DEMO-RND") and nine demo candidates, one per scenario,
so every state can be seen on the Applications page and the Dashboard.

All rows are clearly marked: job ID "DEMO-RND", candidate IDs "DEMO-RND-01" ... "DEMO-RND-09",
emails @example.com, and round decisions by "demo-seed".

Usage (from the project folder):
    python scripts/seed_demo_rounds.py            # dry run: show what would be created
    python scripts/seed_demo_rounds.py --apply    # create (re-creates if already present)
    python scripts/seed_demo_rounds.py --remove   # delete every demo row again
"""
import json
import os
import struct
import sys
import zlib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.connection import get_connection  # noqa: E402
from utils.logger import debug_log, info_log, error_log  # noqa: E402

JOB_ID = "DEMO-RND"
PREFIX = JOB_ID + "-"
NUM_QUESTIONS = 5          # each answer is scored out of 5, so the max total is 25

ROUND_CONFIG = {
    "round_1": {"name": "Online Assessment", "pass": "60", "pending_min": "40", "pending_max": "59", "fail": "39"},
    "round_2": {"name": "Technical", "marks": "10"},
    "round_3": {"name": "HR", "marks": "5"},
}

# score = interview score in %, None = has not taken the assessment
# rounds = {round_no: (decision, marks, remarks)}
SCENARIOS = [
    dict(n=1, name="Demo Arjun Rao", status="NOT SHORTLISTED", match=0.28, score=None,
         story="CV not shortlisted - never reaches the rounds"),
    dict(n=2, name="Demo Bhavya Iyer", status="SHORTLISTED", match=0.66, score=None,
         story="Shortlisted, assessment link sent, not taken yet"),
    dict(n=3, name="Demo Charan Das", status="SHORTLISTED", match=0.61, score=50,
         story="Assessment done (50%), assessment decision pending"),
    dict(n=4, name="Demo Divya Menon", status="SHORTLISTED", match=0.58, score=30,
         story="Assessment failed (30%) - ended at Online Assessment"),
    dict(n=5, name="Demo Eshan Kapoor", status="SHORTLISTED", match=0.74, score=78,
         story="Passed assessment (78%) - Technical decision due"),
    dict(n=6, name="Demo Farah Khan", status="SHORTLISTED", match=0.81, score=85,
         rounds={2: ("PASSED", 8, "Strong Java and Spring fundamentals.")},
         story="Passed Technical (8/10) - HR decision due"),
    dict(n=7, name="Demo Gautam Shah", status="SHORTLISTED", match=0.69, score=72,
         rounds={2: ("REJECTED", 4, "Struggled with collections and concurrency questions.")},
         story="Rejected in Technical (4/10)"),
    dict(n=8, name="Demo Harini Pillai", status="SHORTLISTED", match=0.88, score=90,
         rounds={2: ("PASSED", 9, "Excellent problem solving."), 3: ("PASSED", 5, "Good culture fit, joins in 30 days.")},
         story="Cleared all rounds (Technical 9/10, HR 5/5)"),
    dict(n=9, name="Demo Imran Ali", status="SHORTLISTED", match=0.77, score=82,
         rounds={2: ("PASSED", 7, "Solid fundamentals."), 3: ("REJECTED", 2, "Notice period of 90 days not acceptable.")},
         story="Passed Technical (7/10), rejected in HR (2/5)"),
]


def demo_png(rgb=(59, 130, 246), size=96):
    """A small solid-colour PNG used as the captured 'face' image."""
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def remove(cursor):
    like = PREFIX + "%"
    counts = {}
    cursor.execute("SHOW TABLES LIKE 'round_schedules'")
    if cursor.fetchall():   # interview schedules (created by the admin app on first use)
        cursor.execute("DELETE FROM ROUND_SCHEDULES WHERE CV_ID LIKE %s", (like,))
        counts["ROUND_SCHEDULES"] = cursor.rowcount
    for table, col in [("CANDIDATE_ROUND_RESULTS", "CV_ID"), ("RESPONSE_TABLE", "ID"), ("QUESTIONS_TABLE", "ID"),
                       ("CV_TIMESTAMPS", "ID"), ("CV_INFO", "ID")]:
        cursor.execute(f"DELETE FROM {table} WHERE {col} LIKE %s", (like,))
        counts[table] = cursor.rowcount
    cursor.execute("DELETE FROM JOB_INFO WHERE ID = %s", (JOB_ID,))
    counts["JOB_INFO"] = cursor.rowcount
    debug_log(f"Existing demo rows deleted: {counts}")
    return counts


def apply(cursor):
    now = datetime.now().replace(microsecond=0)
    face = demo_png()

    cursor.execute("""
        INSERT INTO JOB_INFO (ID, JOB_TITLE, JOB_DESCRIPTION, CREATED_AT, NUM_QUESTIONS, INTERVIEW_TIME, TOTAL_ROUNDS, ROUND_CONFIG)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (JOB_ID, "Demo - Java Developer (3 rounds)",
          "Demo job created by scripts/seed_demo_rounds.py to show the Online Assessment, Technical and HR rounds.",
          now - timedelta(days=10), NUM_QUESTIONS, 30, 3, json.dumps(ROUND_CONFIG)))
    debug_log(f"Demo job {JOB_ID} inserted")

    for sc in SCENARIOS:
        cv_id = f"{PREFIX}{sc['n']:02d}"
        applied = now - timedelta(days=9, hours=sc["n"])
        took = sc["score"] is not None
        shortlisted = sc["status"] == "SHORTLISTED"
        rounds = sc.get("rounds", {})

        # CURRENT_ROUND: 0 = not in rounds, 1 = assessment, 2 = Technical, 3 = HR
        current = 0
        if shortlisted:
            current = 1
            if took and sc["score"] >= 60:
                current = 2
                if rounds.get(2, ("",))[0] == "PASSED":
                    current = 3

        cursor.execute("""
            INSERT INTO CV_INFO (ID, NAME, EMAIL, STATUS, MATCH_SCORE, JOB_ID, JOBTITLE, FACE_IMAGE, CREATED_AT,
                                 SKILLS, EXPERIENCE, EXAM_LINK_USED, CURRENT_ROUND, EVALUATION_REASON)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (cv_id, sc["name"], f"demo.candidate{sc['n']:02d}@example.com", sc["status"], sc["match"], JOB_ID,
              "Demo - Java Developer (3 rounds)", face if took else None, applied,
              "Java, Spring Boot, SQL", 2 + sc["n"] % 5, 1 if took else 0, current, "DEMO: " + sc["story"]))

        cursor.execute("""
            INSERT INTO CV_TIMESTAMPS (ID, mail_received, assessment_mail_sent, image_taken, start_exam, end_exam, response_submit)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (cv_id, applied,
              applied + timedelta(hours=2) if shortlisted else None,
              applied + timedelta(days=1) if took else None,
              applied + timedelta(days=1) if took else None,
              applied + timedelta(days=1, minutes=30) if took else None,
              applied + timedelta(days=1, minutes=30) if took else None))

        if took:
            # 5 questions scored out of 5 each -> total = score% of 25, spread evenly
            total = round(sc["score"] / 100 * NUM_QUESTIONS * 5, 2)
            per_q = round(total / NUM_QUESTIONS, 2)
            for q in range(1, NUM_QUESTIONS + 1):
                qid = f"Q_{cv_id}_{q:02d}"
                score = per_q if q < NUM_QUESTIONS else round(total - per_q * (NUM_QUESTIONS - 1), 2)
                cursor.execute("INSERT INTO QUESTIONS_TABLE (ID, PROG_LANG, QUESTION, QUES_ID) VALUES (%s, %s, %s, %s)",
                               (cv_id, "Java", f"Demo question {q}", qid))
                cursor.execute("""INSERT INTO RESPONSE_TABLE (ID, QUES_ID, RESPONSE, SCORE, ANSWERED_AT)
                                  VALUES (%s, %s, %s, %s, %s)""",
                               (cv_id, qid, "Demo answer", score, applied + timedelta(days=1, minutes=5 * q)))

        for round_no, (decision, marks, remarks) in sorted(rounds.items()):
            cursor.execute("""
                INSERT INTO CANDIDATE_ROUND_RESULTS (CV_ID, ROUND_NO, ROUND_NAME, DECISION, MARKS, REMARKS, DECIDED_BY, DECIDED_AT)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (cv_id, round_no, ROUND_CONFIG[f"round_{round_no}"]["name"], decision, marks, remarks,
                  "demo-seed", applied + timedelta(days=2 + round_no)))

        debug_log(f"Demo candidate {cv_id} inserted - status {sc['status']}, current round {current}, "
                  f"assessment {'taken' if took else 'not taken'}, round decisions: {len(rounds)}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--dry-run"
    if mode not in ("--dry-run", "--apply", "--remove"):
        sys.exit(__doc__)

    print(f"Demo job {JOB_ID}: Online Assessment -> Technical (10 marks) -> HR (5 marks)")
    for sc in SCENARIOS:
        print(f"  {PREFIX}{sc['n']:02d}  {sc['name']:<20} {sc['story']}")
    if mode == "--dry-run":
        print("\nDry run only. Use --apply to create these rows or --remove to delete them.")
        return

    info_log(f"Seeding demo rounds data - mode {mode}")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS CANDIDATE_ROUND_RESULTS (
                ID INT AUTO_INCREMENT PRIMARY KEY, CV_ID VARCHAR(255) NOT NULL, ROUND_NO INT NOT NULL,
                ROUND_NAME VARCHAR(255), DECISION VARCHAR(20) NOT NULL, MARKS DECIMAL(10,2) NULL, REMARKS TEXT,
                DECIDED_BY VARCHAR(100), DECIDED_AT DATETIME NOT NULL, UNIQUE KEY UQ_CV_ROUND (CV_ID, ROUND_NO))
        """)
        removed = remove(cursor)
        if mode == "--apply":
            apply(cursor)
        conn.commit()
    except Exception as e:
        error_log(f"Demo data {mode} failed, rolling back: {e}")
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    if mode == "--remove":
        info_log("Removed demo rows: " + ", ".join(f"{t}={n}" for t, n in removed.items()))
    else:
        info_log(f"Created demo job {JOB_ID} with {len(SCENARIOS)} candidates (old demo rows replaced: "
                 + ", ".join(f"{t}={n}" for t, n in removed.items())
                 + f"). Open Applications and filter by job {JOB_ID}.")


if __name__ == "__main__":
    main()
