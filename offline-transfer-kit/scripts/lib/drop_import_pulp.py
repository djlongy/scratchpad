#!/usr/bin/env python3
"""Import an OTK drop/ folder into Pulp 3 (high side)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# allow running as script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from drop_verify import (  # noqa: E402
    collect_galaxy_tarballs,
    collect_pypi_packages,
    collect_rpm_files,
    verify_drop,
)
from oci_images import collect_oci_images, import_images  # noqa: E402
from pulp_client import PulpClient  # noqa: E402


def log(msg: str) -> None:
    print(f"[drop-import] {msg}", flush=True)


def _created_href(client: PulpClient, result: dict, timeout: int = 600) -> str | None:
    if result.get("task"):
        task = client.wait_task(result["task"], timeout=timeout)
        hrefs = task.get("created_resources") or []
        return hrefs[0] if hrefs else None
    return result.get("pulp_href")


def import_pypi(client: PulpClient, drop: Path, prefix: str) -> str | None:
    packages = collect_pypi_packages(drop)
    if not packages:
        log("pypi: nothing to import")
        return None

    repo_name = f"{prefix}-pypi"
    repo_href = client.ensure_one(
        "/pulp/api/v3/repositories/python/python/",
        "/pulp/api/v3/repositories/python/python/",
        repo_name,
        {"name": repo_name, "description": "OTK drop PyPI"},
    )
    log(f"pypi: repository {repo_name} → {repo_href}")

    content_hrefs = []
    for pkg in packages:
        log(f"pypi: upload {pkg.name}")
        # create content (uploads artifact under the hood)
        # API: POST /pulp/api/v3/content/python/packages/
        # multipart: file + relative_path
        result = client._req(
            "POST",
            "/pulp/api/v3/content/python/packages/",
            form={"relative_path": pkg.name},
            files={"file": pkg},
        )
        if result.get("task"):
            task = client.wait_task(result["task"])
            hrefs = task.get("created_resources") or []
            if hrefs:
                content_hrefs.append(hrefs[0])
        elif result.get("pulp_href"):
            content_hrefs.append(result["pulp_href"])

    if content_hrefs:
        # add to repository
        mod = client.post(
            f"{repo_href}modify/",
            {"add_content_units": content_hrefs},
        )
        if mod.get("task"):
            client.wait_task(mod["task"])
        log(f"pypi: added {len(content_hrefs)} packages to repo")

    # publication + distribution
    pub = client.post(
        "/pulp/api/v3/publications/python/pypi/",
        {"repository": repo_href},
    )
    if pub.get("task"):
        task = client.wait_task(pub["task"])
        pub_href = (task.get("created_resources") or [None])[0]
    else:
        pub_href = pub["pulp_href"]

    # Python distribution base_path is a single URL segment (no slashes).
    base_path = f"{prefix}-pypi"
    listing = client.get(f"/pulp/api/v3/distributions/python/pypi/?name={prefix}-pypi-dist&limit=1")
    if not listing.get("results"):
        listing = client.get(f"/pulp/api/v3/distributions/python/pypi/?base_path={base_path}&limit=1")
    if listing.get("results"):
        dist_href = listing["results"][0]["pulp_href"]
        client.patch(dist_href, {"publication": pub_href, "base_path": base_path})
        if listing["results"][0].get("task") or True:
            # patch may be sync
            pass
    else:
        created = client.post(
            "/pulp/api/v3/distributions/python/pypi/",
            {
                "name": f"{prefix}-pypi-dist",
                "base_path": base_path,
                "publication": pub_href,
            },
        )
        if created.get("task"):
            client.wait_task(created["task"])

    simple_url = f"{client.base}/pypi/{base_path}/simple/"
    log(f"pypi: simple index → {simple_url}")
    log(f"pypi: content simple → {client.base}/pulp/content/{base_path}/simple/")
    return simple_url


def import_galaxy(client: PulpClient, drop: Path, prefix: str) -> str | None:
    cols = collect_galaxy_tarballs(drop)
    if not cols:
        log("galaxy: nothing to import")
        return None

    repo_name = f"{prefix}-galaxy"
    repo_href = client.ensure_one(
        "/pulp/api/v3/repositories/ansible/ansible/",
        "/pulp/api/v3/repositories/ansible/ansible/",
        repo_name,
        {"name": repo_name},
    )
    content_hrefs = []
    for col in cols:
        log(f"galaxy: upload {col.name}")
        # pulp_ansible content type is collection_versions (not collections)
        result = client._req(
            "POST",
            "/pulp/api/v3/content/ansible/collection_versions/",
            files={"file": col},
        )
        hrefs: list[str] = []
        if result.get("task"):
            task = client.wait_task(result["task"], timeout=900)
            hrefs = list(task.get("created_resources") or [])
            err = task.get("error")
            if err:
                raise RuntimeError(f"upload failed for {col.name}: {err}")
        elif result.get("pulp_href"):
            hrefs = [result["pulp_href"]]
        # Prefer content unit hrefs for repository modify
        for h in hrefs:
            if "collection_versions" in h or "content/ansible" in h:
                content_hrefs.append(h)
        if hrefs and not any("collection_versions" in h for h in hrefs):
            # some versions return only artifact; re-query by filename
            listing = client.get(
                f"/pulp/api/v3/content/ansible/collection_versions/?limit=1&ordering=-pulp_created"
            )
            for r in listing.get("results") or []:
                content_hrefs.append(r["pulp_href"])

    # de-dupe preserve order
    seen: set[str] = set()
    unique = []
    for h in content_hrefs:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    content_hrefs = unique

    if content_hrefs:
        log(f"galaxy: adding {len(content_hrefs)} collection versions to repo")
        mod = client.post(f"{repo_href}modify/", {"add_content_units": content_hrefs})
        if mod.get("task"):
            client.wait_task(mod["task"], timeout=900)

    # Ansible distributions can bind a repository directly (auto-publish style)
    base_path = f"{prefix}-galaxy"
    listing = client.get(
        f"/pulp/api/v3/distributions/ansible/ansible/?name={prefix}-galaxy-dist&limit=1"
    )
    if not listing.get("results"):
        listing = client.get(
            f"/pulp/api/v3/distributions/ansible/ansible/?base_path={base_path}&limit=1"
        )
    body = {
        "name": f"{prefix}-galaxy-dist",
        "base_path": base_path,
        "repository": repo_href,
    }
    if listing.get("results"):
        dist_href = listing["results"][0]["pulp_href"]
        client.patch(dist_href, {"repository": repo_href, "base_path": base_path})
    else:
        created = client.post("/pulp/api/v3/distributions/ansible/ansible/", body)
        if created.get("task"):
            client.wait_task(created["task"])

    # Galaxy V3 API base for ansible-galaxy clients
    url = f"{client.base}/pulp_ansible/galaxy/{base_path}/api/"
    log(f"galaxy: distribution → {url}")
    log(f"galaxy: UI content → {client.base}/ui/ (Ansible / Collections)")
    return url


def _rpm_repos(pairs: list[tuple[str, Path]]) -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = {}
    for repo_id, path in pairs:
        grouped.setdefault(repo_id, []).append(path)
    return grouped


def import_rpm(client: PulpClient, drop: Path, prefix: str) -> dict[str, str]:
    """Upload RPMs and publish one yum distribution per list id."""
    pairs = collect_rpm_files(drop)
    if not pairs:
        log("rpm: nothing to import")
        return {}

    urls: dict[str, str] = {}
    for repo_id, rpms in _rpm_repos(pairs).items():
        repo_name = f"{prefix}-rpm-{repo_id}"
        repo_href = client.ensure_one(
            "/pulp/api/v3/repositories/rpm/rpm/",
            "/pulp/api/v3/repositories/rpm/rpm/",
            repo_name,
            {"name": repo_name, "description": f"OTK drop RPM {repo_id}"},
        )
        log(f"rpm: repository {repo_name} → {repo_href}")
        content_hrefs: list[str] = []
        for pkg in rpms:
            log(f"rpm: upload {pkg.name}")
            result = client._req(
                "POST",
                "/pulp/api/v3/content/rpm/packages/",
                files={"file": pkg},
            )
            href = _created_href(client, result, timeout=900)
            if href:
                content_hrefs.append(href)

        if content_hrefs:
            mod = client.post(f"{repo_href}modify/", {"add_content_units": content_hrefs})
            if mod.get("task"):
                client.wait_task(mod["task"], timeout=900)
            log(f"rpm: added {len(content_hrefs)} packages to {repo_name}")

        pub = client.post("/pulp/api/v3/publications/rpm/rpm/", {"repository": repo_href})
        pub_href = _created_href(client, pub, timeout=900)
        if not pub_href:
            raise RuntimeError(f"rpm publication failed for {repo_name}")

        base_path = f"{prefix}-rpm-{repo_id}"
        dist_name = f"{prefix}-rpm-{repo_id}-dist"
        listing = client.get(
            f"/pulp/api/v3/distributions/rpm/rpm/?name={dist_name}&limit=1"
        )
        if not listing.get("results"):
            listing = client.get(
                f"/pulp/api/v3/distributions/rpm/rpm/?base_path={base_path}&limit=1"
            )
        if listing.get("results"):
            client.patch(
                listing["results"][0]["pulp_href"],
                {"publication": pub_href, "base_path": base_path},
            )
        else:
            created = client.post(
                "/pulp/api/v3/distributions/rpm/rpm/",
                {
                    "name": dist_name,
                    "base_path": base_path,
                    "publication": pub_href,
                },
            )
            if created.get("task"):
                client.wait_task(created["task"])

        url = f"{client.base}/pulp/content/{base_path}/"
        log(f"rpm: content → {url}")
        urls[repo_id] = url
    return urls


def import_file_tree(client: PulpClient, drop: Path, prefix: str, sub: str) -> str | None:
    """Publish a directory as pulp_file (e.g. vuln-db)."""
    root = drop / sub
    if not root.is_dir():
        return None
    files = [p for p in root.rglob("*") if p.is_file()]
    if not files:
        return None

    repo_name = f"{prefix}-{sub.replace('/', '-')}"
    repo_href = client.ensure_one(
        "/pulp/api/v3/repositories/file/file/",
        "/pulp/api/v3/repositories/file/file/",
        repo_name,
        {"name": repo_name},
    )
    hrefs = []
    for fpath in files:
        rel = fpath.relative_to(root).as_posix()
        log(f"file[{sub}]: {rel}")
        result = client._req(
            "POST",
            "/pulp/api/v3/content/file/files/",
            form={"relative_path": rel},
            files={"file": fpath},
        )
        if result.get("task"):
            task = client.wait_task(result["task"])
            created = task.get("created_resources") or []
            if created:
                hrefs.append(created[0])
        elif result.get("pulp_href"):
            hrefs.append(result["pulp_href"])

    if hrefs:
        mod = client.post(f"{repo_href}modify/", {"add_content_units": hrefs})
        if mod.get("task"):
            client.wait_task(mod["task"])

    pub = client.post(
        "/pulp/api/v3/publications/file/file/",
        {"repository": repo_href},
    )
    if pub.get("task"):
        task = client.wait_task(pub["task"])
        pub_href = (task.get("created_resources") or [None])[0]
    else:
        pub_href = pub["pulp_href"]

    base_path = f"{prefix}-{sub.replace('/', '-')}"
    listing = client.get(
        f"/pulp/api/v3/distributions/file/file/?base_path={base_path}&limit=1"
    )
    if listing.get("results"):
        client.patch(listing["results"][0]["pulp_href"], {"publication": pub_href})
    else:
        created = client.post(
            "/pulp/api/v3/distributions/file/file/",
            {
                "name": f"{prefix}-{sub.replace('/', '-')}-dist",
                "base_path": base_path,
                "publication": pub_href,
            },
        )
        if created.get("task"):
            client.wait_task(created["task"])

    url = f"{client.base}/pulp/content/{base_path}/"
    log(f"file[{sub}]: {url}")
    return url


def import_oci(
    client: PulpClient,
    drop: Path,
    prefix: str,
    pulp_user: str,
    pulp_password: str,
) -> list[dict]:
    """Publish oci-archives to Harbor (if configured) or the Pulp registry."""
    images = collect_oci_images(drop)
    if not images:
        log("oci: nothing to import")
        return []

    harbor = (os.environ.get("OTK_HARBOR_URL") or "").strip()
    skip_harbor = os.environ.get("SKIP_HARBOR", "1") == "1"
    if harbor and not skip_harbor:
        creds = ""
        h_user = os.environ.get("OTK_HARBOR_USER") or ""
        h_pass = os.environ.get("OTK_HARBOR_PASSWORD") or ""
        if h_user or h_pass:
            creds = f"{h_user}:{h_pass}"
        project = os.environ.get("OTK_HARBOR_PROJECT", "airgap")
        log(f"oci: pushing {len(images)} archive(s) to Harbor {harbor}")
        return import_images(
            drop,
            dest_kind="harbor",
            dest_host=harbor,
            creds=creds,
            tls_verify=os.environ.get("OTK_HARBOR_TLS_VERIFY", "0") == "1",
            harbor_project=project,
        )

    oci_prefix = os.environ.get("PULP_OCI_PREFIX", f"{prefix}-oci")
    creds = f"{pulp_user}:{pulp_password}" if pulp_user else ""
    log(f"oci: pushing {len(images)} archive(s) to Pulp registry {client.base}")
    return import_images(
        drop,
        dest_kind="pulp",
        dest_host=client.base,
        prefix=oci_prefix,
        creds=creds,
        tls_verify=False,
        pulp_client=client,
    )


def main() -> int:
    drop = Path(os.environ.get("DROP", "drop")).resolve()
    base = os.environ.get("PULP_URL", "http://127.0.0.1:18080").rstrip("/")
    user = os.environ.get("PULP_USER", "admin")
    password = os.environ.get("PULP_PASS") or os.environ.get("PULP_PASSWORD") or ""
    prefix = os.environ.get("REPO_PREFIX", "otk")

    if not password:
        # try default lab password file
        pass_file = Path(os.environ.get("OTK_ROOT", ".")) / "lab" / "pulp" / "admin.password"
        if pass_file.is_file():
            password = pass_file.read_text().strip()
        else:
            password = "admin"  # first-boot may require reset

    log(f"drop={drop}")
    log(f"pulp={base} user={user}")
    manifest = verify_drop(drop)
    log("SHA256SUMS OK")
    log(f"release_id={manifest.get('release_id')}")

    client = PulpClient(base, user, password)

    # probe auth
    try:
        status = client.get("/pulp/api/v3/status/")
        log(f"pulp online workers={len(status.get('online_workers') or [])}")
    except Exception as exc:
        raise SystemExit(
            f"cannot reach Pulp at {base}: {exc}\n"
            "Set PULP_URL / PULP_PASSWORD (admin password after reset-admin-password)."
        ) from exc

    urls: dict[str, str | None] = {}
    foreman: dict | None = None
    urls["pypi_simple"] = import_pypi(client, drop, prefix)
    if urls["pypi_simple"]:
        # CONTENT_ORIGIN may redirect /pypi/… to 127.0.0.1 — clients on other
        # hosts should use the /pulp/content/ simple path directly.
        urls["pypi_content_simple"] = urls["pypi_simple"].replace(
            "/pypi/", "/pulp/content/", 1
        )
    urls["galaxy"] = import_galaxy(client, drop, prefix)
    rpm_urls = import_rpm(client, drop, prefix)
    urls["rpm"] = next(iter(rpm_urls.values()), None)
    urls["vuln_db"] = import_file_tree(client, drop, prefix, "vuln-db")
    oci_map = import_oci(client, drop, prefix, user, password)
    if oci_map:
        urls["oci"] = oci_map[0].get("dest")

    if os.environ.get("FOREMAN_URL"):
        from foreman_upload import upload_drop  # noqa: PLC0415

        log("foreman: FOREMAN_URL set — uploading RPM + PyPI + Galaxy")
        password = os.environ.get("FOREMAN_PASSWORD") or os.environ.get("FOREMAN_PASS") or ""
        if not password:
            raise SystemExit("FOREMAN_PASSWORD is required when FOREMAN_URL is set")
        foreman = upload_drop(
            drop,
            os.environ["FOREMAN_URL"],
            os.environ.get("FOREMAN_USER", "admin"),
            password,
            os.environ.get("FOREMAN_ORG", "Default Organization"),
            os.environ.get("FOREMAN_PRODUCT", "OTK"),
            insecure=os.environ.get("FOREMAN_INSECURE", "1") != "0",
        )
        urls["foreman_pypi"] = (foreman.get("repos") or {}).get("pypi")
        urls["foreman_galaxy"] = (foreman.get("repos") or {}).get("galaxy")

    out = {
        "release_id": manifest.get("release_id"),
        "pulp_base": base,
        "urls": urls,
        "foreman": foreman,
        "rpm_repos": rpm_urls,
        "oci": oci_map,
        "ui_hint": f"{base}/ui/ (if pulp-ui packaged) or API {base}/pulp/api/v3/",
        "status": f"{base}/pulp/api/v3/status/",
    }
    out_path = drop / "HIGH_SIDE_URLS.json"
    out_path.write_text(json.dumps(out, indent=2) + "\n")
    log(f"wrote {out_path}")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
