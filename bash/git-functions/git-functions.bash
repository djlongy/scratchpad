# bash functions that enable fast switching between branches and cleaning up orphaned merged branches by leveraging fzf
# Append these to your .bashrc file

# Git checkout interactive
gci() {
  local current=$(git branch --show-current)
  local branches=$(git for-each-ref --sort=-committerdate \
    --format='%(refname:short)|%(committerdate:relative)|%(subject)' \
    refs/heads refs/remotes/origin | \
    sed 's|origin/||' | \
    awk -F'|' '!seen[$1]++')

  local branch=$(echo "$branches" | \
    awk -F'|' -v curr="$current" '{
      if($1==curr)
        printf "\033[1;32m* %-40s\033[0m \033[36m%-20s\033[0m %s\n", $1, $2, $3
      else
        printf "  \033[33m%-40s\033[0m \033[36m%-20s\033[0m %s\n", $1, $2, $3
    }' | \
    fzf --ansi --height=40% --reverse \
      --preview "git log --oneline --graph --date=short --color=always --pretty='format:%C(auto)%cd %h%d %s' \$(echo {} | awk '{print \$1}' | sed 's/^\* //; s/ *//') --" | \
    sed 's/^\* //' | \
    awk '{print $1}')

  [[ -n "$branch" ]] && git checkout "$branch" 2>&1
}

# Delete local branches where remote is gone
alias gbp='git branch -vv | grep ": gone]" | awk "{print \$1}" | sed "s/^\* //" | xargs -r git branch -D'