# # from hdbcli import dbapi
# # from dotenv import load_dotenv
# # import os

# # load_dotenv()

# # def get_connection():
# #     return dbapi.connect(
# #         address=os.getenv("HANA_HOST"),
# #         port=int(os.getenv("HANA_PORT", 443)),
# #         user=os.getenv("HANA_USER"),
# #         password=os.getenv("HANA_PASSWORD"),
# #     )

# #---------------------------------------
# from hdbcli import dbapi
# from dotenv import load_dotenv
# import os
# import inspect

# def debug_log(message):
#     frame = inspect.currentframe().f_back
#     filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
#     function_name = frame.f_code.co_name
#     line_number = frame.f_lineno
#     print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

# load_dotenv()
# debug_log("Environment variables loaded from .env file")

# def get_connection():
#     debug_log("Attempting to establish database connection")
    
#     hana_host = os.getenv("HANA_HOST")
#     hana_port = int(os.getenv("HANA_PORT", 443))
#     hana_user = os.getenv("HANA_USER")
#     debug_log(f"Connection parameters - Host: {hana_host}, Port: {hana_port}, User: {hana_user}")
    
#     connection = dbapi.connect(
#         address=hana_host,
#         port=hana_port,
#         user=hana_user,
#         password=os.getenv("HANA_PASSWORD"),
#     )
#     debug_log("Database connection established successfully")
    
#     return connection

import os
import pymysql
from dotenv import load_dotenv

load_dotenv(override=True)

print("🔥 USING CONNECTION FILE:", __file__)
print("🔥 DB_USER FROM ENV:", os.getenv("DB_USER"))

def get_connection():
    print("Connected to DB as:", os.getenv("DB_USER"))

    connection = pymysql.connect(
        host=os.getenv("DB_HOST") or "127.0.0.1",
        port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        autocommit=False
    )

    return connection