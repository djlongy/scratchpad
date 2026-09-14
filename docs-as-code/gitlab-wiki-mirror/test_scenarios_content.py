"""Scenario tests for the two-way wiki sync: content, links, attachments, structure.

Covers wiki-sync.py (docs -> wiki), wiki-pull.py / wiki-import.py (wiki -> docs) and
wiki-deploy.sh, against local git repos and temp dirs only. Written against the link spec:

- wiki links are relative to the page's own wiki directory and keep .md on pages; a bare
  extension-less sibling link is never emitted (GitLab resolves that from the wiki root);
- the reverse resolves every form a person types in the GitLab UI back to relative .md links;
- docs -> wiki -> docs is byte-identical for pages already in canonical relative form.

Run: python3 -m pytest -q scripts/test_scenarios_content.py
"""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
CI = "docs ci"
DEV = ("Dev", "dev@example.com")
ALICE = ("Alice Editor", "alice@example.com")
T0, T1, T2, T3 = ("2026-01-01T10:00:00+00:00", "2026-01-02T10:00:00+00:00",
                  "2026-01-03T10:00:00+00:00", "2026-01-04T10:00:00+00:00")
PNG, PNG2 = b"\x89PNG\r\n\x1a\n", b"\x89PNG\r\n\x1a\nv2"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


wiki_sync = load("wiki-sync")
wiki_pull = load("wiki-pull")
wiki_import = load("wiki-import")
docfilter = load("docfilter")


# ------------------------------------------------------------------ helpers


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


def remove(root, *rels):
    for rel in rels:
        (root / rel).unlink()


