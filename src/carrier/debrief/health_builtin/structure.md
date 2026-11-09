---
name: structure
title: Structure and boundaries
theme: Design
mode: project
due: changes
days: 30
severity: medium
---
Look at how the project is organised as a whole. Flag:
- modules or files that do too many unrelated things, or have grown far beyond the rest;
- layers that reach past each other (UI code doing storage, low-level code importing high-level code, circular imports);
- the same responsibility split across several places with no clear owner;
- folders whose contents don't match their name.
Name the files involved and say what a cleaner boundary would be. Don't flag small, reasonable shortcuts.
