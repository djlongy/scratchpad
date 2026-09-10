#!/usr/bin/env python3
"""One-time import of an existing GitLab wiki into a docs/ tree that wiki-sync.py can mirror back.

usage: wiki-import.py WIKI_CLONE DOCS_DIR

Takes a clone of <project>.wiki.git (root pages as <name>.md, child pages under <name>/,
attachments under uploads/) and writes:

    docs/index.md                      from home.md (or a generated list of root pages)
    docs/<name>/index.md               from each root page <name>.md that has children
    docs/<name>.md                     from each root page without children
    docs/<name>/<child>.md             unchanged path
    docs/uploads/...                   attachments, unchanged path

Links are rewritten to relative Markdown links with the .md extension, the style that renders
in GitLab's file view, in MkDocs and (after wiki-sync.py rewrites them back) in the wiki.
Wiki links come in several forms; each target is tried against the wiki root first (how GitLab
resolves them) and then against the page's own directory, with and without .md. Unresolvable
links are left as they are and listed at the end so you can fix them by hand.
"""
import os
import re
import shutil
import sys
from pathlib import Path

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


def resolve_target(target: str, page: Path, wiki: Path):
    """Return the wiki-relative path of a link target, or None."""
    stripped = target.lstrip("/")
    for base in (wiki, page.parent):
        for name in (stripped, stripped + ".md", target, target + ".md"):
            candidate = base / name
            try:
                rel = candidate.resolve().relative_to(wiki.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                return rel
    return None


def rewrite(text: str, page: Path, wiki: Path, dest: Path, unresolved: list) -> str:
    def sub(m):
        target = m.group(2)
        if "://" in target or target.startswith("mailto:"):
            return m.group(0)
        found = resolve_target(target, page, wiki)
        if found is None:
            unresolved.append(f"{page.relative_to(wiki)}: {target}")
            return m.group(0)
        new_target = new_location(found, wiki) if is_page(found) else found
        rel = os.path.relpath(new_target, dest.parent)
        return f"{m.group(1)}{rel}{m.group(3) or ''}{m.group(4)}"

    return LINK.sub(sub, text)


def import_page(src: Path, wiki: Path, docs: Path, unresolved: list) -> None:
    dest = new_location(src.relative_to(wiki), wiki)
    out = docs / dest
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rewrite(src.read_text(), src, wiki, dest, unresolved))


def copy_attachment(src: Path, wiki: Path, docs: Path) -> None:
    out = docs / src.relative_to(wiki)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)


def write_home(docs: Path) -> None:
    """A home page listing the top-level pages and sections, when the wiki had none."""
    roots = sorted(p for p in docs.iterdir() if p.suffix == ".md" or (p.is_dir() and (p / INDEX).exists()))
    lines = ["# Home", ""]
    for p in roots:
        target = p.name if p.suffix else f"{p.name}/{INDEX}"
        lines.append(f"- [{p.stem.replace('-', ' ').title()}]({target})")
    (docs / INDEX).write_text("\n".join(lines) + "\n")


def main(wiki: Path, docs: Path) -> None:
    docs.mkdir(parents=True, exist_ok=True)
    unresolved, pages, files = [], 0, 0
    for src in sorted(wiki.rglob("*")):
        if not src.is_file() or ".git" in src.parts:
            continue
        if src.suffix != ".md":
            copy_attachment(src, wiki, docs)
            files += 1
        elif is_page(src):  # _sidebar.md and templates/ are wiki furniture; wiki-sync regenerates them
            import_page(src, wiki, docs, unresolved)
            pages += 1
    if not (docs / INDEX).exists():
        write_home(docs)
    print(f"imported {pages} pages, {files} attachments into {docs}")
    if unresolved:
        print(f"{len(unresolved)} links could not be resolved (left unchanged):")
        for u in unresolved:
            print("  " + u)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
