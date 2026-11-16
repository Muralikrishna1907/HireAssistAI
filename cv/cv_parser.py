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
import inspect

def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

def extract_name_email(text):
    debug_log(f"Starting extract_name_email with text length: {len(text) if text else 0}")
    
    email_match = re.search(
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text
    )
    debug_log(f"Email regex search completed")
    
    email = email_match.group() if email_match else None
    debug_log(f"Email extracted: {email}")
    
    first_line = text.strip().split("\n")[0][:50] if text else None
    debug_log(f"First line extracted: {first_line[:30] if first_line else 'None'}...")
    
    name = first_line
    debug_log(f"Name assigned: {name[:20] if name else 'None'}")
    
    debug_log(f"Returning - Name: {name[:20] if name else None}, Email: {email}")
    return name, email