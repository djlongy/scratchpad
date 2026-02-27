# Scratchpad

Shell functions, scripts, and tools for sysadmin/DevOps work.
Drop onto any machine with minimal dependencies — no frameworks, no package sprawl.

## Tools

| Tool | What it does | Requires |
|------|-------------|----------|
| [`dotfiles`](dotfiles/README.md) | Stow-managed tmux/dev environment bootstrap for fast machine migration | git, stow, tmux |
| [`bash/git-functions`](bash/git-functions/README.md) | fzf interactive branch picker, prune gone branches | bash, fzf |
| [`bash/ohmybash`](bash/ohmybash/README.md) | Powerline prompt with git/venv/time, deploy script, Nerd Font installer | bash, git |
| [`ansible/action_plugins/get_cli_args`](ansible/action_plugins/get_cli_args/README.md) | Expose `ansible-playbook` CLI args and Semaphore vars to tasks | Python, Ansible |

## Structure

```
scratchpad/
├── dotfiles/
│   └── tmux/               stow package for tmux and helper scripts
├── bash/
│   ├── git-functions/      fzf git helpers
│   └── ohmybash/           Oh My Bash prompt theme + deploy tooling
└── ansible/
    └── action_plugins/
        └── get_cli_args/   Ansible action plugin
```

New tools get their own folder under the relevant language/category directory.
Each tool folder has a README explaining what it does, requirements, and usage.
