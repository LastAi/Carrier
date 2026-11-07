---
name: security
title: Security-sensitive
severity: high
sees_rationale: false
uses_notes: false
---
Flag changes that may introduce a security problem:

- secrets, tokens or passwords in code or config,
- shell commands, SQL or file paths built from untrusted input,
- disabled verification (TLS, signatures, auth checks, sandboxing),
- downloading or executing code, deserialising untrusted data,
- widened file or network permissions.

Only flag concrete risks visible in the diff; do not flag general hygiene.
