# # import requests
# # from dbConnection.connection import get_connection

# # OLLAMA_URL = "http://localhost:11434/api/generate"
# # MODEL_NAME = "gpt-oss:120b-cloud"

# # def score_response_and_save(cv_id, ques_id, question, user_answer):
# #     prompt = f"""
# # You are an evaluator.

# # Question:
# # "{question}"

# # User Answer:
# # "{user_answer}"

# # Evaluate strictly.
# # Return ONLY a number from 0 to 5.

# # 5 = fully correct
# # 4 = mostly correct
# # 3 = partially correct
# # 2 = could do better
# # 1 = weak
# # 0 = incorrect or null
# # """

# #     try:
# #         # Call Ollama
# #         payload = {
# #             "model": MODEL_NAME,
# #             "prompt": prompt,
# #             "stream": False,
# #             "options": {"temperature": 0}
# #         }

# #         res = requests.post(OLLAMA_URL, json=payload, timeout=120)
# #         res.raise_for_status()

# #         score_text = res.json()["response"].strip()

# #         try:
# #             score = int(score_text)
# #             if score < 1 or score > 5:
# #                 score = 1
# #         except:
# #             score = 1

# #         # Update score in DB using CV_ID + QUES_ID
# #         conn = get_connection()
# #         cursor = conn.cursor()

# #         cursor.execute(
# #             """
# #             UPDATE RESPONSE_TABLE
# #             SET SCORE = ?
# #             WHERE ID = ? AND QUES_ID = ?
# #             """,
# #             (score, cv_id, ques_id)
# #         )

# #         conn.commit()
# #         cursor.close()
# #         conn.close()

# #         print(f"Scored CV_ID={cv_id}, QUES_ID={ques_id}, SCORE={score}")

# #     except Exception as e:
# #         print("LLM scoring failed:", e)

# # import requests
# # from db.connection import get_connection

# # OLLAMA_URL = "http://localhost:11434/api/generate"
# # MODEL_NAME = "gpt-oss:120b-cloud"

# # def score_response_and_save(cv_id, ques_id, question, user_answer):

# #     if not user_answer or user_answer.strip() == "":
# #         score = 0

# #         conn = get_connection()
# #         cursor = conn.cursor()

# #         cursor.execute(
# #             """
# #             UPDATE RESPONSE_TABLE
# #             SET SCORE = ?
# #             WHERE ID = ? AND QUES_ID = ?
# #             """,
# #             (score, cv_id, ques_id)
# #         )

# #         conn.commit()
# #         cursor.close()
# #         conn.close()

# #         print(f"Empty answer → CV_ID={cv_id}, QUES_ID={ques_id}, SCORE=0")
# #         return   

# #     prompt = f"""
# # You are an evaluator.

# # Question:
# # "{question}"

# # User Answer:
# # "{user_answer}"

# # Evaluate strictly.
# # Return ONLY a number from 0 to 5.

# # 5 = fully correct
# # 4 = mostly correct
# # 3 = partially correct
# # 2 = could do better
# # 1 = weak
# # 0 = incorrect
# # """

# #     try:
# #         payload = {
# #             "model": MODEL_NAME,
# #             "prompt": prompt,
# #             "stream": False,
# #             "options": {"temperature": 0}
# #         }

# #         res = requests.post(OLLAMA_URL, json=payload, timeout=120)
# #         res.raise_for_status()

# #         score_text = res.json()["response"].strip()

        
# #         try:
# #             score = int(score_text)
# #             if score < 0 or score > 5:
# #                 score = 1
# #         except:
# #             score = 1

       
# #         conn = get_connection()
# #         cursor = conn.cursor()

# #         cursor.execute(
# #             """
# #             UPDATE RESPONSE_TABLE
# #             SET SCORE = ?
# #             WHERE ID = ? AND QUES_ID = ?
# #             """,
# #             (score, cv_id, ques_id)
# #         )

# #         conn.commit()
# #         cursor.close()
# #         conn.close()

# #         print(f"Scored CV_ID={cv_id}, QUES_ID={ques_id}, SCORE={score}")

# #     except Exception as e:
# #         print("LLM scoring failed:", e)

# import requests
# from db.connection import get_connection
# OLLAMA_URL = "http://localhost:11434/api/generate"
# MODEL_NAME = "gpt-oss:120b-cloud"
# def score_with_llm(question, answer):
#     if not answer or not answer.strip():
#         return 0

