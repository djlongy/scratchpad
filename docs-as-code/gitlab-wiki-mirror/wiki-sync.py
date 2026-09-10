#!/usr/bin/env python3
"""Mirror a docs/ tree into a GitLab wiki checkout and write _sidebar.md from the .pages nav.

usage: wiki-sync.py DOCS_DIR WIKI_DIR

- docs/index.md becomes home.md (the wiki front page); section/index.md becomes section.md
  so the wiki titles the page "section" instead of "index"; every other page keeps its path.
- Links to *.md are rewritten to root-absolute wiki paths (/section/page). GitLab resolves
  relative wiki links against the wiki root, not the page's directory, so relative links
  only work from top-level pages; root-absolute ones work from anywhere.
- Links to attachments (any non-Markdown file under docs/, e.g. uploads/) are rewritten to
  the wiki-root-relative form GitLab itself uses (uploads/abc/file.png, no leading slash).
  A leading slash would point at the project's uploads instead of the wiki's.
- Attachments are copied across; pages and attachments no longer in docs/ are deleted.
- Sidebar order follows each folder's .pages `nav` list (awesome-pages format); `...`
  expands to the remaining pages, sorted. Folders without .pages are listed alphabetically.
"""
import re
import shutil
import sys
from pathlib import Path

import yaml

LINK = re.compile(r"(\]\()([^)#\s]+?)(#[^)]*)?(\))")
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


def rewrite_links(text: str, page: Path, root: Path) -> str:
    def sub(m):
        target = m.group(2)
        if "://" in target or target.startswith(("mailto:", "/")):
            return m.group(0)
        resolved = (page.parent / target).resolve()
        try:
            rel = resolved.relative_to(root.resolve())
        except ValueError:
            return m.group(0)  # points outside docs/; leave it
        if not resolved.exists():
            return m.group(0)
        new = "/" + wiki_path(rel) if rel.suffix == ".md" else str(rel)
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


def nav_entries(folder: Path, root: Path) -> list:
    """(title, wiki_link_or_None, children) in .pages order for one folder."""
    items = nav_items(folder)
    rest = unlisted(folder, {path for item in items if item != REST for _, path in [item]})
    entries = []
    for item in items:
        for title, path in ([(None, p) for p in rest] if item == REST else [item]):
            entry = resolve(title, path, root)
            if entry:
                entries.append(entry)
    return entries


def resolve(title, path: Path, root: Path):
    if path.is_dir():
        index = path / INDEX
        fallback = title_of(index) if index.exists() else path.name.replace("-", " ").title()
        title = title or pages_spec(path).get("title") or fallback
        link = wiki_path(index.relative_to(root)) if index.exists() else None
        children = [c for c in nav_entries(path, root) if c[1] != link]
        return (title, link, children)
    if path.suffix == ".md" and path.exists():
        return (title or title_of(path), wiki_path(path.relative_to(root)), [])
    return None


def render(entries, depth=0) -> list:
    lines = []
    for title, link, children in entries:
        label = f"[{title}](/{link})" if link else title
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


def copy_tree(docs: Path, wiki: Path) -> tuple:
    """Write pages (renamed, links rewritten) and copy attachments. Returns (pages, attachments)."""
    pages = attachments = 0
    for src in docs.rglob("*"):
        if not src.is_file() or src.name.startswith("."):
            continue
        rel = src.relative_to(docs)
        if src.suffix == ".md":
            dst = wiki / (wiki_path(rel) + ".md")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(rewrite_links(src.read_text(), src, docs))
            pages += 1
        else:
            dst = wiki / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            attachments += 1
    return pages, attachments


def sidebar(docs: Path) -> list:
    home = docs / INDEX
    title = (title_of(home) if home.exists() else "") or "Home"
    entries = [e for e in nav_entries(docs, docs) if e[1] != "home"]  # the header link already covers it
    return [f"**[{title}](/home)**", ""] + render(entries)


def main(docs: Path, wiki: Path) -> None:
    clear(wiki)
    pages, attachments = copy_tree(docs, wiki)
    lines = sidebar(docs)
    (wiki / "_sidebar.md").write_text("\n".join(lines) + "\n")
    print(f"synced {pages} pages, {attachments} attachments, sidebar {len(lines) - 2} lines")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
