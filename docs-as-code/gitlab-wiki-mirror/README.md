# Repo docs to GitLab wiki, automatically

The second way to publish a Markdown repo when GitLab Pages is not available: keep
`docs/` as the only source and let CI mirror it into the project's wiki after every
merge, with a sidebar in the same order as the MkDocs nav. Readers use the Wiki tab;
nothing to host, nothing to enable.

Companion to the [Material for MkDocs tutorial](../mkdocs-material/). Both views come
from the same `docs/`; use either or both.

## How it works

The wiki is a separate git repo (`<project>.wiki.git`) that GitLab renders. The job:

1. clones the wiki with a project access token,
2. refuses to run if the wiki's last commit is not its own (someone edited a page in the
   wiki UI; that edit would otherwise be silently overwritten),
3. runs `wiki-sync.py docs wiki`, which rewrites the wiki tree from `docs/`,
4. commits and pushes only if something changed.

`wiki-sync.py` (stdlib + PyYAML, ~110 lines):

- `docs/index.md` becomes `home.md`, the wiki front page. `section/index.md` becomes
  `section.md`, so the wiki titles the page "section" rather than "index".
- Relative `.md` links are rewritten to root-absolute wiki paths (`/compute/kubernetes`).
  GitLab resolves relative wiki links against the wiki root, not the page's directory,
  so `../runbooks/x.md` from a subpage would break otherwise. Anchors are preserved.
- `_sidebar.md` is generated from each folder's `.pages` file (the
  [awesome-pages](https://github.com/lukasgeiter/mkdocs-awesome-pages-plugin) format:
  `nav:` list, `...` for "everything else, sorted", optional `title:`). Folders without
  a `.pages` file are listed alphabetically. Page titles come from the first `# ` heading.
- Stale pages are deleted, so removals in `docs/` propagate.

## Setup

1. Copy `wiki-sync.py` into the docs repo (for example `scripts/wiki-sync.py`) and add
   the `wiki` job from [`.gitlab-ci.yml`](.gitlab-ci.yml) to the repo's pipeline,
   adjusting the script path and stage name.
2. Create a project access token: *Settings > Access tokens*, scope `write_repository`,
   role Developer, an expiry you will remember. Store it somewhere safe first.
3. Add it as a CI/CD variable `WIKI_TOKEN`, masked, protected if the default branch is.
4. Merge to the default branch. The first run populates the wiki; every later merge
   updates it.

Without `WIKI_TOKEN` the job is skipped by its rule, so a repo can carry the job before
the token exists.

Why not `CI_JOB_TOKEN`: it can clone the wiki, but GitLab refuses its pushes
(`You are not allowed to write to this project's wiki`), even with the project's
"Allow Git push requests to the repository" setting on. That setting covers the
project repository only.

## Try it locally

```bash
pip install pyyaml
git clone git@gitlab.example.com:group/project.wiki.git /tmp/wiki
python3 wiki-sync.py docs /tmp/wiki
cat /tmp/wiki/_sidebar.md
```

Against the tutorial's example site (no `.pages` files, so alphabetical order):

```text
$ python3 wiki-sync.py ../mkdocs-material/example/docs /tmp/wiki
synced 6 pages, sidebar 4 lines
```

## Rules of the road

- Edit `docs/`, never the wiki. The guard fails the pipeline if a hand edit is found;
  copy the change into `docs/` and let the next merge overwrite it.
- Keep links relative with the `.md` extension in `docs/` (`../runbooks/x.md`). That
  style renders in GitLab's file view, in MkDocs and, after rewriting, in the wiki.
- The wiki has no CI of its own and job tokens cannot push to it; these are open GitLab
  issues ([16261](https://gitlab.com/gitlab-org/gitlab/-/issues/16261),
  [419680](https://gitlab.com/gitlab-org/gitlab/-/issues/419680)), so a token is the way.
