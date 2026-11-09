# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Thomas and Carrier contributors
"""Reading shell commands for a project's pre-approved actions (no command is ever run here).

Claude Code's Bash tool runs commands like::

    JP="/c/Program Files/Java/jdk-17/bin/javap.exe"; "$JP" -p -cp lib.jar Foo | grep -v x | tail -8

:func:`check` splits such a command into its simple commands (at ``;``, ``&&``, ``||``,
``|``, ``&`` and new lines), follows variables set earlier in the same command, and
approves it only if **every** program in it is pre-approved and nothing else in it could
do more than those programs. It fails closed: whatever it can't follow for certain makes
the command ask — command substitution, here-documents, ``if``/``for``/subshells, brace
expansion and ``$'…'`` quoting, variables it can't see, unquoted variables that would
split into several words, environment settings that change how programs behave
(``PATH=``, ``GIT_…``, ``LD_PRELOAD`` …), ``cd`` out of the project, and programs whose
job is running other commands (``bash -c``, ``env``, ``xargs`` …).

Pre-approved entries (:func:`parse_entry`):

* ``javap`` — a program by name, as found on the PATH or in a usual install location
  (``/c/Program Files/Java/jdk-17/bin/javap.exe`` counts as ``javap``);
* ``./gradlew`` or a full path — that program file exactly (a program file inside the
  project, or anywhere unusual, only matches this way: Claude could have written it);
* ``git status`` / ``bash build.sh`` — a program with its first arguments.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Programs whose job is running other commands: never pre-approved on their own name.
WRAPPERS = {"bash", "sh", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "cmd", "powershell", "pwsh", "wsl",
            "env", "xargs", "sudo", "su", "doas", "nohup", "timeout", "time", "nice", "ionice", "stdbuf",
            "exec", "eval", "command", "builtin", "source", ".", "start", "call", "runas", "watch",
            "parallel", "script", "setsid", "chroot", "busybox", "unbuffer", "strace", "ltrace", "flock",
            "taskset", "unshare", "nsenter", "ssh", "open", "xdg-open", "osascript", "expect", "perl",
            "ruby", "php", "lua", "tclsh", "wish", "alias", "unalias", "set", "shopt", "trap", "read",
            "mapfile", "readarray", "declare", "typeset", "local", "let", "enable", "hash", "fc", "history"}
SHELLS = {"bash", "sh", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "cmd", "powershell", "pwsh"}
# Shell syntax that can't be checked here: always ask.
KEYWORDS = {"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac",
            "function", "select", "{", "}", "(", ")", "[[", "]]", "!", "coproc"}
# Harmless shell built-ins that need no listing (``cd`` is checked on its own).
BUILTINS = {"true", "false", ":", "test", "[", "pwd", "echo", "printf"}

# Environment settings that change what programs run or load: an assignment to one asks.
_RISKY_ENV = re.compile(
    r"^(PATH|IFS|CDPATH|ENV|HOME|SHELL|PS4|PROMPT_COMMAND|GLOBIGNORE|SHELLOPTS|BASHOPTS|BASH_.*|LD_.*|DYLD_.*"
    r"|GIT_.*|.*_OPTIONS|.*_OPTS|.*PAGER|EDITOR|VISUAL|BROWSER|.*_HOME|PYTHON.*|NODE_.*|NPM_.*|npm_.*|PERL.*"
    r"|RUBY.*|GEM_.*|CARGO_.*|RUSTC.*|RUSTFLAGS|GO.*|DOTNET_.*|MSBUILD.*|JAVA_TOOL_OPTIONS|_JAVA_OPTIONS"
    r"|LESS.*|MANPAGER|SSH_.*|GNUPG.*|XDG_.*|TMPDIR|TEMP|TMP|COMSPEC|PATHEXT|SystemRoot|windir"
    r"|.*PATH|.*_PRELOAD|.*LIBRARY.*|.*_ROOT|.*CONFIG.*|LOCPATH|NLSPATH|HOSTALIASES|RESOLV_.*|MALLOC_.*)$",
    re.IGNORECASE)

# Options that make a pre-approved program run other code, or read or write files outside
# the project. Long options also match GNU-style abbreviations (--compress-prog); short
# ones also with the value attached (-o/etc/x).
RISKY_ARGS = {
    "git": {"-c", "--config-env", "--exec-path", "-C", "--git-dir", "--work-tree", "--namespace", "config",
            "filter-branch", "difftool", "mergetool", "submodule", "bisect", "--output", "--no-index",
            "--ext-diff", "--textconv", "--upload-pack", "--receive-pack", "--exec", "--open-files-in-pager",
            "-O"},
    "find": {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"},
    "rg": {"--pre", "--pre-glob"},
    "grep": {"--pre"},
    "sort": {"-o", "--output", "--compress-program", "-T", "--temporary-directory"},
    "tree": {"-o", "-R"},
    "tar": {"--to-command", "--use-compress-program", "-I", "--checkpoint-action", "--info-script", "-F",
            "--new-volume-script"},
    "sed": {"-i", "--in-place", "-f", "--file", "-s", "--separate", "-e", "--expression"},
    "awk": {"-f", "--file", "-i", "--include", "-l", "--load", "-E", "--exec", "-e", "--source"},
    "file": {"-C", "--compile"},
}
# For one git command only (``git status -u`` is fine; ``git branch -u`` changes the repository).
GIT_SUB_RISKY = {
    "branch": {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "--delete", "--move", "--copy", "--force",
               "--set-upstream-to", "--unset-upstream", "--edit-description", "--track", "--no-track"},
    "blame": {"--contents", "--ignore-revs-file", "-S"},  # read a file given by path
    "ls-files": {"--exclude-from", "-X", "--exclude-per-directory"},
}
# Folders and files that make programs (or Claude Code itself) run commands: never pre-approved edits.
PROTECTED = (".git", ".claude", ".carrier", ".mcp.json", ".gitmodules",
             # files other tools run when the folder is opened: VS Code tasks, direnv, git hooks …
             ".vscode", ".idea", ".husky", ".devcontainer", ".envrc")
EXE_SUFFIXES = (".exe", ".cmd", ".bat", ".com")
NULL_TARGETS = {"/dev/null", "nul", "NUL"}
# Their first plain argument is a pattern or script, not a file.
PATTERN_FIRST = {"grep", "egrep", "fgrep", "rg", "awk", "sed"}
# Programs that only read: every file they're given must be inside the project's folders.
SCOPED_READERS = {"cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "sort", "uniq", "cut", "tr",
                  "ls", "dir", "tree", "file", "stat", "du", "diff", "cmp", "less", "more", "nl", "od",
                  "xxd", "hexdump", "strings", "md5sum", "sha1sum", "sha256sum", "awk", "sed", "which"}
# sed scripts that only print or delete lines, or substitute without the e/w flags.
_SAFE_SED = re.compile(r"^(?:(?:\d+|\$)(?:,(?:\d+|\$))?[pd]|/[^/\n]*/(?:,/[^/\n]*/)?[pd]"
                       r"|s(.)(?:(?!\1)[^\n])*\1(?:(?!\1)[^\n])*\1[gpI0-9]*)$")


class Unreadable(Exception):
    """The command uses something :func:`check` can't follow."""


