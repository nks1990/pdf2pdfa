import os
import subprocess
from pathlib import Path
import shutil
import pytest
from pdf2pdfa.converter import Converter

DATA_DIR = Path(__file__).parent / 'data'


def test_convert_basic(tmp_path):
    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    assert output_pdf.exists()

    if shutil.which('verapdf') is None:
        pytest.skip('verapdf command not available')

    cmd = ['verapdf', '-q', '--exit-zero', str(output_pdf)]
    if os.name == 'nt':
        cmd = ['cmd', '/c'] + cmd
    subprocess.run(cmd, check=True)

