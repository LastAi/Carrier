# Writing criteria

A criterion tells one review agent what to flag. It's a markdown file with
a small header. Put project criteria in `.carrier/criteria/` in your
repository; `debrief criteria` shows what's active.

```markdown
---
name: data-compat
title: Breaks stored data
severity: high
sees_rationale: false
uses_notes: true
explore: false
---
Flag any change to how stored data is written or read that would stop data
saved by the previous version from loading.

Do not flag: new optional fields with defaults, or changes to debug output.
```

## Header fields

| Field | Default | Meaning |
|---|---|---|
| `name` | file name | Unique id. A project file with a built-in's name **replaces** it. |
| `title` | from name | Shown in the UI. |
| `severity` | `medium` | Default severity: `high`, `medium` or `low`. The agent may adjust it per flag. |
| `sees_rationale` | `false` | Whether the agent sees what *Claude* said while making the change. Leave off for independent judgement; turn on when intent matters (e.g. "is this risky change justified?"). The user's requests are always included. |
| `uses_notes` | `true` | Include project notes (CLAUDE.md, docs/…). Turn off to save tokens if irrelevant. |
| `explore` | `false` | Give the agent read-only tools (Read, Grep, Glob) to inspect the repo beyond the diff. Slower and costlier; use when the diff alone isn't enough (e.g. "callers of a changed function"). |
| `model` | `[models].criteria` | Model alias or ID for this criterion only. |
| `enabled` | `true` | `false` turns it off (handy for overriding a built-in). |

## The body

The body is the instruction. Debrief wraps it in a fixed prompt that already
supplies:

- the changes (diff + session record per item),
- what the user asked for,
- the project notes (if `uses_notes`),
- the required JSON reply format.

So you only describe **what to look for**. Tips:

- **Say what *not* to flag.** This matters most — noisy criteria get ignored.
- **Ask for evidence.** "Quote the line" or "name the note" makes flags
  checkable.
- **One concern per criterion.** Two narrow criteria beat one broad one.
- **Use your project's words.** "plugin hooks", "user settings", "order records" —
  whatever your notes call them.

## Turning criteria off

In `.carrier/config.toml`:

```toml
[criteria]
disabled = ["security", "silent-failure"]
```

## Examples

See [`examples/criteria/`](../examples/criteria/) in this repository.