@dataclass
class Word:
    text: str
    unresolved: bool = False  # used a variable whose value isn't known
    glob: bool = False  # has unquoted * ? [ (expanded by the shell)
    splits: bool = False  # an unquoted variable we can't see: the shell may split it into several words


@dataclass
class Simple:
    """One simple command: ``VAR=x prog args >file``."""
    assigns: list[tuple[str, str, bool]] = field(default_factory=list)  # (name, value, unresolved)
    words: list[Word] = field(default_factory=list)
    redirects: list[tuple[str, Word]] = field(default_factory=list)  # (operator, target)
    exported: bool = False


_REDIR = re.compile(r"(&?)(>>|>&|<&|<<<|<<|<>|>\||>|<)")
_ENDS_WORD = set(" \t\n;|&<>()")


def split(command: str, env: dict[str, str] | None = None) -> list[Simple]:
    """Split a command into simple commands, expanding variables it sets itself.

    Raises :class:`Unreadable` for anything that can't be followed safely.
    """
    if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", command):
        raise Unreadable("control characters")
    env = dict(env or {})
    unknown: set[str] = set()  # names set in the command to a value we can't know
    out: list[Simple] = []
    cur = Simple()
    word: list[str] = []
    in_word = False
    unresolved = False
    glob = False
    splits = False
    pending_redir: str | None = None
    i, n = 0, len(command)

    def finish_word() -> None:
        nonlocal word, in_word, unresolved, glob, splits, pending_redir
        if not in_word:
            return
        w = Word("".join(word), unresolved, glob, splits)
        if pending_redir is not None:
            cur.redirects.append((pending_redir, w))
            pending_redir = None
        elif not cur.words and _is_assign(w.text):
            name, _, value = w.text.partition("=")
            cur.assigns.append((name, value, w.unresolved))
        elif not cur.words and not cur.assigns and w.text == "export" and not cur.exported:
            cur.exported = True
        else:
            cur.words.append(w)
        word, in_word, unresolved, glob, splits = [], False, False, False, False

    def finish_command() -> None:
        nonlocal cur
        finish_word()
        if pending_redir is not None:
            raise Unreadable("a redirect without a target")
        if cur.words or cur.assigns or cur.redirects or cur.exported:
            if cur.words and cur.words[0].text in KEYWORDS:
                raise Unreadable(f"shell syntax ({cur.words[0].text})")
            if cur.exported and cur.words:
                raise Unreadable("export with options")
            if not cur.words:  # plain assignments: they last for the rest of the command
                for k, v, unres in cur.assigns:
                    if unres:
                        env.pop(k, None)
                        unknown.add(k)
                    else:
                        env[k] = v
                        unknown.discard(k)
            out.append(cur)
        cur = Simple()

    def subst(name: str, quoted: bool) -> None:
        nonlocal unresolved, splits
        if name in env and name not in unknown:
            value = env[name]
            if not quoted and (re.search(r"\s", value) or re.search(r"[*?\[{]", value)):
                raise Unreadable("an unquoted variable that the shell would split or expand")
            word.append(value)
        else:
            unresolved = True
            splits = splits or not quoted

    def expand(j: int, quoted: bool) -> int:
        """Expand ``$...`` at command[j]; returns the index after it."""
        nonlocal unresolved, in_word, splits
        in_word = True
        if j + 1 >= n:
            word.append("$")
            return j + 1
        c = command[j + 1]
        if c == "(":
            raise Unreadable("command substitution $( )")
        if c in "'\"" and not quoted:
            raise Unreadable(f"${c}…{c} quoting")
        if c == "{":
            end = command.find("}", j + 2)
            name = command[j + 2:end] if end >= 0 else ""
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise Unreadable("a ${…} expansion")
            subst(name, quoted)
            return end + 1
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", command[j + 1:])
        if m:
            subst(m.group(0), quoted)
            return j + 1 + len(m.group(0))
        if c in "?#$!@*-0123456789":
            unresolved = True
            splits = splits or not quoted
            return j + 2
        word.append("$")
        return j + 1

    while i < n:
        c = command[i]
        if c == "\\":
            if i + 1 < n and command[i + 1] == "\n":
                i += 2
                continue
            word.append(command[i + 1] if i + 1 < n else "")
            in_word = True
            i += 2
            continue
        if c == "'":
            end = command.find("'", i + 1)
            if end < 0:
                raise Unreadable("an unfinished quote")
            word.append(command[i + 1:end])
            in_word = True
            i = end + 1
            continue
        if c == '"':
            in_word = True
            i += 1
            while i < n and command[i] != '"':
                d = command[i]
                if d == "\\" and i + 1 < n and command[i + 1] in '"\\$`\n':
                    word.append(command[i + 1])
                    i += 2
                elif d == "`":
                    raise Unreadable("command substitution with backticks")
                elif d == "$":
                    i = expand(i, True)
                else:
                    word.append(d)
                    i += 1
            if i >= n:
                raise Unreadable("an unfinished quote")
            i += 1
            continue
        if c == "`":
            raise Unreadable("command substitution with backticks")
        if c == "$":
            i = expand(i, False)
            continue
        if c == "#" and not in_word:
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if c in " \t":
            finish_word()
            i += 1
            continue
        if c in "<>" and i + 1 < n and command[i + 1] == "(":
            raise Unreadable("process substitution")
        if c in "(){}":
            raise Unreadable("a group, subshell or brace expansion")
        if c == "~":
            nxt = command[i + 1:i + 2]
            plain = nxt in ("", "/", " ", "\t", "\n", ";", "|", "&", ":")
            if in_word and word and word[-1] in "=:" and plain and _is_assign("".join(word) + "x"):
                word.append(str(Path.home()))  # X=~/dir: bash expands ~ in assignments
                i += 1
                continue
            if in_word or not plain:
                raise Unreadable("a ~ the shell might expand")
        if c in "<>" or (c == "&" and command[i + 1:i + 2] == ">"):
            if in_word and "".join(word).isdigit() and not unresolved:
                word, in_word = [], False  # "2>": the digits name the stream, not a word
            else:
                finish_word()  # "a>b" is "a" then a redirect
            m = _REDIR.match(command, i)
            if not m:
                raise Unreadable("a redirect")
            op = m.group(0)
            if "<<" in op and "<<<" not in op:
                raise Unreadable("a here-document")
            i = m.end()
            if op in (">&", "<&"):
                j = i
                while j < n and command[j].isdigit():
                    j += 1
                if j < n and command[j] == "-" and j == i:
                    j += 1
                if j > i and (j >= n or command[j] in _ENDS_WORD):
                    cur.redirects.append((op, Word(command[i:j])))  # 2>&1: copies a stream, no file
                    i = j
                    continue
                op = ">" if op == ">&" else "<"  # >&file is a file
            pending_redir = op
            continue
        if c in ";\n":
            finish_command()
            i += 1
            continue
        if c == "&":
            if i + 1 < n and command[i + 1] == "&":
                finish_command()
                i += 2
                continue
            finish_command()
            i += 1
            continue
        if c == "|":
            finish_command()
            i += 2 if command[i + 1:i + 2] in ("|", "&") else 1
            continue
        if c in "*?[":
            glob = True
        word.append(c)
        in_word = True
        i += 1
    finish_command()
    return out


