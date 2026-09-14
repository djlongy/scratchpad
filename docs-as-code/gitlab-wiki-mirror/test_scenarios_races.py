"""Race-condition and git-edge scenarios for the two-way wiki sync (wiki-deploy.sh + wiki-pull.py +
wiki-sync.py). Local bare repos only; nothing touches GitLab.

Run: python3 -m pytest -q scripts/test_scenarios_races.py

Every test drives scripts/wiki-deploy.sh end to end (subprocess) the way CI does, against:

    repo.git   bare "origin" of the docs repo         repo/   the checkout the script runs in
    wiki.git   bare "<project>.wiki.git"              (the script makes its own wiki clone)

The spec the tests encode (see the docstring of each test for the concrete expectation):

    W  wipe        wiki with 0 commits, or 0 .md pages after the human commits since the last CI
                   sync, is a reset: pull nothing, delete nothing in docs/, reseed the wiki, write
                   a CI sync commit. Next run is a normal run.
    D  delete      a page deleted in the wiki is deleted from docs/ (committed by the wiki author)
                   unless the repo edited it after the last sync; then docs/ keeps it and the
                   wiki gets it back.
    R  races       (1) wiki push rejected because a human pushed between clone and push: retry
                   the whole cycle, the late edit lands in docs/, the wiki ends consistent, no
                   edit is lost. (2) repo push rejected because someone pushed main meanwhile:
                   fetch, rebase the pulled commits, push; both sides survive. (3) a retry that
                   still fails exits non-zero with the wiki untouched.
    A  attribution one repo commit per wiki commit with the wiki commit's author name/email/date;
                   committer is the git config identity (CI identity when the repo has none).
    I  idempotence a run with nothing to do creates no commit anywhere.

Race mechanism (R1, R2): the script is one process, so the concurrent push is injected from a
`pre-receive` hook on the bare repo the script pushes to. The first time the hook fires it
pushes a competing commit from a second clone into the same bare (with the hook environment
cleared, so the nested receive-pack is not confined to the outer push's quarantine), then
rejects the outer push with "simulated race". Later pushes pass. From the script's point of
view this is exactly a human who pushed after it cloned and before it pushed. R3 uses a hook
that rejects every push.
"""
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from test_wiki_pull import ALICE, CI, DEV, HERE, T1, T2, commit_all, git, wiki_edit, write
from test_wiki_pull import estate  # noqa: F401  (pytest fixture; every test parameter named estate is this one)
# ruff: noqa: F811

BOB = ("Bob Reviewer", "bob@example.com")
DEPLOY = HERE / "wiki-deploy.sh"
# Everything wiki-deploy.sh reads from the environment; dropped so the developer's shell (a sourced
# .env, a CI job) cannot point a test at a real host.
DEPLOY_ENV_VARS = ("CI", "CI_JOB_TOKEN", "REPO_PUSH_URL", "DRY_RUN", "WIKI_TOKEN", "WIKI_URL",
                   "CI_SERVER_FQDN", "GITLAB_HOST", "CI_PROJECT_PATH", "GITLAB_PROJECT_PATH",
                   "CI_DEFAULT_BRANCH", "CI_AUTHOR_NAME", "CI_AUTHOR_EMAIL", "MKDOCS_CONFIG")


# --- helpers --------------------------------------------------------------------------------

