# General shell conveniences, kept separate from the git-specific functions in
# bash/git-functions/. Installed to ~/.oh-my-bash/custom/ by
# install.d/20-bash.sh, where Oh My Bash sources it on every interactive start.

# h — shorthand for history.
alias h='history'

# hg — search shell history: `hg <pattern>`. A function rather than an alias so
# the pattern lands after the pipe; all arguments are joined into one
# case-insensitive pattern, so `hg vault kv` finds lines containing that phrase.
# NOTE: this shadows the Mercurial CLI (`hg`) on any box that has it installed —
# rename or drop this function if you use Mercurial.
hg() {
  if [ $# -eq 0 ]; then
    echo "usage: hg <pattern>" >&2
    return 2
  fi
  history | grep -i -- "$*"
}
