"""Minimal Pulp 3 REST client for drop-folder import (no pulp-cli required)."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from base64 import b64encode


class PulpClient:
    def __init__(
        self,
        base_url: str,
        username: str = "admin",
        password: str = "",
        timeout: int = 120,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        token = b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {token}"

    def _req(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        form: dict[str, Any] | None = None,
        files: dict[str, Path] | None = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base}{path}"
        hdrs = {"Authorization": self._auth, "Accept": "application/json"}
        body = data
        if files or form:
            boundary = f"----otk{os.urandom(8).hex()}"
            parts: list[bytes] = []
            if form:
                for k, v in form.items():
                    parts.append(
                        (
                            f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                            f"{v}\r\n"
                        ).encode()
                    )
            if files:
                for name, fpath in files.items():
                    raw = fpath.read_bytes()
                    ctype = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"
                    parts.append(
                        (
                            f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="{name}"; '
                            f'filename="{fpath.name}"\r\n'
                            f"Content-Type: {ctype}\r\n\r\n"
                        ).encode()
                        + raw
                        + b"\r\n"
                    )
            parts.append(f"--{boundary}--\r\n".encode())
            body = b"".join(parts)
            hdrs["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif data is not None and "Content-Type" not in (headers or {}):
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode())
        except urllib.error.HTTPError as exc:
            err = exc.read().decode(errors="replace")
            raise RuntimeError(f"{method} {url} → {exc.code}: {err}") from exc

    def get(self, path: str) -> Any:
        return self._req("GET", path)

    def post(self, path: str, payload: dict | None = None) -> Any:
        data = json.dumps(payload or {}).encode() if payload is not None else None
        return self._req("POST", path, data=data)

    def put(self, path: str, payload: dict) -> Any:
        return self._req("PUT", path, data=json.dumps(payload).encode())

    def patch(self, path: str, payload: dict) -> Any:
        return self._req("PATCH", path, data=json.dumps(payload).encode())

    def wait_task(self, task_href: str, timeout: int = 600) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.get(task_href)
            state = task.get("state")
            if state == "completed":
                return task
            if state in ("failed", "canceled", "skipped"):
                raise RuntimeError(f"task {state}: {json.dumps(task, indent=2)}")
            time.sleep(1)
        raise TimeoutError(task_href)

    def ensure_one(self, list_path: str, create_path: str, name: str, body: dict) -> str:
        """Return pulp_href for named resource; create if missing."""
        listing = self.get(f"{list_path}?name={name}&limit=1")
        results = listing.get("results") or []
        if results:
            return results[0]["pulp_href"]
        created = self.post(create_path, body)
        # create may return task
        if created.get("task"):
            task = self.wait_task(created["task"])
            # re-list
            listing = self.get(f"{list_path}?name={name}&limit=1")
            results = listing.get("results") or []
            if results:
                return results[0]["pulp_href"]
            created_res = task.get("created_resources") or []
            if created_res:
                return created_res[0]
        return created["pulp_href"]

    def upload_artifact(self, path: Path) -> str:
        size = path.stat().st_size
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        # check existing
        existing = self.get(f"/pulp/api/v3/artifacts/?sha256={sha256}&limit=1")
        if existing.get("results"):
            return existing["results"][0]["pulp_href"]
        result = self._req(
            "POST",
            "/pulp/api/v3/artifacts/",
            form={"sha256": sha256},
            files={"file": path},
        )
        if result.get("task"):
            task = self.wait_task(result["task"])
            hrefs = task.get("created_resources") or []
            if hrefs:
                return hrefs[0]
        return result["pulp_href"]

    def sha256_file(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
