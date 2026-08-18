#!/usr/bin/env python3
"""Parse OTK catalog lists (PyPI, RPM, Galaxy, OCI) without extra dependencies."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from oci_images import parse_image_lines
from write_rpm_repo_files import parse_repos_yml


def parse_pin_lines(text: str) -> list[str]:
    """Return non-comment, non-blank requirement or RPM package lines."""
    pins: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pins.append(line)
    return pins


def parse_galaxy_requirements(text: str) -> list[dict[str, str]]:
    """Tiny YAML subset: collections: - name: ... / version: ..."""
    collections: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    in_collections = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if re.match(r"^collections:\s*$", line):
            in_collections = True
            continue
        if not in_collections:
            continue
        name_m = re.match(r"^\s*-\s*name:\s*(.+?)\s*$", line)
        if name_m:
            if current:
                collections.append(current)
            name = name_m.group(1).strip()
            if name.startswith(("'", '"')) and name.endswith(("'", '"')) and len(name) >= 2:
                name = name[1:-1]
            current = {"name": name}
            continue
        if current is None:
            continue
        kv_m = re.match(r"^\s+([A-Za-z0-9_]+):\s*(.+?)\s*$", line)
        if not kv_m:
            continue
        key, val = kv_m.group(1), kv_m.group(2).strip()
        if val.startswith(("'", '"')) and val.endswith(("'", '"')) and len(val) >= 2:
            val = val[1:-1]
        current[key] = val
    if current:
        collections.append(current)
    return collections


def load_rpm_lists(rpm_dir: Path) -> dict[str, list[str]]:
    packages: dict[str, list[str]] = {}
    if not rpm_dir.is_dir():
        return packages
    for list_path in sorted(rpm_dir.glob("*.txt")):
        pins = parse_pin_lines(list_path.read_text(encoding="utf-8"))
        if pins:
            packages[list_path.stem] = pins
    return packages


def load_catalog(catalog_dir: Path) -> dict[str, Any]:
    """Load pip / rpm / galaxy / oci allowlists from a catalog root."""
    pypi_path = catalog_dir / "pypi" / "requirements.txt"
    galaxy_path = catalog_dir / "galaxy" / "requirements.yml"
    repos_path = catalog_dir / "rpm" / "repos.yml"
    images_path = catalog_dir / "images" / "images.txt"

    pypi = parse_pin_lines(pypi_path.read_text(encoding="utf-8")) if pypi_path.is_file() else []
    galaxy = (
        parse_galaxy_requirements(galaxy_path.read_text(encoding="utf-8"))
        if galaxy_path.is_file()
        else []
    )
    rpm_packages = load_rpm_lists(catalog_dir / "rpm")
    repos: dict[str, dict[str, str | bool]] = {}
    if repos_path.is_file():
        repos = parse_repos_yml(repos_path.read_text(encoding="utf-8"))
    oci = (
        parse_image_lines(images_path.read_text(encoding="utf-8"))
        if images_path.is_file()
        else []
    )

    rpm_count = sum(len(v) for v in rpm_packages.values())
    return {
        "catalog": str(catalog_dir),
        "pypi": pypi,
        "rpm": {"packages": rpm_packages, "repos": repos},
        "galaxy": galaxy,
        "oci": oci,
        "counts": {
            "pypi": len(pypi),
            "rpm": rpm_count,
            "galaxy": len(galaxy),
            "oci": len(oci),
        },
    }


def require_components(catalog: dict[str, Any], names: list[str]) -> None:
    counts = catalog.get("counts") or {}
    missing = [name for name in names if int(counts.get(name) or 0) <= 0]
    if missing:
        raise SystemExit(
            "catalog missing required list entries: " + ", ".join(missing)
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="Print parsed catalog as JSON")
    parser.add_argument(
        "--require",
        default="",
        help="Comma list of components that must have entries (pypi,rpm,galaxy,oci)",
    )
    args = parser.parse_args()
    if not args.catalog.is_dir():
        print(f"ERROR: catalog dir missing: {args.catalog}", file=sys.stderr)
        return 1
    catalog = load_catalog(args.catalog)
    required = [part.strip() for part in args.require.split(",") if part.strip()]
    if required:
        require_components(catalog, required)
    if args.json:
        print(json.dumps(catalog, indent=2))
    else:
        counts = catalog["counts"]
        print(
            f"pypi={counts['pypi']} rpm={counts['rpm']} "
            f"galaxy={counts['galaxy']} oci={counts['oci']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