def tree(root):
    """Sorted relative paths of every file under root, minus git metadata."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)


def snapshot(root):
    return {rel: (root / rel).read_bytes() for rel in tree(root)}


def links(md):
    return re.findall(r"\]\(([^)]+)\)", md.read_text())


def sync(docs, wiki, flt=None):
    wiki.mkdir(exist_ok=True)
    wiki_sync.main(docs, wiki, flt)


def pull(repo, wiki, **kw):
    return wiki_pull.main(repo / "docs", wiki, **kw)


def wiki_edit(wiki, files, msg, author=ALICE, date=T1, delete=()):
    """What a person does in the wiki UI: one commit on the wiki repo, pushed."""
    write(wiki, files)
    remove(wiki, *delete)
    commit_all(wiki, msg, author, date)
    git(wiki, "push", "-q", "origin", "HEAD:main")


def repo_edit(repo, files, msg, date=T1, delete=()):
    write(repo, files)
    remove(repo, *delete)
    commit_all(repo, msg, DEV, date)
    git(repo, "push", "-q", "origin", "HEAD:main")


def ci_sync(repo, wiki, date):
    """What wiki-deploy.sh step 4 does: regenerate the wiki and commit as the CI author."""
    wiki_sync.main(repo / "docs", wiki)
    commit_all(wiki, "Sync docs from abc1234", (CI, "ci@example.com"), date)
    git(wiki, "push", "-q", "origin", "HEAD:main")


def deploy(repo, wiki_bare, **extra):
    env = dict(os.environ, WIKI_URL=str(wiki_bare), REPO_PUSH_URL="origin", CI_PROJECT_PATH="group/docs",
               CI_DEFAULT_BRANCH="main", **extra)
    for k in ("CI", "CI_JOB_TOKEN"):
        env.pop(k, None)
    return subprocess.run(["bash", str(HERE / "wiki-deploy.sh"), "docs", "wiki-stage"], cwd=repo, env=env,
                          capture_output=True, text=True)


def bare_files(bare):
    """Files on the single branch of a bare repo (empty when it has no commits)."""
    refs = git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    return sorted(git(bare, "ls-tree", "-r", "--name-only", refs[0]).split()) if refs else []


def wiki_link_problems(wiki):
    """Links in wiki pages that do not resolve from the page's own directory, or that reach a
    page without the .md extension. Links outside the wiki, external, mailto and root-absolute
    ones are left to GitLab. Empty list means every link is in the canonical relative form."""
    pages = (p for p in wiki.rglob("*.md") if not p.name.startswith("_") and ".git" not in p.parts)
    return [f"{page.relative_to(wiki)}: {target} ({why})"
            for page in pages for target in links(page)
            if (why := link_problem(wiki, page, target))]


def link_problem(wiki, page, target):
    """'dangling', 'bare page link' or None for one link target as seen from its page."""
    if "://" in target or target.startswith(("mailto:", "/", "#")):
        return None
    resolved = (page.parent / target.split("#")[0]).resolve()
    if not resolved.is_relative_to(wiki.resolve()):
        return None
    if not resolved.exists():
        return "dangling"
    if resolved.suffix != ".md" and resolved.with_suffix(".md").exists():
        return "bare page link"
    return None


# ------------------------------------------------------------------ fixtures

DOCS = {
    "index.md": "# Home\n\n[Ops](ops/index.md) [Backups](ops/backups.md) ![n](uploads/1/n.png) [Dev](dev/index.md)\n",
    ".pages": "nav:\n  - index.md\n  - Ops: ops\n  - ...\n",
    "ops/index.md": (
        "# Ops\n\n[Release](../dev/release.md) [Restore](restore.md) ![n](../uploads/1/n.png) "
        "[Home](../index.md) [Dev](../dev/index.md) [Me](index.md)\n"
    ),
    "ops/backups.md": (
        "# Backups\n\n[x](../dev/release.md) [y](restore.md#steps) ![n](../uploads/1/n.png) [ops](index.md) "
        "[home](../index.md) [ext](https://example.com/a) [mail](mailto:a@b.c) [abs](/abs/path) [out](../../outside.md)\n"
    ),
    "ops/restore.md": "# Restore\n\n## Steps\n\n[back](backups.md#top)\n",
    "dev/index.md": "# Dev\n\n[Release](release.md)\n",
    "dev/release.md": "# Release\n\n[Backups](../ops/backups.md)\n",
    "glossary.md": "# Glossary\n\nterms\n",
    "uploads/1/n.png": PNG,
}

WIKI_LINKS = {  # the spec: what each docs page's links become in its wiki location
    "home.md": ["ops.md", "ops/backups.md", "uploads/1/n.png", "dev.md"],
    "ops.md": ["dev/release.md", "ops/restore.md", "uploads/1/n.png", "home.md", "dev.md", "ops.md"],
    "ops/backups.md": ["../dev/release.md", "restore.md#steps", "../uploads/1/n.png", "../ops.md", "../home.md",
                       "https://example.com/a", "mailto:a@b.c", "/abs/path", "../../outside.md"],
    "ops/restore.md": ["backups.md#top"],
    "dev.md": ["dev/release.md"],
    "dev/release.md": ["../ops/backups.md"],
}


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "docs"
    write(root, DOCS)
    (tmp_path / "outside.md").write_text("# outside\n")
    return root


@pytest.fixture
def estate(tmp_path):
    """A docs repo with an origin, and a wiki that CI has synced once at T0."""
    repo_bare, wiki_bare = tmp_path / "repo.git", tmp_path / "wiki.git"
    for bare in (repo_bare, wiki_bare):
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
        git(bare, "config", "receive.advertisePushOptions", "true")  # wiki-deploy.sh pushes -o ci.skip
    repo = tmp_path / "repo"
    git(tmp_path, "clone", "-q", str(repo_bare), str(repo))
    git(repo, "config", "user.name", "CI")
    git(repo, "config", "user.email", "ci@example.com")
    write(repo, {f"docs/{rel}": content for rel, content in DOCS.items()})
    write(repo, {"mkdocs.yml": "site_name: t\ndocs_dir: docs\n"})
    commit_all(repo, "docs", DEV, T0)
    git(repo, "push", "-q", "origin", "HEAD:main")
    wiki = tmp_path / "wiki"
    git(tmp_path, "clone", "-q", str(wiki_bare), str(wiki))
    ci_sync(repo, wiki, T0)
    return repo, wiki, repo_bare, wiki_bare


# ================================================================== docs -> wiki links


@pytest.mark.parametrize("page", sorted(WIKI_LINKS))
def test_sync_links_relative_to_wiki_page_dir(docs, tmp_path, page):
    """Each page's links are rewritten relative to where the page lands in the wiki,
    keeping .md on pages; external, mailto, absolute and outside-docs links are untouched."""
    sync(docs, tmp_path / "wiki")
    assert links(tmp_path / "wiki" / page) == WIKI_LINKS[page]


def test_sync_never_emits_bare_or_dangling_links(docs, tmp_path):
    """No wiki page may hold a link that fails to resolve from its own directory or that
    names a page without .md (GitLab resolves a bare name from the wiki root)."""
    sync(docs, tmp_path / "wiki")
    assert wiki_link_problems(tmp_path / "wiki") == []


def test_sync_link_to_excluded_page_is_untouched(docs, tmp_path):
    """A link to a page the filter drops stays exactly as written in docs/."""
    sync(docs, tmp_path / "wiki", docfilter.Filter(exclude=["/dev/**"]))
    assert "dev/release.md" not in tree(tmp_path / "wiki")
    assert links(tmp_path / "wiki/ops/backups.md")[0] == "../dev/release.md"  # as in docs/


def test_sync_self_link(docs, tmp_path):
    """A page linking to itself: home.md -> home.md, ops.md -> ops.md, nested -> its own name."""
    write(docs, {"index.md": "# Home\n\n[me](index.md)\n", "ops/index.md": "# Ops\n\n[me](index.md)\n",
                 "ops/backups.md": "# B\n\n[me](backups.md#top)\n"})
    sync(docs, tmp_path / "wiki")
    assert links(tmp_path / "wiki/home.md") == ["home.md"]
    assert links(tmp_path / "wiki/ops.md") == ["ops.md"]
    assert links(tmp_path / "wiki/ops/backups.md") == ["backups.md#top"]


def test_sync_mutual_links_with_anchors(docs, tmp_path):
    """Two pages linking each other with anchors keep both link and fragment."""
    write(docs, {"dev/a.md": "# A\n\n## Top\n\n[b](b.md#bottom)\n", "dev/b.md": "# B\n\n## Bottom\n\n[a](a.md#top)\n"})
    sync(docs, tmp_path / "wiki")
    assert links(tmp_path / "wiki/dev/a.md") == ["b.md#bottom"]
    assert links(tmp_path / "wiki/dev/b.md") == ["a.md#top"]


def test_sync_unicode_page_name_and_link(docs, tmp_path):
    """A unicode file name is kept and a link to it from a relocated section page is recomputed."""
    write(docs, {"ops/Über-cool.md": "# Über\n\n[back](index.md)\n", "ops/index.md": "# Ops\n\n[u](Über-cool.md)\n"})
    sync(docs, tmp_path / "wiki")
    assert (tmp_path / "wiki/ops/Über-cool.md").read_text() == "# Über\n\n[back](../ops.md)\n"
    assert links(tmp_path / "wiki/ops.md") == ["ops/Über-cool.md"]


def test_sync_percent_encoded_space_link(docs, tmp_path):
    """A link to a file with a space is written percent-encoded in Markdown; the sync must decode
    it to find the file and re-encode the rewritten target."""
    write(docs, {"ops/my notes.md": "# Notes\n", "ops/index.md": "# Ops\n\n[n](my%20notes.md)\n"})
    sync(docs, tmp_path / "wiki")
    assert (tmp_path / "wiki/ops/my notes.md").exists()
    assert links(tmp_path / "wiki/ops.md") == ["ops/my%20notes.md"]


def test_sync_sidebar_links_resolve(docs, tmp_path):
    """Every sidebar entry points at an existing wiki page, in page-name order from .pages."""
    sync(docs, tmp_path / "wiki")
    side = (tmp_path / "wiki/_sidebar.md").read_text()
    assert re.findall(r"\[([^\]]+)\]", side) == ["Home", "Ops", "Backups", "Restore", "Dev", "Release", "Glossary"]
    for target in links(tmp_path / "wiki/_sidebar.md"):
        name = target.lstrip("/")
        assert (tmp_path / "wiki" / name).exists() or (tmp_path / "wiki" / (name + ".md")).exists(), target


# ================================================================== wiki -> docs links

# The forms a person types in the GitLab UI, on a nested page ops/backups.md whose wiki also
# holds a root page restore.md AND a sibling ops/restore.md (the ambiguity that matters).
NESTED_FORMS = [
    ("restore", "../restore.md"),            # bare: GitLab resolves from the wiki root
    ("./restore", "restore.md"),             # explicit ./ : the page's own directory
    ("restore.md", "restore.md"),            # .md sibling: the page's own directory
    ("../restore", "../restore.md"),
    ("../restore.md", "../restore.md"),
    ("/restore", "../restore.md"),
    ("ops/restore", "restore.md"),           # root-relative, as the editor inserts
    ("/ops/restore", "restore.md"),
    ("../dev/release", "../dev/release.md"),
    ("../dev/release.md", "../dev/release.md"),
    ("dev/release", "../dev/release.md"),
    ("home", "../index.md"),
    ("../home.md", "../index.md"),
    ("ops", "index.md"),                     # the section page, back to its index
    ("../ops.md", "index.md"),
    ("uploads/1/n.png", "../uploads/1/n.png"),   # root-relative, what the editor inserts
    ("../uploads/1/n.png", "../uploads/1/n.png"),
    ("restore#steps", "../restore.md#steps"),
    ("./restore#steps", "restore.md#steps"),
]


def wiki_with_ambiguous_restore(tmp_path):
    wiki = tmp_path / "wiki"
    write(wiki, DOCS_AS_WIKI)
    write(wiki, {"restore.md": "# Root restore\n"})
    return wiki


DOCS_AS_WIKI = {
    "home.md": "# Home\n", "ops.md": "# Ops\n", "ops/backups.md": "# Backups\n", "ops/restore.md": "# Restore\n",
    "dev.md": "# Dev\n", "dev/release.md": "# Release\n", "glossary.md": "# Glossary\n", "uploads/1/n.png": PNG,
}


@pytest.mark.parametrize("typed,expected", NESTED_FORMS)
def test_import_resolves_every_link_form_from_nested_page(tmp_path, typed, expected):
    """From wiki/ops/backups.md each typed form resolves to the relative .md link docs/ needs."""
    wiki = wiki_with_ambiguous_restore(tmp_path)
    write(wiki, {"ops/backups.md": f"# Backups\n\n[l]({typed})\n"})
    wiki_import.main(wiki, tmp_path / "docs")
    assert links(tmp_path / "docs/ops/backups.md") == [expected]


SECTION_FORMS = [  # typed on the root page ops.md, which lands in docs/ops/index.md
    ("ops/restore", "restore.md"),
    ("ops/restore.md", "restore.md"),
    ("dev/release", "../dev/release.md"),
    ("home", "../index.md"),
    ("dev", "../dev/index.md"),
    ("ops", "index.md"),
    ("uploads/1/n.png", "../uploads/1/n.png"),
    ("restore", "../restore.md"),
]


@pytest.mark.parametrize("typed,expected", SECTION_FORMS)
def test_import_resolves_every_link_form_from_section_page(tmp_path, typed, expected):
    """From wiki/ops.md (moves down into ops/index.md) each typed form is recomputed."""
    wiki = wiki_with_ambiguous_restore(tmp_path)
    write(wiki, {"ops.md": f"# Ops\n\n[l]({typed})\n"})
    wiki_import.main(wiki, tmp_path / "docs")
    assert links(tmp_path / "docs/ops/index.md") == [expected]


def test_import_leaves_project_uploads_alone(tmp_path, capsys):
    """/uploads/... is a project upload, not a wiki attachment: untouched and not reported."""
    wiki = wiki_with_ambiguous_restore(tmp_path)
    write(wiki, {"ops/backups.md": "# B\n\n![p](/uploads/abc/proj.png) ![q](/uploads/1/n.png)\n"})
    wiki_import.main(wiki, tmp_path / "docs")
    assert links(tmp_path / "docs/ops/backups.md") == ["/uploads/abc/proj.png", "/uploads/1/n.png"]
    assert "/uploads/" not in capsys.readouterr().out  # a valid project upload is not "unresolved"


def test_import_leaves_external_and_missing_alone(tmp_path, capsys):
    """External, mailto and dangling links are untouched; only the dangling one is reported."""
    wiki = wiki_with_ambiguous_restore(tmp_path)
    write(wiki, {"ops/backups.md": "# B\n\n[e](https://x.y/z) [m](mailto:a@b.c) [d](nowhere)\n"})
    wiki_import.main(wiki, tmp_path / "docs")
    assert links(tmp_path / "docs/ops/backups.md") == ["https://x.y/z", "mailto:a@b.c", "nowhere"]
    out = capsys.readouterr().out
    assert "ops/backups.md: nowhere" in out and "https://x.y/z" not in out


def test_pull_uses_the_same_link_resolution(estate):
    """wiki-pull converts a GitLab-UI-style edit exactly like wiki-import does."""
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"ops/backups.md": "# Backups\n\n[y](restore) [z](./restore) ![n](uploads/1/n.png) [h](home)\n"}, "ui links")
    pull(repo, wiki)
    assert links(repo / "docs/ops/backups.md") == ["restore.md", "restore.md", "../uploads/1/n.png", "../index.md"]


# ================================================================== round trips


def test_round_trip_is_byte_identical_for_canonical_docs(docs, tmp_path):
    """docs -> wiki -> docs reproduces every page and attachment byte for byte."""
    sync(docs, tmp_path / "wiki")
    wiki_import.main(tmp_path / "wiki", tmp_path / "back")
    expected = {rel: content for rel, content in snapshot(docs).items() if not Path(rel).name.startswith(".")}
    assert snapshot(tmp_path / "back") == expected


def test_round_trip_through_pull_keeps_links_unchanged(estate):
    """A wiki edit that only appends text leaves the page's canonical links exactly as they were."""
    repo, wiki, *_ = estate
    before = (repo / "docs/ops/backups.md").read_text()
    wiki_edit(wiki, {"ops/backups.md": (wiki / "ops/backups.md").read_text() + "\nappended in wiki\n"}, "append")
    pull(repo, wiki)
    assert (repo / "docs/ops/backups.md").read_text() == before + "\nappended in wiki\n"


