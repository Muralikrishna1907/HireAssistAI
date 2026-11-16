import inspect
from datetime import datetime

def get_current_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_log_context():
    frame = inspect.currentframe().f_back.f_back
    # Handles both Windows (\\) and Mac/Linux (/) file paths
    filename = inspect.getframeinfo(frame).filename.replace('\\', '/').split('/')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    return filename, function_name, line_number

def debug_log(message):
    file, func, line = get_log_context()
    print(f"[{get_current_time()}] [DEBUG] {file}::{func}:{line} - {message}")

def info_log(message):
    file, func, line = get_log_context()
    print(f"[{get_current_time()}] [INFO] {file}::{func}:{line} - {message}")

def warning_log(message):
    file, func, line = get_log_context()
    print(f"[{get_current_time()}] [WARNING] {file}::{func}:{line} - {message}")

def error_log(message):
    file, func, line = get_log_context()
    print(f"[{get_current_time()}] [ERROR] {file}::{func}:{line} - {message}")