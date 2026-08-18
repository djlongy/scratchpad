#!/usr/bin/env python3
"""OCI list parse, archive pull, images.json provenance, high-side dest map.

Low side writes drop/oci/*.tar + images.json (source ref + content digest).
High side republishes from the archive only — no public-registry pull.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from harbor_map import harbor_dest, safe_archive_name, split_ref

IMAGES_JSON = "images.json"
_OPTIONAL_IMAGE_KEYS = ("dest", "dest_digest", "bytes", "sbom")


def parse_image_lines(text: str) -> list[str]:
    """Return non-comment, non-blank image refs (docker:// prefix stripped)."""
    refs: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("docker://"):
            line = line[len("docker://") :]
        refs.append(line)
    return refs


def load_image_list(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return parse_image_lines(path.read_text(encoding="utf-8"))


def _payload_images(raw: Any) -> list[Any]:
    if isinstance(raw, dict):
        return list(raw.get("images") or [])
    if isinstance(raw, list):
        return raw
    return []


def _normalize_image(item: dict[str, Any]) -> dict[str, Any] | None:
    ref = str(item.get("ref") or item.get("source") or "").strip()
    archive = str(item.get("archive") or "").strip()
    digest = str(item.get("digest") or item.get("source_digest") or "").strip()
    if not archive and ref:
        archive = safe_archive_name(ref)
    if not archive:
        return None
    entry: dict[str, Any] = {"ref": ref, "archive": archive}
    if digest:
        entry["digest"] = digest
    for key in _OPTIONAL_IMAGE_KEYS:
        if item.get(key) not in (None, ""):
            entry[key] = item[key]
    return entry


def normalize_images_payload(raw: Any) -> list[dict[str, Any]]:
    """Accept {images: [...]} or a bare list of image maps."""
    out: list[dict[str, Any]] = []
    for item in _payload_images(raw):
        if not isinstance(item, dict):
            continue
        entry = _normalize_image(item)
        if entry:
            out.append(entry)
    return out


def load_images_json(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return normalize_images_payload(raw)


def write_images_json(path: Path, images: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "images": images}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def collect_oci_archives(drop: Path) -> list[Path]:
    root = drop / "oci"
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*.tar") if path.is_file())


def collect_oci_images(drop: Path) -> list[dict[str, Any]]:
    """Prefer drop/oci/images.json; fall back to archive names only."""
    images = load_images_json(drop / "oci" / IMAGES_JSON)
    if images:
        return images
    return [{"ref": "", "archive": path.name} for path in collect_oci_archives(drop)]


def pulp_registry_host(pulp_url: str) -> str:
    parsed = urlparse(pulp_url)
    if parsed.netloc:
        return parsed.netloc
    return pulp_url.removeprefix("https://").removeprefix("http://").rstrip("/")


def pulp_dest_ref(source_ref: str, registry_host: str, prefix: str = "otk-oci") -> str:
    """Map a public source ref onto a Pulp registry path (no docker://)."""
    _registry, path, tag = split_ref(source_ref)
    host = pulp_registry_host(registry_host)
    prefix = prefix.strip("/")
    if tag.startswith("sha256:"):
        return f"{host}/{prefix}/{path}@{tag}"
    return f"{host}/{prefix}/{path}:{tag}"


def pulp_base_path(source_ref: str, prefix: str = "otk-oci") -> str:
    _registry, path, _tag = split_ref(source_ref)
    return f"{prefix.strip('/')}/{path}"


def dest_map_entry(
    image: dict[str, Any],
    dest_ref: str,
    dest_digest: str = "",
) -> dict[str, Any]:
    """Provenance row written into HIGH_SIDE_URLS / images.json after ingest."""
    row = {
        "ref": image.get("ref") or "",
        "archive": image.get("archive") or "",
        "digest": image.get("digest") or "",
        "dest": dest_ref,
    }
    if dest_digest:
        row["dest_digest"] = dest_digest
    return row


def assert_digest_match(source_digest: str, dest_digest: str) -> None:
    src = (source_digest or "").strip()
    dst = (dest_digest or "").strip()
    if not src or src == "unknown":
        raise ValueError("source digest missing")
    if not dst:
        raise ValueError("destination digest missing")
    if src != dst:
        raise ValueError(f"digest mismatch source={src} dest={dst}")


def _skopeo_cmd() -> str:
    exe = shutil.which("skopeo")
    if not exe:
        raise SystemExit("skopeo is required for OCI pull/ingest")
    return exe


def _platform_args(platform: str) -> list[str]:
    plat = (platform or "").strip()
    if not plat:
        return []
    mapping = {
        "linux/amd64": ("linux", "amd64"),
        "amd64": ("linux", "amd64"),
        "x86_64": ("linux", "amd64"),
        "linux/arm64": ("linux", "arm64"),
        "arm64": ("linux", "arm64"),
        "aarch64": ("linux", "arm64"),
    }
    if plat in mapping:
        os_name, arch = mapping[plat]
        return ["--override-os", os_name, "--override-arch", arch]
    if "/" in plat:
        os_name, arch = plat.split("/", 1)
        return ["--override-os", os_name, "--override-arch", arch]
    return []


def inspect_digest(transport: str, extra: list[str] | None = None) -> str:
    cmd = [_skopeo_cmd(), "inspect", "--format", "{{.Digest}}"]
    if extra:
        cmd.extend(extra)
    cmd.append(transport)
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"skopeo inspect failed for {transport}: {err}")
    return proc.stdout.strip()


def pull_images(
    image_list: Path,
    dest_dir: Path,
    platform: str = "",
) -> list[dict[str, Any]]:
    refs = load_image_list(image_list)
    dest_dir.mkdir(parents=True, exist_ok=True)
    plat = _platform_args(platform)
    images: list[dict[str, Any]] = []
    for ref in refs:
        archive = safe_archive_name(ref)
        dest = dest_dir / archive
        if dest.exists():
            dest.unlink()
        cmd = [_skopeo_cmd(), "copy", *plat, f"docker://{ref}", f"oci-archive:{dest}"]
        print(f"[oci] skopeo copy docker://{ref} → {archive}", flush=True)
        subprocess.check_call(cmd)
        digest = inspect_digest(f"oci-archive:{dest}")
        images.append(
            {
                "ref": ref,
                "archive": archive,
                "digest": digest,
                "bytes": dest.stat().st_size,
            }
        )
    write_images_json(dest_dir / IMAGES_JSON, images)
    return images


def _ensure_push_distribution(client: Any, name: str, base_path: str) -> str:
    listing = client.get(
        f"/pulp/api/v3/distributions/container/container/?name={name}&limit=1"
    )
    results = listing.get("results") or []
    if not results:
        listing = client.get(
            f"/pulp/api/v3/distributions/container/container/?base_path={base_path}&limit=1"
        )
        results = listing.get("results") or []
    if results:
        return results[0]["pulp_href"]
    created = client.post(
        "/pulp/api/v3/distributions/container/container/",
        {"name": name, "base_path": base_path, "private": False},
    )
    if created.get("task"):
        task = client.wait_task(created["task"])
        hrefs = task.get("created_resources") or []
        if hrefs:
            return hrefs[0]
    return created["pulp_href"]


def assert_registry_blobs_not_loopback(dest_ref: str, creds: str = "") -> None:
    """Fail if blob 302s target 127.0.0.1 — remote docker then reports unknown blob."""
    import urllib.error
    import urllib.request
    from base64 import b64encode

    host_path = dest_ref.split("@", 1)[0]
    if "/" not in host_path:
        return
    registry, repo_tag = host_path.split("/", 1)
    repo, _, tag = repo_tag.rpartition(":")
    if not repo or not tag or tag.startswith("sha256:"):
        return
    url = f"http://{registry}/v2/{repo}/manifests/{tag}"
    headers = {
        "Accept": (
            "application/vnd.docker.distribution.manifest.v2+json, "
            "application/vnd.oci.image.manifest.v1+json"
        )
    }
    if creds:
        headers["Authorization"] = "Basic " + b64encode(creds.encode()).decode()
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            manifest = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return
    cfg = (manifest.get("config") or {}).get("digest")
    if not cfg:
        return
    blob_url = f"http://{registry}/v2/{repo}/blobs/{cfg}"
    req = urllib.request.Request(blob_url, headers=headers, method="GET")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    loc = blob_url
    try:
        with opener.open(req, timeout=30) as resp:
            loc = resp.headers.get("Location") or resp.geturl()
    except urllib.error.HTTPError as exc:
        loc = exc.headers.get("Location") or ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return
    if "127.0.0.1" in loc or "://localhost" in loc:
        raise RuntimeError(
            "Pulp CONTENT_ORIGIN redirects container blobs to loopback "
            f"({loc!r}). Recreate the lab Pulp container with "
            "PULP_CONTENT_ORIGIN=http://<address-receiving-vms-use>:18080 "
            "or remote docker pull will fail with unknown blob."
        )


def push_archive(
    archive: Path,
    dest_ref: str,
    creds: str = "",
    tls_verify: bool = False,
) -> str:
    extra = []
    if not tls_verify:
        extra.append("--dest-tls-verify=false")
    if creds:
        extra.extend(["--dest-creds", creds])
    cmd = [
        _skopeo_cmd(),
        "copy",
        *extra,
        f"oci-archive:{archive}",
        f"docker://{dest_ref}",
    ]
    print(f"[oci] skopeo copy {archive.name} → docker://{dest_ref}", flush=True)
    subprocess.check_call(cmd)
    inspect_extra = []
    if not tls_verify:
        inspect_extra.append("--tls-verify=false")
    if creds:
        inspect_extra.extend(["--creds", creds])
    digest = inspect_digest(f"docker://{dest_ref}", extra=inspect_extra)
    assert_registry_blobs_not_loopback(dest_ref, creds=creds)
    return digest


def _dest_ref_for(
    source_ref: str,
    dest_kind: str,
    dest_host: str,
    prefix: str,
    harbor_project: str,
    pulp_client: Any | None,
) -> str:
    if dest_kind == "harbor":
        return harbor_dest(source_ref, dest_host, project=harbor_project)
    dest_ref = pulp_dest_ref(source_ref, dest_host, prefix=prefix)
    if pulp_client is not None:
        base_path = pulp_base_path(source_ref, prefix=prefix)
        _ensure_push_distribution(pulp_client, base_path.replace("/", "-"), base_path)
    return dest_ref


def _publish_one(
    drop: Path,
    image: dict[str, Any],
    dest_kind: str,
    dest_host: str,
    prefix: str,
    creds: str,
    tls_verify: bool,
    harbor_project: str,
    pulp_client: Any | None,
) -> dict[str, Any]:
    archive = drop / "oci" / (image.get("archive") or "")
    if not archive.is_file():
        raise SystemExit(f"oci archive missing: {archive}")
    dest_ref = _dest_ref_for(
        image.get("ref") or "",
        dest_kind,
        dest_host,
        prefix,
        harbor_project,
        pulp_client,
    )
    dest_digest = push_archive(archive, dest_ref, creds=creds, tls_verify=tls_verify)
    source_digest = str(image.get("digest") or "")
    if source_digest and source_digest != "unknown":
        assert_digest_match(source_digest, dest_digest)
    return dest_map_entry(image, dest_ref, dest_digest)


def import_images(
    drop: Path,
    dest_kind: str,
    dest_host: str,
    prefix: str = "otk-oci",
    creds: str = "",
    tls_verify: bool = False,
    harbor_project: str = "airgap",
    pulp_client: Any | None = None,
) -> list[dict[str, Any]]:
    """Publish each oci-archive to Harbor or a Pulp registry; return dest map."""
    images = collect_oci_images(drop)
    if not images:
        return []
    out = [
        _publish_one(
            drop,
            image,
            dest_kind,
            dest_host,
            prefix,
            creds,
            tls_verify,
            harbor_project,
            pulp_client,
        )
        for image in images
    ]
    write_images_json(drop / "oci" / IMAGES_JSON, out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_parse = sub.add_parser("parse", help="Print image refs from a list file")
    p_parse.add_argument("--list", type=Path, required=True)

    p_pull = sub.add_parser("pull", help="skopeo copy listed images into dest/")
    p_pull.add_argument("--list", type=Path, required=True)
    p_pull.add_argument("--dest", type=Path, required=True)
    p_pull.add_argument("--platform", default=os.environ.get("OTK_OCI_PLATFORM", ""))

    p_dest = sub.add_parser("dest", help="Print a Pulp dest ref for a source ref")
    p_dest.add_argument("ref")
    p_dest.add_argument("--pulp-url", required=True)
    p_dest.add_argument("--prefix", default="otk-oci")

    args = parser.parse_args()
    if args.cmd == "parse":
        for ref in load_image_list(args.list):
            print(ref)
        return 0
    if args.cmd == "pull":
        images = pull_images(args.list, args.dest, platform=args.platform)
        print(json.dumps({"images": images}, indent=2))
        return 0
    if args.cmd == "dest":
        print(pulp_dest_ref(args.ref, args.pulp_url, prefix=args.prefix))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
