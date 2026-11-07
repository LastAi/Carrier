---
name: design-drift
title: Drifts from the project notes
severity: high
sees_rationale: false
uses_notes: true
---
Compare each change with the project notes (design docs, CLAUDE.md, README,
review notes). Flag a change when it:

- contradicts a decision, rule or constraint written in the notes,
- implements something differently from how the notes describe it,
- quietly changes behaviour the notes promise (formats, workflows, naming
  conventions, folder layout).

Quote the specific note line in `evidence` and name the note file. Do not
flag changes the notes simply don't mention — only real conflicts.