@pytest.mark.parametrize("content", [
    pytest.param(b"# CRLF\r\n\r\nline one\r\nline two\r\n", id="crlf"),
    pytest.param(b"# No newline\n\nlast line has no newline", id="no-trailing-newline"),
    pytest.param(b"# Mixed\r\nline\nline\r\n", id="mixed-endings"),
])
def test_sync_preserves_line_endings(docs, tmp_path, content):
    """Page bytes reach the wiki unchanged: CRLF stays CRLF, a missing final newline stays missing."""
    write(docs, {"ops/raw.md": content})
    sync(docs, tmp_path / "wiki")
    assert (tmp_path / "wiki/ops/raw.md").read_bytes() == content


@pytest.mark.parametrize("content", [
    pytest.param(b"# CRLF\r\n\r\nline one\r\nline two\r\n", id="crlf"),
    pytest.param(b"# No newline\n\nlast line has no newline", id="no-trailing-newline"),
])
def test_pull_preserves_line_endings(estate, content):
    """A wiki page saved with CRLF or without a final newline lands in docs/ byte for byte."""
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"ops/raw.md": content}, "raw")
    pull(repo, wiki)
    assert (repo / "docs/ops/raw.md").read_bytes() == content


def test_pull_edit_of_crlf_page_keeps_crlf(estate):
    """A page that already uses CRLF in docs/, edited in the wiki (still CRLF), stays CRLF and
    carries the edit without a spurious conflict."""
    repo, wiki, *_ = estate
    repo_edit(repo, {"docs/ops/crlf.md": b"# C\r\n\r\none\r\ntwo\r\n"}, "crlf page", date=T1)
    ci_sync(repo, wiki, T1)
    wiki_edit(wiki, {"ops/crlf.md": b"# C\r\n\r\none\r\ntwo\r\nthree\r\n"}, "add three", date=T2)
    pull(repo, wiki)
    assert (repo / "docs/ops/crlf.md").read_bytes() == b"# C\r\n\r\none\r\ntwo\r\nthree\r\n"


