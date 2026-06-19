# Artifactory captured state (per environment)

`mode: backup` writes this tree, one folder per `artifactory_env`. Everything
here is **REFERENCE ONLY** — the role never applies it; desired state lives in
group_vars. To manage a section, copy it from a reference file into group_vars.

```
<env>/artifactory.config.reference.yml             # bundled capture (repos / security / projects / xray …)
<env>/artifactory.system-config.reference.yml   # system config, named-root, PATCH-ready
<env>/artifactory.drift.yml                 # written by mode: compare
```

`artifactory_state_dir` defaults to this directory; override it to relocate.

## Why captures are gitignored by default

A capture contains real internal configuration — instance hostname, LDAP manager
and group DNs, user emails, repository layout, and the full system config — so
the captured files are gitignored (see `.gitignore`); only this README and the
ignore rule are tracked, so the structure stays documented.

To keep a git-tracked As-Built **history** (e.g. to diff prod over time, or to
prune against a committed baseline), set `artifactory_state_dir` to a location
where committing the captured config is acceptable (e.g. a dedicated inventory/ops
repo).
