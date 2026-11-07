---
name: tests-weakened
title: Tests removed or weakened
severity: high
sees_rationale: false
uses_notes: false
---
Flag any change that reduces how much the tests actually check:

- deleted or skipped tests (`skip`, `xfail`, `@Ignore`, commented out),
- loosened assertions (exact value replaced by "not null", wider
  tolerances, catching broad exceptions in a test),
- expected values edited to match new output without an explanation,
- test fixtures or mocks changed so a test can no longer fail.

New tests and stricter tests are good — do not flag them.