def deploy(repo, wiki_bare, **extra):
    """Run wiki-deploy.sh in `repo` against the local wiki bare, as CI would; pulled edits go to the
    checkout's `origin` (repo.git). Returns the CompletedProcess."""
    env = {k: v for k, v in os.environ.items() if k not in DEPLOY_ENV_VARS}
    # CI_SERVER_FQDN=example.com makes the script's sync identity "docs ci <ci@example.com>",
    # the identity the fixture's first sync commit carries, so a no-op run is recognised as one.
    env.update(WIKI_URL=str(wiki_bare), REPO_PUSH_URL="origin", CI_PROJECT_PATH="group/docs",
               CI_SERVER_FQDN="example.com", CI_DEFAULT_BRANCH="main",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    env.update(extra)
    # The wiki clone goes beside the checkout, not inside it, so `git status` in the repo stays clean.
    return subprocess.run(["bash", str(DEPLOY), "docs", str(Path(repo).parent / "wiki-stage")], cwd=repo, env=env,
                          capture_output=True, text=True)


def ts(iso):
    """Unix timestamp of an ISO date, for comparing against git's %at (git prints UTC as Z, not +00:00)."""
    return str(int(datetime.fromisoformat(iso).timestamp()))


def ok(result):
    assert result.returncode == 0, f"exit {result.returncode}\n--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}"
    return result


def head(bare, branch="main"):
    """Commit hash of a branch in a bare repo, or None when the branch has no commits."""
    out = subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                         capture_output=True, text=True)
    return out.stdout.strip() or None


def commits(bare, branch="main", fmt="%H"):
    """One formatted line per commit on the branch, newest first."""
    return git(bare, "log", f"--format={fmt}", branch).splitlines() if head(bare, branch) else []


def tree(bare, branch="main"):
    """Set of paths in the branch's tree."""
    paths = git(bare, "ls-tree", "-r", "--name-only", branch).split() if head(bare, branch) else []
    return {p for p in paths if not p.startswith(".gitlab/")}   # .gitlab/ is sync furniture


def blob(bare, path, branch="main"):
    return git(bare, "show", f"{branch}:{path}")


def files_in(bare, commit):
    return git(bare, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).split()


def clone(bare, path):
    git(path.parent, "clone", "-q", str(bare), str(path))
    return path


def install_race_hook(bare, competitor, reject_always=False):
    """pre-receive on `bare`. First push: push `competitor`'s HEAD into the bare first, then reject
    (the race). Later pushes pass. With reject_always every push is refused and nothing is
    injected (a push failure that no retry can fix). Returns the marker file the hook creates."""
    hook = Path(bare) / "hooks" / "pre-receive"
    marker = hook.with_suffix(".fired")
    inject = "" if reject_always else (
        f'env -i PATH="$PATH" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null '
        f'git -C "{competitor}" push -q origin HEAD:main || {{ echo "competing push failed" >&2; exit 2; }}\n')
    passthrough = "" if reject_always else f'[ -e "{marker}" ] && exit 0\n'
    hook.write_text("#!/bin/sh\n" + passthrough + f': > "{marker}"\n' + inject
                    + 'echo "simulated race: a human pushed meanwhile" >&2\nexit 1\n')
    hook.chmod(0o755)
    return marker


def snapshot(repo_bare, wiki_bare):
    return head(repo_bare), head(wiki_bare)


def docs_pages(repo):
    return sorted(str(p.relative_to(repo / "docs")) for p in (repo / "docs").rglob("*.md"))


FIXTURE_PAGES = ["glossary.md", "guide/index.md", "guide/setup.md", "index.md"]
WIKI_PAGES = {"home.md", "guide.md", "guide/setup.md", "glossary.md", "_sidebar.md", "uploads/a.png"}


# --- W: wipe --------------------------------------------------------------------------------

def test_w_empty_wiki_first_run_reseeds(tmp_path, estate):
    """W: a brand-new wiki bare (0 commits). Expected: exit 0, docs/ untouched, no repo commit,
    the wiki's first commit is a CI sync holding every page; the next run changes nothing."""
    repo, _, repo_bare, _ = estate
    fresh = tmp_path / "fresh-wiki.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(fresh))
    repo_before = head(repo_bare)
    ok(deploy(repo, fresh))
    assert head(repo_bare) == repo_before
    assert docs_pages(repo) == FIXTURE_PAGES
    assert commits(fresh, fmt="%an") == [CI]
    assert tree(fresh) == WIKI_PAGES
    ok(deploy(repo, fresh))
    assert commits(fresh, fmt="%an") == [CI] and head(repo_bare) == repo_before


