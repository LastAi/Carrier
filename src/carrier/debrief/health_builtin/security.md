---
name: security
title: Security sweep
theme: Safety
mode: files
due: always
days: 14
severity: high
---
Look for security problems in the code as it stands: injection (shell, SQL, path, HTML/script), unsafe
deserialisation, secrets or credentials in code or config, weak or missing permission checks, files
written or read outside where they should be, unsafe temporary files, untrusted input reaching
dangerous calls, insecure defaults, and data sent somewhere it shouldn't go. Explain how it could be
exploited, and how likely that is for this project. Don't flag theoretical issues with no realistic path.