def test_large_page_round_trip(estate):
    """A 2 MB page survives sync and pull intact."""
    repo, wiki, *_ = estate
    big = "# Big\n\n" + ("x" * 99 + "\n") * 21000  # ~2.1 MB
    wiki_edit(wiki, {"ops/big.md": big}, "big")
    pull(repo, wiki)
    assert (repo / "docs/ops/big.md").read_text() == big
    ci_sync(repo, wiki, T2)
    assert (wiki / "ops/big.md").read_text() == big


def test_merge_markers_never_reach_docs(estate):
    """Whichever side wins a conflict, no conflict markers are written anywhere under docs/."""
    repo, wiki, *_ = estate
    repo_edit(repo, {"docs/glossary.md": "# Glossary\n\nterms by repo\n"}, "repo", date=T3)
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nterms by wiki\n", "ops/restore.md": "# Restore\n\nwiki\n"}, "wiki", date=T2)
    pull(repo, wiki)
    for rel, content in snapshot(repo / "docs").items():
        assert b"<<<<<<<" not in content and b">>>>>>>" not in content, rel
    assert (repo / "docs/glossary.md").read_text() == "# Glossary\n\nterms by repo\n"


# ================================================================== attachments


def test_pull_attachment_added_in_wiki(estate):
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"uploads/2/new.png": PNG2}, "upload")
    pull(repo, wiki)
    assert (repo / "docs/uploads/2/new.png").read_bytes() == PNG2
    assert git(repo, "status", "--porcelain").strip() == ""


