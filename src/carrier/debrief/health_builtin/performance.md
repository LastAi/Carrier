---
name: performance
title: Performance hotspots
theme: Reliability
mode: files
due: changes
days: 30
severity: medium
---
Flag code likely to be slow where it matters: work repeated inside loops (queries, file reads, process
starts, regex compiles), loading whole large files or datasets into memory, quadratic algorithms over
data that can grow, blocking calls on a UI or event-loop thread, and missing caching of expensive results
that are reused. Say roughly how bad it gets and at what size. Ignore code that runs once or on tiny data.
