#!/usr/bin/env python3
"""Upload drop/ RPM files into a Foreman/Katello yum repository (high side)."""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from pathlib import Path
from typing import Any
from urllib.request import Request

# allow running as script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from drop_verify import (  # noqa: E402
    collect_galaxy_tarballs,
    collect_pypi_packages,
    collect_rpm_files,
)

API_ORGS = "/katello/api/organizations"
API_PRODUCTS = "/katello/api/products"
API_REPOS = "/katello/api/repositories"
TYPE_YUM = "yum"
TYPE_PYTHON = "python"
# Katello 4.18 rejects upload_content for ansible_collection
# ("Cannot upload Ansible collections") — that type is sync-only.
# File repos still redistribute the tarballs for offline
# `ansible-galaxy collection install ./name.tar.gz`.
TYPE_FILE = "file"
REPO_PYPI = "pypi"
REPO_GALAXY = "galaxy"


def log(msg: str) -> None:
    print(f"[foreman-upload] {msg}", flush=True)


class ForemanClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: int = 120,
        insecure: bool = False,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        token = b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {token}"
        self._ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()

    def _req(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        files: dict[str, Path] | None = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base}{path}"
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        body: bytes | None = None
        if files:
            boundary = f"----otk{os.urandom(8).hex()}"
            parts: list[bytes] = []
            if payload:
                for key, value in payload.items():
                    parts.append(
                        (
                            f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                            f"{value}\r\n"
                        ).encode()
                    )
            for name, fpath in files.items():
                raw = fpath.read_bytes()
                parts.append(
                    (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{name}"; '
                        f'filename="{fpath.name}"\r\n'
                        f"Content-Type: application/octet-stream\r\n\r\n"
                    ).encode()
                    + raw
                    + b"\r\n"
                )
            parts.append(f"--{boundary}--\r\n".encode())
            body = b"".join(parts)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode())
        except urllib.error.HTTPError as exc:
            err = exc.read().decode(errors="replace")
            raise RuntimeError(f"{method} {url} → {exc.code}: {err}") from exc

    def get(self, path: str) -> Any:
        return self._req("GET", path)

    def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        files: dict[str, Path] | None = None,
    ) -> Any:
        return self._req("POST", path, payload=payload, files=files)


def _first_result(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict):
                return first
    return None


def _org_id(client: ForemanClient, org_name: str) -> int:
    orgs = client.get(API_ORGS + "?" + urllib.parse.urlencode({"search": f'name = "{org_name}"'}))
    org = _first_result(orgs)
    if org is None:
        org = _first_result(client.get(API_ORGS))
    if org is None:
        raise RuntimeError(f"no Katello organization matching {org_name!r}")
    return int(org["id"])


def _product_id(client: ForemanClient, org_id: int, product_name: str) -> int:
    products = client.get(
        API_PRODUCTS
        + "?"
        + urllib.parse.urlencode({"organization_id": org_id, "search": f'name = "{product_name}"'})
    )
    product = _first_result(products)
    if product is None:
        product = client.post(API_PRODUCTS, {"name": product_name, "organization_id": org_id})
    return int(product["id"])


def ensure_repository(
    client: ForemanClient,
    org_name: str,
    product_name: str,
    repo_name: str,
    content_type: str,
) -> dict[str, Any]:
    org_id = _org_id(client, org_name)
    product_id = _product_id(client, org_id, product_name)
    repos = client.get(
        API_REPOS
        + "?"
        + urllib.parse.urlencode(
            {"organization_id": org_id, "product_id": product_id, "name": repo_name}
        )
    )
    repo = _first_result(repos)
    if repo is None:
        repo = client.post(
            API_REPOS,
            {
                "name": repo_name,
                "product_id": product_id,
                "content_type": content_type,
                "unprotected": True,
            },
        )
    return repo


def ensure_yum_repository(
    client: ForemanClient,
    org_name: str,
    product_name: str,
    repo_name: str,
) -> dict[str, Any]:
    return ensure_repository(client, org_name, product_name, repo_name, TYPE_YUM)


