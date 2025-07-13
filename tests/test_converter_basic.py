import subprocess
from pathlib import Path

from pdf2pdfa.converter import Converter

DATA_DIR = Path(__file__).parent / 'data'


def test_convert_basic(tmp_path):
    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    assert output_pdf.exists()

    subprocess.run(['verapdf', '-q', '--exit-zero', str(output_pdf)], check=True)
