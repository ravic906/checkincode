"""
Extracts plain text from an uploaded resume (PDF or DOCX) so it can be fed
to the interview LLM as context. No OCR -- scanned/image-only PDFs will
yield little or no text, which is an acceptable MVP gap.
"""

import io

import pdfplumber
from docx import Document

MAX_RESUME_CHARS = 8000


class UnsupportedResumeFormat(Exception):
    pass


def extract_text(filename: str, file_bytes: bytes) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        text = _extract_pdf(file_bytes)
    elif lower.endswith(".docx"):
        text = _extract_docx(file_bytes)
    else:
        raise UnsupportedResumeFormat("Only .pdf and .docx resumes are supported.")

    text = text.strip()
    if not text:
        raise UnsupportedResumeFormat(
            "Couldn't extract any text from that file -- it may be a scanned "
            "image rather than a text-based document."
        )
    return text[:MAX_RESUME_CHARS]


def _extract_pdf(file_bytes: bytes) -> str:
    parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                parts.append(page_text)
    return "\n".join(parts)


def _extract_docx(file_bytes: bytes) -> str:
    doc = Document(io.BytesIO(file_bytes))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
