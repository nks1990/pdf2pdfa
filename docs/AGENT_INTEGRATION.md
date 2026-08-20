# Agent and headless integration

`pdf2pdfa` v5 is designed to run without a GUI, external PDF engine or runtime service. Agents can integrate either through the Python API or the versioned JSON CLI.

## Python API

For an agent running in the same Python process, prefer the public library API:

```python
from pdf2pdfa import Converter

converter = Converter(
    level="2b",
    fidelity="auto",
    max_input_bytes=256 * 1024 * 1024,
)

inspection = converter.inspect("input.pdf")
if not inspection.repairable:
    for blocker in inspection.plan.blockers:
        print(blocker.code, blocker.path, blocker.message)
else:
    result = converter.convert("input.pdf", "output.pdf")
    assert result.validation.compliant
```

`inspect()` is a dry-run of the same pre-repair preparation path used by `convert()`: input limits, decryption, signature policy, explicit font preprocessing, target-profile serialization, owned validation and repair planning.

The installed runtime remains dependency-free and does not open a network listener.

## Machine JSON CLI

For subprocess/tool-calling agents, use `--json`:

```bash
pdf2pdfa inspect input.pdf --level 2b --json
pdf2pdfa convert input.pdf output.pdf --level 2b --json
pdf2pdfa validate output.pdf --level 2b --json
pdf2pdfa batch one.pdf two.pdf --level 2b --json
```

When `--json` is active, stdout contains exactly one JSON document for normal command completion, blockers, validation failures, usage errors and runtime errors. Human-readable error text is not emitted to stderr for these machine responses.

## Protocol envelope

Schema version 1 uses this top-level shape:

```json
{
  "schema_version": "1",
  "pdf2pdfa_version": "5.0.0",
  "ok": true,
  "status": "converted",
  "exit_code": 0,
  "command": "convert",
  "result": {}
}
```

`schema_version` versions the machine contract independently from the package version. Agents should branch on `schema_version` before consuming fields from a future incompatible schema.

`ok` means the requested outcome was achieved. It does not merely mean that Python executed successfully.

Examples:

- compliant validation: `ok=true`, `status=compliant`, exit `0`;
- non-compliant validation: `ok=false`, `status=invalid`, exit `1`, with the validation report under `result`;
- repairable inspection: `ok=true`, `status=repairable`, exit `0`;
- blocked inspection: `ok=false`, `status=blocked`, exit `2`, with blockers under `result.plan.blockers`;
- successful conversion: `ok=true`, `status=converted` or `passthrough`, exit `0`;
- partially failed batch: `ok=false`, `status=partial_failure`, exit `1`.

## Structured errors

Execution/usage failures contain `error` instead of `result`:

```json
{
  "schema_version": "1",
  "pdf2pdfa_version": "5.0.0",
  "ok": false,
  "status": "blocked",
  "exit_code": 2,
  "command": "convert",
  "error": {
    "code": "SIGNATURE_INVALIDATION_BLOCKED",
    "type": "SignatureInvalidationError",
    "category": "blocked",
    "message": "input contains an applied digital signature; rewriting would invalidate it",
    "retryable": false
  }
}
```

Stable v1 error codes include:

| Code | Category | Typical agent action |
|---|---|---|
| `INPUT_NOT_FOUND` | `invalid_input` | fix/provide the path |
| `INPUT_LIMIT_EXCEEDED` | `invalid_input` | reject or change an explicit policy |
| `INVALID_PASSWORD` | `invalid_input` | optionally retry with another credential |
| `UNSUPPORTED_SECURITY_HANDLER` | `blocked` | stop or use a different workflow |
| `SIGNATURE_INVALIDATION_BLOCKED` | `blocked` | request explicit invalidation policy |
| `UNSUPPORTED_REPAIR` | `blocked` | inspect blockers; do not retry blindly |
| `FIDELITY_REJECTED` | `blocked` | investigate preservation failure |
| `OWNED_VALIDATION_FAILED` | `operational_error` | treat as conversion/release-quality failure |
| `INVALID_PDF` | `invalid_input` | reject or repair upstream |
| `INVALID_SECURITY_STRUCTURE` | `invalid_input` | reject malformed encryption structure |
| `INVALID_ARGUMENT` | `invalid_input` | fix tool arguments |
| `IO_ERROR` | `operational_error` | retry only when filesystem state may change |
| `PIPELINE_ERROR` | `operational_error` | inspect message/log context |
| `USAGE_ERROR` | `usage_error` | fix CLI invocation |
| `INTERRUPTED` | `interrupted` | retry only if desired |
| `INTERNAL_ERROR` | `operational_error` | treat as a software defect until classified |

Agents should prefer `error.code` and `error.category` over matching error-message text.

## Exit-code contract

| Exit | Meaning |
|---:|---|
| `0` | requested outcome achieved / inspection is actionable |
| `1` | domain-level negative result such as non-compliance or partial batch failure |
| `2` | blocker, invalid input/usage or operational failure |
| `130` | interrupted |

The JSON envelope repeats the exit code so orchestration layers that only receive stdout do not need shell-specific status handling.

## Credentials and fonts

Do not place passwords directly in command-line arguments. Use `PDF2PDFA_PASSWORD` or `--password-file`.

Explicit font repair is available to both conversion and inspection:

```bash
pdf2pdfa inspect input.pdf --font-dir ./fonts --json
pdf2pdfa convert input.pdf output.pdf --font-dir ./fonts --json
```

There is no implicit system-font substitution.

## Future MCP/HTTP wrappers

The core package intentionally does not ship an HTTP server, FastAPI dependency or MCP runtime. A remote service should be a separate adapter that imports the Python API and reuses `pdf2pdfa.agent_protocol` so the owned PDF engine remains zero-dependency and network-agnostic.
