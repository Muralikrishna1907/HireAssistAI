# from sentence_transformers import util
# from db.cv_repository import get_all_job_embeddings, generate_missing_job_embeddings, get_job_by_id
# from config import MATCH_THRESHOLD
# import inspect

# def debug_log(message):
#     frame = inspect.currentframe().f_back
#     filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
#     function_name = frame.f_code.co_name
#     line_number = frame.f_lineno
#     print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

# def calculate_skill_match_score(cv_skills, jd_skills):
#     if not jd_skills:
#         return 0.0, []

#     def normalize(skill):
#         return skill.lower().replace("(", "").replace(")", "").strip()

#     cv_set = set([normalize(s) for s in cv_skills])
#     jd_set = set([normalize(s) for s in jd_skills])

#     matched = []

#     for jd_skill in jd_set:
#         for cv_skill in cv_set:
#             if jd_skill in cv_skill or cv_skill in jd_skill:
#                 matched.append(jd_skill)
#                 break

#     score = len(matched) / len(jd_set) if jd_set else 0.0
#     debug_log(f"Normalized CV Skills: {cv_set}")
#     debug_log(f"Normalized JD Skills: {jd_set}")
#     debug_log(f"Matched Skills: {matched}")

#     return round(score, 4), matched
# def calculate_experience_score(cv_exp, jd_exp):
#     import re

#     try:
#         cv_exp = float(cv_exp)
#     except:
#         cv_exp = 0.0

#     match = re.search(r"\d+(\.\d+)?", str(jd_exp))
#     debug_log(f"JD Experience raw: {jd_exp}")
#     jd_exp_val = float(match.group()) if match else 0.0

#     if jd_exp_val == 0:
#         return 0.0

#     return round(min(cv_exp / jd_exp_val, 1.0), 4)


# def calculate_final_score(skill_score, exp_score, embedding_score):
#     return round(
#     0.4 * skill_score +   # reduced
#     0.2 * exp_score +
#     0.4 * embedding_score,  # increased
#     4
# )

# def match_cv_to_best_job(model, cv_text, conn):
#     debug_log(f"Starting match_cv_to_best_job - CV text length: {len(cv_text)} characters")
    
#     # cleaned_cv = clean_text(cv_text)
#     debug_log("Generating missing job embeddings")
#     generate_missing_job_embeddings(model, conn)
#     debug_log("Missing job embeddings generation completed")
    
#     debug_log("Encoding CV text to embedding")
#     cv_embedding = model.encode(cv_text, convert_to_tensor=True)
#     debug_log(f"CV embedding generated - Shape: {cv_embedding.shape}")
    
#     debug_log("Fetching all job embeddings from database")
#     jobs = get_all_job_embeddings(conn)
#     debug_log(f"Retrieved {len(jobs)} jobs from database")
    
#     best_score = -1
#     best_job_title = None
#     best_job_description = None
    
#     for job_index, job in enumerate(jobs, start=1):
#         debug_log(f"Calculating similarity for job {job_index}/{len(jobs)} - Job ID: {job['id']}, Title: {job['title']}")
        
#         score = util.cos_sim(cv_embedding, job["embedding"]).item()
#         debug_log(f"Similarity score calculated: {score:.4f}")
        
#         if score > best_score:
#             debug_log(f"New best match found - Score: {score:.4f} > Previous: {best_score:.4f}")
#             best_score = score
#             best_job_title = job["title"]
#             best_job_description = job["description"]
    
#     if best_job_title is None:
#         debug_log("No matching job found - Returning None values")
#         return None, None, 0.0
    
#     final_score = round(best_score, 4)
#     debug_log(f"match_cv_to_best_job completed - Best Job: {best_job_title}, Final Score: {final_score}")
#     return best_job_title, best_job_description, final_score
    
# def match_cv_to_specific_job(model, cv_text, job_id, conn, cv_data):
#     debug_log(f"Starting match_cv_to_specific_job - Job_ID: {job_id}, CV text length: {len(cv_text)} characters")
    
#     debug_log("Generating missing job embeddings")
#     generate_missing_job_embeddings(model, conn)
#     debug_log("Missing job embeddings generation completed")

#     debug_log(f"Fetching job details for Job_ID: {job_id}")
#     job = get_job_by_id(conn, job_id)
#     debug_log(f"Job fetch completed - Found: {job is not None}")

#     if not job:
#         debug_log(f"Job not found for Job_ID: {job_id} - Returning default values")
#         return None, None, 0.0

#     debug_log("Encoding CV text to embedding")
#     cv_embedding = model.encode(cv_text, convert_to_tensor=True)
#     debug_log(f"CV embedding generated - Shape: {cv_embedding.shape}")
    
#     debug_log(f"Calculating similarity between CV and Job_ID: {job_id}")
    
#     # 🔥 embedding score (keep this)
#     embedding_score = util.cos_sim(cv_embedding, job["embedding"]).item()

#     # 🔥 skill match
#     cv_skills = cv_data.get("skills", [])
#     jd_skills = job.get("required_skills", "")  # ✅ correct column

#     jd_skills_list = [s.strip() for s in jd_skills.split(",") if s.strip()]

#     skill_score, matched_skills = calculate_skill_match_score(cv_skills, jd_skills_list)
#     debug_log(f"JD Skills RAW: {jd_skills}")
#     debug_log(f"JD Skills LIST: {jd_skills_list}")
#     # 🔥 experience
#     cv_exp = cv_data.get("total_experience", 0)
#     jd_exp = job.get("experience_required", "0")  # or correct column name

#     exp_score = calculate_experience_score(cv_exp, jd_exp)

#     # 🔥 final score
#     score = calculate_final_score(skill_score, exp_score, embedding_score)

