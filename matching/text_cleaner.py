import re

from utils.logger import debug_log


def clean_text(text):
    original_length = len(text) if text else 0
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    text = text.strip()
    debug_log(f"Text cleaned - {original_length} chars in, {len(text)} chars out")
    return text
