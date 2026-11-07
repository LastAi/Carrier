---
name: callers-updated
title: Changed function, callers not updated
severity: medium
explore: true
uses_notes: false
---
For each function, method or class whose signature or behaviour changed,
use Grep to find its callers in the repository. Flag the change if any
caller was not updated and would now break or behave differently.

Name the caller file and line in `evidence`. This criterion uses repository
tools, so it is slower — enable it for codebases where this matters.