def _is_assign(text: str) -> bool:
    return bool(re.match(r"[A-Za-z_][A-Za-z0-9_]*=", text))


# -- entries ------------------------------------------------------------------------------


def program_name(token: str) -> str:
    """``/c/Program Files/Java/jdk-17/bin/javap.exe`` -> ``javap``."""
    base = re.split(r"[\\/]", token)[-1]
    low = base.lower()
    for suf in EXE_SUFFIXES:
        if low.endswith(suf):
            return base[: -len(suf)]
    return base


def is_path(token: str) -> bool:
    return "/" in token or "\\" in token


def written_absolute(token: str) -> bool:
    """Written as a full path (``/usr/bin/x``, ``C:/x``, ``~/x``), not relative to the current folder."""
    return token.startswith(("/", "\\", "~")) or bool(re.match(r"^[A-Za-z]:[\\/]", token))


def parse_entry(entry: str) -> list[str]:
    """An entry as words, or raise ValueError saying what's wrong with it."""
    entry = entry.strip()
    if not entry:
        raise ValueError("empty")
    if re.search(r"[;&|`<>$()\"'*?{}=\[\]\x00-\x1f]", entry):
        raise ValueError(f"“{entry}”: list programs (like javap) or a program with its first arguments "
                         "(like git status), not a whole command")
    words = entry.split()
    first = words[0]
    name = program_name(first).lower()
    if name in WRAPPERS:
        if name not in SHELLS:
            raise ValueError(f"“{entry}”: {name} runs other commands, so it can't be pre-approved. "
                             "List the programs it would run instead.")
        if len(words) != 2 or words[1].startswith("-") or not is_script_arg(words[1]):
            raise ValueError(f"“{entry}”: {name} can run any command, so it can't be pre-approved on its own. "
                             f"List a script it runs instead, like “{name} build.sh”.")
        if written_absolute(words[1]) or ".." in re.split(r"[\\/]", words[1]):
            raise ValueError(f"“{entry}”: list a script inside the project, by its path from the project folder")
    if name in KEYWORDS or name in BUILTINS or name == "cd":
        raise ValueError(f"“{entry}” doesn't need listing" if name in BUILTINS else f"“{entry}” isn't a program")
    return words


