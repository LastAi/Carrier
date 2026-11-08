---
name: secrets
title: Secrets and credentials
severity: high
sees_rationale: false
uses_notes: false
---
Flag anything that looks like a real secret being written into the code or its files:

- API keys and tokens (e.g. `sk-…`, `ghp_…`, `AKIA…`, `xox…`), passwords, connection strings with a
  password in them, private keys (`-----BEGIN … PRIVATE KEY-----`), session cookies;
- `.env`, key or credential files being added, or their contents copied into code, logs, docs or tests;
- secrets printed to logs or error messages, or sent somewhere they shouldn't go.

Don't flag obvious placeholders (`YOUR_KEY_HERE`, `xxxx`, `example`), test fixtures clearly marked as fake,
or code that reads a secret from the environment or a secrets store without exposing it.
