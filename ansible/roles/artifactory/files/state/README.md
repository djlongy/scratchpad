# Artifactory captured state (per environment)

`mode: backup` writes this tree, one folder per `artifactory_env`:

```
<env>/artifactory.yml                       # the state file (apply/compare/merge read it)
<env>/artifactory.system-config.xml         # raw global descriptor (self-hosted only)
<env>/artifactory.system-config.parsed.yml  # XML→YAML reference
<env>/artifactory.drift.yml                 # written by mode: compare
```

`artifactory_state_dir` defaults to this directory; override it to relocate.

## Why captures are gitignored here

A capture contains real internal configuration — instance hostname, LDAP manager
and group DNs, user emails, repository layout, and the full system-config XML.
**This is a public repository**, so the captured files are gitignored (see
`.gitignore`); only this README and the ignore rule are tracked, so the structure
stays documented.

To keep a git-tracked As-Built **history** (e.g. to diff prod over time, or to
prune against a committed baseline), set `artifactory_state_dir` to a **private**
path — e.g. a private inventory/ops repo — where committing the real config is
safe.
