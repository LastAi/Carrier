---
name: silent-failure
title: Hides errors
severity: medium
sees_rationale: false
uses_notes: false
---
Flag changes that make failures invisible:

- exceptions caught and ignored, or replaced with a default value,
- errors downgraded to log messages without being handled,
- fallbacks that hide that the main path failed,
- removed validation or input checks.

A clearly intentional, explained fallback is fine; say what would now go
unnoticed if something breaks.
