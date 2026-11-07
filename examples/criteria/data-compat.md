---
name: data-compat
title: Breaks existing stored data
severity: high
uses_notes: true
---
Flag any change to how persisted data (saved user data, settings, database
records, exported files) is written or read that would stop data created by the
previous version from loading or would change its meaning.

Quote the changed field, key or format in `evidence`.

Do not flag: new optional fields that have a default, or changes to logs
and debug output.