def test_w_all_pages_deleted_in_wiki_is_a_reset_not_a_mass_delete(estate):
    """W: a human deletes every .md in the wiki after the last sync. Expected: nothing pulled,
    docs/ keeps all pages, no repo commit, the wiki is reseeded by a CI commit on top of the
    human's commit; the next run is a no-op."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {}, "delete everything", delete=["home.md", "guide.md", "guide/setup.md", "glossary.md", "_sidebar.md"])
    assert not any(p.endswith(".md") for p in tree(wiki_bare))
    repo_before = head(repo_bare)
    ok(deploy(repo, wiki_bare))
    assert head(repo_bare) == repo_before, "docs/ was deleted and pushed"
    assert docs_pages(repo) == FIXTURE_PAGES
    assert commits(wiki_bare, fmt="%an")[:2] == [CI, ALICE[0]]
    assert tree(wiki_bare) == WIKI_PAGES
    ok(deploy(repo, wiki_bare))
    assert commits(wiki_bare, fmt="%an")[:2] == [CI, ALICE[0]] and head(repo_bare) == repo_before


def test_w_force_pushed_empty_branch_is_a_reset(tmp_path, estate):
    """W: someone force-pushes an orphan commit with an empty tree over the wiki branch. Expected:
    same as any wipe: nothing pulled, docs/ intact, wiki reseeded with a CI commit."""
    repo, _, repo_bare, wiki_bare = estate
    other = clone(wiki_bare, tmp_path / "wiki-other")
    git(other, "checkout", "-q", "--orphan", "blank")
    git(other, "rm", "-rfq", ".")
    git(other, "commit", "-q", "--allow-empty", "-m", "blank slate", author=BOB, date=T2)
    git(other, "push", "-q", "--force", "origin", "HEAD:main")
    assert tree(wiki_bare) == set() and len(commits(wiki_bare)) == 1
    repo_before = head(repo_bare)
    ok(deploy(repo, wiki_bare))
    assert head(repo_bare) == repo_before
    assert docs_pages(repo) == FIXTURE_PAGES
    assert commits(wiki_bare, fmt="%an") == [CI, BOB[0]]
    assert tree(wiki_bare) == WIKI_PAGES


# --- D: delete ------------------------------------------------------------------------------

def test_d_page_deleted_in_wiki_is_deleted_from_docs_by_wiki_author(estate):
    """D: one page deleted in the wiki. Expected: docs/glossary.md removed by a repo commit
    authored by the wiki editor, pushed to origin; the page stays absent from the wiki."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {}, "drop glossary", delete=["glossary.md"])
    ok(deploy(repo, wiki_bare))
    assert commits(repo_bare, fmt="%an %s")[0] == f"{ALICE[0]} wiki: drop glossary"
    assert "docs/glossary.md" not in tree(repo_bare)
    assert not (repo / "docs/glossary.md").exists()
    assert "glossary.md" not in tree(wiki_bare) and commits(wiki_bare, fmt="%an")[0] == CI


def test_d_page_deleted_in_wiki_but_edited_in_repo_comes_back(estate):
    """D: the repo edited glossary.md after the last sync, then a wiki user deleted it. Expected:
    docs/ keeps the repo version, no deletion commit, and the wiki gets the page back."""
    repo, wiki, repo_bare, wiki_bare = estate
    write(repo, {"docs/glossary.md": "# Glossary\n\nterms, edited in repo\n"})
    commit_all(repo, "repo edit", DEV, T1)
    git(repo, "push", "-q", "origin", "HEAD:main")
    repo_before = head(repo_bare)
    wiki_edit(wiki, {}, "drop glossary", delete=["glossary.md"], date=T2)
    ok(deploy(repo, wiki_bare))
    assert head(repo_bare) == repo_before
    assert (repo / "docs/glossary.md").read_text() == "# Glossary\n\nterms, edited in repo\n"
    assert blob(wiki_bare, "glossary.md") == "# Glossary\n\nterms, edited in repo\n"


