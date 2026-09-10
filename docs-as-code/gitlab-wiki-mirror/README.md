# Repo docs to GitLab wiki, automatically

Keep documentation as Markdown in a git repo, review it through merge requests, and let CI
publish it to the project's GitLab wiki after every merge. For GitLab instances without
Pages this is the whole publishing story; with Pages it is a second view of the same files.

Companion to the [Material for MkDocs tutorial](../mkdocs-material/). Both build from the
same `docs/` tree.

What is here:

| File | Purpose |
|---|---|
| [`wiki-import.py`](wiki-import.py) | One-time: turn a clone of an existing wiki into a `docs/` tree with relative links |
| [`wiki-sync.py`](wiki-sync.py) | Every merge: rewrite the wiki from `docs/`, links and attachments included, sidebar from the nav |
| [`docfilter.py`](docfilter.py) | Shared include/exclude matching, the MkDocs `exclude_docs` / `not_in_nav` rules; both scripts import it |
| [`.gitlab-ci.yml`](.gitlab-ci.yml) | The full pipeline: lint on MRs, wiki (and optionally Pages) on the default branch |
| [`example/`](example/) | A two-section wiki as GitLab stores it, and the `docs/` tree the import produces from it |

The scripts are stdlib plus PyYAML, no other dependencies, no assumptions about the instance
beyond how GitLab lays out a wiki repository. Keep the three `.py` files together (`scripts/`).

## The repo layout to aim for

```text
docs/
  index.md                       wiki home page
  .pages                         nav order (optional; alphabetical without it)
  operations/
    index.md                     the section's landing page (was the root wiki page)
    .pages
    backups.md
    onboarding.md
  engineering/
    index.md
    release-process.md
    coding-standards.md
  uploads/                       attachments, wherever the wiki had them
    3f2a/network.png
mkdocs.yml                       if you also want a MkDocs site
requirements.txt
scripts/wiki-sync.py
.gitlab-ci.yml
.markdownlint.yaml
```

Rules that make every renderer happy (GitLab file view, merge-request diffs, MkDocs, the
wiki after sync):

- Links are **relative, with the `.md` extension**: `backups.md`, `../engineering/release-process.md`.
- Attachments are linked relatively too: `../uploads/3f2a/network.png`.
- A section's landing page is `index.md` inside its folder. MkDocs treats it as the
  section index; the sync turns it into the wiki's `section` page so children nest under it.
- One topic per page; a page name is its URL, so pick names you can live with.

## Migrating an existing wiki (the two-root-page case)

A typical wiki has a couple of root pages, each with children and attachments, and links
between them. GitLab stores it as a git repo:

```text
home.md
operations.md            root page
operations/backups.md    child pages
operations/onboarding.md
engineering.md
engineering/release-process.md
uploads/3f2a/network.png             attachments
_sidebar.md                          if a custom sidebar was set
```

Links in those pages come in whatever form the author typed: `operations/backups`,
`../engineering/release-process`, `/operations/backups`, `onboarding`,
`uploads/3f2a/network.png`. GitLab resolves every one of them against the wiki root, which
is why moving pages by hand breaks links.

The import does the move and the rewriting:

```bash
git clone git@gitlab.example.com:group/project.wiki.git wiki-export
python3 wiki-import.py wiki-export docs
```

What it does:

1. `home.md` becomes `docs/index.md`. A root page that has a child folder becomes that
   folder's `index.md` (`operations.md` → `operations/index.md`);
   a root page without children stays a top-level file.
2. Child pages and attachments keep their paths.
3. Every link is resolved the way GitLab would (wiki root first, then the page's own
   folder, with and without `.md`) and rewritten as a relative `.md` link from the page's
   new location. Cross-section links (`../engineering/release-process.md`) and
   attachment links (`../uploads/3f2a/network.png`) come out of this naturally.
4. Links it cannot resolve are left unchanged and printed at the end. Fix those by hand.
5. `_sidebar.md` and `templates/` are dropped; the sync regenerates the sidebar.

**Root folders instead of root pages.** Some wikis have no `operations.md` at all, only an
`operations/` folder holding the children, with attachments nested inside it rather than under
`uploads/`. The import handles that shape too: paths are kept, attachment links resolve wherever
the file lives, and every folder that holds pages but no landing page gets a generated
`index.md` listing its contents (and a home page if the wiki had none), so the sections still
appear in the sidebar and in MkDocs. Generated pages are ordinary files; rewrite them once the
import is in the repo.

Then add `.pages` files if you want a fixed order (see [`example/docs/`](example/docs/));
without them each folder is listed alphabetically. Commit `docs/`, add the pipeline, create
the token (below), merge. The first `wiki` job rewrites the wiki from the repo.

The [`example/`](example/) folder is exactly this: `wiki-export/` is a wiki with two root
pages, cross-links in all five forms, and two attachments; `docs/` is what the import
produced from it, untouched except for the three `.pages` files.

## The sync, every merge

`wiki-sync.py docs wiki` rewrites the wiki checkout:

- `docs/index.md` → `home.md`; `section/index.md` → `section.md`; other pages keep paths.
- `.md` links → root-absolute wiki paths without extension (`/operations/backups`).
  These resolve from any page depth.
- Attachment links → wiki-root-relative without a leading slash (`uploads/3f2a/network.png`),
  the form GitLab writes itself. A leading slash would point at the project's uploads, and
  `../uploads/...` does not resolve from a nested page.
