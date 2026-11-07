---
name: magic-numbers
title: Unexplained constants
severity: low
uses_notes: false
---
Flag new numeric or string constants that control behaviour (timeouts,
limits, balance values, thresholds, retry counts) when there is no name,
comment or note explaining where the value comes from.

Do not flag: 0, 1, -1, array indices, obvious unit conversions, test data.
