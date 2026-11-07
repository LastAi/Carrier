# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""PyInstaller entry point for the Carrier desktop app (windowed, no console)."""

from carrier.desktop.main import main

raise SystemExit(main())