def valid_entries(entries: list[str]) -> list[str]:
    """Drop entries that aren't valid (e.g. saved by an older version)."""
    out = []
    for e in entries:
        try:
            parse_entry(e)
        except ValueError:
            continue
        if e.strip() not in out:
            out.append(e.strip())
    return out


def is_script_arg(arg: str) -> bool:
    return bool(re.search(r"\.(sh|bash|ps1|bat|cmd|py)$", arg, re.I)) and not re.search(r"[;&|`<>$]", arg)


# -- where programs live -------------------------------------------------------------------


def _install_roots() -> list[str]:
    """Folders programs are installed in: anything found there matches a listed name."""
    home = Path.home()
    # PATH folders, except ones in the home folder (go install, cargo install, pip --user and
    # the like write there, so Claude could put a program of its own in them).
    roots = [p for p in os.environ.get("PATH", "").split(os.pathsep)
             if p and os.path.isabs(p) and not _inside(p, [str(home)])]
    roots += ["/usr", "/bin", "/sbin", "/opt", "/Library", "/System", "/Applications", "/nix", "/snap",
              "C:/Program Files", "C:/Program Files (x86)", "C:/Windows", "C:/ProgramData/chocolatey",
              str(home / ".jdks"), str(home / ".sdkman"), str(home / "scoop"), str(home / ".cargo" / "bin"),
              str(home / "go" / "bin"), str(home / "AppData" / "Local" / "Programs")]
    return roots


