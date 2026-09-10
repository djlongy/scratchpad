"""Include/exclude filtering for wiki-sync.py and wiki-import.py, matching MkDocs' rules.

Patterns are gitignore-style, the same form MkDocs accepts in `exclude_docs` and `not_in_nav`:

    drafts/            a folder anywhere, and everything under it
    *.tmp              a name anywhere
    /internal/*.md     anchored at the docs root
    reference/**       everything under a folder

Sources, all optional and merged: `--include PATTERN` / `--exclude PATTERN` (repeatable) and
`--config mkdocs.yml`, whose `exclude_docs` and `not_in_nav` blocks are read so one file
governs the site, the wiki and the import. Excluded paths are neither copied nor linked;
`not_in_nav` paths are copied but left out of the wiki sidebar, as MkDocs leaves them out of
its nav.
"""
import argparse
import fnmatch
from pathlib import Path

import yaml


def matches(rel: str, pattern: str) -> bool:
    """Does the docs-relative posix path match one gitignore-style pattern?"""
    pattern = pattern.strip()
    if not pattern or pattern.startswith("#"):
        return False
    pattern = pattern.rstrip("/")
    parts = rel.split("/")
    if "/" not in pattern:  # a bare name matches that name at any depth, files and folders alike
        return any(fnmatch.fnmatchcase(part, pattern) for part in parts)
    pattern = pattern.lstrip("/")
    return fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(rel, pattern + "/*")


def patterns(block) -> list:
    """MkDocs writes these as a multi-line string; a YAML list works too."""
    if not block:
        return []
    lines = block.splitlines() if isinstance(block, str) else list(block)
    return [line for line in (ln.strip() for ln in lines) if line and not line.startswith("#")]


def read_mkdocs(config: Path) -> tuple:
    """(exclude_docs, not_in_nav) from mkdocs.yml. BaseLoader keeps the !!python/name tags MkDocs
    configs carry from breaking the parse; every value comes back as a string, which is all we read."""
    data = yaml.load(config.read_text(), Loader=yaml.BaseLoader) or {}
    return patterns(data.get("exclude_docs")), patterns(data.get("not_in_nav"))


class Filter:
    def __init__(self, include=(), exclude=(), not_in_nav=()):
        self.include, self.exclude, self.not_in_nav = list(include), list(exclude), list(not_in_nav)

    def allows(self, rel: Path) -> bool:
        posix = rel.as_posix()
        if self.include and not any(matches(posix, p) for p in self.include):
            return False
        return not any(matches(posix, p) for p in self.exclude)

    def in_nav(self, rel: Path) -> bool:
        posix = rel.as_posix()
        return not any(matches(posix, p) for p in self.not_in_nav)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--include", action="append", default=[], metavar="PATTERN",
                        help="only paths matching (repeatable); gitignore-style, relative to the docs root")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                        help="skip paths matching (repeatable)")
    parser.add_argument("--config", type=Path, metavar="MKDOCS_YML",
                        help="also apply exclude_docs and not_in_nav from this mkdocs.yml")


def from_args(args: argparse.Namespace) -> Filter:
    exclude, not_in_nav = list(args.exclude), []
    if args.config:
        cfg_exclude, cfg_nav = read_mkdocs(args.config)
        exclude += cfg_exclude
        not_in_nav += cfg_nav
    return Filter(args.include, exclude, not_in_nav)
