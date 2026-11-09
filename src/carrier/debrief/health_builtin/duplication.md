---
name: duplication
title: Duplication
theme: Design
mode: project
due: changes
days: 30
severity: low
---
Find logic that is written out more than once: copied functions, near-identical blocks that differ in a
name or constant, parallel implementations of the same rule, the same parsing or validation done in
several places. Flag only duplication that would cost something — a fix applied to one copy and missed in
another. Name every copy (file and line) and suggest where the shared version should live. Ignore
boilerplate the language forces, and tests that repeat setup on purpose.