_GITBASH_INSTALL = re.compile(r"^/[A-Za-z]/(Program Files|Program Files \(x86\)|Windows|ProgramData/chocolatey)/",
                              re.IGNORECASE)


def installed_program(token: str, cwd: Path, avoid: list[str]) -> bool:
    """Is this absolute program path in an install location (not somewhere Claude writes)?"""
    if ".." in re.split(r"[\\/]", token):
        return False
    if _GITBASH_INSTALL.match(token.replace("\\", "/")):
        return True
    full = _resolve(token, cwd)
    if _inside(full, avoid):
        return False
    temp = [os.environ.get(k, "") for k in ("TMPDIR", "TEMP", "TMP")] + ["/tmp", "/var/tmp"]
    if _inside(full, [t for t in temp if t]):
        return False
    return _inside(full, _install_roots())


# -- checking -----------------------------------------------------------------------------


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def _inside(path: str, roots: list[str]) -> bool:
    t = _norm(path)
    for r in roots:
        root = _norm(r)
        if t == root or t.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def _resolve(token: str, cwd: Path) -> str:
    p = token
    if p == "~" or p.startswith("~/"):
        p = str(Path.home()) + p[1:]
    m = re.match(r"^/([a-zA-Z])(/.*)?$", p) if os.name == "nt" else None  # Git Bash: /c/Users -> C:/Users
    if m:
        p = f"{m.group(1)}:{m.group(2) or '/'}"
    path = Path(p)
    return str(path if path.is_absolute() else cwd / path)


@dataclass
class Verdict:
    ok: bool
    reason: str = ""  # why it asks
    programs: list[dict[str, Any]] = field(default_factory=list)  # [{"name", "listed", "entry", "why"}]

    @property
    def missing(self) -> list[str]:
        """Entries that would make this command pre-approved (empty if something else is in the way)."""
        return [p["entry"] for p in self.programs if not p["listed"] and p.get("entry")]


