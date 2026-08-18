#!/usr/bin/env python3
"""Resolve the full pip dependency tree into a drop and prove it is closed.

`pip download` already walks Requires-Dist (not --no-deps). This module:
  * downloads the host tree (wheels + sdists)
  * downloads a manylinux/CPython tree per high-side Python (39–312)
  * fails if any target tree is incomplete
  * dry-runs `pip install --no-index --find-links` per target so a missing
    transitive pin cannot slip through
  * writes meta/provenance/pypi-tree.json and pypi/requirements.lock
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_PY_TAGS = ("39", "310", "311", "312")
DEFAULT_PLATFORMS = (
    "manylinux2014_x86_64",
    "manylinux_2_17_x86_64",
    "manylinux_2_28_x86_64",
)
SCHEMA_VERSION = 1


def log(msg: str) -> None:
    print(f"[pypi-resolve] {msg}", flush=True)


def pip_cmd(args: list[str]) -> list[str]:
    return [sys.executable, "-m", "pip", *args]


def run_pip(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = pip_cmd(args)
    log("+ " + " ".join(cmd))
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def require_ok(proc: subprocess.CompletedProcess[str], label: str) -> None:
    if proc.returncode == 0:
        return
    tail = (proc.stderr or proc.stdout or "").strip()
    raise SystemExit(f"{label} failed (rc={proc.returncode}):\n{tail}")


def platform_flags(py_tag: str) -> list[str]:
    flags = [
        "--python-version",
        py_tag,
        "--implementation",
        "cp",
        "--abi",
        f"cp{py_tag}",
        "--only-binary=:all:",
    ]
    for plat in DEFAULT_PLATFORMS:
        flags.extend(["--platform", plat])
    return flags


def download_host(req: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    proc = run_pip(
        ["download", "-r", str(req), "-d", str(dest), "--no-cache-dir", "--exists-action", "i"]
    )
    require_ok(proc, "pip download (host / sdists + native wheels)")


def download_linux(req: Path, dest: Path, py_tag: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    args = [
        "download",
        "-r",
        str(req),
        "-d",
        str(dest),
        "--no-cache-dir",
        "--exists-action",
        "i",
        *platform_flags(py_tag),
    ]
    proc = run_pip(args)
    require_ok(proc, f"pip download (linux/cp{py_tag} + transitive deps)")


def install_report_args(req: Path, dest: Path, report: Path, py_tag: str | None) -> list[str]:
    args = [
        "install",
        "--dry-run",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        str(dest),
        "--report",
        str(report),
        "-r",
        str(req),
    ]
    if py_tag:
        args.extend(platform_flags(py_tag))
    return args


def packages_from_report(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in payload.get("install") or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        name = str(meta.get("name") or "")
        version = str(meta.get("version") or "")
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "version": version,
                "requested": bool(item.get("requested")),
            }
        )
    return rows


def assert_closed(req: Path, dest: Path, report: Path, py_tag: str | None) -> list[dict[str, Any]]:
    label = f"linux/cp{py_tag}" if py_tag else "host"
    proc = run_pip(install_report_args(req, dest, report, py_tag))
    require_ok(
        proc,
        f"pip install --dry-run --no-index closure ({label})",
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    rows = packages_from_report(payload)
    if not rows:
        raise SystemExit(f"closure ({label}) produced an empty install plan")
    log(f"closure {label}: {len(rows)} node(s)")
    return rows


def closed_lock_rows(rows_by_target: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """One installable pin set. Host is what `pip install -r lock` on the builder uses."""
    host_rows = rows_by_target.get("host") or []
    if host_rows:
        return host_rows
    for rows in rows_by_target.values():
        if rows:
            return rows
    return []


def write_lock(rows: list[dict[str, Any]], lock_path: Path) -> None:
    pins: dict[str, str] = {}
    for row in rows:
        key = row["name"].lower()
        if key in pins:
            continue
        pins[key] = f"{row['name']}=={row['version']}"
    lines = ["# Fully resolved by pypi_resolve.py — do not edit by hand"]
    lines.extend(pins[name] for name in sorted(pins))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_tree(
    req: Path,
    dest: Path,
    rows_by_target: dict[str, list[dict[str, Any]]],
    tree_path: Path,
) -> None:
    tree = {
        "schema_version": SCHEMA_VERSION,
        "requirements": str(req),
        "dest": str(dest),
        "targets": {
            name: {"count": len(rows), "packages": rows} for name, rows in rows_by_target.items()
        },
    }
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    tree_path.write_text(json.dumps(tree, indent=2) + "\n", encoding="utf-8")


def resolve(
    req: Path,
    dest: Path,
    tree_path: Path,
    lock_path: Path,
    py_tags: tuple[str, ...] = DEFAULT_PY_TAGS,
) -> dict[str, list[dict[str, Any]]]:
    if not req.is_file():
        raise SystemExit(f"requirements missing: {req}")
    dest.mkdir(parents=True, exist_ok=True)
    work = tree_path.parent / "pypi-resolve"
    work.mkdir(parents=True, exist_ok=True)

    download_host(req, dest)
    for tag in py_tags:
        download_linux(req, dest, tag)

    rows_by_target: dict[str, list[dict[str, Any]]] = {}
    rows_by_target["host"] = assert_closed(req, dest, work / "host.json", None)
    for tag in py_tags:
        rows_by_target[f"linux-cp{tag}"] = assert_closed(
            req, dest, work / f"linux-cp{tag}.json", tag
        )

    write_tree(req, dest, rows_by_target, tree_path)
    write_lock(closed_lock_rows(rows_by_target), lock_path)
    log(f"wrote {tree_path}")
    log(f"wrote {lock_path}")
    return rows_by_target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument(
        "--py-tags",
        default=",".join(DEFAULT_PY_TAGS),
        help="Comma list of CPython tags (default 39,310,311,312)",
    )
    args = parser.parse_args()
    tags = tuple(part.strip() for part in args.py_tags.split(",") if part.strip())
    resolve(args.requirements, args.dest, args.tree, args.lock, py_tags=tags)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
