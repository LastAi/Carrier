---
name: dead-code
title: Dead code and leftovers
theme: Upkeep
mode: project
due: changes
days: 30
severity: low
---
Find code nothing uses any more and leftovers of finished work: functions, classes, settings or files that
no longer have callers; commented-out blocks; debug prints and temporary logging; TODO/FIXME notes about
work that seems done or abandoned; feature flags that are always on or off. Search for callers before you
flag something as unused (including dynamic uses such as names in strings, plugins and entry points),
and say how sure you are.
