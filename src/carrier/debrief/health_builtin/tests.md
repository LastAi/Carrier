---
name: tests
title: Tests
theme: Reliability
mode: project
due: changes
days: 21
severity: medium
---
Judge the project's tests as a whole. Flag important behaviour with no test at all (name the code),
tests that can't fail (no real assertion, everything mocked away), tests that depend on timing, order
or the machine they run on, skipped or disabled tests with no reason given, and test helpers that hide
what a test checks. Prefer a few high-value gaps over a long list.
