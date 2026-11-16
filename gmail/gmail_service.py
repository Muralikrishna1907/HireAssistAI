# import os
# from google.oauth2.credentials import Credentials
# from google_auth_oauthlib.flow import InstalledAppFlow
# from google.auth.transport.requests import Request
# from googleapiclient.discovery import build
# from config import SCOPES, TOKEN_FILE, CREDENTIALS_FILE

# def get_gmail_service():
#     creds = None

#     if os.path.exists(TOKEN_FILE):
#         creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

#     if not creds or not creds.valid:
#         if creds and creds.expired and creds.refresh_token:
#             creds.refresh(Request())
#         else:
#             flow = InstalledAppFlow.from_client_secrets_file(
#                 CREDENTIALS_FILE, SCOPES
#             )
#             creds = flow.run_local_server(port=0)

#         with open(TOKEN_FILE, "w") as f:
#             f.write(creds.to_json())

#     return build("gmail", "v1", credentials=creds)
#-----------------------------------------

import os
import inspect
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from config import SCOPES, TOKEN_FILE, CREDENTIALS_FILE

def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

def get_gmail_service():
    debug_log("Starting get_gmail_service")
    
    creds = None
    debug_log(f"Checking if token file exists - Path: {TOKEN_FILE}")

    if os.path.exists(TOKEN_FILE):
        debug_log("Token file found - loading credentials from file")
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        debug_log(f"Credentials loaded successfully - Valid: {creds.valid}")
    else:
        debug_log("Token file not found")

    if not creds or not creds.valid:
        debug_log("Credentials invalid or missing - checking refresh token")
        
        if creds and creds.expired and creds.refresh_token:
            debug_log("Credentials expired but refresh token available - refreshing")
            creds.refresh(Request())
            debug_log("Credentials refreshed successfully")
        else:
            debug_log("No valid credentials or refresh token - initiating OAuth flow")
            debug_log(f"Using credentials file: {CREDENTIALS_FILE}")
            
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, SCOPES
            )
            debug_log("OAuth flow initialized - starting local server")
            
            creds = flow.run_local_server(port=0)
            debug_log("OAuth flow completed - credentials obtained")

        debug_log(f"Saving credentials to token file: {TOKEN_FILE}")
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        debug_log("Credentials saved successfully")

    debug_log("Building Gmail service")
    service = build("gmail", "v1", credentials=creds)
    debug_log("Gmail service built successfully - Ready for API calls")
    
    return service