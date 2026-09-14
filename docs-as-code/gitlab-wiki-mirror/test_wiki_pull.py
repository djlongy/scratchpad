"""Tests for wiki-pull.py and wiki-deploy.sh against local git repos. Run: python3 -m pytest -q"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
CI = "docs ci"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


wiki_pull = load("wiki-pull")
wiki_sync = load("wiki-sync")


def git(repo, *args, author=None, date=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    if author:
        name, email = author
        env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def commit_all(repo, msg, author, date=None):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg, author=author, date=date)


def write(root, files):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content) if isinstance(content, bytes) else p.write_text(content)


DEV = ("Dev", "dev@example.com")
ALICE = ("Alice Editor", "alice@example.com")
T0, T1, T2, T3 = ("2026-01-01T10:00:00+00:00", "2026-01-02T10:00:00+00:00",
                  "2026-01-03T10:00:00+00:00", "2026-01-04T10:00:00+00:00")


@pytest.fixture
def estate(tmp_path):
    """A docs repo with an origin, and a wiki that CI has synced once."""
    repo_bare, wiki_bare = tmp_path / "repo.git", tmp_path / "wiki.git"
    for bare in (repo_bare, wiki_bare):
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
        git(bare, "config", "receive.advertisePushOptions", "true")
    repo = tmp_path / "repo"
    git(tmp_path, "clone", "-q", str(repo_bare), str(repo))
    # committer identity, as wiki-deploy.sh sets it before calling wiki-pull.py (CI has none)
    git(repo, "config", "user.name", "CI")
    git(repo, "config", "user.email", "ci@example.com")
    write(repo, {
        "docs/index.md": "# Home\n\n[Guide](guide/index.md) [Glossary](glossary.md)\n",
        "docs/guide/index.md": "# Guide\n\n[Setup](setup.md)\n",
        "docs/guide/setup.md": "# Setup\n\nline one\nline two\nline three\n",
        "docs/glossary.md": "# Glossary\n\nterms\n",
        "docs/uploads/a.png": b"\x89PNG",
        "mkdocs.yml": "site_name: t\ndocs_dir: docs\n",
    })
    commit_all(repo, "docs", DEV, T0)
    git(repo, "push", "-q", "origin", "HEAD:main")
    wiki = tmp_path / "wiki"
    git(tmp_path, "clone", "-q", str(wiki_bare), str(wiki))
    wiki_sync.main(repo / "docs", wiki)
    commit_all(wiki, "Sync docs from abc1234", (CI, "ci@example.com"), T0)
    git(wiki, "push", "-q", "origin", "HEAD:main")
    return repo, wiki, repo_bare, wiki_bare


def wiki_edit(wiki, files, msg, author=ALICE, date=T1, delete=()):
    """What a person does in the wiki UI: one commit on the wiki repo."""
    write(wiki, files)
    for rel in delete:
        (wiki / rel).unlink()
    commit_all(wiki, msg, author, date)
    git(wiki, "push", "-q", "origin", "HEAD:main")


def pull(repo, wiki, **kw):
    return wiki_pull.main(repo / "docs", wiki, **kw)


def test_pull_edit_lands_in_docs_with_wiki_author(estate, capsys):
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"guide/setup.md": "# Setup\n\nline one\nline two\nline three\nline four by alice\n"}, "Update Setup")
    assert pull(repo, wiki) == 0
    assert (repo / "docs/guide/setup.md").read_text().endswith("line four by alice\n")
    assert git(repo, "log", "-1", "--format=%an <%ae>").strip() == "Alice Editor <alice@example.com>"
    assert git(repo, "log", "-1", "--format=%s").strip() == "wiki: Update Setup"
    assert "fast-forward" in capsys.readouterr().out


def test_pull_rewrites_links_and_section_pages(estate):
    repo, wiki, *_ = estate
    # In the wiki the section page is guide.md (root page with a guide/ folder); links are root-absolute.
    wiki_edit(wiki, {"guide.md": "# Guide\n\n[Setup](/guide/setup) [Home](/home) ![a](uploads/a.png)\n"}, "Guide links")
    pull(repo, wiki)
    text = (repo / "docs/guide/index.md").read_text()
    assert "[Setup](setup.md)" in text and "[Home](../index.md)" in text and "(../uploads/a.png)" in text


def test_pull_merges_non_overlapping_repo_edit(estate):
    repo, wiki, *_ = estate
    write(repo, {"docs/guide/setup.md": "# Setup\n\nrepo intro\n\nline one\nline two\nline three\n"})
    commit_all(repo, "repo edit", DEV, T1)
    wiki_edit(wiki, {"guide/setup.md": "# Setup\n\nline one\nline two\nline three\nwiki outro\n"}, "wiki edit", date=T2)
    pull(repo, wiki)
    text = (repo / "docs/guide/setup.md").read_text()
    assert "repo intro" in text and "wiki outro" in text and "<<<<" not in text


@pytest.mark.parametrize("repo_date,wiki_date,winner", [(T1, T2, "wiki"), (T3, T2, "repo")])
def test_pull_conflict_newer_edit_wins(estate, repo_date, wiki_date, winner, capsys):
    repo, wiki, *_ = estate
    write(repo, {"docs/guide/setup.md": "# Setup\n\nline one\nline two by repo\nline three\n"})
    commit_all(repo, "repo edit", DEV, repo_date)
    wiki_edit(wiki, {"guide/setup.md": "# Setup\n\nline one\nline two by wiki\nline three\n"}, "wiki edit", date=wiki_date)
    pull(repo, wiki)
    text = (repo / "docs/guide/setup.md").read_text()
    assert f"line two by {winner}" in text and "<<<<" not in text
    assert ("wiki wins" if winner == "wiki" else "repo wins") in capsys.readouterr().out


def test_pull_delete_new_page_and_attachment(estate):
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"guide/faq.md": "# FAQ\n\nq\n", "uploads/b.png": b"\x89PNGb"}, "add faq", delete=["glossary.md"])
    pull(repo, wiki)
    assert not (repo / "docs/glossary.md").exists()
    assert (repo / "docs/guide/faq.md").read_text() == "# FAQ\n\nq\n"
    assert (repo / "docs/uploads/b.png").read_bytes() == b"\x89PNGb"
    assert git(repo, "status", "--porcelain").strip() == ""  # everything committed


def test_pull_keeps_repo_edited_page_deleted_in_wiki(estate, capsys):
    repo, wiki, *_ = estate
    write(repo, {"docs/glossary.md": "# Glossary\n\nterms, edited in repo\n"})
    commit_all(repo, "repo edit", DEV, T1)
    wiki_edit(wiki, {}, "drop glossary", delete=["glossary.md"], date=T2)
    pull(repo, wiki)
    assert (repo / "docs/glossary.md").exists()
    assert "keep" in capsys.readouterr().out


def test_pull_page_created_then_deleted_in_the_same_batch(estate):
    """Two wiki commits since the sync: create t/x, then delete it. The pull's own commit for the
    create must not count as a repo edit that protects the page from the delete."""
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"t/x.md": "# x\n"}, "create x", date=T1)
    wiki_edit(wiki, {}, "delete x", delete=["t/x.md"], date=T2)
    pull(repo, wiki)
    assert not (repo / "docs/t/x.md").exists()
    assert git(repo, "log", "-1", "--format=%s").strip() == "wiki: delete x"


def test_pull_ignores_sidebar_and_is_idempotent(estate, capsys):
    repo, wiki, *_ = estate
    # _sidebar.md is regenerated by wiki-sync; .gitlab/redirects.yml is written by GitLab itself.
    wiki_edit(wiki, {"_sidebar.md": "hand edited\n", ".gitlab/redirects.yml": "old: new\n"}, "furniture")
    head = git(repo, "rev-parse", "HEAD")
    pull(repo, wiki)
    assert git(repo, "rev-parse", "HEAD") == head
    assert not (repo / "docs/.gitlab").exists()
    # After CI syncs again, the same wiki commits are not pulled twice.
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nmore\n"}, "glossary")
    pull(repo, wiki)
    commit_all(wiki, "Sync docs from def5678", (CI, "ci@example.com"), T3)
    head = git(repo, "rev-parse", "HEAD")
    pull(repo, wiki)
    assert git(repo, "rev-parse", "HEAD") == head
    assert "no edits since the last sync" in capsys.readouterr().out


def test_pull_dry_run_changes_nothing(estate, capsys):
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nchanged\n"}, "glossary")
    head = git(repo, "rev-parse", "HEAD")
    pull(repo, wiki, dry_run=True)
    assert git(repo, "rev-parse", "HEAD") == head
    assert "terms" in (repo / "docs/glossary.md").read_text()
    assert "dry-run: would commit" in capsys.readouterr().out


def test_deploy_round_trip(estate):
    """wiki-deploy.sh: wiki edit -> repo commit pushed to origin (as Alice) -> wiki resynced by CI."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"guide/setup.md": "# Setup\n\nedited in the wiki\n"}, "Setup from wiki")
    env = dict(os.environ, WIKI_URL=str(wiki_bare), REPO_PUSH_URL="origin", CI_PROJECT_PATH="group/docs", CI_DEFAULT_BRANCH="main")
    env.pop("CI_JOB_TOKEN", None)
    out = subprocess.run(["bash", str(HERE / "wiki-deploy.sh"), "docs", "wiki-stage"], cwd=repo, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr + out.stdout
    assert git(repo_bare, "log", "-1", "--format=%an %s", "main").strip() == "Alice Editor wiki: Setup from wiki"
    assert git(wiki_bare, "log", "-1", "--format=%an", "main").strip() == CI
    assert git(wiki_bare, "show", "main:guide/setup.md") == "# Setup\n\nedited in the wiki\n"
    # second run: nothing to pull, wiki already current
    out = subprocess.run(["bash", str(HERE / "wiki-deploy.sh"), "docs", "wiki-stage"], cwd=repo, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0 and "wiki already current" in out.stdout and "no edits since" in out.stdout


def test_deploy_refuses_or_removes_a_nested_docs_repo(estate):
    """A stray docs/.git (seen on reused CI build dirs) would hijack every git call."""
    repo, wiki, repo_bare, wiki_bare = estate
    git(repo / "docs", "init", "-q")
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nvia ci\n"}, "g")
    base = dict(os.environ, WIKI_URL=str(wiki_bare), REPO_PUSH_URL="origin", CI_PROJECT_PATH="g/p")
    for k in ("CI", "CI_JOB_TOKEN"):  # under GitLab CI these would point the push at a real host
        base.pop(k, None)
    out = subprocess.run(["bash", str(HERE / "wiki-deploy.sh"), "docs", "wiki-stage"], cwd=repo, env=base,
                         capture_output=True, text=True)
    assert out.returncode == 1 and "docs/.git" in out.stderr
    out = subprocess.run(["bash", str(HERE / "wiki-deploy.sh"), "docs", "wiki-stage"], cwd=repo, env=dict(base, CI="true"),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "removed the nested repository" in out.stderr and not (repo / "docs/.git").exists()
    assert git(repo_bare, "log", "-1", "--format=%an", "main").strip() == "Alice Editor"


def test_deploy_dry_run_pushes_nothing(estate):
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nx\n"}, "g")
    write(repo, {"docs/new.md": "# New\n"})
    commit_all(repo, "new page", DEV, T2)
    env = dict(os.environ, WIKI_URL=str(wiki_bare), REPO_PUSH_URL="origin", CI_PROJECT_PATH="g/p", DRY_RUN="1")
    out = subprocess.run(["bash", str(HERE / "wiki-deploy.sh"), "docs", "wiki-stage"], cwd=repo, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "dry-run" in out.stdout and "new.md" in out.stdout
    assert git(wiki_bare, "log", "-1", "--format=%an", "main").strip() == "Alice Editor"
    assert git(repo_bare, "rev-list", "--count", "main").strip() == "1"