def test_d_page_deleted_in_repo_since_sync_is_removed_from_wiki_not_resurrected(estate):
    """Guard for the never-synced rule below: a page the repo deleted after the last sync must be
    removed from the wiki, not pulled back as new."""
    repo, _, repo_bare, wiki_bare = estate
    (repo / "docs/glossary.md").unlink()
    write(repo, {"docs/index.md": "# Home\n\n[Guide](guide/index.md)\n"})
    commit_all(repo, "drop glossary from docs", DEV, T1)
    git(repo, "push", "-q", "origin", "HEAD:main")
    repo_before = head(repo_bare)
    ok(deploy(repo, wiki_bare))
    assert head(repo_bare) == repo_before
    assert not (repo / "docs/glossary.md").exists()
    assert "glossary.md" not in tree(wiki_bare)


# --- R: races -------------------------------------------------------------------------------

def test_r1_wiki_push_rejected_by_late_human_commit_is_retried(tmp_path, estate):
    """R1: Alice edits before the run; Bob's commit lands on the wiki after the script cloned it
    and before its sync push (injected by the pre-receive hook, which then rejects the push).
    Expected: exit 0; both edits are in docs/ (two repo commits, Alice then Bob); the wiki tip
    is a CI commit whose tree carries both edits; the hook fired exactly once."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"guide/setup.md": "# Setup\n\nline one by alice\nline two\nline three\n"}, "alice setup")
    late = clone(wiki_bare, tmp_path / "wiki-bob")
    write(late, {"glossary.md": "# Glossary\n\nterms by bob\n"})
    commit_all(late, "bob glossary", BOB, T2)  # not pushed: the hook pushes it mid-run
    marker = install_race_hook(wiki_bare, late)
    ok(deploy(repo, wiki_bare))
    assert marker.exists(), "the race never fired"
    assert commits(repo_bare, fmt="%an %s")[:2] == [f"{BOB[0]} wiki: bob glossary", f"{ALICE[0]} wiki: alice setup"]
    assert blob(repo_bare, "docs/glossary.md") == "# Glossary\n\nterms by bob\n"
    assert blob(repo_bare, "docs/guide/setup.md").startswith("# Setup\n\nline one by alice\n")
    assert commits(wiki_bare, fmt="%an")[:3] == [CI, BOB[0], ALICE[0]]
    assert blob(wiki_bare, "glossary.md") == "# Glossary\n\nterms by bob\n"
    assert blob(wiki_bare, "guide/setup.md").startswith("# Setup\n\nline one by alice\n")
    assert git(repo, "status", "--porcelain").strip() == ""


def test_r2_repo_push_rejected_by_concurrent_main_commit_is_rebased(tmp_path, estate):
    """R2 (at push time): Dev pushes docs/other.md to main while the script runs (injected by the
    pre-receive hook on repo.git at the script's push, which is then rejected). Expected: exit 0;
    main holds Dev's commit and Alice's wiki commit; the wiki holds both pages."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nterms by alice\n"}, "alice glossary")
    dev = clone(repo_bare, tmp_path / "repo-dev")
    write(dev, {"docs/other.md": "# Other\n\nby dev\n"})
    commit_all(dev, "add other page", DEV, T2)  # not pushed: the hook pushes it mid-run
    marker = install_race_hook(repo_bare, dev)
    ok(deploy(repo, wiki_bare))
    assert marker.exists(), "the race never fired"
    subjects = commits(repo_bare, fmt="%s")
    assert "add other page" in subjects and "wiki: alice glossary" in subjects
    assert blob(repo_bare, "docs/other.md") == "# Other\n\nby dev\n"
    assert blob(repo_bare, "docs/glossary.md") == "# Glossary\n\nterms by alice\n"
    assert commits(wiki_bare, fmt="%an")[0] == CI
    assert blob(wiki_bare, "other.md") == "# Other\n\nby dev\n"
    assert blob(wiki_bare, "glossary.md") == "# Glossary\n\nterms by alice\n"
    assert git(repo, "status", "--porcelain").strip() == ""


