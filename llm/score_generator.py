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
import inspect

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gpt-oss:120b-cloud"
# MODEL_NAME = "gemma3:4b"

def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

def score_with_llm(question, answer):
    debug_log(f"Starting score_with_llm - Question length: {len(question)} characters, Answer length: {len(answer) if answer else 0} characters")
    
    if not answer or not answer.strip():
        debug_log("Answer is empty or None - returning score 0")
        return 0
    
    debug_log(f"Answer validation passed - Trimmed answer length: {len(answer.strip())} characters")

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
    debug_log(f"Request payload prepared - Model: {MODEL_NAME}, Stream: False")

    try:
        debug_log(f"Sending request to Ollama API - URL: {OLLAMA_URL}, Timeout: 60s")
        response = requests.post(OLLAMA_URL, json=payload, timeout=60)
        debug_log(f"Ollama API response received - Status Code: {response.status_code}")
        
        response.raise_for_status()
        debug_log("Response validation passed - Status code indicates success")

        data = response.json()
        debug_log("Response JSON parsed successfully")
        
        score_text = data.get("response", "").strip()
        debug_log(f"Score text extracted from response: '{score_text}'")

        score = int(score_text)
        debug_log(f"Score converted to integer: {score}")
        
        if score < 0 or score > 5:
            debug_log(f"Score {score} out of valid range [0-5] - clamping to 1")
            score = 1
        
        debug_log(f"Final score: {score}")
        return score

    except ValueError as e:
        debug_log(f"ValueError: Could not convert '{score_text}' to integer - Error: {str(e)}, Using default score: 1")
        return 1
    
    except requests.exceptions.Timeout as e:
        debug_log(f"Timeout Error: Ollama API did not respond within 60s - Error: {str(e)}")
        return 0
    
    except requests.exceptions.RequestException as e:
        debug_log(f"Request Error: Failed to connect to Ollama API - Error: {str(e)}")
        return 0
    
    except Exception as e:
        debug_log(f"Unexpected Error during LLM scoring - Error: {str(e)}, Type: {type(e).__name__}")
        return 0
    
def score_communication_with_llm(question, answer):
    debug_log("Starting communication scoring")

    if not answer or not answer.strip():
        debug_log("Empty answer → communication score = 0")
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

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=60)
        response.raise_for_status()

        data = response.json()
        score_text = data.get("response", "").strip()

        score = int(score_text)

        if score < 0 or score > 5:
            debug_log(f"Invalid communication score {score}, setting to 1")
            score = 1

        debug_log(f"Communication score: {score}")
        return score

    except Exception as e:
        debug_log(f"Communication scoring failed: {str(e)}")
        return 0