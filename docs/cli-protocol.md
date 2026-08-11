# CLI protocol

Every executed command result is one JSON document using protocol version 1.
Built-in `--help` remains human-readable text and is outside the machine result
protocol. Success envelopes are written to stdout:

```json
{
  "protocol_version": 1,
  "command": "transaction.list",
  "status": "listed",
  "data": {}
}
```

Error envelopes, including argument-parser failures, are written to stderr:

```json
{
  "protocol_version": 1,
  "command": "cli",
  "status": "error",
  "error": {
    "code": "invalid_arguments",
    "message": "...",
    "details": {}
  }
}
```

Exit codes have stable meanings:

| Code | Meaning |
| --- | --- |
| `0` | The requested outcome was achieved. |
| `1` | The command ran and returned valid evidence, but the requested outcome was not achieved; for example, `doctor` found an unsafe state. |
| `2` | The request or execution failed. |

Exit code `1` may carry either envelope type. For example, an unhealthy `doctor`
result is a success envelope on stdout, while policy blockers such as
`review_required` are error envelopes on stderr. Clients must select the stream
from the envelope type rather than assuming that every not-achieved result uses
stdout.

Known domain failures retain a specific error code. An unexpected exception is
reported as a generic `execution_error` without its exception message, paths, or
traceback. `--debug` re-raises unexpected exceptions for local development and
must not be used when output may cross a trust boundary.

Clients must branch on `command`, `status`, structured `data`, and `error.code`.
They must not infer state from human-readable messages. Transaction recovery
decisions use the `state`, `allowed_actions`, and `blocking_reasons` returned by
`vault-pr transaction show`.
