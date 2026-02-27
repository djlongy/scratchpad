# bash functions that enable fast switching between branches and cleaning up orphaned merged branches by leveraging fzf
# Append these to your .bashrc file

# Git checkout interactive
gci() {
  local current
  current=$(git branch --show-current)

  local branches
  branches=$(
    {
      git for-each-ref --sort=-committerdate \
        --format='%(refname:short)|%(refname:short)|%(committerdate:relative)|%(subject)' \
        refs/heads

      git for-each-ref --sort=-committerdate \
        --format='%(refname:short)|%(committerdate:relative)|%(subject)' \
        refs/remotes/origin | \
        awk -F'|' '$1 != "origin/HEAD" {
          name = $1
          sub(/^origin\//, "", name)
          print name "|" $1 "|" $2 "|" $3
        }'
    } | awk -F'|' '!seen[$1]++'
  )

  local branch
  branch=$(printf '%s\n' "$branches" | \
    awk -F'|' -v curr="$current" 'BEGIN { OFS="\t" }
      {
        marker = ($1 == curr) ? "* " : "  "
        branch_color = ($1 == curr) ? "\033[1;32m" : "\033[33m"
        branch_col = sprintf("%-40s", $1)
        date_col = sprintf("%-20s", $3)
        branch_display = branch_color marker branch_col "\033[0m"
        date_display = "\033[36m" date_col "\033[0m"
        print $1, $2, branch_display, date_display, $4
      }' | \
    fzf --ansi --height=40% --reverse --delimiter=$'\t' --with-nth=3,4,5 \
      --preview "git log --oneline --graph --date=short --color=always --pretty='format:%C(auto)%cd %h%d %s' {2} --" | \
    awk -F'\t' '{print $1}')

  [[ -n "$branch" ]] && git checkout "$branch" 2>&1
}

# Delete local branches where remote is gone
alias gbp='git branch -vv | grep ": gone]" | awk "{print \$1}" | sed "s/^\* //" | xargs -r git branch -D'