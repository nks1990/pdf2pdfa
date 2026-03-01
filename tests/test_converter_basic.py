import os
import subprocess
from pathlib import Path
import shutil
import pytest
from xml.etree import ElementTree
from pdf2pdfa.converter import Converter

DATA_DIR = Path(__file__).parent / 'data'


def test_convert_basic(tmp_path):
    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    assert output_pdf.exists()


def test_convert_verapdf(tmp_path):
    """Run verapdf validation and parse XML output for 0 failures."""
    if shutil.which('verapdf') is None:
        pytest.skip('verapdf command not available')

    input_pdf = DATA_DIR / 'sample.pdf'
    output_pdf = tmp_path / 'output.pdf'

    conv = Converter()
    conv.convert(str(input_pdf), str(output_pdf))

    cmd = ['verapdf', '--format', 'xml', str(output_pdf)]
    if os.name == 'nt':
        cmd = ['cmd', '/c'] + cmd
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    if result.stdout.strip():
        root = ElementTree.fromstring(result.stdout)
        # Look for failed rules
        failed = root.findall('.//{http://www.verapdf.org/ValidationProfile}failedChecks')
        if not failed:
            failed = root.findall('.//failedChecks')
        for f in failed:
            count = int(f.get('failedRules', f.get('count', '0')))
            assert count == 0, f"verapdf reported {count} failed rules"
