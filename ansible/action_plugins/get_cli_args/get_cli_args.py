# action_plugins/get_cli_args.py
from ansible.plugins.action import ActionBase
import sys
import os
import re
import json
import subprocess


def _run_git(cmd, cwd):
    """Run a git command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
                # rstrip only — strip() would eat the leading space off the first
            # porcelain line, turning " M file" into "M file" (staged false positive).
            return result.stdout.rstrip()
    except Exception:
        pass
    return None


def _decode_git_path(path):
    """
    Decode a path token as returned by git status --porcelain.

    Git wraps paths that contain spaces, non-ASCII characters, or other
    special bytes in double-quotes and represents those bytes as C-style
    escape sequences:
        \\  -> backslash
        \"  -> double-quote
        \n  -> newline
        \t  -> tab
        \r  -> carriage return
        \a  -> bell
        \b  -> backspace
        \nnn -> octal byte (used for non-ASCII / UTF-8 sequences)

    If the path is not quoted it is returned unchanged.
    """
    if not (path.startswith('"') and path.endswith('"')):
        return path

    inner = path[1:-1]  # strip surrounding double-quotes
    result = bytearray()
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == '\\' and i + 1 < len(inner):
            nx = inner[i + 1]
            if nx == '"':
                result += b'"';  i += 2
            elif nx == '\\':
                result += b'\\'; i += 2
            elif nx == 'n':
                result += b'\n'; i += 2
            elif nx == 't':
                result += b'\t'; i += 2
            elif nx == 'r':
                result += b'\r'; i += 2
            elif nx == 'a':
                result += b'\x07'; i += 2
            elif nx == 'b':
                result += b'\x08'; i += 2
            elif nx in '01234567' and i + 3 < len(inner):
                # Octal escape \nnn — raw byte, collect as bytes then UTF-8 decode
                octal = inner[i + 1:i + 4]
                if re.match(r'^[0-7]{3}$', octal):
                    result.append(int(octal, 8)); i += 4
                else:
                    result += ch.encode(); i += 1
            else:
                result += ch.encode(); i += 1
        else:
            result += ch.encode('utf-8')
            i += 1

    return result.decode('utf-8')


def _parse_rename_dest(raw_field):
    """
    Extract and decode the destination path from a porcelain rename/copy field.

    The raw field (everything after 'XY ') can take four forms depending on
    whether either filename contains characters that require quoting:

        old.txt -> new.txt                          (neither quoted)
        "old file.txt" -> new.txt                   (old quoted)
        old.txt -> "new file.txt"                   (new quoted)
        "old file.txt" -> "new file.txt"            (both quoted)
    """
    if raw_field.startswith('"'):
        # Old path is quoted: match the closing quote then the arrow
        m = re.match(r'^"(?:[^"\\]|\\.)*"\s*->\s*(.+)$', raw_field)
        if m:
            return _decode_git_path(m.group(1).strip())
    # Old path is unquoted: find the first ' -> ' separator
    arrow = raw_field.find(' -> ')
    if arrow != -1:
        return _decode_git_path(raw_field[arrow + 4:].strip())
    # Fallback: no arrow found, treat entire field as destination
    return _decode_git_path(raw_field)


def _get_git_status(playbook_dir):
    """
    Collect git state for the playbook's working tree.

    Returns a dict with:
        branch        - current branch name (or 'HEAD' if detached)
        commit        - full commit SHA
        commit_short  - 7-char short SHA
        tag           - nearest tag (git describe), or None
        modified      - list of modified tracked files (not staged)
        staged        - list of staged files (destination path for renames/copies)
        untracked     - list of untracked files
        deleted       - list of unstaged-deleted tracked files
        is_clean      - True if working tree and index are clean
        error         - set if git is unavailable or dir is not a repo
    """
    cwd = playbook_dir or os.getcwd()

    branch = _run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if branch is None:
        return {"error": "not a git repository or git unavailable"}

    commit = _run_git(["git", "rev-parse", "HEAD"], cwd) or "unknown"
    commit_short = commit[:7] if commit != "unknown" else "unknown"
    tag = _run_git(["git", "describe", "--tags", "--always", "--dirty"], cwd)

    porcelain = _run_git(["git", "status", "--porcelain"], cwd) or ""

    modified, staged, untracked, deleted = [], [], [], []

    for line in porcelain.splitlines():
        # Porcelain v1 format: XY<space><path>
        # X = index (staged) status  Y = worktree status
        # Minimum valid line: XY<space><one char> = 4 chars
        if len(line) < 4:
            continue

        index_status   = line[0]
        worktree_status = line[1]
        # raw_path: everything after the two-char status + one space separator.
        # May be a bare path, a quoted path ("..."), or a rename pair.
        raw_path = line[3:]

        # ── Staged changes (index column) ────────────────────────────────────
        # Includes modifications (M), additions (A), deletions (D),
        # renames (R), copies (C), and type changes (T).
        # For renames/copies we record only the destination (the file that
        # now exists), decoded from the possibly-quoted "old -> new" field.
        if index_status in ("M", "A", "D", "R", "C", "T"):
            if index_status in ("R", "C"):
                staged.append(_parse_rename_dest(raw_path))
            else:
                staged.append(_decode_git_path(raw_path))

        # ── Worktree changes (not yet staged) ────────────────────────────────
        if worktree_status == "M":
            modified.append(_decode_git_path(raw_path))
        elif worktree_status == "D":
            deleted.append(_decode_git_path(raw_path))

        # ── Untracked (??) ───────────────────────────────────────────────────
        if index_status == "?" and worktree_status == "?":
            untracked.append(_decode_git_path(raw_path))

    is_clean = not (modified or staged or untracked or deleted)

    return {
        "branch": branch,
        "commit": commit,
        "commit_short": commit_short,
        "tag": tag,
        "modified": modified,
        "staged": staged,
        "untracked": untracked,
        "deleted": deleted,
        "is_clean": is_clean,
    }


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super(ActionModule, self).run(tmp, task_vars)
        argv = sys.argv

        # Strip full path from first argument
        if argv and '/' in argv[0]:
            argv[0] = os.path.basename(argv[0])

        # Extract semaphore_vars and other internal extra-vars
        semaphore_data = {}
        cleaned_argv = []
        skip_next = False

        for i, arg in enumerate(argv):
            if skip_next:
                skip_next = False
                continue

            if arg in ['--extra-vars', '-e'] and i + 1 < len(argv):
                next_arg = argv[i + 1]
                if next_arg.startswith('{'):
                    try:
                        extra_vars_dict = json.loads(next_arg)
                        if 'semaphore_vars' in extra_vars_dict:
                            semaphore_data = extra_vars_dict['semaphore_vars']
                    except json.JSONDecodeError:
                        pass
                    skip_next = True
                    continue
                else:
                    cleaned_argv.append(arg)
                    cleaned_argv.append(next_arg)
                    skip_next = True
            elif arg.startswith('-e') and '=' in arg and not arg.startswith('-e@'):
                cleaned_argv.append(arg)
            elif arg.startswith('-e@'):
                cleaned_argv.append(arg)
            else:
                cleaned_argv.append(arg)

        playbook_dir = (task_vars or {}).get("playbook_dir") or os.getcwd()
        git_status = _get_git_status(playbook_dir)

        result.update({
            "changed": False,
            "ansible_playbook_argv": cleaned_argv,
            "ansible_playbook_cmd": " ".join(cleaned_argv),
            "semaphore_vars": semaphore_data if semaphore_data else None,
            "git_status": git_status,
        })
        return result