#     payload = {
#         "model": MODEL_NAME,
#         "prompt": f"""
#         Score the following answer from 0 to 5.

#         Question:
#         {question}

#         Answer:
#         {answer}

#         Only return a number between 0 and 5.
#         """,
#         "stream": False
#     }

#     try:
#         response = requests.post(OLLAMA_URL, json=payload, timeout=60)
#         response.raise_for_status()

#         data = response.json()
#         score_text = data.get("response", "").strip()

#         return int(score_text)

#     except Exception as e:
#         print("LLM ERROR:", e)
#         return 0
#---------------------------------------------
import requests
from db.connection import get_connection

from utils.logger import debug_log, info_log, warning_log, error_log, log_duration, short

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gpt-oss:120b-cloud"
# MODEL_NAME = "gemma3:4b"

def score_with_llm(question, answer):
    debug_log(f"Starting answer scoring - Question length: {len(question)} chars, Answer length: {len(answer) if answer else 0} chars")

    if not answer or not answer.strip():
        info_log("Answer is empty - technical score 0 (LLM not called)")
        return 0

    payload = {
        "model": MODEL_NAME,
        "prompt": f"""
        Score the following answer from 0 to 5.

        Question:
        {question}

        Answer:
        {answer}

        Only return a number between 0 and 5.
        """,
        "stream": False
    }
    debug_log(f"Scoring request - Model: {MODEL_NAME}, Prompt length: {len(payload['prompt'])} chars, Timeout: 60s")

    try:
        with log_duration(f"LLM answer scoring ({MODEL_NAME})", level="DEBUG"):
            response = requests.post(OLLAMA_URL, json=payload, timeout=60)
        debug_log(f"Ollama response status: {response.status_code}")

        response.raise_for_status()

        data = response.json()

        score_text = data.get("response", "").strip()
        debug_log(f"LLM score output: '{short(score_text, 100)}'")

        score = int(score_text)

        if score < 0 or score > 5:
            warning_log(f"LLM score {score} is outside 0-5 - using 1")
            score = 1

        info_log(f"Technical answer score: {score}/5")
        return score

    except ValueError as e:
        warning_log(f"LLM score output is not an integer ('{short(score_text, 100)}') - using default score 1: {e}")
        return 1

    except requests.exceptions.Timeout as e:
        error_log(f"Ollama scoring timed out after 60s ({MODEL_NAME}) - score 0: {e}")
        return 0

    except requests.exceptions.RequestException as e:
        error_log(f"Ollama scoring request failed ({MODEL_NAME}) - score 0: {e}")
        return 0

    except Exception as e:
        error_log(f"Unexpected error during LLM scoring ({type(e).__name__}) - score 0: {e}")
        return 0

def score_communication_with_llm(question, answer):
    debug_log(f"Starting communication scoring - Answer length: {len(answer) if answer else 0} chars")

    if not answer or not answer.strip():
        info_log("Answer is empty - communication score 0 (LLM not called)")
        return 0

    payload = {
        "model": MODEL_NAME,
        "prompt": f"""
        Evaluate the COMMUNICATION QUALITY of the answer.

        Focus on:
        - Clarity
        - Grammar
        - Sentence structure
        - Professional tone
        - Fluency

        Question:
        {question}

        Answer:
        {answer}

        Return ONLY a number between 0 and 5.
        """,
        "stream": False
    }

    debug_log(f"Communication scoring request - Model: {MODEL_NAME}, Prompt length: {len(payload['prompt'])} chars, Timeout: 60s")

    try:
        with log_duration(f"LLM communication scoring ({MODEL_NAME})", level="DEBUG"):
            response = requests.post(OLLAMA_URL, json=payload, timeout=60)
        debug_log(f"Ollama response status: {response.status_code}")
        response.raise_for_status()

        data = response.json()
        score_text = data.get("response", "").strip()
        debug_log(f"LLM communication score output: '{short(score_text, 100)}'")

        score = int(score_text)

        if score < 0 or score > 5:
            warning_log(f"LLM communication score {score} is outside 0-5 - using 1")
            score = 1

        info_log(f"Communication score: {score}/5")
        return score

    except ValueError as e:
        warning_log(f"Communication score output is not an integer - score 0: {e}")
        return 0
    except Exception as e:
        error_log(f"Communication scoring failed ({MODEL_NAME}) - score 0: {e}")
        return 0