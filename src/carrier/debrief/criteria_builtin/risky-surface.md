---
name: risky-surface
title: Touches something hard to undo
severity: medium
sees_rationale: true
uses_notes: true
---
Flag changes to things that are expensive to get wrong or hard to reverse:

- public interfaces other code or users depend on (function signatures,
  APIs, CLI flags, plugin or extension hooks),
- persisted data: file formats, save files, database schemas, migrations,
- configuration defaults and build or dependency files,
- deletion or moving of files and data.

Explain in one sentence what could break and for whom.
