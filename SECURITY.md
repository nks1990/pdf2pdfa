# Security policy

## Supported versions

Security fixes are applied to the current supported major release. Users should upgrade to the latest release before reporting a suspected vulnerability as unresolved.

## Reporting a vulnerability

Please do not publish exploit details in a public issue before maintainers have had a reasonable opportunity to investigate. Use GitHub's private vulnerability reporting feature for this repository when available.

A useful report includes:

- the affected `pdf2pdfa` version or commit;
- operating system and Python version;
- whether Ghostscript or veraPDF was involved;
- a minimal reproducer or carefully sanitized sample PDF;
- expected versus observed behavior;
- whether the problem permits code execution, path access, secret disclosure, denial of service or document corruption.

Do not include real passwords, private keys, confidential PDFs or other secrets in a report.

## Threat model

PDFs are complex, attacker-controlled containers. `pdf2pdfa` treats input documents as untrusted.

The project is designed to reduce several avoidable risks:

### Password disclosure

PDF passwords are accepted through the Python API, `--password-file`, or `PDF2PDFA_PASSWORD`. There is intentionally no plaintext `--password TEXT` CLI option because command-line arguments may be observable by other local processes or process-monitoring systems.

When an encrypted PDF requires an external backend, pikepdf decrypts it in-process to a private temporary working file. Ghostscript receives only that working PDF and never receives the password.

### Partial or corrupt output

Backends write to a temporary candidate. In strict validation mode, veraPDF checks that candidate before publication. The destination is replaced atomically only after the pipeline succeeds.

### Digital signatures

Rewriting a signed PDF can invalidate its signature. Applied signatures are rejected by default. Conversion requires explicit `allow_signature_invalidation=True` or the corresponding CLI option.

An empty signature form field is not considered an applied signature.

### Resource exhaustion

Callers processing untrusted uploads can configure `max_input_bytes` or `--max-input-mib`. This is an input-size guard, not a complete denial-of-service sandbox: a small malicious PDF can still expand into expensive content.

Production services should additionally enforce process-level CPU, memory, wall-clock and filesystem quotas around PDF processing.

### External tools

Ghostscript and veraPDF are external executables. Keep them patched independently of `pdf2pdfa` and obtain them from trusted sources.

`pdf2pdfa` does not bundle Ghostscript. The Ghostscript backend invokes safer mode (`-dSAFER`) and grants read permission only to the generated ICC profile needed by the PDF/A definition, but this should not be treated as a substitute for operating-system sandboxing when processing hostile files at scale.

### Temporary files

Temporary working files are created through Python's temporary-directory facilities and are removed when the conversion scope exits. Applications with unusually strong confidentiality requirements should place their temporary directory on an encrypted filesystem and configure platform-specific access controls.

## Out of scope guarantees

`pdf2pdfa` is not:

- an antivirus scanner;
- a malware sandbox;
- a digital-signature validator;
- a general-purpose PDF sanitizer for active-content security;
- a redaction tool.

PDF/A conversion may remove or rewrite active or incompatible features, but that is a conformance operation rather than a security certification.
