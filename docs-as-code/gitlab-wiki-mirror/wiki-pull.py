#!/usr/bin/env python3
"""Bring wiki edits back into docs/ (the wiki -> repo half of the bidirectional sync).

usage: wiki-pull.py DOCS_DIR WIKI_CLONE [--ci-author NAME] [--dry-run]
                    [--include P]... [--exclude P]... [--config mkdocs.yml]

wiki-sync.py mirrors docs/ into the wiki and commits as the CI author. Every wiki commit after
the last CI commit was made by a person in the wiki UI. This script walks those commits, turns
each changed page back into its docs/ form (the wiki-import.py rules: home.md -> index.md,
<section>.md beside a <section>/ folder -> <section>/index.md, links back to relative .md) and
merges it into docs/ three ways with `git merge-file`:

    base   = the page as CI last wrote it (the wiki tree at the last CI commit)
    ours   = the page in docs/ now (may carry repo edits made since that sync)
    theirs = the page in the wiki now

A clean merge keeps both sides. On conflict the newer edit wins: the wiki commit's author date
against the repo's last author date for that file. A page deleted in the wiki is deleted from
docs/ unless the repo edited it after the sync (then the repo copy stays and the next
wiki-sync restores the wiki page).

Each wiki commit becomes one repo commit carrying that wiki commit's author name, email and
date, so attribution follows the person who edited the wiki. Nothing is pushed here; the caller
(wiki-deploy.sh) pushes. Prints what it did; exit 0 (also when there is nothing to do), 1 on error.
"""
import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docfilter  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).resolve().parent / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wiki_import = load("wiki-import")

CI_AUTHOR = "docs ci"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
EPOCH = "1970-01-01T00:00:00+00:00"
INDEX = "index.md"


def git(repo, *args, env=None):
    # List form, no shell: every argument reaches git as one argv element, so a path or a
    # commit subject with shell characters cannot become a command. Inputs are repo paths
    # and git's own output; none come from the network.
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, shell=False)
    if result.returncode:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}:\n{result.stderr}")
    return result.stdout


def show(repo, rev, path):
    """File bytes at rev, or None if it does not exist there."""
    result = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True)
    return result.stdout if result.returncode == 0 else None


def when(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def last_ci_commit(wiki, ci_author):
    """(hash, committer date) of the newest CI commit in the wiki; the empty tree if there is none."""
    # --author matches "Name <email>"; anchor on the name so "ci" does not match "cindy".
    out = git(wiki, "log", "-1", "--format=%H%x00%cI", f"--author=^{ci_author} <").strip()
    return tuple(out.split("\x00")) if out else (EMPTY_TREE, EPOCH)


def human_commits(wiki, base):
    """(hash, name, email, author date, subject) for every wiki commit after base, oldest first."""
    rng = "HEAD" if base == EMPTY_TREE else f"{base}..HEAD"
    out = git(wiki, "log", "--reverse", "--format=%H%x00%an%x00%ae%x00%aI%x00%s", rng)
    return [tuple(line.split("\x00")) for line in out.splitlines() if line]


def changed_files(wiki, commit):
    """(status, path) per file in one wiki commit; renames come through as delete + add."""
    out = git(wiki, "show", "--name-status", "--format=", "--no-renames", commit)
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if line]


