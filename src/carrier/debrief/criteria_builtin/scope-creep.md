---
name: scope-creep
title: Unrequested changes
severity: medium
sees_rationale: false
uses_notes: false
---
Compare each change with what the user actually asked for (the requests
listed under "What the user asked for"). Flag a change when it goes beyond
the requests, for example:

- refactors, renames or reformatting nobody asked for,
- new features, options or dependencies not requested,
- edits to files unrelated to any request.

Small, necessary supporting edits (an import, a type fix needed for the
requested change) are fine — do not flag those. If there is no session
record for a change, say so in the reason rather than assuming.
