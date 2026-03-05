# action_plugins/get_cli_args.py
from ansible.plugins.action import ActionBase
import sys
import os
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
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _get_git_status(playbook_dir):
    """
    Collect git state for the playbook's working tree.

    Returns a dict with:
        branch        - current branch name (or 'HEAD' if detached)
        commit        - full commit SHA
        commit_short  - 7-char short SHA
        tag           - nearest tag (git describe), or None
        modified      - list of modified tracked files (not staged)
        staged        - list of staged files (destination path for renames)
        untracked     - list of untracked files
        deleted       - list of deleted tracked files (unstaged)
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
        # Porcelain v1 format: XY<space>filepath
        # X = index (staged) status, Y = worktree status
        # Minimum valid line is 4 chars: XY<space><char>
        if len(line) < 4:
            continue

        index_status = line[0]
        worktree_status = line[1]
        # line[2] is always the space separator; use line[3:] to avoid
        # accidentally stripping a filename that begins with a space.
        filepath = line[3:]

        # Staged changes (index column).
        # For renames/copies, porcelain v1 encodes the entry as
        # "old_path -> new_path". We record only the destination so that
        # callers see the file that now exists, not the one that was removed.
        # Staged deletions (D) are also included — the file is gone from the
        # working tree but the removal is recorded in the index.
        if index_status in ("M", "A", "D", "R", "C", "T"):
            if index_status in ("R", "C") and " -> " in filepath:
                staged_path = filepath.split(" -> ", 1)[1]
            else:
                staged_path = filepath
            staged.append(staged_path)

        # Worktree changes (not yet staged).
        if worktree_status == "M":
            modified.append(filepath)
        elif worktree_status == "D":
            deleted.append(filepath)

        # Untracked files (?? in both columns).
        if index_status == "?" and worktree_status == "?":
            untracked.append(filepath)

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

            # Handle --extra-vars or -e with JSON dict (internal Ansible format)
            if arg in ['--extra-vars', '-e'] and i + 1 < len(argv):
                next_arg = argv[i + 1]
                # Check if it's a JSON dict structure (internal Ansible format)
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
                    # Keep file-based extra-vars like -e@file.yml
                    cleaned_argv.append(arg)
                    cleaned_argv.append(next_arg)
                    skip_next = True
            # Handle -ekey=value format
            elif arg.startswith('-e') and '=' in arg and not arg.startswith('-e@'):
                cleaned_argv.append(arg)
            # Handle -e@file format
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
