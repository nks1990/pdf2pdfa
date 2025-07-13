import base64
from pathlib import Path


def ensure_sample_pdf() -> Path:
    b64_path = Path(__file__).parent / 'data' / 'sample.pdf.b64'
    pdf_path = Path(__file__).parent / 'data' / 'sample.pdf'
    if not pdf_path.exists():
        pdf_bytes = base64.b64decode(b64_path.read_text())
        pdf_path.write_bytes(pdf_bytes)
    return pdf_path


def pytest_configure(config):
    ensure_sample_pdf()
