---
name: cross-platform
title: Cross-platform
theme: Reliability
mode: files
due: changes
days: 30
severity: medium
---
Flag code that would break on another operating system: hard-coded path separators or drive letters,
case-sensitive file name assumptions, shell commands or tools that exist on only one system, line-ending
assumptions, file locking and permissions that differ (Windows can't delete or rename open files), text
encodings left to the system default, and limits such as long Windows paths. Say which system breaks and how.
