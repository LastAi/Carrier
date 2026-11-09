---
name: magic-numbers
title: Magic numbers and config
theme: Design
mode: files
due: changes
days: 30
severity: low
---
Flag unexplained literal values that control behaviour (timeouts, limits, sizes, retry counts, URLs,
paths, thresholds), especially when the same value appears in several places or a user would want to
change it. Suggest a named constant or a setting, and where it belongs. Ignore obvious values (0, 1, an
empty string) and values whose meaning is clear from the line.
