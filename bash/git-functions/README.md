# git-functions

Interactive git utilities for day-to-day branch management. Powered by `fzf`.

## Functions

### `gci` — Interactive branch checkout

Presents all local and remote branches sorted by last commit date in an fzf picker.
The current branch is highlighted in green. A preview pane shows the commit log
for the highlighted branch.

```
  main                                     3 days ago          Add CI pipeline
* feature/auth              ← current      2 hours ago         Implement JWT middleware
  fix/login-redirect                       5 hours ago         Handle redirect after login
  origin/dependabot/...                    1 week ago          Bump lodash to 4.17.21
```

Select a branch and press Enter to check it out.

**Requires:** `fzf`

### `gbp` — Git branch prune

Deletes all local branches where the upstream remote tracking branch has been deleted
(i.e., the PR was merged and the remote branch cleaned up).

```bash
gbp
# Equivalent to:
# git branch -vv | grep ': gone]' | awk '{print $1}' | xargs git branch -D
```

No confirmation prompt — use with awareness in repos with many local branches.

## Installation

Source the file from your `~/.bashrc`:

```bash
# Add to ~/.bashrc
source /path/to/scratchpad/bash/git-functions/git-functions.bash
```

Or copy just the functions you want directly into your `~/.bashrc`.

## Requirements

| Tool | Notes |
|------|-------|
| bash 4.0+ | Ships on RHEL 8+, Ubuntu 20.04+, macOS (system bash is 3.x — install via brew) |
| git | Any recent version |
| fzf | Required for `gci`. Install: `brew install fzf` / `dnf install fzf` / [github.com/junegunn/fzf](https://github.com/junegunn/fzf) |

## Files

```
git-functions/
├── README.md
└── git-functions.bash    # Source this into your shell
```
