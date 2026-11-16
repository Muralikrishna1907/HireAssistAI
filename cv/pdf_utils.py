# from pypdf import PdfReader
# from io import BytesIO

# def extract_text_from_pdf_bytes(pdf_bytes):
#     reader = PdfReader(BytesIO(pdf_bytes))
#     text = ""
#     for page in reader.pages:
#         text += page.extract_text() or ""
#     return text

#-----------------------------------------
from pypdf import PdfReader
from io import BytesIO
import inspect

def debug_log(message):
    frame = inspect.currentframe().f_back
    filename = inspect.getframeinfo(frame).filename.split('\\')[-1]
    function_name = frame.f_code.co_name
    line_number = frame.f_lineno
    print(f"[DEBUG] {filename}::{function_name}:{line_number} - {message}")

def extract_text_from_pdf_bytes(pdf_bytes):
    debug_log(f"Starting extract_text_from_pdf_bytes with PDF size: {len(pdf_bytes)} bytes")
    
    reader = PdfReader(BytesIO(pdf_bytes))
    debug_log(f"PdfReader initialized successfully")
    
    text = ""
    debug_log(f"Total pages in PDF: {len(reader.pages)}")
    
    for page_num, page in enumerate(reader.pages):
        debug_log(f"Extracting text from page {page_num + 1}")
        page_text = page.extract_text() or ""
        debug_log(f"Page {page_num + 1} extracted - Text length: {len(page_text)} characters")
        text += page_text
    
    debug_log(f"Text extraction completed - Total text length: {len(text)} characters")
    return text