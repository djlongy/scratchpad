"""Tests for wiki-import.py and wiki-sync.py. Run: python3 -m pytest -q"""
import argparse
import importlib.util
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


wiki_import = load("wiki-import")
wiki_sync = load("wiki-sync")


def write(root: Path, files: dict):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)


def links(md: Path):
    return re.findall(r"\]\(([^)]+)\)", md.read_text())


# ---------------------------------------------------------------- wiki-import


@pytest.fixture
def wiki(tmp_path):
    root = tmp_path / "wiki"
    write(root, {
        "home.md": "# Team wiki\n\n- [Ops](ops)\n- [Dev](dev)\n- [Glossary](glossary)\n",
        "ops.md": "# Ops\n\n[Backups](ops/backups) [Release](dev/release) ![n](uploads/1/n.png)\n",
        "ops/backups.md": "# Backups\n\n[release](../dev/release) [onboard](onboarding) [abs](/ops/onboarding)\n",
        "ops/onboarding.md": "# Onboarding\n\n[missing](nowhere) [ext](https://example.com/x)\n",
        "dev.md": "# Dev\n\n[Release](dev/release) [Ops](ops)\n",
        "dev/release.md": "# Release\n\n![p](../uploads/1/p.png) [b](/ops/backups#restore)\n",
        "glossary.md": "# Glossary\n\nno children, stays top-level\n",
        "_sidebar.md": "**[x](/home)**\n",
        "uploads/1/n.png": b"\x89PNG",
        "uploads/1/p.png": b"\x89PNG",
    })
    return root


def test_import_layout(wiki, tmp_path, capsys):
    docs = tmp_path / "docs"
    wiki_import.main(wiki, docs)
    got = sorted(str(p.relative_to(docs)) for p in docs.rglob("*") if p.is_file())
    assert got == [
        "dev/index.md", "dev/release.md", "glossary.md", "index.md", "ops/backups.md",
        "ops/index.md", "ops/onboarding.md", "uploads/1/n.png", "uploads/1/p.png",
    ]
    assert "_sidebar.md" not in got
    out = capsys.readouterr().out
    assert "imported 7 pages, 2 attachments" in out
    assert "ops/onboarding.md: nowhere" in out  # unresolved link reported


def test_import_rewrites_every_link_form(wiki, tmp_path):
    docs = tmp_path / "docs"
    wiki_import.main(wiki, docs)
    assert links(docs / "index.md") == ["ops/index.md", "dev/index.md", "glossary.md"]
    assert links(docs / "ops/index.md") == ["backups.md", "../dev/release.md", "../uploads/1/n.png"]
    assert links(docs / "ops/backups.md") == ["../dev/release.md", "onboarding.md", "onboarding.md"]
    assert links(docs / "ops/onboarding.md") == ["nowhere", "https://example.com/x"]  # untouched
    assert links(docs / "dev/release.md") == ["../uploads/1/p.png", "../ops/backups.md#restore"]


def test_import_generates_home_when_missing(tmp_path):
    wiki = tmp_path / "wiki"
    write(wiki, {"ops.md": "# Ops\n", "ops/a.md": "# A\n", "notes.md": "# Notes\n"})
    docs = tmp_path / "docs"
    wiki_import.main(wiki, docs)
    assert links(docs / "index.md") == ["notes.md", "ops/index.md"]


def test_import_root_folders_only(tmp_path, capsys):
    """Two root folders, no root pages, attachments nested inside, cross-folder links."""
    wiki = tmp_path / "wiki"
    write(wiki, {
        "Operations/stuff01.md": "# Stuff 01\n\n[dev](Engineering/stuff02) ![d](Operations/attachments/diagram.png)\n",
        "Operations/attachments/diagram.png": b"\x89PNG",
        "Operations/deeper/stuff03.md": "# Stuff 03\n\n[up](../stuff01)\n",
        "Engineering/stuff02.md": "# Stuff 02\n\n[ops](Operations/stuff01)\n",
    })
    docs = tmp_path / "docs"
    wiki_import.main(wiki, docs)
    assert "generated 4 index pages" in capsys.readouterr().out
    assert links(docs / "index.md") == ["Engineering/index.md", "Operations/index.md"]
    assert links(docs / "Operations/index.md") == ["deeper/index.md", "stuff01.md"]
    assert links(docs / "Operations/deeper/index.md") == ["stuff03.md"]
    assert links(docs / "Operations/stuff01.md") == ["../Engineering/stuff02.md", "attachments/diagram.png"]
    assert links(docs / "Operations/deeper/stuff03.md") == ["../stuff01.md"]
    assert (docs / "Operations/attachments/diagram.png").exists()
    out = tmp_path / "out"
    out.mkdir()
    wiki_sync.main(docs, out)
    assert links(out / "Operations/stuff01.md") == ["../Engineering/stuff02", "./attachments/diagram.png"]
    assert (out / "_sidebar.md").read_text() == (
        "**[Home](/home)**\n\n"
        "- [Engineering](/Engineering)\n"
        "  - [Stuff 02](/Engineering/stuff02)\n"
        "- [Operations](/Operations)\n"
        "  - [deeper](/Operations/deeper)\n"
        "    - [Stuff 03](/Operations/deeper/stuff03)\n"
        "  - [Stuff 01](/Operations/stuff01)\n"
    )