@pytest.mark.parametrize("detached", [False, True], ids=["on-branch", "detached-HEAD"])
def test_r2_repo_main_moved_before_the_run_is_rebased(tmp_path, estate, detached):
    """R2 (stale checkout): main already moved on origin when the script starts (the checkout is
    behind, as a CI job's is). Expected: fetch + rebase + push; both commits on main, wiki holds
    both pages. Run once on a branch and once with the checkout detached, as CI checks out."""
    repo, wiki, repo_bare, wiki_bare = estate
    dev = clone(repo_bare, tmp_path / "repo-dev")
    write(dev, {"docs/other.md": "# Other\n\nby dev\n"})
    commit_all(dev, "add other page", DEV, T2)
    git(dev, "push", "-q", "origin", "HEAD:main")
    if detached:
        git(repo, "checkout", "-q", "--detach")
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nterms by alice\n"}, "alice glossary")
    ok(deploy(repo, wiki_bare))
    subjects = commits(repo_bare, fmt="%s")
    assert "add other page" in subjects and "wiki: alice glossary" in subjects
    assert blob(repo_bare, "docs/other.md") == "# Other\n\nby dev\n"
    assert blob(repo_bare, "docs/glossary.md") == "# Glossary\n\nterms by alice\n"
    assert blob(wiki_bare, "other.md") == "# Other\n\nby dev\n"
    assert blob(wiki_bare, "glossary.md") == "# Glossary\n\nterms by alice\n"


def test_r3_wiki_push_that_keeps_failing_exits_nonzero_and_leaves_wiki_untouched(estate):
    """R3: every wiki push is refused. Expected: exit non-zero, an error line on stderr, wiki tip
    unchanged (Alice's edit still there). Once the hook is gone the next run
    succeeds and Alice's edit lands exactly once in the repo."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nterms by alice\n"}, "alice glossary")
    wiki_before = head(wiki_bare)
    marker = install_race_hook(wiki_bare, None, reject_always=True)
    result = deploy(repo, wiki_bare)
    assert result.returncode != 0
    assert result.stderr.strip(), "no message explaining the failure"
    assert head(wiki_bare) == wiki_before
    (Path(wiki_bare) / "hooks" / "pre-receive").unlink()
    marker.unlink(missing_ok=True)
    ok(deploy(repo, wiki_bare))
    assert commits(repo_bare, fmt="%an %s").count(f"{ALICE[0]} wiki: alice glossary") == 1
    assert commits(wiki_bare, fmt="%an")[:2] == [CI, ALICE[0]]
    assert blob(wiki_bare, "glossary.md") == "# Glossary\n\nterms by alice\n"


# --- A: attribution -------------------------------------------------------------------------

def test_a_each_wiki_commit_maps_to_one_repo_commit_with_wiki_author_and_date(estate):
    """A: two wiki commits by two people. Expected: two repo commits, oldest first, each with the
    wiki commit's author name, email and author date; committer is the repo's configured
    identity ("CI <ci@example.com>" in the fixture)."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nby alice\n"}, "alice glossary", ALICE, T1)
    wiki_edit(wiki, {"guide/setup.md": "# Setup\n\nby bob\n"}, "bob setup", BOB, T2)
    ok(deploy(repo, wiki_bare))
    got = commits(repo_bare, fmt="%an|%ae|%at|%cn|%ce|%s")[:2]
    assert got == [f"{BOB[0]}|{BOB[1]}|{ts(T2)}|CI|ci@example.com|wiki: bob setup",
                   f"{ALICE[0]}|{ALICE[1]}|{ts(T1)}|CI|ci@example.com|wiki: alice glossary"]
    assert len(commits(repo_bare)) == 3


def test_a_committer_is_ci_identity_when_repo_has_none(estate):
    """A: CI checkouts carry no user.name/email. Expected: the script sets the CI identity, so the
    committer is "<project> ci <ci@host>" while the author stays the wiki editor."""
    repo, wiki, repo_bare, wiki_bare = estate
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nby alice\n"}, "alice glossary")
    ok(deploy(repo, wiki_bare, CI_SERVER_FQDN="gitlab.example.com"))
    assert commits(repo_bare, fmt="%an|%cn|%ce")[0] == f"{ALICE[0]}|{CI}|ci@gitlab.example.com"
    assert commits(wiki_bare, fmt="%an|%ae")[0] == f"{CI}|ci@gitlab.example.com"


