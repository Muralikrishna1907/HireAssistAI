from db.connection import get_connection
from db.cv_repository import insert_cv, update_cv
from vector.vector_store import load_store, save_store
from utils.pdf_utils import extract_text
from utils.logger import debug_log, info_log, warning_log, log_duration

with log_duration("Vector store load", level="DEBUG"):
    vector_store = load_store()


def process_cv(cv_id, name, email, pdf_bytes):
    info_log(f"Processing CV {cv_id} - PDF size: {len(pdf_bytes) if pdf_bytes else 0} bytes")

    conn = get_connection()

    # 1. Save CV
    insert_cv(conn, cv_id, name, email, pdf_bytes)
    debug_log(f"CV {cv_id} saved")

    # 2. Extract text
    with log_duration(f"PDF text extraction for CV {cv_id}", level="DEBUG"):
        text = extract_text(pdf_bytes)
    debug_log(f"CV {cv_id} text extracted - {len(text) if text else 0} chars")

    # 3. Match
    with log_duration(f"Vector search for CV {cv_id}", level="DEBUG"):
        results = vector_store.search(text, k=1)

    if results:
        best = results[0]

        score = 1 / (1 + best["distance"])

        update_cv(conn, cv_id, {
            "job_id": best["meta"]["job_id"],
            "job_title": best["meta"]["job_title"],
            "score": score * 100
        })
        info_log(f"CV {cv_id} matched to job {best['meta']['job_id']} ({best['meta']['job_title']}) - score {score * 100:.2f}")
    else:
        warning_log(f"No vector match found for CV {cv_id}")

    conn.close()
