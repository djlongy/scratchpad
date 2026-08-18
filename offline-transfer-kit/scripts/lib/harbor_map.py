#!/usr/bin/env python3
"""Map public container refs → Harbor destination refs (OTK Phase 4).

Uses config/release.yml harbor.rewrite + project, or CLI overrides.

  docker.io/library/alpine:3.20
    → <harbor_host>/<project>/docker-hub/library/alpine:3.20

Archive safe names (build convention):
  '/' → '__' , ':' → '--'
  docker.io__library__alpine--3.20.tar
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


DEFAULT_REWRITE = {
    "docker.io": "docker-hub",
    "ghcr.io": "ghcr",
    "quay.io": "quay",
    "registry.k8s.io": "k8s",
}


def load_release_yml(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {}
    # Minimal YAML subset: only harbor.project / harbor.rewrite / harbor.url
    text = path.read_text(encoding="utf-8")
    out: dict = {"rewrite": dict(DEFAULT_REWRITE), "project": "airgap", "url": ""}
    in_harbor = False
    in_rewrite = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if re.match(r"^harbor:\s*$", line):
            in_harbor = True
            in_rewrite = False
            continue
        if in_harbor and re.match(r"^[A-Za-z]", line):
            # left harbor block
            in_harbor = False
            in_rewrite = False
        if not in_harbor:
            continue
        if re.match(r"^\s+rewrite:\s*$", line):
            in_rewrite = True
            out["rewrite"] = {}
            continue
        m_url = re.match(r'^\s+url:\s*["\']?([^"\']+)["\']?\s*$', line)
        if m_url and not in_rewrite:
            out["url"] = m_url.group(1).strip()
            continue
        m_proj = re.match(r'^\s+project:\s*["\']?([^"\']+)["\']?\s*$', line)
        if m_proj and not in_rewrite:
            out["project"] = m_proj.group(1).strip()
            continue
        if in_rewrite:
            m_rw = re.match(
                r'^\s+["\']?([A-Za-z0-9._-]+)["\']?\s*:\s*["\']?([A-Za-z0-9._-]+)["\']?\s*$',
                line,
            )
            if m_rw:
                out["rewrite"][m_rw.group(1)] = m_rw.group(2)
            elif re.match(r"^\s+[A-Za-z]", line) and not line.strip().startswith("#"):
                in_rewrite = False
    if not out.get("rewrite"):
        out["rewrite"] = dict(DEFAULT_REWRITE)
    return out


def split_ref(ref: str) -> tuple[str, str, str]:
    """Return (registry, path, tag_or_digest). Default registry docker.io."""
    ref = ref.strip()
    if ref.startswith("docker://"):
        ref = ref[len("docker://") :]
    # digest form @sha256:
    tag = "latest"
    if "@sha256:" in ref:
        path_part, dig = ref.split("@", 1)
        tag = dig  # includes sha256:...
    elif ":" in ref.rsplit("/", 1)[-1]:
        path_part, tag = ref.rsplit(":", 1)
    else:
        path_part = ref
    # registry?
    first, _, rest = path_part.partition("/")
    if "." in first or ":" in first or first == "localhost":
        registry = first
        path = rest or first
        if not rest:
            # unusual
            path = first
            registry = "docker.io"
    else:
        registry = "docker.io"
        path = path_part
        # docker hub library shorthand
        if "/" not in path:
            path = f"library/{path}"
    return registry, path, tag


def safe_archive_name(ref: str) -> str:
    registry, path, tag = split_ref(ref)
    full = f"{registry}/{path}:{tag}" if not tag.startswith("sha256:") else f"{registry}/{path}@{tag}"
    # encode
    s = full.replace("/", "__").replace(":", "--").replace("@", "_AT_")
    return f"{s}.tar"


def parse_safe_name(name: str) -> str | None:
    """Best-effort reverse of safe_archive_name (lossy if path had '--')."""
    base = name
    if base.endswith(".tar"):
        base = base[: -len(".tar")]
    base = base.replace("_AT_", "@").replace("--", ":").replace("__", "/")
    return base


def harbor_dest(
    ref: str,
    harbor_host: str,
    project: str = "airgap",
    rewrite: dict[str, str] | None = None,
) -> str:
    """Return host/project/...:tag without docker:// prefix."""
    rewrite = rewrite or DEFAULT_REWRITE
    registry, path, tag = split_ref(ref)
    host = harbor_host.removeprefix("https://").removeprefix("http://").rstrip("/")
    mapped = rewrite.get(registry, registry.replace(".", "-"))
    repo_path = f"{project}/{mapped}/{path}"
    if tag.startswith("sha256:"):
        return f"{host}/{repo_path}@{tag}"
    return f"{host}/{repo_path}:{tag}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_safe = sub.add_parser("safe-name", help="Archive basename for a ref")
    p_safe.add_argument("ref")

    p_dest = sub.add_parser("dest", help="Harbor destination for a ref")
    p_dest.add_argument("ref")
    p_dest.add_argument("--harbor-url", required=True)
    p_dest.add_argument("--project", default="")
    p_dest.add_argument("--release-yml", type=Path, default=None)

    p_parse = sub.add_parser("parse-archive", help="Recover ref from archive name")
    p_parse.add_argument("archive")

    args = ap.parse_args()
    if args.cmd == "safe-name":
        print(safe_archive_name(args.ref))
        return 0
    if args.cmd == "parse-archive":
        print(parse_safe_name(args.archive) or "")
        return 0
    if args.cmd == "dest":
        cfg = load_release_yml(args.release_yml)
        project = args.project or cfg.get("project") or "airgap"
        rewrite = cfg.get("rewrite") or DEFAULT_REWRITE
        print(
            harbor_dest(
                args.ref,
                args.harbor_url,
                project=project,
                rewrite=rewrite,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
