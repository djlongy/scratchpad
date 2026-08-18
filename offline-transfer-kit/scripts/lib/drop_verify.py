#!/usr/bin/env python3
"""Write and verify drop/ SHA-256 sums + MANIFEST.json."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oci_images import collect_oci_archives, collect_oci_images

SKIP_HASH_NAMES = frozenset({"SHA256SUMS", "MANIFEST.json", "HIGH_SIDE_URLS.json", ".DS_Store"})
RPM_SKIP_DIRS = frozenset({"client-repos", "keys"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_drop_files(drop: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(drop.rglob("*")):
        if not path.is_file():
            continue
        if path.name in SKIP_HASH_NAMES:
            continue
        files.append(path)
    return files


def write_sha256sums(drop: Path) -> Path:
    lines = []
    for path in iter_drop_files(drop):
        rel = path.relative_to(drop).as_posix()
        lines.append(f"{sha256_file(path)}  {rel}")
    out = drop / "SHA256SUMS"
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out


def parse_sha256sums(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        digest, rel = line.split(None, 1)
        rel = rel.lstrip("*")
        entries.append((digest, rel))
    return entries


def verify_sha256sums(drop: Path, sums_path: Path | None = None) -> list[str]:
    """Return a list of failure strings (empty means OK)."""
    path = sums_path or (drop / "SHA256SUMS")
    if not path.is_file():
        return [f"missing {path}"]
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    bad: list[str] = []
    for digest, rel in parse_sha256sums(text):
        artifact = drop / rel
        if not artifact.is_file():
            bad.append(f"missing {rel}")
            continue
        got = sha256_file(artifact)
        if got != digest:
            bad.append(f"mismatch {rel}")
    return bad


def _count_files(path: Path, pattern: str) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.glob(pattern) if item.is_file())


def pypi_package_dir(drop: Path) -> Path:
    for candidate in (drop / "pypi" / "packages", drop / "artifacts" / "pypi" / "packages"):
        if candidate.is_dir():
            return candidate
    return drop / "pypi" / "packages"


def galaxy_collection_dir(drop: Path) -> Path:
    for candidate in (
        drop / "galaxy" / "collections",
        drop / "artifacts" / "galaxy" / "collections",
    ):
        if candidate.is_dir():
            return candidate
    return drop / "galaxy" / "collections"


def rpm_root(drop: Path) -> Path:
    for candidate in (drop / "rpm", drop / "artifacts" / "rpm"):
        if candidate.is_dir():
            return candidate
    return drop / "rpm"


def collect_pypi_packages(drop: Path) -> list[Path]:
    root = pypi_package_dir(drop)
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )


def collect_galaxy_tarballs(drop: Path) -> list[Path]:
    root = galaxy_collection_dir(drop)
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_file() and path.name.endswith(".tar.gz")
    )


def collect_rpm_files(drop: Path) -> list[tuple[str, Path]]:
    """Return (repo_id, rpm_path) pairs from drop/rpm/<id>/*.rpm or flat *.rpm."""
    root = rpm_root(drop)
    if not root.is_dir():
        return []
    pairs: list[tuple[str, Path]] = []
    for child in sorted(root.iterdir()):
        if child.is_dir():
            if child.name in RPM_SKIP_DIRS:
                continue
            for rpm in sorted(child.glob("*.rpm")):
                if rpm.is_file():
                    pairs.append((child.name, rpm))
        elif child.is_file() and child.name.endswith(".rpm"):
            pairs.append(("default", child))
    return pairs


def component_counts(drop: Path) -> dict[str, int]:
    rpm_n = len(collect_rpm_files(drop))
    oci_n = len(collect_oci_archives(drop))
    if oci_n == 0:
        oci_n = len(collect_oci_images(drop))
    return {
        "pypi": len(collect_pypi_packages(drop)),
        "rpm": rpm_n,
        "galaxy": len(collect_galaxy_tarballs(drop)),
        "oci": oci_n,
    }


def write_drop_manifest(drop: Path, release_id: str) -> dict[str, Any]:
    counts = component_counts(drop)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "otk-drop",
        "release_id": release_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pypi": counts["pypi"],
        "rpm": counts["rpm"],
        "galaxy": counts["galaxy"],
        "oci": counts["oci"],
        "counts": counts,
        "components": {
            "pypi_packages": counts["pypi"],
            "galaxy_collections": counts["galaxy"],
            "rpm_files": counts["rpm"],
            "oci_archives": counts["oci"],
        },
        "high_side": {
            "pypi": "pulp",
            "rpm": "pulp",
            "galaxy": "pulp",
            "oci": "pulp-or-harbor",
            "vuln_db": "optional-file",
        },
        "scanner": "grype",
    }
    (drop / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify_drop(drop: Path) -> dict[str, Any]:
    man_path = drop / "MANIFEST.json"
    if not man_path.is_file():
        raise SystemExit(f"missing MANIFEST.json under {drop}")
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    sums = drop / "SHA256SUMS"
    if sums.is_file() and sums.read_text(encoding="utf-8").strip():
        bad = verify_sha256sums(drop, sums)
        if bad:
            raise SystemExit("SHA256SUMS failed:\n  " + "\n  ".join(bad))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("drop", type=Path, nargs="?", help="drop/ directory")
    parser.add_argument("--write", action="store_true", help="Write SHA256SUMS + MANIFEST")
    parser.add_argument("--verify", action="store_true", help="Verify SHA256SUMS + MANIFEST")
    parser.add_argument("--release-id", default="local")
    args = parser.parse_args()
    drop = args.drop
    if drop is None:
        print("ERROR: drop path required", file=sys.stderr)
        return 2
    drop = drop.resolve()
    if args.write:
        drop.mkdir(parents=True, exist_ok=True)
        write_sha256sums(drop)
        manifest = write_drop_manifest(drop, args.release_id)
        print(json.dumps(manifest, indent=2))
        return 0
    if args.verify or not args.write:
        manifest = verify_drop(drop)
        print(json.dumps({"ok": True, "release_id": manifest.get("release_id")}, indent=2))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