# ------------------------------------------------------------------ wiki-sync


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "docs"
    write(root, {
        "index.md": "# Team docs\n\n[Ops](ops/index.md) [Glossary](glossary.md)\n",
        ".pages": "nav:\n  - index.md\n  - Ops: ops\n  - ...\n",
        "ops/index.md": (
            "# Ops\n\n[Backups](backups.md) [Dev](../dev/index.md) ![n](../uploads/1/n.png) "
            "[ext](https://example.com) [anchor](backups.md#restore) [out](../../outside.md)\n"
        ),
        "ops/.pages": "title: Operations\nnav:\n  - index.md\n  - onboarding.md\n  - ...\n",
        "ops/backups.md": "# Backups\n\n[home](../index.md)\n",
        "ops/onboarding.md": "# Onboarding\n",
        "dev/index.md": "# Dev\n",
        "dev/zeta.md": "# Zeta\n",
        "dev/alpha.md": "# Alpha\n",
        "glossary.md": "# Glossary\n",
        "uploads/1/n.png": b"\x89PNG",
    })
    (tmp_path / "outside.md").write_text("# outside\n")
    return root


def test_sync_layout_and_links(docs, tmp_path, capsys):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wiki_sync.main(docs, wiki)
    got = sorted(str(p.relative_to(wiki)) for p in wiki.rglob("*") if p.is_file())
    assert got == [".gitlab/docs-map.json", "_sidebar.md", "dev.md", "dev/alpha.md", "dev/zeta.md", "glossary.md", "home.md",
                   "ops.md", "ops/backups.md", "ops/onboarding.md", "uploads/1/n.png"]
    assert links(wiki / "home.md") == ["./ops", "./glossary"]
    assert links(wiki / "ops.md") == ["./ops/backups", "./dev", "./uploads/1/n.png", "https://example.com",
                                      "./ops/backups#restore", "../../outside.md"]
    assert links(wiki / "ops/backups.md") == ["../home"]
    assert "synced 8 pages, 1 attachments, sidebar 7 lines" in capsys.readouterr().out


def test_sync_keeps_targets_below_the_page_folder_page_relative(tmp_path):
    """A bare target is resolved from the wiki ROOT, so anything at or under the page's own folder
    needs the ./ : a section link `deep/` from compute/nodes.md would otherwise read as /deep."""
    docs = tmp_path / "docs"
    write(docs, {
        "index.md": "# Home\n",
        "compute/index.md": "# Compute\n",
        "compute/nodes.md": "# Nodes\n\n[deep](deep/) [rack](deep/rack.md) ![d](shots/d.png)\n",
        "compute/deep/index.md": "# Deep\n",
        "compute/deep/rack.md": "# Rack\n",
        "compute/shots/d.png": b"\x89PNG",
    })
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wiki_sync.main(docs, wiki)
    assert links(wiki / "compute/nodes.md") == ["./deep", "./deep/rack", "./shots/d.png"]


def test_sync_sidebar_quotes_page_names(tmp_path):
    """The sidebar is Markdown too: an unquoted space makes `[x](/release notes)` not a link."""
    docs = tmp_path / "docs"
    write(docs, {"index.md": "# Home\n", "release notes.md": "# Release notes\n", "r\u00e9seau.md": "# R\u00e9seau\n"})
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wiki_sync.main(docs, wiki)
    assert links(wiki / "_sidebar.md") == ["/home", "/release%20notes", "/r\u00e9seau"]