def test_pull_attachment_modified_in_wiki(estate):
    """Binary content change with the same name and size class is carried over."""
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"uploads/1/n.png": PNG2}, "replace image")
    pull(repo, wiki)
    assert (repo / "docs/uploads/1/n.png").read_bytes() == PNG2


def test_pull_attachment_deleted_in_wiki(estate):
    repo, wiki, *_ = estate
    wiki_edit(wiki, {}, "drop image", delete=["uploads/1/n.png"])
    pull(repo, wiki)
    assert not (repo / "docs/uploads/1/n.png").exists()
    assert git(repo, "status", "--porcelain").strip() == ""


def test_sync_mirrors_repo_attachment_changes(estate):
    """Added, modified and deleted attachments in docs/ are reflected in the wiki clone."""
    repo, wiki, *_ = estate
    repo_edit(repo, {"docs/uploads/1/n.png": PNG2, "docs/uploads/3/add.png": PNG}, "images", delete=["docs/glossary.md"])
    write(repo, {"docs/ops/diagram.svg": "<svg/>\n"})
    remove(repo, "docs/uploads/3/add.png")
    write(repo, {"docs/uploads/4/moved.png": PNG})
    commit_all(repo, "more", DEV, T2)
    wiki_sync.main(repo / "docs", wiki)
    assert (wiki / "uploads/1/n.png").read_bytes() == PNG2
    assert (wiki / "ops/diagram.svg").read_text() == "<svg/>\n"
    assert (wiki / "uploads/4/moved.png").exists() and not (wiki / "uploads/3").exists()
    assert not (wiki / "glossary.md").exists()