- Attachments are copied; pages and files no longer in `docs/` are deleted. Hidden files and
  hidden directories under `docs/` (`.pages`, `.git`, tool caches) are never copied; a stray
  `.git` copied into the wiki clone would replace its remote and break the push.
- `_sidebar.md` is generated from the `.pages` nav: section titles link to the section page,
  children nest under them, `...` expands to the remaining pages sorted.

Round trip on the example (a wiki page's links after import and sync):

```text
wiki-export/operations.md    operations/backups   engineering/release-process   uploads/3f2a/network.png
docs/operations/index.md     backups.md                       ../engineering/release-process.md   ../uploads/3f2a/network.png
wiki/operations.md           /operations/backups  /engineering/release-process  uploads/3f2a/network.png
```

## Filters: the same options as MkDocs

Both scripts take `--include PATTERN`, `--exclude PATTERN` (repeatable) and
`--config mkdocs.yml`. Patterns are gitignore-style, exactly what MkDocs accepts in
`exclude_docs` and `not_in_nav`:

```text
drafts/            a folder anywhere, and everything under it
*.tmp              a name anywhere
/internal/*.md     anchored at the docs root
reference/**       everything under a folder
```

With `--config mkdocs.yml` the `exclude_docs` block is applied to the sync (excluded pages
and attachments are neither copied nor linked; a link to one is left as written) and
`not_in_nav` pages are copied but left out of the sidebar, matching what MkDocs does with its
nav. One file then governs the site, the wiki mirror and the import:

```yaml
# mkdocs.yml
exclude_docs: |
  drafts/
  *.tmp
not_in_nav: |
  /glossary.md
```

```bash
python3 scripts/wiki-sync.py docs wiki --config mkdocs.yml
python3 wiki-import.py wiki-export docs --exclude 'templates/' --include '/operations/**' --include '/home.md'
```

`--include` keeps only matching paths (useful for importing one section of a large wiki);
`--exclude` and the config's `exclude_docs` are added on top.

## The pipeline

[`.gitlab-ci.yml`](.gitlab-ci.yml), in full, is what to copy. Jobs:

| Job | When | What |
|---|---|---|
| `lint` | MR, feature branch | `mkdocs build --strict`: broken links and pages missing from the nav fail |
| `markdownlint` | MR, feature branch | `markdownlint-cli2` with `.markdownlint.yaml` |
| `links` | MR, feature branch | `lychee --offline` over `docs/`: file links only, so private hosts do not fail it |
| `wiki` | default branch, only if `WIKI_TOKEN` is set | clone wiki, refuse over a hand edit, sync, push on change |
| `pages` | default branch | GitLab Pages; delete this job on an instance without Pages |

Setup, once per repo:

1. Copy `wiki-sync.py` and `docfilter.py` to `scripts/`, plus `.gitlab-ci.yml`, `requirements.txt`,
   `.markdownlint.yaml`, and `mkdocs.yml` (needed by `lint` even if you never publish a
   site; it is what validates the links).
2. Create a project access token: *Settings > Access tokens*, name `wiki-sync`, scope
   `write_repository`, role Developer. Save it in your secret store first.
3. *Settings > CI/CD > Variables*: `WIKI_TOKEN`, masked, protected if the default branch is.
4. Merge to the default branch.

Without `WIKI_TOKEN` the `wiki` job is skipped by its rule, so the pipeline can land
before the token exists.

Why a token: `CI_JOB_TOKEN` can clone a wiki but GitLab refuses its pushes
(`You are not allowed to write to this project's wiki`), even with "Allow Git push
requests to the repository" on; that setting covers the project repository only. Wiki CI
and job-token wiki pushes are open GitLab issues
([16261](https://gitlab.com/gitlab-org/gitlab/-/issues/16261),
[419680](https://gitlab.com/gitlab-org/gitlab/-/issues/419680)).

## The hand-edit guard

The wiki stays editable in the UI, and someone will use it. The `wiki` job checks the
author of the wiki's last commit; if it is not `docs ci`, the job fails with the author's
name and the pipeline goes red. Copy that change into `docs/` through a merge request; the
next merge overwrites the wiki and the guard passes again. Nothing is lost silently.

## Tests and quality

`test_wiki_tools.py` covers all three modules: every link form the import must resolve, the
page and attachment layout, unresolved-link reporting, generated home and section pages
(including a wiki made of root folders only), sidebar order from `.pages` (`...`, a parent
label overriding a folder title), stale page and attachment deletion, idempotence, the
filter grammar, `mkdocs.yml` parsing with `!!python/name` tags present, include/exclude and
`not_in_nav` behaviour, the command-line entry points, and the import-then-sync round trip.

```bash
pip install pyyaml pytest
python3 -m pytest -q test_wiki_tools.py        # 25 passed
```

Checked with ruff (`E,F,W,B,C90,N,UP,SIM`, clean) and a SonarQube "Sonar way" scan of the
scripts and the tests: quality gate passed, 0 bugs, 0 vulnerabilities, 0 code smells,
0 security hotspots, 98% line coverage. The uncovered lines are the `__main__` guards.

## Try it locally

```bash
pip install pyyaml
python3 wiki-import.py example/wiki-export /tmp/docs        # imported 7 pages, 2 attachments
python3 wiki-sync.py example/docs /tmp/wiki                 # synced 7 pages, 2 attachments, sidebar 6 lines
cat /tmp/wiki/_sidebar.md
```

To see it in a real wiki without touching production: create a throwaway project, clone
its (empty) wiki, run the sync into that clone, push, open the Wiki tab.
