#!/usr/bin/env python3
"""One-time import of an existing GitLab wiki into a docs/ tree that wiki-sync.py can mirror back.

usage: wiki-import.py WIKI_CLONE DOCS_DIR [--include P]... [--exclude P]... [--config mkdocs.yml]

Takes a clone of <project>.wiki.git (root pages as <name>.md, child pages under <name>/,
attachments under uploads/) and writes:

    docs/index.md                      from home.md (or generated: a list of the top level)
    docs/<name>/index.md               from each root page <name>.md that has children
    docs/<name>.md                     from each root page without children
    docs/<name>/<child>.md             unchanged path
    docs/uploads/...                   attachments, unchanged path (anywhere; nested ones too)

Folders that hold pages but no landing page get a generated index.md listing them, so a wiki
made of root folders only (operations/, engineering/) gets section pages and a home page.
Generated pages are ordinary files; edit them afterwards.

Links are rewritten to relative Markdown links with the .md extension, the style that renders
in GitLab's file view, in MkDocs and (after wiki-sync.py rewrites them back) in the wiki.
Wiki links come in several forms; each target is tried against the wiki root first (how GitLab
resolves them) and then against the page's own directory, with and without .md. Unresolvable
links are left as they are and listed at the end so you can fix them by hand.

--include / --exclude apply to wiki paths (gitignore-style, see docfilter.py); a link to an
excluded page is reported as unresolved rather than rewritten.
"""
import argparse
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docfilter  # noqa: E402

LINK = re.compile(r"(\]\()([^)#\s]+?)(#[^)]*)?(\))")
INDEX = "index.md"


def is_page(p: Path) -> bool:
    return p.suffix == ".md" and not p.name.startswith("_")


def new_location(rel: Path, wiki: Path) -> Path:
    """Where a wiki page lands in docs/."""
    if str(rel) == "home.md":
        return Path(INDEX)
    if rel.parent == Path(".") and (wiki / rel.stem).is_dir():
        return Path(rel.stem) / INDEX  # root page with children becomes the section index
    return rel


def resolve_target(target: str, page: Path, wiki: Path, flt: docfilter.Filter):
    """Return the wiki-relative path of a link target, or None."""
    stripped = target.lstrip("/")
    for base in (wiki, page.parent):
        for name in (stripped, stripped + ".md", target, target + ".md"):
            candidate = base / name
            try:
                rel = candidate.resolve().relative_to(wiki.resolve())
            except ValueError:
                continue
            if candidate.is_file() and flt.allows(rel):
                return rel
    return None


def rewrite(text: str, page: Path, wiki: Path, dest: Path, unresolved: list, flt: docfilter.Filter) -> str:
    def sub(m):
        target = m.group(2)
        if "://" in target or target.startswith("mailto:"):
            return m.group(0)
        found = resolve_target(target, page, wiki, flt)
        if found is None:
            unresolved.append(f"{page.relative_to(wiki)}: {target}")
            return m.group(0)
        new_target = new_location(found, wiki) if is_page(found) else found
        rel = os.path.relpath(new_target, dest.parent)
        return f"{m.group(1)}{rel}{m.group(3) or ''}{m.group(4)}"

    return LINK.sub(sub, text)


def import_page(src: Path, wiki: Path, docs: Path, unresolved: list, flt: docfilter.Filter) -> None:
    dest = new_location(src.relative_to(wiki), wiki)
    out = docs / dest
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rewrite(src.read_text(), src, wiki, dest, unresolved, flt))


def copy_attachment(src: Path, wiki: Path, docs: Path) -> None:
    out = docs / src.relative_to(wiki)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)


def title_of(md: Path) -> str:
    for line in md.read_text().splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return md.stem.replace("-", " ")


def listing(title: str, folder: Path) -> str:
    """A Markdown page linking every page and section directly under folder."""
    lines = [f"# {title}", ""]
    for p in sorted(folder.iterdir()):
        if p.suffix == ".md" and p.name != INDEX:
            lines.append(f"- [{title_of(p)}]({p.name})")
        elif p.is_dir() and (p / INDEX).exists():
            lines.append(f"- [{title_of(p / INDEX)}]({p.name}/{INDEX})")
    return "\n".join(lines) + "\n"


def write_missing_indexes(docs: Path) -> int:
    """Give every folder that holds pages but no index.md a generated landing page, deepest first,
    so a wiki made of root folders (no root pages) still gets section pages and a home page."""
    made = 0
    folders = sorted({p.parent for p in docs.rglob("*.md")} | {docs}, key=lambda d: -len(d.parts))
    for folder in folders:
        if not (folder / INDEX).exists():
            title = "Home" if folder == docs else folder.name.replace("-", " ")
            (folder / INDEX).write_text(listing(title, folder))
            made += 1
    return made


def main(wiki: Path, docs: Path, flt: docfilter.Filter = None) -> None:
    flt = flt or docfilter.Filter()
    docs.mkdir(parents=True, exist_ok=True)
    unresolved, pages, files = [], 0, 0
    for src in sorted(wiki.rglob("*")):
        if not src.is_file() or ".git" in src.parts or not flt.allows(src.relative_to(wiki)):
            continue
        if src.suffix != ".md":
            copy_attachment(src, wiki, docs)
            files += 1
        elif is_page(src):  # _sidebar.md and templates/ are wiki furniture; wiki-sync regenerates them
            import_page(src, wiki, docs, unresolved, flt)
            pages += 1
    generated = write_missing_indexes(docs)
    print(f"imported {pages} pages, {files} attachments into {docs}, generated {generated} index pages")
    if unresolved:
        print(f"{len(unresolved)} links could not be resolved (left unchanged):")
        for u in unresolved:
            print("  " + u)


def cli(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("wiki", type=Path)
    parser.add_argument("docs", type=Path)
    docfilter.add_arguments(parser)
    args = parser.parse_args(argv)
    main(args.wiki.resolve(), args.docs.resolve(), docfilter.from_args(args))


if __name__ == "__main__":
    cli()