def refresh_repository(client: ForemanClient, repo_id: int) -> dict[str, Any]:
    payload = client.get(f"{API_REPOS}/{repo_id}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"repository {repo_id} lookup returned {type(payload).__name__}")
    return payload


def client_url(repo: dict[str, Any], content_type: str) -> str:
    full = repo.get("full_path")
    if not isinstance(full, str) or not full:
        return ""
    base = full.rstrip("/") + "/"
    if content_type == TYPE_PYTHON:
        return f"{base}simple/"
    return base


def upload_content(client: ForemanClient, repo_id: int, artifact: Path) -> Any:
    return client.post(
        f"{API_REPOS}/{repo_id}/upload_content",
        files={"content": artifact},
    )


def upload_rpm(client: ForemanClient, repo_id: int, rpm: Path) -> Any:
    return upload_content(client, repo_id, rpm)


def _upload_named(
    client: ForemanClient,
    org: str,
    product: str,
    repo_name: str,
    content_type: str,
    files: list[Path],
) -> tuple[int, str]:
    if not files:
        return 0, ""
    repo = ensure_repository(client, org, product, repo_name, content_type)
    katello_id = int(repo["id"])
    log(f"repo {repo_name} type={content_type} → katello id={katello_id}")
    uploaded = 0
    for artifact in files:
        log(f"upload {artifact.name}")
        upload_content(client, katello_id, artifact)
        uploaded += 1
    refreshed = refresh_repository(client, katello_id)
    url = client_url(refreshed, content_type) or client_url(repo, content_type)
    return uploaded, url


def upload_drop(
    drop: Path,
    base_url: str,
    username: str,
    password: str,
    org: str,
    product: str,
    insecure: bool = False,
) -> dict[str, Any]:
    """Upload RPM, PyPI, and Galaxy artifacts from a drop into Katello."""
    client = ForemanClient(base_url, username, password, insecure=insecure)
    uploaded = {"rpm": 0, "pypi": 0, "galaxy": 0}
    repo_urls: dict[str, str] = {}

    by_repo: dict[str, list[Path]] = {}
    for repo_id, path in collect_rpm_files(drop):
        by_repo.setdefault(repo_id, []).append(path)
    for repo_id, rpms in by_repo.items():
        count, url = _upload_named(client, org, product, repo_id, TYPE_YUM, rpms)
        uploaded["rpm"] += count
        if url:
            repo_urls[repo_id] = url

    pypi_n, pypi_url = _upload_named(
        client, org, product, REPO_PYPI, TYPE_PYTHON, collect_pypi_packages(drop)
    )
    uploaded["pypi"] = pypi_n
    if pypi_url:
        repo_urls[REPO_PYPI] = pypi_url

    galaxy_n, galaxy_url = _upload_named(
        client, org, product, REPO_GALAXY, TYPE_FILE, collect_galaxy_tarballs(drop)
    )
    uploaded["galaxy"] = galaxy_n
    if galaxy_url:
        repo_urls[REPO_GALAXY] = galaxy_url

    if sum(uploaded.values()) == 0:
        log("no RPM / PyPI / Galaxy artifacts in drop — skip")

    return {"uploaded": uploaded, "repos": repo_urls}


def upload_drop_rpms(
    drop: Path,
    base_url: str,
    username: str,
    password: str,
    org: str,
    product: str,
    insecure: bool = False,
) -> dict[str, Any]:
    """Backward-compatible RPM-only summary used by existing unit tests."""
    result = upload_drop(drop, base_url, username, password, org, product, insecure=insecure)
    rpm_repos = {
        name: url
        for name, url in result["repos"].items()
        if name not in {REPO_PYPI, REPO_GALAXY}
    }
    return {"uploaded": result["uploaded"]["rpm"], "repos": rpm_repos}


def configured() -> bool:
    return bool(os.environ.get("FOREMAN_URL", "").strip())


def main() -> int:
    if not configured():
        log("FOREMAN_URL unset — skip Satellite/Katello upload")
        print(json.dumps({"skipped": True, "reason": "FOREMAN_URL unset"}))
        raise SystemExit(0)

    drop = Path(os.environ.get("DROP", "drop")).resolve()
    base = os.environ["FOREMAN_URL"]
    user = os.environ.get("FOREMAN_USER", "admin")
    password = os.environ.get("FOREMAN_PASSWORD") or os.environ.get("FOREMAN_PASS") or ""
    if not password:
        raise SystemExit("FOREMAN_PASSWORD is required when FOREMAN_URL is set")
    org = os.environ.get("FOREMAN_ORG", "Default Organization")
    product = os.environ.get("FOREMAN_PRODUCT", "OTK")
    insecure = os.environ.get("FOREMAN_INSECURE", "1") != "0"
    result = upload_drop(drop, base, user, password, org, product, insecure=insecure)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
