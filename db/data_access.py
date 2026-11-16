# from db.connection import get_connection

# def get_candidate_attachment(candidate_id):
#     conn = get_connection()
#     cursor = conn.cursor()

#     cursor.execute(
#         "SELECT ATTACHMENT FROM CV_INFO WHERE ID = ?",
#         (candidate_id,)
#     )

#     row = cursor.fetchone()
#     cursor.close()
#     conn.close()

#     if not row or not row[0]:
#         return None

#     return {
#         "data": row[0]   # memoryview (expected)
#     }

#---------------------------------------
from db.connection import get_connection
import inspect

def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

def get_candidate_attachment(candidate_id):
    debug_log(f"Starting get_candidate_attachment - Candidate_ID: {candidate_id}")
    
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT ATTACHMENT FROM CV_INFO WHERE ID = %s",
        (candidate_id,)
    )
    debug_log(f"Database query executed for Candidate_ID: {candidate_id}")

    row = cursor.fetchone()
    debug_log(f"Row fetched - Found: {row is not None}")
    
    cursor.close()
    conn.close()
    debug_log("Database connection closed")

    if not row or not row[0]:
        debug_log(f"No attachment found for Candidate_ID: {candidate_id}")
        return None

    debug_log(f"Attachment retrieved successfully - Data size: {len(row[0])} bytes")
    
    return {
        "data": row[0]   # memoryview (expected)
    }