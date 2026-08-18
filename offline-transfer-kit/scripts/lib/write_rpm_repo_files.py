#!/usr/bin/env python3
"""Materialize dnf .repo files and offline client snippets from catalog/rpm/repos.yml.

Minimal YAML reader for the fixed shape used by OTK (no PyYAML required):

  repos:
    <id>:
      description: "..."
      baseurl: "https://..."
      metalink: "..."   # optional
      gpgkey: "https://..."
      enabled: true|false

Writes:
  --dnf-dir   → <id>.repo for low-side dnf (only enabled repos with baseurl/metalink)
  --offline-dir → offline-<id>.repo.example for high-side clients (placeholder base URL)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def parse_repos_yml(text: str) -> dict[str, dict[str, str | bool]]:
    """Tiny subset parser — good enough for OTK repos.yml."""
    repos: dict[str, dict[str, str | bool]] = {}
    current: str | None = None
    in_repos = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if re.match(r"^repos:\s*$", line):
            in_repos = True
            continue
        if not in_repos:
            continue
        # top-level empty map
        if re.match(r"^\s*\{\}\s*$", line):
            continue
        m_id = re.match(r"^  ([A-Za-z0-9._-]+):\s*$", line)
        if m_id:
            current = m_id.group(1)
            repos[current] = {}
            continue
        if current is None:
            continue
        m_kv = re.match(
            r"^    ([A-Za-z0-9_]+):\s*(.*)$",
            line,
        )
        if not m_kv:
            continue
        key, val = m_kv.group(1), m_kv.group(2).strip()
        if val.startswith(("'", '"')) and val.endswith(("'", '"')) and len(val) >= 2:
            val = val[1:-1]
        if key == "enabled":
            repos[current][key] = val.lower() in ("true", "yes", "1", "on")
        else:
            repos[current][key] = val
    return repos


def write_dnf_repo(path: Path, repo_id: str, meta: dict[str, str | bool]) -> None:
    desc = str(meta.get("description") or repo_id)
    enabled = "1" if meta.get("enabled", True) else "0"
    lines = [
        f"[{repo_id}]",
        f"name={desc}",
        f"enabled={enabled}",
        "gpgcheck=0",
        "module_hotfixes=1",
    ]
    if meta.get("baseurl"):
        lines.append(f"baseurl={meta['baseurl']}")
    if meta.get("metalink"):
        lines.append(f"metalink={meta['metalink']}")
    if meta.get("gpgkey"):
        lines.append(f"gpgkey={meta['gpgkey']}")
        # still leave gpgcheck=0 for low-side download reliability; keys are staged separately
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_offline_example(
    path: Path, repo_id: str, meta: dict[str, str | bool], base_url: str
) -> None:
    desc = str(meta.get("description") or repo_id)
    base = base_url.rstrip("/")
    lines = [
        f"# High-side client snippet — install under /etc/yum.repos.d/",
        f"# Replace baseurl host or use file:///srv/offline/rpm/{repo_id}",
        f"[offline-{repo_id}]",
        f"name=Offline {desc}",
        f"baseurl={base}/rpm/{repo_id}/",
        "enabled=1",
        "gpgcheck=0",
        "module_hotfixes=1",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repos_yml", type=Path)
    ap.add_argument("--dnf-dir", type=Path, help="Write low-side dnf .repo files here")
    ap.add_argument(
        "--offline-dir",
        type=Path,
        help="Write offline-*.repo.example client snippets here",
    )
    ap.add_argument(
        "--base-url",
        default="https://pkg.example.invalid",
        help="Placeholder high-side package base URL for offline snippets",
    )
    ap.add_argument(
        "--list-enabled",
        action="store_true",
        help="Print enabled repo ids (one per line) and exit",
    )
    args = ap.parse_args()

    if not args.repos_yml.is_file():
        print(f"WARN: no repos.yml at {args.repos_yml}", file=sys.stderr)
        return 0

    repos = parse_repos_yml(args.repos_yml.read_text(encoding="utf-8"))
    if args.list_enabled:
        for rid, meta in sorted(repos.items()):
            if meta.get("enabled", True) and (meta.get("baseurl") or meta.get("metalink")):
                print(rid)
        return 0

    n = 0
    if args.dnf_dir:
        args.dnf_dir.mkdir(parents=True, exist_ok=True)
        for rid, meta in repos.items():
            if not meta.get("enabled", True):
                continue
            if not (meta.get("baseurl") or meta.get("metalink")):
                print(f"WARN: repo {rid} has no baseurl/metalink — skip", file=sys.stderr)
                continue
            write_dnf_repo(args.dnf_dir / f"{rid}.repo", rid, meta)
            n += 1
            print(f"OK: dnf repo {rid} → {args.dnf_dir / f'{rid}.repo'}")

    if args.offline_dir:
        args.offline_dir.mkdir(parents=True, exist_ok=True)
        # Always emit offline snippet per package-list style id if in repos map;
        # also allow empty map (caller may write generic snippets separately).
        for rid, meta in repos.items():
            write_offline_example(
                args.offline_dir / f"offline-{rid}.repo.example",
                rid,
                meta,
                args.base_url,
            )
            n += 1
            print(f"OK: offline snippet {rid}")

    if n == 0 and not args.list_enabled:
        print("OK: no repo files written (empty or disabled map)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
