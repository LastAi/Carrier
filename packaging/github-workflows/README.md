# GitHub Actions workflows

Move these two files into `.github/workflows/` when the repository goes on
GitHub (they could not be written there directly on this machine):

- `tests.yml` - offline test suite on Windows, macOS and Linux.
- `build.yml` - builds the standalone app on all three OSes (manual or on `v*` tags).
