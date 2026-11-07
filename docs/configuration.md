# Configuration

Carrier works with no configuration. To change something, create
`.carrier/config.toml` (run `debrief init` for a commented template) and set
only the keys you need.

```toml
[notes]
# Files the agents read to learn what the code is supposed to do.
paths = ["CLAUDE.md", "AGENTS.md", "README.md", "docs/**/*.md", "notes/**/*.md", ".carrier/notes.md"]
max_file_kb = 24      # each file is truncated beyond this
max_total_kb = 80     # files beyond this total are skipped (with a warning)

[models]
# Claude Code model aliases ("haiku", "sonnet", "opus") or full model IDs.
segment = "haiku"     # groups the diff into logical changes
criteria = "sonnet"   # default for criteria (each may override with `model:`)
guide = "sonnet"      # explanations and answers

[agents]
concurrency = 4       # model calls in parallel
batch_kb = 40         # diff per criteria call — higher = fewer, bigger calls
guide_max_turns = 8   # tool-use steps the guide may take per answer

[criteria]
disabled = []         # criteria names to turn off

[review]
first_review_from = "HEAD~1"        # start point when there is no checkpoint
max_hunks_for_ai_grouping = 300     # above this, group by file instead
```

## Choosing the starting point

| Situation | What Debrief uses |
|---|---|
| `debrief --from <ref>` | that commit, tag or branch |
| a previous review was finished | the checkpoint (`refs/carrier/debrief/last`) |
| first ever review | `first_review_from` (default `HEAD~1`) |

Move the checkpoint by hand with `debrief checkpoint <ref>`.

## Cost and speed

A review costs roughly: 1 grouping call + (criteria × batches) + 1 guide
call per item you open + 1 per question. To make it cheaper:

- disable criteria you don't need,
- set `criteria = "haiku"`,
- raise `batch_kb` so items are checked in fewer calls,
- trim `notes.paths` to the files that matter.

## Claude Code location

Debrief reads Claude Code transcripts from `$CLAUDE_CONFIG_DIR/projects` if that variable is
set, otherwise `~/.claude/projects`.