# ================================================================== structure


def test_wiki_rename_arrives_as_delete_plus_add(estate, capsys):
    """Renaming a page in the wiki: docs/ gets the new file and loses the old one; a page still
    linking to the old name is not rewritten and its link is reported unresolved when pulled."""
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"ops/recovery.md": "# Recovery\n\n## Steps\n\n[back](backups.md#top)\n"}, "rename", delete=["ops/restore.md"])
    pull(repo, wiki)
    assert (repo / "docs/ops/recovery.md").read_text() == "# Recovery\n\n## Steps\n\n[back](backups.md#top)\n"
    assert not (repo / "docs/ops/restore.md").exists()
    assert links(repo / "docs/ops/backups.md")[1] == "restore.md#steps"  # untouched, now dangling
    wiki_edit(wiki, {"ops/backups.md": (wiki / "ops/backups.md").read_text() + "edited\n"}, "touch backups", date=T2)
    pull(repo, wiki)
    assert "unresolved link left as-is: ops/backups.md: restore.md" in capsys.readouterr().out
    assert links(repo / "docs/ops/backups.md")[1] == "restore.md#steps"


def test_repo_rename_leaves_no_stale_wiki_page(estate):
    """Renaming a page in docs/: the wiki page is deleted and the new one created."""
    repo, wiki, *_ = estate
    repo_edit(repo, {"docs/ops/recovery.md": (repo / "docs/ops/restore.md").read_text()}, "rename", delete=["docs/ops/restore.md"])
    wiki_sync.main(repo / "docs", wiki)
    assert not (wiki / "ops/restore.md").exists() and (wiki / "ops/recovery.md").exists()
    status = set(git(wiki, "status", "--porcelain").split("\n"))
    assert " D ops/restore.md" in status and "?? ops/recovery.md" in status


def test_wiki_root_page_with_matching_docs_folder_becomes_section_index(estate):
    """docs/tools/ exists (no index.md); the wiki root page tools.md becomes docs/tools/index.md."""
    repo, wiki, *_ = estate
    repo_edit(repo, {"docs/tools/lint.md": "# Lint\n"}, "tools folder")
    ci_sync(repo, wiki, T1)
    wiki_edit(wiki, {"tools.md": "# Tools\n\n[lint](tools/lint)\n"}, "tools landing", date=T2)
    pull(repo, wiki)
    assert (repo / "docs/tools/index.md").read_text() == "# Tools\n\n[lint](lint.md)\n"
    assert not (repo / "docs/tools.md").exists()


def test_wiki_root_page_without_docs_folder_stays_top_level(estate):
    """No docs/misc/ folder: the wiki root page misc.md becomes docs/misc.md."""
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"misc.md": "# Misc\n\n[g](glossary)\n"}, "misc")
    pull(repo, wiki)
    assert (repo / "docs/misc.md").read_text() == "# Misc\n\n[g](glossary.md)\n"
    assert not (repo / "docs/misc").exists()


