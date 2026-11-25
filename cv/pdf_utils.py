from io import BytesIO

from pypdf import PdfReader

from utils.logger import debug_log, info_log, warning_log, error_log


def _extract_with_pypdf(pdf_bytes):
    """Text per page with pypdf. Returns (text, page_count, empty_pages, failed_pages)."""
    reader = PdfReader(BytesIO(pdf_bytes))
    parts, empty_pages, failed_pages = [], 0, []
    for page_num, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as e:
            # pypdf can crash on unusual fonts (e.g. KeyError 'bbox' when a font has no FontBBox)
            warning_log(f"pypdf could not read page {page_num}: {type(e).__name__}: {e}")
            failed_pages.append(page_num)
            page_text = ""
        if not page_text.strip():
            empty_pages += 1
        parts.append(page_text)
    return "".join(parts), len(reader.pages), empty_pages, failed_pages


def _extract_with_pdfplumber(pdf_bytes):
    """Fallback extractor (pdfminer based) that handles fonts pypdf cannot."""
    import pdfplumber
    parts = []
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            try:
                parts.append(page.extract_text() or "")
            except Exception as e:
                warning_log(f"pdfplumber could not read page {page_num}: {type(e).__name__}: {e}")
        return "\n".join(parts), len(pdf.pages)


def extract_text_from_pdf_bytes(pdf_bytes):
    debug_log(f"Starting PDF text extraction - PDF size: {len(pdf_bytes)} bytes")

    text, page_count, empty_pages, failed_pages = "", 0, 0, []
    try:
        text, page_count, empty_pages, failed_pages = _extract_with_pypdf(pdf_bytes)
        debug_log(f"pypdf extraction - Pages: {page_count}, Empty pages: {empty_pages}, "
                  f"Failed pages: {failed_pages or 'none'}, Text length: {len(text)} chars")
    except Exception as e:
        warning_log(f"pypdf could not open the PDF: {type(e).__name__}: {e}")
        failed_pages = ["all"]

    # some pages failed, or nothing was extracted: try pdfplumber and keep whichever got more text
    if failed_pages or not text.strip():
        try:
            fallback_text, fallback_pages = _extract_with_pdfplumber(pdf_bytes)
            info_log(f"PDF fallback extractor (pdfplumber) used - Pages: {fallback_pages}, "
                     f"Text length: {len(fallback_text)} chars (pypdf had {len(text)})")
            if len(fallback_text.strip()) > len(text.strip()):
                text, page_count = fallback_text, fallback_pages or page_count
        except Exception as e:
            error_log(f"Fallback PDF extraction (pdfplumber) failed: {type(e).__name__}: {e}")

    if not text.strip():
        warning_log(f"PDF has no extractable text ({page_count} page(s), {len(pdf_bytes)} bytes) - "
                    f"possibly a scanned/image-only PDF")
    return text
