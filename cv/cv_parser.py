# import re

# def extract_name_email(text):
#     email_match = re.search(
#         r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text
#     )
#     email = email_match.group() if email_match else None
#     first_line = text.strip().split("\n")[0][:50] if text else None
#     name = first_line
#     return name, email



#-----------------------------------------


import re

from utils.logger import debug_log, warning_log, mask_email


def extract_name_email(text):
    debug_log(f"Starting extract_name_email - Text length: {len(text) if text else 0}")

    email_match = re.search(
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text
    )

    email = email_match.group() if email_match else None

    first_line = text.strip().split("\n")[0][:50] if text else None

    name = first_line

    if email:
        debug_log(f"Email found in CV text: {mask_email(email)}")
    else:
        warning_log("No email address found in CV text")
    if name:
        debug_log(f"Name taken from first line: {name[:30]}")
    else:
        warning_log("No name found in CV text (empty text)")
    return name, email
