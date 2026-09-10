#!/usr/bin/env python3
"""Mirror docs/ into a GitLab wiki checkout and write _sidebar.md from the .pages nav.

usage: wiki-sync.py DOCS_DIR WIKI_DIR

- docs/index.md becomes home.md (the wiki front page); section/index.md becomes section.md
  so the wiki title reads "section" instead of "index"; every other page keeps its path.
- Links to *.md are rewritten to root-absolute wiki paths (/section/page), which render the
  same in the web UI and the API. Relative links are resolved against the wiki root by
  GitLab, so they only work from top-level pages.
- Sidebar order follows each folder's .pages `nav` list; `...` expands to the remaining pages.
"""
import re
import shutil
import sys
from pathlib import Path

import yaml

LINK = re.compile(r"(\]\()([^)#\s]+?)\.md(#[^)]*)?(\))")


def title_of(md: Path) -> str:
    for line in md.read_text().splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return md.stem.replace("-", " ").title()


def wiki_path(rel: Path) -> str:
    if str(rel) == "index.md":
        return "home"
    if rel.name == "index.md":
        return str(rel.parent)
    return str(rel.with_suffix(""))


def rewrite_links(text: str, page: Path, root: Path) -> str:
    def sub(m):
        target = (page.parent / (m.group(2) + ".md")).resolve()
        new = "/" + wiki_path(target.relative_to(root.resolve()))
        return f"{m.group(1)}{new}{m.group(3) or ''}{m.group(4)}"

    return LINK.sub(sub, text)


def nav_entries(folder: Path, root: Path):
    """Yield (title, rel_path_or_None, children) in .pages order for one folder."""
    pages = folder / ".pages"
    spec = yaml.safe_load(pages.read_text()) if pages.exists() else {}
    nav = spec.get("nav") or ["..."]
    listed, out = set(), []
    for item in nav:
        if item == "...":
            out.append("...")
            continue
        title, target = (next(iter(item.items())) if isinstance(item, dict) else (None, item))
        path = folder / target
        listed.add(path)
        out.append((title, path))
    rest = sorted(p for p in folder.iterdir() if p not in listed and not p.name.startswith(".")
                  and (p.suffix == ".md" or p.is_dir()))
    entries = []
    for e in out:
        for title, path in (rest_items(rest) if e == "..." else [e]):
            entries.append(resolve(title, path, root))
    return [e for e in entries if e]


def rest_items(rest):
    return [(None, p) for p in rest]


def resolve(title, path: Path, root: Path):
    if path.is_dir():
        spec_file = path / ".pages"
        spec = yaml.safe_load(spec_file.read_text()) if spec_file.exists() else {}
        title = title or spec.get("title") or path.name.replace("-", " ").title()
        index = path / "index.md"
        link = wiki_path(index.relative_to(root)) if index.exists() else None
        children = [c for c in nav_entries(path, root) if c[1] != link]
        return (title, link, children)
    if path.suffix == ".md" and path.exists():
        return (title or title_of(path), wiki_path(path.relative_to(root)), [])
    return None


def render(entries, depth=0):
    lines = []
    for title, link, children in entries:
        label = f"[{title}](/{link})" if link else title
        lines.append("  " * depth + f"- {label}")
        lines.extend(render(children, depth + 1))
    return lines


def main(docs: Path, wiki: Path):
    for old in wiki.rglob("*.md"):
        if ".git" not in old.parts:
            old.unlink()
    for src in docs.rglob("*.md"):
        rel = src.relative_to(docs)
        dst = wiki / (wiki_path(rel) + ".md")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(rewrite_links(src.read_text(), src, docs))
    for empty in sorted((d for d in wiki.rglob("*") if d.is_dir() and ".git" not in d.parts), reverse=True):
        if not any(empty.iterdir()):
            empty.rmdir()
    home = docs / "index.md"
    title = title_of(home) if home.exists() else "Home"
    entries = [e for e in nav_entries(docs, docs) if e[1] != "home"]  # the header link already covers it
    sidebar = [f"**[{title}](/home)**", ""] + render(entries)
    (wiki / "_sidebar.md").write_text("\n".join(sidebar) + "\n")
    print(f"synced {sum(1 for _ in wiki.rglob('*.md'))} pages, sidebar {len(sidebar) - 2} lines")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
