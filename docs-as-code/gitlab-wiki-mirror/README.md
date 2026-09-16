# Repo docs and the GitLab wiki, kept in sync both ways

Keep documentation as Markdown in a git repo, review it through merge requests, and let CI
publish it to the project's GitLab wiki after every merge. Edits made in the wiki UI flow
back into the repo as commits by the person who made them. For GitLab instances without
Pages this is the whole publishing story; with Pages it is a second view of the same files.

Companion to the [Material for MkDocs tutorial](../mkdocs-material/). Both build from the
same `docs/` tree.

## Use the component library instead, if you can

This folder is the technique in its rawest form: a handful of scripts you copy into a repo.
It is **not** the way to adopt it if you have a choice.

The same code is packaged as a GitLab CI component in
**[djlongy/gitlab-ci-templates](https://github.com/djlongy/gitlab-ci-templates)**, where a
consuming repository's whole `.gitlab-ci.yml` is three lines and the component carries its
runtime embedded, so there is nothing to copy and nothing to keep up to date. It also adds
the thing standalone scripts cannot have: the job repairs its own wiki webhook and trigger
token on every default-branch run, so a project that is renamed or moved keeps working.

- Adoption, end to end, for a reader with no context:
  [`docs/howto/docs-wiki-sync.md`](https://github.com/djlongy/gitlab-ci-templates/blob/main/docs/howto/docs-wiki-sync.md)
- Getting the library onto your own GitLab, and the two ways to include it:
  [`docs/howto/consuming-the-library.md`](https://github.com/djlongy/gitlab-ci-templates/blob/main/docs/howto/consuming-the-library.md)

**Keep reading here if you cannot include a library at all** — an air-gapped instance with
no mirror of it, a repository that must vendor everything it runs, or a single project where
a component is more machinery than the job is worth. The scripts below are a **verbatim copy
of that library's `runtime/wiki/`**, kept byte-identical so the two cannot drift; that is why
a comment in `wiki-deploy.sh` still names the library's path layout.

What is here:

| File | Purpose |
|---|---|
| [`wiki-import.py`](wiki-import.py) | One-time: turn a clone of an existing wiki into a `docs/` tree with relative links |
| [`wiki-sync.py`](wiki-sync.py) | Repo → wiki: rewrite the wiki from `docs/`, links and attachments included, sidebar from the nav |
| [`wiki-pull.py`](wiki-pull.py) | Wiki → repo: every wiki commit since the last sync becomes a repo commit by its author, three-way merged, newer edit wins |
| [`wiki-deploy.sh`](wiki-deploy.sh) | The job: clone, pull, push (`ci.skip`), sync, push. Same command in CI and from a shell: `scripts/wiki-deploy.sh docs wiki` |
| [`.env.example`](.env.example) | The variables for a local run; copy to `.env` (gitignored) |
| [`docfilter.py`](docfilter.py) | Shared include/exclude matching, the MkDocs `exclude_docs` / `not_in_nav` rules; both scripts import it |
| [`reconcile.py`](reconcile.py) | Creates and repairs the wiki webhook and its pipeline trigger token; writes only on a difference |
| [`wiki-bootstrap.sh`](wiki-bootstrap.sh) | First-time wiring for one project: runs `reconcile.py`, then creates the hourly safety-net schedule |
| [`httpjson.py`](httpjson.py) | The tiny stdlib HTTP/JSON helper `reconcile.py` imports |
| [`.gitlab-ci.yml`](.gitlab-ci.yml) | The full pipeline: lint on MRs, two-way wiki sync (and optionally Pages) on the default branch |
| [`example/`](example/) | A two-section wiki as GitLab stores it, and the `docs/` tree the import produces from it |

The scripts are stdlib plus PyYAML, no other dependencies, no assumptions about the instance
beyond how GitLab lays out a wiki repository. Keep the `.py` files and `wiki-deploy.sh`
together (`scripts/`); `reconcile.py` imports `httpjson.py` from beside it, so those two move
as a pair.

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
- Page links are relative to the page's own directory with no extension and an explicit
  `./` on siblings (`../engineering/release-process`, `./restore`, `../home`); attachment
  links keep their extension (`../uploads/3f2a/network.png`). Measured on GitLab 18.9 with
  `curl`: a wiki URL ending in `.md` returns the raw file (`text/plain`), the extension-less
  one returns the rendered page (`text/html`), `../` segments normalise in the browser, and a
  bare sibling without `./` is resolved from the wiki root. So this is the one form the wiki
  UI renders correctly from any depth; in a clone of the wiki repository an editor will open
  the attachments but not follow the extension-less page links (GitLab's own convention).
  `/uploads/...` (project uploads) is left alone in both directions.
- Attachments are copied; pages and files no longer in `docs/` are deleted. Hidden files and
  hidden directories under `docs/` (`.pages`, `.git`, tool caches) are never copied; a stray
  `.git` copied into the wiki clone would replace its remote and break the push.
- `_sidebar.md` is generated from the `.pages` nav: section titles link to the section page,
  children nest under them, `...` expands to the remaining pages sorted.

Round trip on the example (a wiki page's links after import and sync):

```text
wiki-export/operations.md    operations/backups     engineering/release-process         uploads/3f2a/network.png
docs/operations/index.md     backups.md             ../engineering/release-process.md   ../uploads/3f2a/network.png
wiki/operations.md           ./operations/backups   ./engineering/release-process       uploads/3f2a/network.png
```

(The section page moved up one directory on the way back, so its relative links lost one
`../`.) CRLF pages and files without a trailing newline cross unchanged; names with spaces
are percent-encoded in links, unicode names are not.

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
| `lint` | MR, feature branch | `zensical build --clean --strict`: broken links and pages missing from the nav fail |
| `markdownlint` | MR, feature branch | `markdownlint-cli2` with `.markdownlint.yaml` |
| `links` | MR, feature branch | `lychee --offline` over `docs/`: file links only, so private hosts do not fail it |
| `scripts-test` | MR, branch, default branch | the scripts' 149 tests, with `coverage.xml` for a SonarQube job |
| `wiki` | default branch, only if `WIKI_TOKEN` is set | `scripts/wiki-deploy.sh docs wiki`: pull wiki edits into the repo, then sync the repo into the wiki |
| `deploy-docs` | default branch | GitLab Pages (`pages: publish: site`, GitLab 17.9+); delete this job on an instance without Pages |

Setup, once per repo:

1. Copy the `.py` files, `wiki-deploy.sh` and `wiki-bootstrap.sh` to `scripts/`, plus
   `.gitlab-ci.yml`, `requirements.txt`,
   `.markdownlint.yaml`, and `mkdocs.yml` (needed by `lint` even if you never publish a
   site; it is what validates the links). `requirements.txt` pins Zensical; the Material
   for MkDocs pins are commented in it as the fallback.
2. Create a project access token: *Settings > Access tokens*, name `wiki-sync`, scope
   `write_repository`, role Maintainer if the default branch is protected (the token pushes
   pulled wiki edits to it), Developer otherwise. Save it in your secret store first.
3. *Settings > CI/CD > Variables*: `WIKI_TOKEN`, masked, protected if the default branch is.
4. Add `wiki/` and `.env*` to `.gitignore` (the job clones the wiki into `wiki/`; a local run
   does the same and must never be committed).
5. Merge to the default branch.
6. Optional, to make **wiki edits** start a pipeline rather than waiting for the next push:

   ```bash
   GITLAB_HOST=gitlab.example.com GITLAB_TOKEN=<api-scope token> \
     scripts/wiki-bootstrap.sh group/my-repo
   ```

   It creates the wiki-page-events webhook, the pipeline trigger token the webhook URL
   carries, and an hourly schedule as a safety net for a missed delivery. It looks each one
   up first and reports "already present", so it is safe to run again.

### The webhook is a project setting, and it rots

That webhook URL contains the project id and the default branch name. Rename the project,
move it to another group, change the default branch or move the server, and the URL stops
resolving. **Nothing fails**: the hourly schedule keeps the job green while wiki edits
quietly stop arriving, and the only repair is for somebody to remember to run the script
again.

`reconcile.py` is what fixes that. It rebuilds the URL the hook must have from
`CI_API_V4_URL`, `CI_PROJECT_ID` and `CI_DEFAULT_BRANCH`, compares, and writes only on a
difference, so a second run reports `webhook already correct` and writes nothing. Run it
from the job on every default-branch run and the wiring repairs itself:

```bash
WIKI_ADMIN_TOKEN=<api-scope token> python3 scripts/reconcile.py
```

It needs the `api` scope, which the sync itself does not, so give it its own variable rather
than widening `WIKI_TOKEN`. It never prints a token, including the one inside the hook URL.

**It keeps the trigger token the webhook already carries** and rewrites only the address
around it, minting one only when there is none to keep. That is not politeness. A pipeline
started by a trigger token runs **as that token's owner** and sees only the variables that
identity can see, so if `WIKI_TOKEN` is a group-level protected variable and the repair
swapped in a token owned by a project access token's bot, the triggered pipeline could no
longer see it: every rule requires `$WIKI_TOKEN`, no job would match, and a wiki edit would
produce a failed pipeline containing nothing. Repairing a hostname must not change who the
delivery runs as. GitLab returns the hook URL verbatim, token included, so the value is
readable whoever created it and the repair never needs to own anything. When it does mint a
token, because there is no webhook or the URL carries none, it says so in the log.

The component library does this for you, gated by one input. That is the main reason to
prefer it over copying these files.

The script never uses anyone's ssh keys or the checkout's `origin`: every clone and push
goes over https with `WIKI_TOKEN`, as a service account would, and git is told never to
prompt. In CI without a `WIKI_TOKEN` the repo push falls back to `CI_JOB_TOKEN` (then
*Settings > CI/CD > Token Access > "Allow Git push requests to the repository"* must be on).
The push carries `-o ci.skip`, so it starts no pipeline; the same job mirrors the result into
the wiki straight afterwards.

Without `WIKI_TOKEN` the `wiki` job is skipped by its rule, so the pipeline can land
before the token exists.

**Shell-executor runners, no registry, no internet.** Nothing here needs a container image:
`wiki-deploy.sh` and the three Python scripts are stdlib plus PyYAML, and a shell executor
runs them on the host. Drop the `image:` key from the `wiki` job, install PyYAML from the
distribution (`dnf install python3-pyyaml`, `apt-get install python3-yaml`) or from an
internal index with `PIP_INDEX_URL`, and the job never reaches the network except to talk
to GitLab. Beware one trap if you keep a `requirements.txt` with `--require-hashes`: the
hashes name specific wheels, so a set built for one interpreter fails on a host running
another — EL9 ships Python 3.9, and PyYAML 5.4.1 from AppStream works as well as 6.0.2.
The packaged version of the same technique, as a GitLab component with an `executor`
input, is in
[djlongy/gitlab-ci-templates](https://github.com/djlongy/gitlab-ci-templates) under
`templates/docs-wiki-sync/`.


**A wiki edit does not start a pipeline by itself.** Pipelines start on a push to the repo,
a schedule or a trigger, so without one more piece an edit made in the wiki UI waits for the
next repo push. Two pieces, both native:

1. A pipeline trigger token (*Settings > CI/CD > Pipeline trigger tokens*) and a project
   webhook (*Settings > Webhooks*) with only **Wiki page events** ticked, whose URL is the
   trigger endpoint:
   `https://<gitlab>/api/v4/projects/<id>/ref/main/trigger/pipeline?token=<trigger token>`.
   Every page create, edit or delete in the UI starts a pipeline within seconds. The CI's own
   push into the wiki repository does not fire wiki page events, so there is no loop; the
   `ci.skip` on the repo push covers the other direction.
2. A pipeline schedule (*Build > Pipeline schedules*, hourly is plenty) as the safety net for
   a missed webhook.

`.not_for_wiki_sync` in `.gitlab-ci.yml` keeps every job but `wiki` out of those
trigger and schedule pipelines, so an edit costs one short job, not a full run.

Why a token: `CI_JOB_TOKEN` can clone a wiki but GitLab refuses its pushes
(`You are not allowed to write to this project's wiki`), even with "Allow Git push
requests to the repository" on; that setting covers the project repository only. Wiki CI
and job-token wiki pushes are open GitLab issues
([16261](https://gitlab.com/gitlab-org/gitlab/-/issues/16261),
[419680](https://gitlab.com/gitlab-org/gitlab/-/issues/419680)).

## Wiki to repo, step by step

The repo-to-wiki half needs nothing beyond the `wiki` job. The wiki-to-repo half needs the
pipeline to run when a page is saved. Every step is a GitLab setting, no extra service.

1. **Token that starts pipelines.** *Settings > CI/CD > Pipeline trigger tokens > Add new
   token*, description `wiki-sync`. Copy it now: GitLab shows it once. It can do one thing,
   start a pipeline on this project.
2. **Webhook on wiki saves.** *Settings > Webhooks > Add new webhook*.
   - URL: `https://<gitlab>/api/v4/projects/<project id>/ref/<default branch>/trigger/pipeline?token=<trigger token>`
     (the project id is on the project's home page under its name).
   - Trigger: tick **Wiki page events** only.
   - Enable SSL verification. Save.
   - *Test > Wiki page events* on the new hook should return 201 and a pipeline appears under
     *Build > Pipelines* with source `trigger`.
3. **Token that pushes.** `WIKI_TOKEN` as in the setup list above: a project access token,
   scope `write_repository`, Maintainer if the default branch is protected, saved as a masked
   CI variable. This is the identity the `wiki` job pushes with, in both directions.
4. **Keep the other jobs out.** `.not_for_wiki_sync` in `.gitlab-ci.yml` is already applied
   to every job except `wiki`, so a trigger pipeline runs one short job.
5. **Safety net.** *Build > Pipeline schedules > New schedule*, description `wiki sync
   safety net`, cron `0 * * * *`, target the default branch. It catches a missed webhook and
   the one case the webhook cannot see: a direct `git push` to `<project>.wiki.git`, which
   fires no wiki page event.
6. **Prove it.** Edit any page in the wiki UI and save. Within a minute a pipeline with
   source `trigger` runs the `wiki` job, and the default branch gains a commit with you as
   the author, titled `wiki: ` plus the wiki's own commit message, touching only that
   page's docs file. The wiki then shows a "Sync docs from <sha>" commit by the CI author.

If step 6 shows a pipeline but no commit, read the `wiki` job log: it names the wiki
commits it found and what it did with each. If step 6 shows no pipeline, the webhook's
recent deliveries (*Settings > Webhooks > Edit > Recent events*) show the response GitLab
gave the trigger call.

## Two-way: wiki edits come back as commits

The wiki stays editable in the UI, and someone will use it. Every wiki commit after the
last CI commit was typed by a person, so `wiki-pull.py` walks those commits and, for each
changed page:

1. converts it to its `docs/` form (`home.md` → `index.md`, `section.md` beside a
   `section/` folder → `section/index.md`, links back to relative `.md` links);
2. three-way merges it with `git merge-file`: base is the page as CI last wrote it, ours is
   `docs/` now, theirs is the wiki. A clean merge keeps both sides;
3. on conflict the newer edit wins (the wiki commit's date against the repo's last commit
   for that file);
4. a page deleted in the wiki is deleted from `docs/`, unless the repo changed it after the
   sync, in which case the repo copy stays and the next sync restores the wiki page;
5. commits the result with the wiki commit's author name, email and date, one repo commit
   per wiki commit, so `git blame` and the MR history show who wrote what.

Attachments added or removed in the wiki are copied or deleted the same way. `_sidebar.md`
and GitLab's own `.gitlab/redirects.yml` are wiki furniture and never pulled.

### The relationship is recorded, not inferred

The layout rule is not invertible on its own: a wiki page `compute/kubernetes` beside a
folder `compute/kubernetes/` is the section's `index.md`, but if that folder holds only
`.pages` (which the wiki never sees) the wiki has no folder and the page looks plain. So
every sync writes `.gitlab/docs-map.json` into the wiki, `wiki path → docs path` for every
page it wrote, committed with the sync. The pull reads that file at the last CI commit and
maps an edited or deleted page back exactly, at any depth, before any rule runs. Users never
open it; GitLab keeps its own metadata in the same folder; a dot file is not a page.

What is still a rule, because no record can exist:

- A page **created** in the wiki UI at `X` becomes `docs/X/index.md` when a section `X/`
  exists on either side, else `docs/X.md`.
- A page **renamed** in the wiki (a delete plus an add that git pairs by content, `-M25%`)
  becomes a `git mv` in `docs/`, so history follows the page; the new path follows the rule.
  A rename that also rewrites most of the page falls below the pairing threshold and lands as
  a delete plus a new file.
- A page **copied** in the UI is a new page. If two wiki pages would land on one docs file,
  the pull refuses with both paths and exits non-zero; the cycle stops before the wiki is
  rewritten, so both pages stay where they are for a person to decide.

On the docs side the sync refuses two files that would become one wiki page
(`a/b.md` next to `a/b/index.md`). Front matter was considered for carrying the record
inside each page and rejected: a wiki UI edit strips it.

`wiki-deploy.sh` runs the pull first, pushes those commits, and only then rewrites the wiki
from `docs/`, so a wiki edit is never overwritten before it has landed in the repo. The CI
sync commit is written whenever the wiki's tip is not already a CI commit, even when the
content is right, because that commit is the marker "everything before here is synced".

Cases the scripts settle on purpose (each has a test):

- **Wiped wiki.** A wiki with no commits, or with no pages left after the human commits
  (everything deleted in the UI, an empty branch force-pushed), is a reset: nothing is pulled,
  no docs file is deleted, the wiki is reseeded from `docs/`. Deleting one page still
  propagates to `docs/`.
- **Newer edit wins, in both directions.** A page deleted in the repo after a wiki edit stays
  deleted; deleted before the wiki edit, the wiki edit restores it. A page created and deleted
  in the wiki within the same batch ends deleted.
- **Someone edits the wiki while the job runs.** The wiki push is rejected as
  non-fast-forward, and the whole cycle runs again (clone, pull, push, sync; `SYNC_ATTEMPTS`,
  default 3) so the late edit is pulled too. If it keeps failing the job exits non-zero with
  the wiki untouched; the edits are still in the wiki for the next run.
- **Someone pushes to the docs branch while the job runs.** The repo push is rejected, the
  pulled commits are rebased onto the new tip and pushed again; a conflict aborts the rebase
  and fails the job before the wiki is touched.
- **A person using the CI's display name** is still a person: the sync marker is matched on
  name and email.
- **Non-ASCII page names**, names with spaces, CRLF pages, 150-file wiki commits, a detached
  HEAD checkout (CI) pushing to the branch, dry runs in every state.

Run it from a shell exactly as CI does:

```bash
cp .env.example .env    # host, project path, token
source .env
DRY_RUN=1 scripts/wiki-deploy.sh docs wiki      # report only
scripts/wiki-deploy.sh docs wiki                # pull, push, sync
```

Locally and in CI alike the repo push goes over https with `WIKI_TOKEN`, never your ssh
keys and never the checkout's `origin`. A run in an empty `HOME` with no git identity, no
ssh keys and no credential helper completes without a prompt: git is told not to ask
(`GIT_TERMINAL_PROMPT=0`), the global and system git configs are ignored, and the token
lives in a mode-600 temp file removed on exit, so no clone keeps it in `.git/config`.
`WIKI_URL` and `REPO_PUSH_URL` override the URLs (the tests point them at `file://` repos).

Two things a reused CI build directory can do to this job, both handled: a stray
`docs/.git` from an earlier job would make every git command act on that nested repo, so
the script resolves the repo from the folder above `docs/`, reports and removes the nested
one in CI, and refuses to run over it locally; and a global git config in the runner image
is ignored (`GIT_CONFIG_GLOBAL=/dev/null`).

## Tests and quality

`test_wiki_tools.py` covers the import, sync and filter modules: every link form the import must resolve, the
page and attachment layout, unresolved-link reporting, generated home and section pages
(including a wiki made of root folders only), sidebar order from `.pages` (`...`, a parent
label overriding a folder title), stale page and attachment deletion, idempotence, the
filter grammar, `mkdocs.yml` parsing with `!!python/name` tags present, include/exclude and
`not_in_nav` behaviour, the command-line entry points, and the import-then-sync round trip.
`test_wiki_pull.py` builds a repo with an origin and a wiki that CI has synced once, all as
local git repos, then covers: an edit landing in `docs/` with the wiki author, link and
section-page rewriting, a clean merge with a non-overlapping repo edit, conflicts won by
the newer side in both directions, delete plus new page plus attachment, a delete refused
because the repo edited the page, wiki furniture ignored, idempotence across a second sync,
dry run, and `wiki-deploy.sh` end to end (round trip, dry run, the nested `docs/.git` case).
`test_scenarios_content.py` (74 tests) walks every link form a person can type in the wiki
UI from root, section and nested pages, attachments, renames, unicode and CRLF, filters,
byte-identical round trips. `test_scenarios_races.py` (25 tests) drives `wiki-deploy.sh`
end to end against bare repos whose `pre-receive` hook pushes a competing commit and rejects
the first push, to prove the retry and rebase paths, plus wipes, resets, attribution edge
cases and dry runs.

```bash
pip install pyyaml pytest
python3 -m pytest -q .                          # 143 passed, 1 xfailed, about 6 minutes
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

The same scripts are packaged as a reusable CI template, so a consumer repo adds one
`include:` instead of copying them: [djlongy/gitlab-ci-templates](https://github.com/djlongy/gitlab-ci-templates) (`docs/wiki-sync.yml`, setup in `docs/WIKI_SYNC.md`).
