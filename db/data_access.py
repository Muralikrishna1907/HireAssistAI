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
from utils.logger import debug_log, warning_log


def get_candidate_attachment(candidate_id):
    debug_log(f"Fetching CV attachment for candidate_id={candidate_id}")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT ATTACHMENT FROM CV_INFO WHERE ID = %s",
        (candidate_id,)
    )

    row = cursor.fetchone()

    cursor.close()
    conn.close()

    if not row or not row[0]:
        warning_log(f"No CV attachment found for candidate_id={candidate_id} (row found: {row is not None})")
        return None

    debug_log(f"CV attachment retrieved for candidate_id={candidate_id} - {len(row[0])} bytes")

    return {
        "data": row[0]   # memoryview (expected)
    }
