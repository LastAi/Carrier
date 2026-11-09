---
name: robustness
title: Error handling and robustness
theme: Reliability
mode: files
due: changes
days: 21
severity: medium
---
Flag code that can fail badly or silently: errors that are caught and ignored; broad catches that hide
bugs; missing handling of files, folders, network or processes that may not exist or may fail; resources
not closed; unchecked results; assumptions about input that a user or another program could break
(empty, very large, malformed, unexpected encoding); race conditions between threads or processes.
Say what goes wrong and when.