def check(command: str, entries: list[str], cwd: Path, readable: list[str], editable: list[str],
          can_edit: bool = False, can_read: bool = False) -> Verdict:
    """Is ``command`` covered by the pre-approved ``entries``? Every program in it must be."""
    try:
        parts = split(command)
    except Unreadable as exc:
        return Verdict(False, f"it uses {exc}, which can't be checked before it runs")
    parsed = []
    for e in entries:
        try:
            parsed.append(parse_entry(e))
        except ValueError:
            continue
    v = Verdict(True)
    reasons: list[str] = []
    here = cwd
    for part in parts:
        for name, _value, _unres in part.assigns:
            if _RISKY_ENV.match(name):
                reasons.append(f"it sets {name}, which changes what programs run or load")
        if part.assigns and part.words:
            reasons.append(f"it sets {part.assigns[0][0]} for one program only")  # VAR=x prog: ask
        for op, target in part.redirects:
            if op in (">&", "<&"):
                continue  # 2>&1 (a file target was turned into > or < above)
            if target.unresolved or not target.text or target.glob:
                reasons.append(f"it redirects to {target.text or 'a variable'} that can't be checked")
                continue
            if target.text in NULL_TARGETS:
                continue
            path = _resolve(target.text, here)
            if op.startswith("<") and op != "<>":
                if op == "<<<" or (can_read and _inside(path, readable)):
                    continue
                reasons.append(f"it reads {target.text}")
            elif can_edit and _inside(path, editable) and not protected(path, editable):
                continue
            else:
                reasons.append(f"it writes to {target.text}")
        if not part.words:
            continue
        w0 = part.words[0]
        if w0.unresolved:
            reasons.append("its program comes from a variable that isn't set in the command")
            continue
        if w0.glob:
            reasons.append(f"its program is a pattern ({w0.text})")
            continue
        argv = [w.text for w in part.words]
        prog = argv[0]
        name = program_name(prog)
        low = name.lower()
        if any(w.splits for w in part.words):
            reasons.append("an unquoted variable it can't see could become several arguments")
        if (low in RISKY_ARGS or low in SCOPED_READERS) and any(w.unresolved for w in part.words[1:]):
            reasons.append(f"{name} gets an argument from a variable it can't see")
        if low == "cd" and not is_path(prog):
            target = part.words[1] if len(part.words) > 1 else None
            if target is None or target.unresolved or target.glob or target.text in ("-", "") \
                    or not _inside(_resolve(target.text, here), readable):
                reasons.append("it changes to a folder outside the project's folders")
                here = Path("/nonexistent-carrier-cd")  # nothing relative can be trusted after this
            else:
                here = Path(_resolve(target.text, here))
            continue
        if low in BUILTINS and not is_path(prog):
            if low == "printf" and any(a == "-v" or a.startswith("-v") for a in argv[1:]):
                reasons.append("printf -v sets a variable")
            continue
        entry = _match(argv, parsed, cwd, here, readable + editable)
        info: dict[str, Any] = {"name": name, "listed": entry is not None, "entry": None, "why": ""}
        if entry is None:
            full = _resolve(prog, here) if is_path(prog) else ""
            if low in WRAPPERS and not is_path(prog):
                info["why"] = f"{name} can run any command"
                if low in SHELLS and len(argv) > 1 and not argv[1].startswith("-") and is_script_arg(argv[1]) \
                        and _inside(_resolve(argv[1], here), editable):
                    info["entry"] = f"{name} {os.path.relpath(_resolve(argv[1], here), str(cwd))}".replace("\\", "/")
            elif low in WRAPPERS:
                info["why"] = f"{name} can run any command"
            elif full and _inside(full, editable):
                rel = os.path.relpath(full, str(cwd)).replace("\\", "/")
                info["entry"] = rel if rel.startswith(("./", "../")) else "./" + rel
                info["why"] = "a program file in the project"
            elif full and not written_absolute(prog):
                info["why"] = "a program outside the project given by a relative path"
            elif full and not installed_program(prog, here, readable + editable):
                info["entry"] = full.replace("\\", "/")
                info["why"] = f"a program in an unusual place ({full})"
            else:
                info["entry"] = name
        else:
            rest = argv[len(entry):]
            risky = _risky(low, rest, argv[1:])
            if risky:
                info.update(listed=False, why=f"{name} {risky} can run other commands or touch other files",
                            entry=None)
            elif low in SCOPED_READERS:
                outside = _outside_args(low, part.words[len(entry):], here, readable)
                if outside:
                    info.update(listed=False, why=f"it reads {outside}, outside the project's folders", entry=None)
        v.programs.append(info)
    if reasons or any(not p["listed"] for p in v.programs):
        v.ok = False
        whys = reasons + [p["why"] or f"{p['name']} isn't pre-approved" for p in v.programs if not p["listed"]]
        v.reason = "; ".join(dict.fromkeys(whys))
    return v


def protected(path: str, roots: list[str]) -> bool:
    """Is ``path`` inside one of these folders' .git, .claude, .carrier or .mcp.json?

    Editing those can make git, Claude Code or Carrier run commands (git hooks and config,
    Claude Code settings and MCP servers), so edits there always ask."""
    t = _norm(path)
    for r in roots:
        root = _norm(r).rstrip(os.sep)
        if t == root or not t.startswith(root + os.sep):
            continue
        parts = t[len(root) + 1:].split(os.sep)
        if any(p.lower() in PROTECTED for p in parts):
            return True
    return False


