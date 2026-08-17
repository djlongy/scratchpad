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

### `gstash` — Named stash save / pop / apply

Wrapper around `git stash` that lets you give stashes a memorable name and
retrieve them later either by that name or via an fzf picker with a diff
preview.

```bash
gstash save <name>     # stash tracked changes with a named message
                       # (rejects if a stash named <name> already exists)
gstash pop  [name]     # pop a stash. With <name>: match by exact message.
                       #              Without args: fzf picker.
gstash apply [name]    # apply a stash (keeps it). Same matching rules as pop.
gstash delete [name]   # drop a stash for good. With <name>: match by exact message
                       #                        (refuses if several match).
                       #                        Without args: fzf multi-select + y/N.
gstash list            # show all stashes (alias: gstash ls)
gstash help            # usage
```

> The name `gstash` (rather than the shorter `gst`) is deliberate: oh-my-bash's
> git plugin already aliases `gst` to `git status`. Defining a function called
> `gst` after that alias is loaded causes bash to alias-expand the function
> name inside its own definition line, producing a parse error.

The picker shows ref / age / message in the list and `git stash show -p`
output in the preview pane on the right:

```
stash@{0}    2 hours ago    On main: fix-login
stash@{1}    yesterday      On main: wip-auth-refactor
stash@{2}    3 days ago     WIP on main: abc1234 Initial commit
```

`apply` is useful when you want to use the same fix in multiple branches —
restore the changes without consuming the stash.

#### Deleting stashes

`gstash delete` is the one destructive subcommand, so it is deliberately harder
to fire by accident than `pop`:

- `gstash delete <name>` drops the single stash whose message is exactly
  `<name>`. If two stashes share that message it drops **neither** — it prints
  both and tells you to pick interactively. A dropped stash is not listed
  anywhere afterwards, so guessing is not an option.
- `gstash delete` with no name opens the picker with multi-select enabled
  (`TAB` to mark, `Enter` to confirm), prints exactly what it is about to drop,
  and waits for a `y`/`N` confirmation. Anything other than `y`/`yes` aborts
  without touching a single stash.

Selected entries are dropped highest-index-first, because `git stash drop`
renumbers the stack — `stash@{2}` becomes `stash@{1}` the moment `stash@{1}`
goes, and dropping in list order would take the wrong entries.

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

### Via the dotfiles installer (recommended)

This directory lives inside the dotfiles package, so `install.sh` already ships
these functions. Its `install.d/20-bash.sh` hook copies this file to
`~/.oh-my-bash/custom/git-functions.sh`, which Oh My Bash sources automatically
on every interactive start — nothing is added to `~/.bashrc` for it. The
installed copy carries a marker line at the top so `uninstall.sh` can remove
exactly that file and leave anything else in `custom/` alone.

The hook resolves this file inside the package (`bash/git-functions/`), so an
extracted bundle needs nothing from outside it. Set `BASH_GIT_FUNCTIONS_SRC` to
an absolute path if you keep the functions somewhere else.

### By hand

Source the file from your `~/.bashrc`:

```bash
# Add to ~/.bashrc
source /path/to/dotfiles/bash/git-functions/git-functions.bash
```

Or copy just the functions you want directly into your `~/.bashrc`.

## Requirements

| Tool | Notes |
|------|-------|
| bash 4.0+ | Ships on RHEL 8+, Ubuntu 20.04+, macOS (system bash is 3.x — install via brew) |
| git | Any recent version |
| fzf | Required for `gci` and `gstash`. Install: `brew install fzf` / `dnf install fzf` / [github.com/junegunn/fzf](https://github.com/junegunn/fzf) |

## Files

```
git-functions/
├── README.md
└── git-functions.bash    # Source this into your shell
```
