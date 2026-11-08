# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Find things that look like secrets — fast, local, no AI and no network.

Used by the review gate (nothing that publishes starts while a likely secret
is in the project's files), by Flight Deck before a message is sent, and by
the Ready Room's check that key files are ignored by git.

A hit the user has checked can be marked safe: its fingerprint (file + the
exact line) is remembered per project, so the same line isn't reported
again, but any change to it is.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path
from typing import Any

from carrier.core import gitutil

# (kind, pattern). Kept specific: a gate that cries wolf gets switched off.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS access key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    ("Stripe live key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
    ("Discord/bot token", re.compile(r"\b[MN][A-Za-z\d]{23,25}\.[\w-]{6}\.[\w-]{27,}")),
    ("password in URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]{4,}@")),
    # NAME = value / NAME: value, where NAME contains a secret-ish word — also DB_PASSWORD,
    # SECRET_KEY, AWS_SECRET_ACCESS_KEY (underscores count as word characters for \b, so
    # letters/digits are the boundary here). Quoted or bare (.env, YAML) values.
    ("hard-coded secret", re.compile(
        r"""(?i)(?<![A-Za-z0-9])[A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|client[_-]?secret|"""
        r"""access[_-]?key|private[_-]?key)[A-Za-z0-9_.-]*["']?\s*[:=]\s*["']?([^"'\s,;#]{12,})""")),
]
# Values that are clearly not real (checked against the value only).
PLACEHOLDER = re.compile(r"(?i)^(?:x{4,}|\*{4,}|your[_-].*|.*example.*|.*placeholder.*|changeme|dummy.*|fake.*|"
                         r"test[_-]?(?:key|token|secret|password).*|<[^>]+>|\$\{.*|\$[A-Z_]+|.*process\.env.*|"
                         r".*os\.environ.*|.*getenv.*|true|false|null|none|[\d.]+|.*[()\[\]{}].*)$")  # last: code, not a value
# Files that usually hold secrets and should be ignored by git.
SECRET_FILES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_ed25519", "credentials.json",
                "secrets.json", "secrets.yaml", "secrets.yml", ".npmrc", ".pypirc", "*.keystore")
SAFE_FILES = (".env.example", ".env.sample", ".env.template")
MAX_FILE_BYTES = 1_000_000


def fingerprint(path: str, line: str) -> str:
    return hashlib.sha256(f"{path}\n{line.strip()}".encode()).hexdigest()[:20]


def mask(value: str) -> str:
    v = value.strip()
    return v[:4] + "…" + ("*" * 6) if len(v) > 8 else "****"


def scan_text(text: str, path: str = "") -> list[dict[str, Any]]:
    """Likely secrets in ``text``: ``[{kind, line, preview, fingerprint}]`` (previews are masked)."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        long_line = len(line) > 2000  # minified or data: only the specific (anchored) patterns
        for kind, rx in PATTERNS:
            if long_line and kind == "hard-coded secret":
                continue
            m = rx.search(line)
            if not m:
                continue
            value = m.group(1) if m.groups() and m.group(1) else m.group(0)
            if PLACEHOLDER.match(value):
                continue
            hits.append({"kind": kind, "path": path, "line": n, "preview": mask(value),
                         "fingerprint": fingerprint(path, line)})
            break  # one report per line
    return hits


def decode(raw: bytes) -> str | None:
    """Text of a file, or None if it's binary. Handles UTF-16 (PowerShell's default) too."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\0" in raw[:8000]:
        return None
    return raw.decode("utf-8", errors="replace")


def _files(folder: Path) -> tuple[Path, list[str]]:
    """The repository top and the files under ``folder`` git would publish (tracked or not ignored)."""
    repo, sub = gitutil.locate(folder)
    out = gitutil.git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", sub or ".",
                      check=False)
    return repo, sorted({f for f in out.split("\0") if f})


def scan_folder(folder: Path, safe: set[str] | None = None, label: str = "") -> list[dict[str, Any]]:
    """Scan every publishable text file under ``folder`` (skipping binary and very large files)."""
    try:
        repo, files = _files(folder)
    except gitutil.GitError:
        return []
    safe = safe or set()
    hits = []
    for rel in files:
        p = repo / rel
        try:
            if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        text = decode(raw)
        if text is None:
            continue  # binary
        shown = f"{label}/{rel}" if label else rel
        for h in scan_text(text, shown):
            if h["fingerprint"] not in safe:
                hits.append(h)
    return hits


def unignored_secret_files(folder: Path) -> list[dict[str, Any]]:
    """Files under ``folder`` that look like secrets and git would publish: ``[{path, tracked}]``."""
    try:
        repo, sub = gitutil.locate(folder)
    except gitutil.GitError:
        return []
    tracked = set(gitutil.git(repo, "ls-files", "-z", "--", sub or ".", check=False).split("\0"))
    out = []
    for rel in _files(folder)[1]:
        name = rel.rsplit("/", 1)[-1]
        if name in SAFE_FILES:
            continue
        if any(fnmatch.fnmatch(name, pat) for pat in SECRET_FILES):
            out.append({"path": rel, "tracked": rel in tracked, "repo": str(repo)})
    return out


def append_gitignore(path: Path, lines: list[str], heading: str) -> list[str]:
    """Add ``lines`` to a .gitignore without rewriting what's there (encoding and line
    endings kept). ``!`` lines (exceptions) are always written last so they win. Returns
    the lines added."""
    raw = path.read_bytes() if path.exists() else b""
    have = {ln.strip() for ln in raw.decode("utf-8", errors="replace").splitlines()}
    plain = [ln for ln in lines if not ln.startswith("!") and ln not in have]
    if not plain:
        return []
    negations = [ln for ln in lines if ln.startswith("!")]  # repeated after the new block on purpose
    nl = b"\r\n" if b"\r\n" in raw else b"\n"
    block = [f"# {heading}", *plain, *negations]
    data = raw
    if data and not data.endswith(b"\n"):
        data += nl
    data += (nl if data else b"") + nl.join(x.encode("utf-8") for x in block) + nl
    tmp = path.with_name(path.name + ".carrier-tmp")
    tmp.write_bytes(data)
    tmp.replace(path)  # atomic: never a half-written .gitignore
    return plain + negations


def ignore_secret_files(folder: Path) -> list[str]:
    """Add the secret-file patterns to the repository's .gitignore (once). Returns the lines added.

    Files git already tracks stay tracked: they must be removed from git by hand
    (``git rm --cached <file>``), which the app explains.
    """
    repo, _sub = gitutil.locate(folder)
    return append_gitignore(repo / ".gitignore", [*SECRET_FILES, *(f"!{p}" for p in SAFE_FILES)],
                            "Secrets and keys (added by Carrier)")
