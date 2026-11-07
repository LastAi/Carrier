# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Allows ``python -m carrier`` (used by background prepares and hooks)."""

from carrier.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