def merge_file(base, ours, theirs, label):
    """Three-way merge; returns (text, clean)."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, text in (("ours", ours), ("base", base or ""), ("theirs", theirs)):
            p = Path(tmp) / name
            p.write_text(text)
            paths.append(str(p))
        result = subprocess.run(["git", "merge-file", "-p", "-L", "repo", "-L", "last-sync", "-L", label, *paths],
                                capture_output=True, text=True)
        return result.stdout, result.returncode == 0


class Puller:
    def __init__(self, repo, docs, wiki, flt, ci_author, dry_run):
        self.repo, self.docs, self.wiki, self.flt = repo, docs, wiki, flt
        self.ci_author, self.dry_run = ci_author, dry_run
        self.base, self.base_date = last_ci_commit(wiki, ci_author)
        self.pulled = 0

    # --- repo side helpers ---------------------------------------------------------------
    def repo_rel(self, dest):
        return str(self.docs.relative_to(self.repo) / dest)

    def repo_edited_since_sync(self, dest):
        # Strictly after the CI commit: the repo commit CI synced from can share its second.
        out = git(self.repo, "log", "-1", "--format=%cI", "--", self.repo_rel(dest)).strip()
        return bool(out) and when(out) > when(self.base_date)

    def repo_last_edit(self, dest):
        out = git(self.repo, "log", "-1", "--format=%aI", "--", self.repo_rel(dest)).strip()
        return when(out) if out else when(EPOCH)

    def convert(self, text, rel):
        """Wiki page text -> (docs-relative path, docs-form text)."""
        dest = wiki_import.new_location(rel, self.wiki)
        unresolved = []
        out = wiki_import.rewrite(text, self.wiki / rel, self.wiki, dest, unresolved, self.flt)
        for u in unresolved:
            print(f"  unresolved link left as-is: {u}")
        return dest, out

    def docs_file_for(self, rel):
        """Where a (possibly deleted) wiki page lives in docs/, or None."""
        candidates = [wiki_import.new_location(rel, self.wiki), rel]
        if rel.parent == Path("."):
            candidates.insert(1, Path(rel.stem) / INDEX)
        return next((c for c in candidates if (self.docs / c).exists()), None)

    # --- one wiki commit -------------------------------------------------------------------
    def run(self):
        commits = human_commits(self.wiki, self.base)
        if not commits:
            print("wiki has no edits since the last sync")
            return
        for commit in commits:
            self.apply(*commit)
        print(f"{self.pulled} file(s) pulled from {len(commits)} wiki commit(s)")

    def apply(self, chash, name, email, date, subject):
        touched = []
        for status, path in changed_files(self.wiki, chash):
            rel = Path(path)
            if rel.name.startswith("_") or any(p.startswith(".") for p in rel.parts) or not self.flt.allows(rel):
                continue  # _sidebar.md, GitLab's .gitlab/redirects.yml and excluded paths are wiki furniture
            dest = self.pull_page(status, rel, chash, when(date), name) if rel.suffix == ".md" \
                else self.pull_attachment(status, rel, chash)
            if dest:
                touched.append(dest)
        if not touched:
            return
        self.pulled += len(touched)
        if self.dry_run:
            print(f"dry-run: would commit {len(touched)} file(s) as {name} <{email}>: {subject}")
            return
        git(self.repo, "add", "-A", "--", *[self.repo_rel(t) for t in touched])
        env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_DATE=date)
        git(self.repo, "commit", "-q", "-m", f"wiki: {subject}\n\nPulled from wiki commit {chash[:8]} by {name}.", env=env)
        print(f"committed {len(touched)} file(s) as {name} <{email}>: {subject}")

    def pull_page(self, status, rel, chash, date, who):
        if status == "D":
            return self.delete_page(rel, who)
        dest, theirs = self.convert(show(self.wiki, chash, str(rel)).decode(), rel)
        target = self.docs / dest
        ours = target.read_text() if target.exists() else None
        base_wiki = show(self.wiki, self.base, str(rel))
        base = self.convert(base_wiki.decode(), rel)[1] if base_wiki is not None else None
        outcome = self.reconcile(dest, base, ours, theirs, date, who)
        if outcome is None:
            return None
        merged, how = outcome
        print(f"pull   {rel} -> {dest} ({how})")
        if not self.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(merged)
        return dest

    def reconcile(self, dest, base, ours, theirs, date, who):
        """(text to write, how) for one page, or None when docs/ should stay as it is."""
        if ours == theirs:
            return None
        if ours is None:
            return theirs, "new"
        if base == ours:
            return theirs, "fast-forward"
        merged, clean = merge_file(base, ours, theirs, f"wiki ({who})")
        if clean:
            return merged, "merged with repo edits"
        if date >= self.repo_last_edit(dest):
            return theirs, "conflict, wiki edit is newer, wiki wins"
        print(f"keep   {dest}: conflict, repo edit is newer, repo wins")
        return None

    def delete_page(self, rel, who):
        dest = self.docs_file_for(rel)
        if dest is None:
            return None
        if self.repo_edited_since_sync(dest):
            print(f"keep   {dest}: deleted in wiki but edited in repo since the sync")
            return None
        print(f"delete {dest} (removed in wiki by {who})")
        if not self.dry_run:
            (self.docs / dest).unlink()
        return dest

    def pull_attachment(self, status, rel, chash):
        target = self.docs / rel
        if status == "D":
            if not target.exists():
                return None
            print(f"delete {rel}")
            if not self.dry_run:
                target.unlink()
            return rel
        data = show(self.wiki, chash, str(rel))
        if target.exists() and target.read_bytes() == data:
            return None
        print(f"pull   {rel} (attachment)")
        if not self.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return rel


def main(docs, wiki, flt=None, ci_author=CI_AUTHOR, dry_run=False, repo=None):
    flt = flt or docfilter.Filter()
    if not (wiki / ".git").exists():
        raise SystemExit(f"{wiki} is not a git clone of the wiki")
    if (docs / ".git").exists():
        raise SystemExit(f"{docs}/.git exists: docs/ must be a plain folder of the repo, not a nested repository")
    repo = Path(repo or git(docs.parent, "rev-parse", "--show-toplevel").strip()).resolve()
    Puller(repo, docs.resolve(), wiki.resolve(), flt, ci_author, dry_run).run()
    return 0


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("docs", type=Path)
    parser.add_argument("wiki", type=Path)
    parser.add_argument("--ci-author", default=CI_AUTHOR, help="author name wiki-sync.py commits as")
    parser.add_argument("--repo", type=Path, help="repo to commit into (default: the one containing DOCS_DIR)")
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    docfilter.add_arguments(parser)
    args = parser.parse_args(argv)
    sys.exit(main(args.docs.resolve(), args.wiki.resolve(), docfilter.from_args(args), args.ci_author, args.dry_run, args.repo))


if __name__ == "__main__":
    cli()