#     debug_log(f"Skill Score: {skill_score}, Matched: {matched_skills}")
#     debug_log(f"Experience Score: {exp_score}")
#     debug_log(f"Embedding Score: {embedding_score}")
#     debug_log(f"Final Score: {score}")

#     final_score = round(score, 4)
#     debug_log(f"match_cv_to_specific_job completed - Job Title: {job['title']}, Final Score: {final_score}")
#     return job["title"], job["description"], final_score

# def match_cv_to_other_jobs(model, cv_text, exclude_job_id, conn):
#     debug_log(f"Starting match_cv_to_other_jobs - Excluding Job_ID: {exclude_job_id}, CV text length: {len(cv_text)} characters")
#     debug_log(f"Match threshold: {MATCH_THRESHOLD}")
    
#     debug_log("Encoding CV text to embedding")
#     cv_embedding = model.encode(cv_text, convert_to_tensor=True)
#     debug_log(f"CV embedding generated - Shape: {cv_embedding.shape}")
    
#     debug_log("Fetching all job embeddings from database")
#     jobs = get_all_job_embeddings(conn)
#     debug_log(f"Retrieved {len(jobs)} total jobs from database")
    
#     matched_jobs = []
#     jobs_checked = 0
    
#     for job_index, job in enumerate(jobs, start=1):
#         if job["id"] == exclude_job_id:
#             debug_log(f"Skipping excluded job - Job_ID: {exclude_job_id}")
#             continue
        
#         jobs_checked += 1
#         debug_log(f"Checking job {jobs_checked} - Job_ID: {job['id']}, Title: {job['title']}")
        
#         score = util.cos_sim(cv_embedding, job["embedding"]).item()
#         debug_log(f"Similarity score calculated: {score:.4f} vs threshold {MATCH_THRESHOLD}")
        
#         if score >= MATCH_THRESHOLD:
#             rounded_score = round(score, 4)
#             debug_log(f"Match found - Job_ID: {job['id']}, Title: {job['title']}, Score: {rounded_score}")
#             matched_jobs.append({
#                 "id": job["id"],
#                 "title": job["title"],
#                 "score": rounded_score
#             })
#         else:
#             debug_log(f"Score below threshold - Job_ID: {job['id']}, Title: {job['title']}, Score: {score:.4f}")
    
#     debug_log(f"match_cv_to_other_jobs completed - Total matches found: {len(matched_jobs)}/{jobs_checked} jobs checked")
#     return matched_jobs


#-------------------------------------------------------------------------------------------------------------

from sentence_transformers import util
from db.cv_repository import get_all_job_embeddings, generate_missing_job_embeddings, get_job_by_id
from config import MATCH_THRESHOLD
import inspect

def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")


# ---------------------------------------------------
# BEST JOB MATCH (EMBEDDING ONLY)
# ---------------------------------------------------
def match_cv_to_best_job(model, cv_text, conn):
    debug_log(f"Starting match_cv_to_best_job - CV text length: {len(cv_text)} characters")
    
    generate_missing_job_embeddings(model, conn)
    
    cv_embedding = model.encode(cv_text, convert_to_tensor=True)
    debug_log(f"CV embedding generated - Shape: {cv_embedding.shape}")
    
    jobs = get_all_job_embeddings(conn)
    debug_log(f"Retrieved {len(jobs)} jobs from database")
    
    best_score = -1
    best_job_title = None
    best_job_description = None
    
    for job in jobs:
        score = util.cos_sim(cv_embedding, job["embedding"]).item()
        
        if score > best_score:
            best_score = score
            best_job_title = job["title"]
            best_job_description = job["description"]
    
    if best_job_title is None:
        return None, None, 0.0
    
    final_score = round(best_score, 4)
    debug_log(f"Best Job: {best_job_title}, Score: {final_score}")
    
    return best_job_title, best_job_description, final_score


# ---------------------------------------------------
# SPECIFIC JOB MATCH (EMBEDDING ONLY)
# ---------------------------------------------------
def match_cv_to_specific_job(model, cv_text, job_id, conn, cv_data):
    debug_log(f"Starting match_cv_to_specific_job - Job_ID: {job_id}")

    generate_missing_job_embeddings(model, conn)

    job = get_job_by_id(conn, job_id)

    if not job:
        debug_log(f"Job not found for Job_ID: {job_id}")
        return None, None, 0.0

    cv_embedding = model.encode(cv_text, convert_to_tensor=True)

    # 🔥 ONLY EMBEDDING SCORE
    score = util.cos_sim(cv_embedding, job["embedding"]).item()

    final_score = round(score, 4)

    debug_log(f"Embedding Score: {final_score}")
    debug_log(f"match_cv_to_specific_job completed - Job Title: {job['title']}")

    return job["title"], job["description"], final_score


# ---------------------------------------------------
# OTHER JOB MATCH (EMBEDDING ONLY)
# ---------------------------------------------------
def match_cv_to_other_jobs(model, cv_text, exclude_job_id, conn):
    debug_log(f"Starting match_cv_to_other_jobs - Excluding Job_ID: {exclude_job_id}")
    
    cv_embedding = model.encode(cv_text, convert_to_tensor=True)
    
    jobs = get_all_job_embeddings(conn)
    
    matched_jobs = []

    for job in jobs:
        if job["id"] == exclude_job_id:
            continue
        
        score = util.cos_sim(cv_embedding, job["embedding"]).item()

        if score >= MATCH_THRESHOLD:
            matched_jobs.append({
                "id": job["id"],
                "title": job["title"],
                "score": round(score, 4)
            })
    
    debug_log(f"Other matches found: {len(matched_jobs)}")

    return matched_jobs