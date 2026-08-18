#!/usr/bin/env python3
"""Build a PEP 503 simple index from a flat packages directory.

Layout produced under --root:
  packages/   (existing wheels/sdists — left in place or copied)
  simple/<project>/index.html
  simple/index.html

Usage:
  make_simple_index.py /path/to/artifacts/pypi
  # expects packages already in <root>/packages/
"""
from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
from collections import defaultdict
from pathlib import Path


_WHEEL_RE = re.compile(
    r"^(?P<name>.+?)-(?P<ver>\d[^-]*)-.*\.(whl)$",
    re.IGNORECASE,
)
_SDIST_RE = re.compile(
    r"^(?P<name>.+?)-(?P<ver>\d.*)\.(tar\.gz|tar\.bz2|tar\.xz|zip)$",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    """PEP 503 normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def project_from_filename(filename: str) -> str | None:
    m = _WHEEL_RE.match(filename)
    if m:
        return normalize_name(m.group("name"))
    m = _SDIST_RE.match(filename)
    if m:
        return normalize_name(m.group("name"))
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_index(path: Path, title: str, links: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "<!DOCTYPE html>",
        "<html><head><meta charset=\"utf-8\">",
        f"<title>{html.escape(title)}</title></head><body>",
        f"<h1>{html.escape(title)}</h1>",
    ]
    for href, text in sorted(links, key=lambda x: x[1].lower()):
        lines.append(f'<a href="{html.escape(href)}">{html.escape(text)}</a><br/>')
    lines.append("</body></html>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "root",
        type=Path,
        help="PyPI artifact root (contains packages/)",
    )
    ap.add_argument(
        "--packages-subdir",
        default="packages",
        help="Subdir under root holding wheels/sdists (default: packages)",
    )
    args = ap.parse_args()
    root: Path = args.root.resolve()
    pkg_dir = root / args.packages_subdir
    if not pkg_dir.is_dir():
        print(f"ERROR: package dir not found: {pkg_dir}", file=sys.stderr)
        return 1

    by_project: dict[str, list[str]] = defaultdict(list)
    digests: dict[str, str] = {}
    for f in sorted(pkg_dir.iterdir()):
        if not f.is_file():
            continue
        if f.name.startswith("."):
            continue
        proj = project_from_filename(f.name)
        if not proj:
            print(f"WARN: skip unrecognized filename: {f.name}", file=sys.stderr)
            continue
        by_project[proj].append(f.name)
        digests[f.name] = sha256_file(f)

    if not by_project:
        print(f"WARN: no packages found in {pkg_dir}", file=sys.stderr)

    simple = root / "simple"
    root_links: list[tuple[str, str]] = []
    for proj, files in sorted(by_project.items()):
        links = []
        for fn in files:
            # PEP 503: relative URL + optional #sha256= fragment for integrity
            href = f"../../{args.packages_subdir}/{fn}#sha256={digests[fn]}"
            links.append((href, fn))
        write_index(simple / proj / "index.html", proj, links)
        root_links.append((f"{proj}/", proj))

    write_index(simple / "index.html", "Simple Index", root_links)
    print(f"OK: simple index for {len(by_project)} project(s) under {simple}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
