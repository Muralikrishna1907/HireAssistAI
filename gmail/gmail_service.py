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

import logging
import os
import time
from contextlib import contextmanager
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from config import SCOPES, TOKEN_FILE, CREDENTIALS_FILE
from utils.logger import debug_log, info_log, warning_log, log_duration

_logger = logging.getLogger("hireassist")      # same logger utils.logger configures


@contextmanager
def gmail_call(action, level="DEBUG"):
    """Times one Gmail API call:  with gmail_call("Gmail send to x"): ...
    Logs '<action> completed in N ms' at `level`; on failure logs an ERROR (a rate limit,
    HTTP 429, is called out explicitly) and re-raises, so the caller's flow is unchanged."""
    start = time.perf_counter()
    try:
        yield
    except HttpError as e:
        elapsed = (time.perf_counter() - start) * 1000
        status = getattr(getattr(e, "resp", None), "status", None)
        if str(status) == "429":
            _logger.error(f"Gmail API RATE LIMIT hit (HTTP 429 Too Many Requests) during {action} "
                          f"after {elapsed:.0f} ms - Gmail is throttling this account: {e}",
                          exc_info=True, stacklevel=3)
        else:
            _logger.error(f"Gmail API HTTP {status} error during {action} after {elapsed:.0f} ms: {e}",
                          exc_info=True, stacklevel=3)
        raise
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        _logger.error(f"{action} failed after {elapsed:.0f} ms: {e}", exc_info=True, stacklevel=3)
        raise
    elapsed = (time.perf_counter() - start) * 1000
    _logger.log(getattr(logging, level.upper(), logging.DEBUG), f"{action} completed in {elapsed:.0f} ms",
                stacklevel=3)


def get_gmail_service():
    debug_log(f"Initialising Gmail service - token file: {TOKEN_FILE}")

    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        debug_log(f"Gmail token loaded - valid: {creds.valid}, expired: {creds.expired}, "
                  f"has refresh token: {bool(creds.refresh_token)}")
    else:
        warning_log(f"Gmail token file not found: {TOKEN_FILE}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            info_log("Gmail access token expired - refreshing with refresh token")
            with log_duration("Gmail token refresh"):
                creds.refresh(Request())
        else:
            warning_log(f"No valid Gmail credentials or refresh token - starting interactive OAuth flow "
                        f"(browser login required) using {CREDENTIALS_FILE}")
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, SCOPES
            )
            with log_duration("Gmail interactive OAuth flow"):
                creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        info_log(f"Gmail credentials saved to {TOKEN_FILE}")

    with log_duration("Build Gmail API client", level="DEBUG"):
        service = build("gmail", "v1", credentials=creds)

    return service
