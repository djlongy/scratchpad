#!/usr/bin/env python3
"""Mirror a docs/ tree into a GitLab wiki checkout and write _sidebar.md from the .pages nav.

usage: wiki-sync.py DOCS_DIR WIKI_DIR [--include P]... [--exclude P]... [--config mkdocs.yml]

- docs/index.md becomes home.md (the wiki front page); section/index.md becomes section.md
  so the wiki titles the page "section" instead of "index"; every other page keeps its path.
- Links stay relative to the page's own directory and keep the .md extension on pages,
  recomputed for the two pages that move (index.md -> home.md, section/index.md ->
  section.md). GitLab renders `other.md`, `../dir/page.md` and `../uploads/x/f.png` from
  any depth, and a clone of the wiki repo opens the same links in an editor. A bare
  extension-less sibling (`other`) is never written: GitLab resolves that from the wiki root.
- Attachments are copied across; pages and attachments no longer in docs/ are deleted.
- Sidebar order follows each folder's .pages `nav` list (awesome-pages format); `...`
  expands to the remaining pages, sorted. Folders without .pages are listed alphabetically.
- Filtering follows MkDocs: --exclude and exclude_docs skip paths entirely, --include keeps
  only matches, not_in_nav keeps the page but drops it from the sidebar (see docfilter.py).
"""
import argparse
import os
import re
import urllib.parse
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docfilter  # noqa: E402

LINK = re.compile(r"(\]\()([^)#\s]+?)(#[^)]*)?(\))")


def link_quote(path: str) -> str:
    """Percent-encode only what breaks a Markdown link target; unicode names stay readable."""
    return re.sub(r"[ %#?]", lambda m: urllib.parse.quote(m.group()), path)
INDEX = "index.md"
PAGES = ".pages"
REST = "..."


def title_of(md: Path) -> str:
    for line in md.read_text().splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return md.stem.replace("-", " ").title()


def wiki_path(rel: Path) -> str:
    if str(rel) == INDEX:
        return "home"
    if rel.name == INDEX:
        return str(rel.parent)
    return str(rel.with_suffix(""))


def wiki_file(rel: Path) -> Path:
    """Where a docs file lands in the wiki clone (pages move for home and section indexes)."""
    return Path(wiki_path(rel) + ".md") if rel.suffix == ".md" else rel


def rewrite_links(text: str, page: Path, root: Path, flt: docfilter.Filter) -> str:
    """Re-point every resolvable relative link so it is relative to the page's wiki location."""
    page_rel = page.relative_to(root)
    wiki_dir = wiki_file(page_rel).parent

    def sub(m):
        target = m.group(2)
        if "://" in target or target.startswith(("mailto:", "/")):
            return m.group(0)
        resolved = (page.parent / urllib.parse.unquote(target)).resolve()   # "my%20notes.md" is a file with a space
        try:
            rel = resolved.relative_to(root.resolve())
        except ValueError:
            return m.group(0)  # points outside docs/; leave it
        if not resolved.exists() or not flt.allows(rel):
            return m.group(0)
        new = link_quote(os.path.relpath(wiki_file(rel), wiki_dir))
        return f"{m.group(1)}{new}{m.group(3) or ''}{m.group(4)}"

    return LINK.sub(sub, text)


def pages_spec(folder: Path) -> dict:
    spec_file = folder / PAGES
    return (yaml.safe_load(spec_file.read_text()) if spec_file.exists() else None) or {}


def nav_items(folder: Path) -> list:
    """(title_or_None, path) per .pages nav entry, with the string REST kept as a marker."""
    items = []
    for item in pages_spec(folder).get("nav") or [REST]:
        if item == REST:
            items.append(REST)
        else:
            title, target = next(iter(item.items())) if isinstance(item, dict) else (None, item)
            items.append((title, folder / target))
    return items


def unlisted(folder: Path, listed: set) -> list:
    """Pages and page-bearing folders not named in .pages, sorted, for the REST marker."""
    return sorted(
        p for p in folder.iterdir()
        if p not in listed and not p.name.startswith(".")
        and (p.suffix == ".md" or (p.is_dir() and any(p.rglob("*.md"))))
    )


