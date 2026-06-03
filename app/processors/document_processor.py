from pathlib import Path
import fitz
import docx


def extract_pdf(path: str) -> str:
    document = fitz.open(path)
    return "\n".join(page.get_text() for page in document)


def extract_docx(path: str) -> str:
    document = docx.Document(path)
    return "\n".join(p.text for p in document.paragraphs)


def extract_text_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def process_document(path: str) -> str:
    suffix = Path(path).suffix.lower()

    if suffix == ".pdf":
        return extract_pdf(path)

    if suffix == ".docx":
        return extract_docx(path)

    if suffix in [".txt", ".md"]:
        return extract_text_file(path)

    raise ValueError(f"Unsupported file type: {suffix}")