def test_a_human_with_ci_name_but_other_email_is_still_a_human(estate):
    """A: a wiki user whose display name equals the CI author name but with a different email.
    Expected: treated as a human edit: pulled into docs/ with that name and email, not mistaken
    for the last sync marker (which would drop the edit and overwrite it in the wiki)."""
    repo, wiki, repo_bare, wiki_bare = estate
    impostor = (CI, "human@example.com")
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nby the human named ci\n"}, "ci-named human", impostor, T1)
    ok(deploy(repo, wiki_bare))
    assert blob(repo_bare, "docs/glossary.md") == "# Glossary\n\nby the human named ci\n"
    assert commits(repo_bare, fmt="%an|%ae|%s")[0] == f"{CI}|human@example.com|wiki: ci-named human"
    assert blob(wiki_bare, "glossary.md") == "# Glossary\n\nby the human named ci\n"


def test_a_author_with_empty_email_is_pulled(estate):
    """A: a wiki commit whose author email is empty (git allows "Name <>"). Expected: the edit lands
    in docs/ in a commit with that author name and date; the run does not crash."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nby nobody\n"}, "anonymous edit", ("Nobody", ""), T1)
    assert commits(wiki_bare, fmt="%an|%ae")[0] == "Nobody|"
    ok(deploy(repo, wiki_bare))
    assert blob(repo_bare, "docs/glossary.md") == "# Glossary\n\nby nobody\n"
    assert commits(repo_bare, fmt="%an|%at|%s")[0] == f"Nobody|{ts(T1)}|wiki: anonymous edit"


# --- I: idempotence and no-op states --------------------------------------------------------

def test_i_two_runs_with_no_changes_create_no_commits(estate):
    """I: nothing changed on either side. Expected: two consecutive runs exit 0 and neither bare
    gains a commit."""
    repo, _, repo_bare, wiki_bare = estate
    before = snapshot(repo_bare, wiki_bare)
    ok(deploy(repo, wiki_bare))
    ok(deploy(repo, wiki_bare))
    assert snapshot(repo_bare, wiki_bare) == before


def test_i_run_after_a_pull_run_is_a_noop(estate):
    """I: after a run that pulled an edit, a second run has nothing to do."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nby alice\n"}, "alice glossary")
    ok(deploy(repo, wiki_bare))
    after_first = snapshot(repo_bare, wiki_bare)
    ok(deploy(repo, wiki_bare))
    assert snapshot(repo_bare, wiki_bare) == after_first