def test_sync_sidebar_follows_pages_files(docs, tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wiki_sync.main(docs, wiki)
    assert (wiki / "_sidebar.md").read_text() == (
        "**[Team docs](/home)**\n\n"
        "- [Ops](/ops)\n"  # the parent's explicit label wins over the folder's own title:
        "  - [Onboarding](/ops/onboarding)\n"
        "  - [Backups](/ops/backups)\n"
        "- [Dev](/dev)\n"
        "  - [Alpha](/dev/alpha)\n"
        "  - [Zeta](/dev/zeta)\n"
        "- [Glossary](/glossary)\n"
    )


def test_sync_removes_stale_pages_and_attachments(docs, tmp_path):
    wiki = tmp_path / "wiki"
    write(wiki, {"old.md": "# gone\n", "ops/old.md": "x\n", "uploads/9/old.png": b"x", ".git/HEAD": "ref\n"})
    wiki_sync.main(docs, wiki)
    assert not (wiki / "old.md").exists()
    assert not (wiki / "ops/old.md").exists()
    assert not (wiki / "uploads/9").exists()
    assert (wiki / ".git/HEAD").exists()  # never touches the repo metadata


def test_sync_skips_hidden_directories(docs, tmp_path):
    """A stray .git or tool cache under docs/ must never be copied over the wiki clone."""
    write(docs, {".git/config": "[remote]\n", ".cache/plugin/x.png": b"x", "ops/.hidden/y.png": b"y"})
    wiki = tmp_path / "wiki"
    write(wiki, {".git/config": "[remote \"origin\"]\n\turl = https://oauth2:secret@example.com/w.git\n"})
    wiki_sync.main(docs, wiki)
    assert (wiki / ".git/config").read_text().startswith("[remote \"origin\"]")
    assert not (wiki / ".cache").exists() and not (wiki / "ops/.hidden").exists()


def test_sync_writes_the_docs_map_and_keeps_gitlab_redirects(docs, tmp_path):
    """Every page written is recorded wiki path -> docs path; GitLab's .gitlab/redirects.yml survives."""
    wiki = tmp_path / "wiki"
    write(wiki, {".gitlab/redirects.yml": "old: new\n", "stale.md": "# gone\n"})
    wiki_sync.main(docs, wiki)
    import json
    m = json.loads((wiki / ".gitlab/docs-map.json").read_text())
    assert m["version"] == 1
    assert m["pages"]["home.md"] == "index.md"
    assert m["pages"]["ops.md"] == "ops/index.md"
    assert m["pages"]["ops/backups.md"] == "ops/backups.md"
    assert (wiki / ".gitlab/redirects.yml").read_text() == "old: new\n"
    assert not (wiki / "stale.md").exists()


def test_sync_is_idempotent(docs, tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wiki_sync.main(docs, wiki)
    first = {p: p.read_bytes() for p in wiki.rglob("*") if p.is_file()}
    wiki_sync.main(docs, wiki)
    second = {p: p.read_bytes() for p in wiki.rglob("*") if p.is_file()}
    assert first == second


def test_sync_defaults_without_pages_or_home(tmp_path):
    docs = tmp_path / "docs"
    write(docs, {"b/index.md": "# Bee\n", "b/two.md": "# Two\n", "a.md": "# Ay\n", "b/one.md": "# One\n"})
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wiki_sync.main(docs, wiki)
    assert (wiki / "_sidebar.md").read_text() == (
        "**[Home](/home)**\n\n- [Ay](/a)\n- [Bee](/b)\n  - [One](/b/one)\n  - [Two](/b/two)\n"
    )


def test_round_trip_import_then_sync(wiki, tmp_path):
    docs = tmp_path / "docs"
    wiki_import.main(wiki, docs)
    out = tmp_path / "out"
    out.mkdir()
    wiki_sync.main(docs, out)
    assert links(out / "ops.md") == ["./ops/backups", "./dev/release", "./uploads/1/n.png"]
    assert links(out / "dev/release.md") == ["../uploads/1/p.png", "../ops/backups#restore"]
    assert (out / "uploads/1/n.png").read_bytes() == b"\x89PNG"


# ------------------------------------------------------------------ filters


docfilter = load("docfilter")


@pytest.mark.parametrize("rel,pattern,expected", [
    ("drafts/x.md", "drafts/", True),
    ("a/drafts/x.md", "drafts", True),
    ("a/drafts.md", "drafts", False),
    ("a/b.tmp", "*.tmp", True),
    ("internal/x.md", "/internal/*.md", True),
    ("a/internal/x.md", "/internal/*.md", False),
    ("reference/a/b.md", "reference/**", True),
    ("ref/a.md", "reference/**", False),
    ("x.md", "# comment", False),
])
def test_filter_matches(rel, pattern, expected):
    assert docfilter.matches(rel, pattern) is expected


def test_filter_reads_mkdocs_yml_with_python_tags(tmp_path):
    cfg = tmp_path / "mkdocs.yml"
    cfg.write_text(
        "site_name: x\n"
        "exclude_docs: |\n  drafts/\n  # a comment\n  *.tmp\n"
        "not_in_nav: |\n  /glossary.md\n"
        "markdown_extensions:\n  - pymdownx.emoji:\n"
        "      emoji_index: !!python/name:material.extensions.emoji.twemoji\n"
    )
    assert docfilter.read_mkdocs(cfg) == (["drafts/", "*.tmp"], ["/glossary.md"])
    args = argparse.Namespace(include=[], exclude=["*.bak"], config=cfg)
    flt = docfilter.from_args(args)
    assert flt.exclude == ["*.bak", "drafts/", "*.tmp"] and flt.not_in_nav == ["/glossary.md"]


def test_sync_exclude_and_include(docs, tmp_path):
    write(docs, {"drafts/wip.md": "# WIP\n", "ops/notes.tmp": b"x"})
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wiki_sync.main(docs, wiki, docfilter.Filter(exclude=["drafts/", "*.tmp"]))
    got = {str(p.relative_to(wiki)) for p in wiki.rglob("*") if p.is_file()}
    assert "drafts/wip.md" not in got and "ops/notes.tmp" not in got and "ops.md" in got
    assert "WIP" not in (wiki / "_sidebar.md").read_text()
    only = tmp_path / "only"
    only.mkdir()
    wiki_sync.main(docs, only, docfilter.Filter(include=["/ops/**", "/index.md"], exclude=["*.tmp"]))
    got = sorted(str(p.relative_to(only)) for p in only.rglob("*") if p.is_file())
    assert got == [".gitlab/docs-map.json", "_sidebar.md", "home.md", "ops.md", "ops/backups.md", "ops/onboarding.md"]
    assert links(only / "home.md") == ["./ops", "glossary.md"]  # link to an excluded page stays as written


def test_sync_not_in_nav_keeps_page_but_hides_it(docs, tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wiki_sync.main(docs, wiki, docfilter.Filter(not_in_nav=["/glossary.md", "dev/"]))
    assert (wiki / "glossary.md").exists() and (wiki / "dev/zeta.md").exists()
    side = (wiki / "_sidebar.md").read_text()
    assert "Glossary" not in side and "Zeta" not in side and "[Dev]" not in side and "Backups" in side


def test_import_exclude(wiki, tmp_path, capsys):
    docs = tmp_path / "docs"
    wiki_import.main(wiki, docs, docfilter.Filter(exclude=["uploads/", "/glossary.md"]))
    got = sorted(str(p.relative_to(docs)) for p in docs.rglob("*") if p.is_file())
    assert "uploads/1/n.png" not in got and "glossary.md" not in got and "ops/index.md" in got
    out = capsys.readouterr().out
    assert "ops.md: uploads/1/n.png" in out  # link to an excluded attachment: reported, not rewritten


def test_cli_entrypoints(docs, tmp_path):
    cfg = tmp_path / "mkdocs.yml"
    cfg.write_text("exclude_docs: |\n  /glossary.md\n")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wiki_sync.cli([str(docs), str(wiki), "--config", str(cfg), "--exclude", "dev/"])
    got = sorted(str(p.relative_to(wiki)) for p in wiki.rglob("*") if p.is_file())
    assert "glossary.md" not in got and "dev.md" not in got and "ops.md" in got
    back = tmp_path / "back"
    wiki_import.cli([str(wiki), str(back), "--exclude", "_sidebar.md"])
    assert (back / "ops/index.md").exists()


# --- reconcile.py in the flat layout ----------------------------------------
#
# The library keeps reconcile.py in runtime/wiki/ and httpjson.py one directory
# above it. Here every script sits in one folder, so the sibling import is the
# one thing the flat copy can get wrong, and it would fail only when somebody
# actually runs the webhook repair. These two exercise it cheaply.

reconcile = load("reconcile")


def test_reconcile_imports_httpjson_from_the_same_folder():
    assert reconcile.HOOK_NAME == "wiki-sync"
    assert reconcile.HttpError is not None


def test_reconcile_never_prints_the_trigger_token():
    url = "https://gitlab.example.com/api/v4/projects/7/ref/main/trigger/pipeline?token=glptt-secret"
    assert reconcile.elide_token(url).endswith("?token=<token>")
    assert "glptt-secret" not in reconcile.elide_token(url)


def test_reconcile_builds_the_hook_url_from_the_jobs_own_environment():
    project = reconcile.Project("https://gitlab.example.com/api/v4", "7", "t")
    assert reconcile.desired_url(project, "main", "abc") == (
        "https://gitlab.example.com/api/v4/projects/7/ref/main/trigger/pipeline?token=abc"
    )
