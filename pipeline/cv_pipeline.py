from db.connection import get_connection
from db.cv_repository import insert_cv, update_cv
from vector.vector_store import load_store, save_store
from utils.pdf_utils import extract_text

vector_store = load_store()


def process_cv(cv_id, name, email, pdf_bytes):

    conn = get_connection()

    # 1. Save CV
    insert_cv(conn, cv_id, name, email, pdf_bytes)

    # 2. Extract text
    text = extract_text(pdf_bytes)

    # 3. Match
    results = vector_store.search(text, k=1)

    if results:
        best = results[0]

        score = 1 / (1 + best["distance"])

        update_cv(conn, cv_id, {
            "job_id": best["meta"]["job_id"],
            "job_title": best["meta"]["job_title"],
            "score": score * 100
        })

    conn.close()