from setuptools import setup, find_packages

setup(
    name='pdf2pdfa',
    version='0.1.0',
    description='Convert arbitrary PDF files to PDF/A-1b',
    author='nks1990',
    author_email='nks1990@example.com',
    packages=find_packages(),
    package_data={'pdf2pdfa': ['data/sRGB.icc.b64']},
    include_package_data=True,
    install_requires=[
        'pikepdf>=4.0.0',
        'fonttools>=4.0.0',
        'lxml>=4.0.0',
        'click>=7.0',
        'importlib_resources; python_version<"3.9"',
        'reportlab>=4.0.0',
    ],
    extras_require={'test': ['pytest>=6.0']},
    entry_points={'console_scripts': ['pdf2pdfa=pdf2pdfa.cli:cli']},
    python_requires='>=3.7',
    license='MIT',
)
