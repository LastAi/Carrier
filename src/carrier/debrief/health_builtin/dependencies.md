---
name: dependencies
title: Dependencies and build
theme: Upkeep
mode: project
due: changes
days: 30
severity: medium
---
Look at the project's dependencies and build setup (package manifests, lock files, build scripts, CI
configuration). Flag dependencies that are declared but unused, used but not declared, pinned
inconsistently, or duplicated in different versions; build steps that only work on one machine (hard-coded
paths, tools assumed to be installed); and scripts that would fail on a clean checkout. Don't guess about
security advisories or latest versions: you can't look them up.