def _risky(prog: str, args: list[str], all_args: list[str] | None = None) -> str:
    """The first argument that makes ``prog`` do more than it's trusted to ("" if none).
    ``all_args``: every argument (``args`` may leave out the ones a listed entry names)."""
    risky = set(RISKY_ARGS.get(prog, set()))
    if prog == "git":
        sub = next((a for a in (all_args or args) if not a.startswith("-")), "")
        risky |= GIT_SUB_RISKY.get(sub, set())
    for a in args:
        if a == "--":
            break
        for r in risky:
            if a == r:
                return a
            if r.startswith("--") and a.startswith("--"):
                opt = a.split("=", 1)[0]
                if len(opt) > 3 and r.startswith(opt):  # GNU abbreviations: --compress-prog
                    return a
            elif r.startswith("-") and len(r) == 2 and a.startswith(r) and not a.startswith("--"):
                return a  # -o/etc/x
            elif len(r) == 2 and r[0] == "-" and a.startswith("-") and not a.startswith("--") and r[1] in a[1:]:
                return a  # bundled short options: -ni
    if prog == "git" and sub == "branch" and \
            len([a for a in (all_args or args) if not a.startswith("-")]) > 1:
        return "(a new branch name)"  # git branch NAME creates a branch
    if prog in ("uniq", "xxd") and len([a for a in args if not a.startswith("-")]) > 1:
        return "OUTPUT"  # uniq IN OUT / xxd IN OUT write OUT
    if prog == "sed":
        scripts = [a for a in args if not a.startswith("-")][:1]
        if not scripts or not _SAFE_SED.match(scripts[0]):
            return "(a script that isn't a plain print, delete or substitute)"
    return ""


def _match(argv: list[str], parsed: list[list[str]], cwd: Path, here: Path, project: list[str]) -> list[str] | None:
    prog = argv[0]
    full = _resolve(prog, here) if is_path(prog) else ""
    for words in sorted(parsed, key=len, reverse=True):
        if len(words) > len(argv):
            continue
        first = words[0]
        if is_path(first):  # a program file by its path: the same file
            if not full or _norm(_resolve(first, cwd)) != _norm(full):
                continue
        else:
            if full and not (written_absolute(prog) and installed_program(prog, here, project)):
                continue  # a program file elsewhere only matches by its path
            if program_name(prog).lower() != first.lower():
                continue
        if argv[1:len(words)] != words[1:]:
            continue
        if first.lower() in SHELLS and _norm(_resolve(argv[1], here)) != _norm(_resolve(words[1], cwd)):
            continue  # "bash build.sh" means the project's build.sh, not one somewhere else
        return words
    return None


def _pattern_option(prog: str, a: str) -> bool:
    """Does this argument give grep/rg its pattern (so the first plain word is a file)?"""
    opt = a.split("=", 1)[0]
    if opt.startswith("--") and len(opt) >= 5 and ("--regexp".startswith(opt) or "--file".startswith(opt)):
        return True  # also GNU abbreviations: --regex, --fil
    return prog in ("grep", "egrep", "fgrep", "rg") and a.startswith("-") and not a.startswith("--") \
        and ("e" in a[1:] or "f" in a[1:])


def _outside_args(prog: str, words: list[Word], here: Path, readable: list[str]) -> str:
    """The first argument that could be a file outside the readable folders ("" if none).

    Every plain argument counts as a file (after the pattern of grep, awk and sed), and so
    do option values that look like paths. Unknown or pattern arguments count as outside.
    """
    skip_pattern = prog in PATTERN_FIRST and not any(_pattern_option(prog, w.text) for w in words)
    for w in words:
        a = w.text
        if a.startswith("-") and len(a) > 1:
            value = a.split("=", 1)[1] if "=" in a else (a[2:] if not a.startswith("--") else "")
            if value and (is_path(value) or value.startswith("~")) and \
                    (w.unresolved or not _inside(_resolve(value, here), readable)):
                return value
            continue
        if skip_pattern:
            skip_pattern = False  # the pattern or script
            continue
        if w.unresolved:
            return a or "a variable"
        if not _inside(_resolve(a, here), readable):
            return a
    return ""