def test_sidebar_only_wiki_commit_makes_no_repo_commit(estate):
    """A wiki commit that only touches _sidebar.md is furniture. Expected: no repo commit; the wiki
    gets one CI commit restoring the generated sidebar; a further run changes nothing."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"_sidebar.md": "hand edited\n"}, "sidebar by hand")
    repo_before = head(repo_bare)
    ok(deploy(repo, wiki_bare))
    assert head(repo_bare) == repo_before
    assert commits(wiki_bare, fmt="%an")[:2] == [CI, ALICE[0]]
    assert blob(wiki_bare, "_sidebar.md") != "hand edited\n"
    after = snapshot(repo_bare, wiki_bare)
    ok(deploy(repo, wiki_bare))
    assert snapshot(repo_bare, wiki_bare) == after


# --- other git edges ------------------------------------------------------------------------

def test_wiki_commit_touching_150_files_becomes_one_repo_commit(estate):
    """One wiki commit adding 150 pages. Expected: one repo commit with 150 files, all present in
    docs/ and mirrored back into the wiki."""
    repo, wiki, repo_bare, wiki_bare = estate
    pages = {f"bulk/page-{i:03d}.md": f"# Page {i}\n\nbody {i}\n" for i in range(150)}
    wiki_edit(wiki, pages, "bulk import")
    ok(deploy(repo, wiki_bare))
    top = commits(repo_bare)[0]
    assert commits(repo_bare, fmt="%an %s")[0] == f"{ALICE[0]} wiki: bulk import"
    assert len(files_in(repo_bare, top)) == 150
    assert (repo / "docs/bulk/page-149.md").read_text() == "# Page 149\n\nbody 149\n"
    assert {p for p in tree(wiki_bare) if p.startswith("bulk/")} == set(pages)


def test_detached_head_checkout_pushes_pulled_edits_to_main(estate):
    """CI checks out a detached HEAD. Expected: the pulled commit is pushed to main all the same."""
    repo, wiki, repo_bare, wiki_bare = estate
    git(repo, "checkout", "-q", "--detach")
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nby alice\n"}, "alice glossary")
    ok(deploy(repo, wiki_bare))
    assert commits(repo_bare, fmt="%an %s")[0] == f"{ALICE[0]} wiki: alice glossary"
    assert blob(repo_bare, "docs/glossary.md") == "# Glossary\n\nby alice\n"


@pytest.mark.xfail(strict=True, reason="out of scope: needs a CI sync commit that left a foreign page in place, "
                   "which wiki-sync.py never writes (every sync clears pages not in docs/); adopting an "
                   "existing wiki is the wiki-import.py path, and a wiki with no CI commit yet pulls every page")
def test_page_the_wiki_always_had_but_docs_never_did_is_pulled_as_new(estate):
    """A page present in the wiki since before the last CI sync but never in docs/ (the CI sync
    commit left it in place). Expected: pulled into docs/ as a new page and kept in the wiki,
    not silently deleted by the mirror step."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"legacy.md": "# Legacy\n\nolder than the sync\n"}, "legacy page", ALICE, T1)
    commit_all(wiki, "Sync docs from 0000000", (CI, "ci@example.com"), T2)  # a sync that kept it
    git(wiki, "push", "-q", "origin", "HEAD:main")
    assert "docs/legacy.md" not in tree(repo_bare) and "legacy.md" in tree(wiki_bare)
    ok(deploy(repo, wiki_bare))
    assert blob(repo_bare, "docs/legacy.md") == "# Legacy\n\nolder than the sync\n"
    assert blob(wiki_bare, "legacy.md") == "# Legacy\n\nolder than the sync\n"


# --- dry-run in every state -----------------------------------------------------------------

def _state_clean(tmp_path, repo, wiki, wiki_bare):
    return wiki_bare


def _state_edit_pending(tmp_path, repo, wiki, wiki_bare):
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nby alice\n"}, "alice glossary")
    write(repo, {"docs/new.md": "# New\n"})
    commit_all(repo, "new page", DEV, T2)
    git(repo, "push", "-q", "origin", "HEAD:main")
    return wiki_bare


def _state_wiped(tmp_path, repo, wiki, wiki_bare):
    wiki_edit(wiki, {}, "delete everything", delete=["home.md", "guide.md", "guide/setup.md", "glossary.md", "_sidebar.md"])
    return wiki_bare


def _state_empty_wiki(tmp_path, repo, wiki, wiki_bare):
    fresh = tmp_path / "fresh-wiki.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(fresh))
    return fresh


@pytest.mark.parametrize("state", [_state_clean, _state_edit_pending, _state_wiped, _state_empty_wiki],
                         ids=["clean", "edit-pending", "wiped", "empty-wiki"])
def test_dry_run_commits_nothing_in_any_state(tmp_path, estate, state):
    """DRY_RUN=1 in each wiki state. Expected: exit 0, no commit on either bare, docs/ unchanged
    on disk and the checkout clean."""
    repo, wiki, repo_bare, wiki_bare = estate
    target = state(tmp_path, repo, wiki, wiki_bare)
    before = snapshot(repo_bare, target)
    pages_before = docs_pages(repo)
    result = ok(deploy(repo, target, DRY_RUN="1"))
    assert snapshot(repo_bare, target) == before, result.stdout
    assert docs_pages(repo) == pages_before
    assert git(repo, "status", "--porcelain").strip() == ""