def test_wiki_nested_page_in_new_folder(estate):
    """A page created two folders deep where docs/ has no such folder lands at the same path."""
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"howto/net/vlan.md": "# VLAN\n\n[b](ops/backups) [h](home)\n"}, "deep page")
    pull(repo, wiki)
    assert (repo / "docs/howto/net/vlan.md").read_text() == "# VLAN\n\n[b](../../ops/backups.md) [h](../../index.md)\n"


def test_wiki_delete_of_section_page_removes_docs_index(estate):
    """Deleting the wiki root page ops.md deletes docs/ops/index.md, leaving the children alone."""
    repo, wiki, *_ = estate
    wiki_edit(wiki, {}, "drop ops landing", delete=["ops.md"])
    pull(repo, wiki)
    assert not (repo / "docs/ops/index.md").exists()
    assert (repo / "docs/ops/backups.md").exists()


def test_both_sides_delete_the_same_page(estate):
    """Repo and wiki both delete glossary: the pull has nothing to do and does not fail."""
    repo, wiki, *_ = estate
    repo_edit(repo, {}, "drop glossary", delete=["docs/glossary.md"], date=T1)
    head = git(repo, "rev-parse", "HEAD")
    wiki_edit(wiki, {}, "drop glossary too", delete=["glossary.md"], date=T2)
    assert pull(repo, wiki) == 0
    assert git(repo, "rev-parse", "HEAD") == head
    assert not (repo / "docs/glossary.md").exists()
    wiki_sync.main(repo / "docs", wiki)
    assert not (wiki / "glossary.md").exists()


def test_wiki_edit_resurrects_page_deleted_in_repo(estate):
    """Repo deletes a page, the wiki edits it after the sync: the newer wiki edit wins."""
    repo, wiki, *_ = estate
    repo_edit(repo, {}, "drop glossary", delete=["docs/glossary.md"], date=T1)
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nterms, plus wiki\n"}, "glossary", date=T2)
    pull(repo, wiki)
    assert (repo / "docs/glossary.md").read_text() == "# Glossary\n\nterms, plus wiki\n"


def test_unicode_and_hyphenated_names_pull_and_sync(estate):
    """A wiki page named with unicode and GitLab's hyphen-for-space slug round-trips by name."""
    repo, wiki, *_ = estate
    wiki_edit(wiki, {"ops/Über-cool-Seite.md": "# Über cool\n\n[b](ops/backups)\n"}, "unicode")
    pull(repo, wiki)
    assert (repo / "docs/ops/Über-cool-Seite.md").read_text() == "# Über cool\n\n[b](backups.md)\n"
    ci_sync(repo, wiki, T2)
    assert (wiki / "ops/Über-cool-Seite.md").read_text() == "# Über cool\n\n[b](backups.md)\n"


def test_empty_docs_only_index(tmp_path):
    """docs/ holding only index.md yields home.md and a sidebar with just the header."""
    docs = tmp_path / "docs"
    write(docs, {"index.md": "# Home\n\nnothing else\n"})
    sync(docs, tmp_path / "wiki")
    assert tree(tmp_path / "wiki") == ["_sidebar.md", "home.md"]
    assert (tmp_path / "wiki/home.md").read_text() == "# Home\n\nnothing else\n"
    assert re.findall(r"\[([^\]]+)\]", (tmp_path / "wiki/_sidebar.md").read_text()) == ["Home"]


def test_wiped_wiki_is_reseeded_without_touching_docs(estate):
    """The wiki repo was reset to zero commits: wiki-deploy.sh pulls nothing, deletes nothing in
    docs/, and pushes a full sync."""
    repo, _, _, wiki_bare = estate
    subprocess.run(["rm", "-rf", str(wiki_bare)], check=True)
    git(wiki_bare.parent, "init", "-q", "--bare", "-b", "main", str(wiki_bare))
    docs_before, head = snapshot(repo / "docs"), git(repo, "rev-parse", "HEAD")
    out = deploy(repo, wiki_bare)
    assert out.returncode == 0, out.stderr + out.stdout
    assert snapshot(repo / "docs") == docs_before and git(repo, "rev-parse", "HEAD") == head
    assert bare_files(wiki_bare) == ["_sidebar.md", "dev.md", "dev/release.md", "glossary.md", "home.md",
                                     "ops.md", "ops/backups.md", "ops/restore.md", "uploads/1/n.png"]


# ================================================================== filters and furniture