def nav_entries(folder: Path, root: Path, flt: docfilter.Filter) -> list:
    """(title, wiki_link_or_None, children) in .pages order for one folder."""
    items = nav_items(folder)
    rest = unlisted(folder, {path for item in items if item != REST for _, path in [item]})
    entries = []
    for item in items:
        for title, path in ([(None, p) for p in rest] if item == REST else [item]):
            entry = resolve(title, path, root, flt)
            if entry:
                entries.append(entry)
    return entries


def resolve(title, path: Path, root: Path, flt: docfilter.Filter):
    rel = path.relative_to(root)
    if not path.exists() or not flt.allows(rel) or not flt.in_nav(rel):
        return None
    if path.is_dir():
        index = path / INDEX
        fallback = title_of(index) if index.exists() else path.name.replace("-", " ").title()
        title = title or pages_spec(path).get("title") or fallback
        link = wiki_path(index.relative_to(root)) if index.exists() else None
        children = [c for c in nav_entries(path, root, flt) if c[1] != link]
        return (title, link, children) if link or children else None
    if path.suffix == ".md":
        return (title or title_of(path), wiki_path(rel), [])
    return None


def render(entries, depth=0) -> list:
    lines = []
    for title, link, children in entries:
        label = f"[{title}]({link}.md)" if link else title   # _sidebar.md sits at the root
        lines.append("  " * depth + f"- {label}")
        lines.extend(render(children, depth + 1))
    return lines


def clear(wiki: Path) -> None:
    """Remove every file except the repo metadata, then any directories left empty."""
    for old in wiki.rglob("*"):
        if ".git" not in old.parts and old.is_file():
            old.unlink()
    for empty in sorted((d for d in wiki.rglob("*") if d.is_dir() and ".git" not in d.parts), reverse=True):
        if not any(empty.iterdir()):
            empty.rmdir()


def copy_tree(docs: Path, wiki: Path, flt: docfilter.Filter) -> tuple:
    """Write pages (renamed, links rewritten) and copy attachments. Returns (pages, attachments)."""
    pages = attachments = 0
    for src in docs.rglob("*"):
        rel = src.relative_to(docs)
        # Hidden files AND hidden directories are skipped: .pages, .git, .cache, tool caches.
        # Copying a stray docs/.git or docs/.cache into the wiki clone would corrupt it.
        if not src.is_file() or any(part.startswith(".") for part in rel.parts) or not flt.allows(rel):
            continue
        if src.suffix == ".md":
            dst = wiki / wiki_file(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            # bytes in, bytes out: read_text() would turn CRLF pages into LF ones
            dst.write_bytes(rewrite_links(src.read_bytes().decode(), src, docs, flt).encode())
            pages += 1
        else:
            dst = wiki / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            attachments += 1
    return pages, attachments


def sidebar(docs: Path, flt: docfilter.Filter) -> list:
    home = docs / INDEX
    title = (title_of(home) if home.exists() else "") or "Home"
    entries = [e for e in nav_entries(docs, docs, flt) if e[1] != "home"]  # the header link already covers it
    return [f"**[{title}](home.md)**", ""] + render(entries)


def main(docs: Path, wiki: Path, flt: docfilter.Filter = None) -> None:
    flt = flt or docfilter.Filter()
    clear(wiki)
    pages, attachments = copy_tree(docs, wiki, flt)
    lines = sidebar(docs, flt)
    (wiki / "_sidebar.md").write_text("\n".join(lines) + "\n")
    print(f"synced {pages} pages, {attachments} attachments, sidebar {len(lines) - 2} lines")


def cli(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("docs", type=Path)
    parser.add_argument("wiki", type=Path)
    docfilter.add_arguments(parser)
    args = parser.parse_args(argv)
    main(args.docs.resolve(), args.wiki.resolve(), docfilter.from_args(args))


if __name__ == "__main__":
    cli()
