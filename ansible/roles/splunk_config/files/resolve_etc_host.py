#!/usr/bin/python3
"""Pick the host-side Splunk etc path from a docker inspect Mounts JSON list.

Reads mounts JSON from stdin. Prints one absolute host path (or nothing).

Named volumes — including local-driver + type=nfs — typically appear as
  <docker_root>/volumes/<name>/_data
so retrieve does not need the NFS server export path.
"""
from __future__ import annotations

import json
import os
import sys


def _src(mount: dict, docker_root: str) -> str:
    src = mount.get("Source") or mount.get("source") or ""
    name = mount.get("Name") or mount.get("name") or ""
    if src:
        return src
    if name:
        return os.path.join(docker_root, "volumes", name, "_data")
    return ""


def resolve(mounts: list, docker_root: str) -> str:
    candidates: list[tuple[int, str]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        dest = mount.get("Destination") or mount.get("destination") or ""
        src = _src(mount, docker_root)
        if not src:
            continue
        score = 0
        if dest in ("/opt/splunk/etc", "/opt/splunkforwarder/etc"):
            score = 3
        elif dest.endswith("/etc"):
            score = 2
        elif "/etc" in dest:
            score = 1
        if score:
            candidates.append((score, src))

    if not candidates:
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            dest = mount.get("Destination") or mount.get("destination") or ""
            src = _src(mount, docker_root)
            name = mount.get("Name") or mount.get("name") or ""
            if dest in (
                "/opt/splunk",
                "/opt/splunkforwarder",
                "/opt/splunk/var",
                "/opt/splunkforwarder/var",
            ):
                continue
            blob = f"{name}{dest}{src}".lower()
            if src and "splunk" in blob:
                candidates.append((0, src))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: -item[0])
    path = candidates[0][1]
    # Volume may hold SPLUNK_HOME rather than etc alone.
    if not os.path.isdir(os.path.join(path, "system")) and os.path.isdir(
        os.path.join(path, "etc", "system")
    ):
        path = os.path.join(path, "etc")
    return path


def main() -> None:
    docker_root = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/docker"
    try:
        mounts = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    if not isinstance(mounts, list):
        return
    path = resolve(mounts, docker_root)
    if path:
        sys.stdout.write(path)


if __name__ == "__main__":
    main()