def test_pull_ignores_edit_to_excluded_page(estate):
    """docs/drafts/ is excluded: a wiki edit under drafts/ is neither pulled nor committed."""
    repo, wiki, *_ = estate
    repo_edit(repo, {"docs/drafts/wip.md": "# WIP\n\nrepo only\n"}, "draft")
    flt = docfilter.Filter(exclude=["drafts/"])
    wiki_sync.main(repo / "docs", wiki, flt)
    assert not (wiki / "drafts").exists()
    commit_all(wiki, "Sync docs from abc1234", (CI, "ci@example.com"), T1)
    head = git(repo, "rev-parse", "HEAD")
    wiki_edit(wiki, {"drafts/wip.md": "# WIP\n\nfrom wiki\n"}, "draft in wiki", date=T2)
    pull(repo, wiki, flt=flt)
    assert (repo / "docs/drafts/wip.md").read_text() == "# WIP\n\nrepo only\n"
    assert git(repo, "rev-parse", "HEAD") == head


def test_not_in_nav_page_is_synced_hidden_and_still_pulled(estate):
    """not_in_nav drops glossary from the sidebar but it is still a page: wiki edits come back."""
    repo, wiki, *_ = estate
    flt = docfilter.Filter(not_in_nav=["/glossary.md"])
    wiki_sync.main(repo / "docs", wiki, flt)
    assert "Glossary" not in (wiki / "_sidebar.md").read_text() and (wiki / "glossary.md").exists()
    commit_all(wiki, "Sync docs from abc1234", (CI, "ci@example.com"), T1)
    wiki_edit(wiki, {"glossary.md": "# Glossary\n\nhidden but editable\n"}, "glossary", date=T2)
    pull(repo, wiki, flt=flt)
    assert (repo / "docs/glossary.md").read_text() == "# Glossary\n\nhidden but editable\n"


def test_deploy_applies_mkdocs_exclude_and_not_in_nav(estate):
    """wiki-deploy.sh reads mkdocs.yml: excluded drafts never reach the wiki, not_in_nav is hidden."""
    repo, _, _, wiki_bare = estate
    repo_edit(repo, {"docs/drafts/wip.md": "# WIP\n", "mkdocs.yml": "site_name: t\nexclude_docs: |\n  drafts/\nnot_in_nav: |\n  /glossary.md\n"}, "config")
    out = deploy(repo, wiki_bare)
    assert out.returncode == 0, out.stderr + out.stdout
    files = bare_files(wiki_bare)
    assert "drafts/wip.md" not in files and "glossary.md" in files
    assert "Glossary" not in git(wiki_bare, "show", "main:_sidebar.md")


def test_hand_edited_sidebar_is_never_pulled_and_is_regenerated(estate):
    repo, wiki, *_ = estate
    head = git(repo, "rev-parse", "HEAD")
    wiki_edit(wiki, {"_sidebar.md": "hand edited\n"}, "sidebar")
    pull(repo, wiki)
    assert git(repo, "rev-parse", "HEAD") == head and not (repo / "docs/_sidebar.md").exists()
    wiki_sync.main(repo / "docs", wiki)
    assert "hand edited" not in (wiki / "_sidebar.md").read_text()


def test_gitlab_redirects_are_never_pulled(estate):
    """GitLab writes .gitlab/redirects.yml on a rename; it is wiki furniture."""
    repo, wiki, *_ = estate
    head = git(repo, "rev-parse", "HEAD")
    wiki_edit(wiki, {".gitlab/redirects.yml": "ops/restore: ops/recovery\n"}, "redirect")
    pull(repo, wiki)
    assert git(repo, "rev-parse", "HEAD") == head and not (repo / "docs/.gitlab").exists()


# ================================================================== end to end


def test_deploy_normalises_ui_link_forms_in_both_places(estate):
    """A UI-style edit (bare and root-relative links) lands in docs/ as relative .md links and
    is re-rendered in the wiki in the canonical relative form."""
    repo, wiki, repo_bare, wiki_bare = estate
    wiki_edit(wiki, {"ops/backups.md": "# Backups\n\n[y](restore) ![n](uploads/1/n.png) [h](home)\n"}, "ui edit")
    out = deploy(repo, wiki_bare)
    assert out.returncode == 0, out.stderr + out.stdout
    assert git(repo_bare, "show", "main:docs/ops/backups.md") == "# Backups\n\n[y](restore.md) ![n](../uploads/1/n.png) [h](../index.md)\n"
    assert git(wiki_bare, "show", "main:ops/backups.md") == "# Backups\n\n[y](restore.md) ![n](../uploads/1/n.png) [h](../home.md)\n"
    assert git(wiki_bare, "log", "-1", "--format=%an", "main").strip() == CI
