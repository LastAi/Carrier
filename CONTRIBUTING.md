# Contributing to Carrier

Thanks for helping! Carrier is small on purpose — a few focused modules and
plain files — so it stays easy to understand and change.

## Set up

```bash
git clone <repo-url> carrier && cd carrier
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium   # once, for the desktop-page test
pytest
```

Tests need no network and no Claude Code login.

Run the desktop app from source with `python -m carrier.desktop` (or
`Carrier.bat` / `./carrier.sh`). The page is plain HTML/CSS/JS in
`src/carrier/web/` — edit and restart, no build step.

To try your changes for real, run `carrier debrief` inside any git
repository where you use Claude Code. `carrier debrief --no-ai` exercises
everything except model calls.

## Where things are

Read [ARCHITECTURE.md](ARCHITECTURE.md) — it maps every module. In short:

- shared code → `src/carrier/core/`
- Debrief's prompts → `src/carrier/debrief/prompts.py`
- built-in criteria → `src/carrier/debrief/criteria_builtin/*.md`
- Debrief's pipeline → `src/carrier/debrief/pipeline.py`
- terminal UI → `src/carrier/debrief/tui/`; desktop → `src/carrier/desktop/` + `src/carrier/web/`
- Ready Room → `src/carrier/readyroom/` (projects, tasks, rules, context text)
- Flight Deck → `src/carrier/flightdeck/` (sessions, coordination, Agent SDK)

## Code conventions

- **Every module starts with a docstring** saying what it does, why it
  exists and anything surprising. Keep it up to date when you change the
  module.
- **Comment the *why*, not the *what*.** If a line exists to work around
  something (a git quirk, a model habit, Windows), say so.
- Type hints on public functions; docstrings on anything non-obvious.
- Model calls go through `Backend.ask` only. Don't import the Agent SDK
  anywhere but `core/llm.py`.
- `carrier.core` never imports a tool package; see "The three tools" in
  ARCHITECTURE.md for the layering rules.
- Every model failure needs a safe fallback and a warning for the user.
- Debrief's agents stay read-only; reviewing never writes to the user's code.
- Keep dependencies minimal (currently: `textual`, `claude-agent-sdk`, `pywebview`).
- **No networking code in `src/carrier`.** Anything that listens on or talks
  to the network belongs in the optional `carrier_net` package
  (`tests/test_isolation.py` enforces this).
- **Desktop page:** never put untrusted text (diffs, model answers, file
  names) into `innerHTML`; use `textContent` or `md()`. No external
  resources (CDNs, fonts) — the page must work offline under its CSP.
- **Accessibility:** colour must never be the only signal; keep markers,
  labels and keyboard access when changing the UI.
- New front-end features go through `session.py` so every front end
  (terminal, desktop, future network) can use them.
- Format: 100-column lines, standard library first in imports.

## Tests

- Deterministic logic gets unit tests.
- Agent behaviour is tested with `FakeBackend` — extend
  `scripted_handler` in `tests/conftest.py` rather than calling a model.
- UI changes: add a step to `tests/test_tui.py` using the Textual pilot.

## Changing prompts

Prompt changes alter behaviour for every user, so in your PR include:

1. why the change is needed (an example of the bad output),
2. before/after output on a real diff if you can.

Keep the source-labelling rules in `GUIDE_SYSTEM` intact.

## Adding a built-in criterion

A good built-in is useful in most projects, rarely noisy, and has a clear
"do not flag" section. Add the markdown file to `debrief/criteria_builtin/`, list it
in the README table, and add a test if it relies on new prompt features.
Project-specific ideas belong in `examples/criteria/` instead.

## License of contributions

Carrier is licensed under the [GNU AGPL v3.0](LICENSE). By sending a pull
request you agree that your contribution is licensed under the same terms
(AGPL-3.0-only). New source files start with the two-line header the others
have:

```python
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
```

Third-party code may only be added if its license is compatible with the
AGPL (MIT, BSD, Apache-2.0, LGPL, GPL-3.0 and similar). Claude Code itself is
not open source and is never committed or bundled in published builds.

## Commits and PRs

- One logical change per PR; describe the user-visible effect.
- Update `CHANGELOG.md` under "Unreleased".
- Run `pytest` before pushing.
