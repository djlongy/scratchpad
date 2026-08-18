"""Drive shipped OTK air-gap parse / hash-verify / Pulp-import functions."""
from __future__ import annotations

import json
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

KIT = Path(__file__).resolve().parents[1]
KIT_LIB = KIT / "scripts" / "lib"
KIT_SCRIPTS = KIT / "scripts"
FIXTURE_CATALOG = KIT / "fixtures" / "airgap-dev" / "catalog"

sys.path.insert(0, str(KIT_LIB))

from catalog_parse import load_catalog, parse_galaxy_requirements, parse_pin_lines  # noqa: E402
from drop_import_pulp import import_galaxy, import_pypi, import_rpm  # noqa: E402
from drop_verify import (  # noqa: E402
    collect_rpm_files,
    component_counts,
    sha256_file,
    verify_drop,
    verify_sha256sums,
    write_drop_manifest,
    write_sha256sums,
)
from foreman_upload import configured, upload_drop_rpms  # noqa: E402
from oci_images import (  # noqa: E402
    assert_digest_match,
    assert_registry_blobs_not_loopback,
    dest_map_entry,
    load_images_json,
    parse_image_lines,
    pulp_dest_ref,
    write_images_json,
)
from pulp_client import PulpClient  # noqa: E402
import pypi_resolve  # noqa: E402
from pypi_resolve import (  # noqa: E402
    DEFAULT_PLATFORMS,
    assert_closed,
    closed_lock_rows,
    install_report_args,
    packages_from_report,
    platform_flags,
    write_lock,
)


def test_fixture_catalog_lists_all_three_types() -> None:
    catalog = load_catalog(FIXTURE_CATALOG)
    assert catalog["counts"]["pypi"] > 0
    assert catalog["counts"]["rpm"] > 0
    assert catalog["counts"]["galaxy"] > 0
    assert catalog["counts"]["oci"] > 0
    assert any(pin.startswith("requests==") for pin in catalog["pypi"])
    assert "smoke-base" in catalog["rpm"]["packages"]
    assert catalog["galaxy"][0]["name"] == "ansible.posix"
    assert catalog["oci"][0].startswith("docker.io/library/busybox:")


def test_pypi_report_includes_transitive_nodes() -> None:
    payload = {
        "install": [
            {"requested": True, "metadata": {"name": "requests", "version": "2.32.3"}},
            {"requested": False, "metadata": {"name": "urllib3", "version": "2.6.3"}},
            {"requested": False, "metadata": {"name": "idna", "version": "3.19"}},
            {"not": "a package"},
        ]
    }
    rows = packages_from_report(payload)
    names = [row["name"] for row in rows]
    assert names == ["requests", "urllib3", "idna"]
    assert rows[0]["requested"] is True
    assert rows[1]["requested"] is False


def test_pypi_linux_flags_force_wheels_for_each_abi() -> None:
    flags = platform_flags("39")
    assert "--only-binary=:all:" in flags
    assert flags[flags.index("--python-version") + 1] == "39"
    assert flags[flags.index("--abi") + 1] == "cp39"
    for plat in DEFAULT_PLATFORMS:
        assert plat in flags


def test_pypi_closure_args_are_offline_and_no_index(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    dest = tmp_path / "packages"
    report = tmp_path / "report.json"
    req.write_text("requests==2.32.3\n", encoding="utf-8")
    dest.mkdir()
    args = install_report_args(req, dest, report, "311")
    assert "--no-index" in args
    assert "--find-links" in args
    assert "--dry-run" in args
    assert "--ignore-installed" in args
    assert str(dest) in args
    assert "--only-binary=:all:" in args


def test_pypi_closure_fails_when_a_transitive_dep_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args,
            1,
            "",
            "ERROR: No matching distribution found for charset-normalizer",
        )

    monkeypatch.setattr(pypi_resolve, "run_pip", fake_run)
    req = tmp_path / "requirements.txt"
    dest = tmp_path / "packages"
    req.write_text("requests==2.32.3\n", encoding="utf-8")
    dest.mkdir()
    with pytest.raises(SystemExit, match="charset-normalizer"):
        assert_closed(req, dest, tmp_path / "report.json", "39")


def test_pypi_lock_is_one_closed_pin_set(tmp_path: Path) -> None:
    rows = closed_lock_rows(
        {
            "host": [
                {"name": "requests", "version": "2.32.3", "requested": True},
                {"name": "urllib3", "version": "2.7.0", "requested": False},
            ],
            "linux-cp39": [
                {"name": "requests", "version": "2.32.3", "requested": True},
                {"name": "urllib3", "version": "2.6.3", "requested": False},
            ],
        }
    )
    lock = tmp_path / "requirements.lock"
    write_lock(rows + [{"name": "urllib3", "version": "2.6.3", "requested": False}], lock)
    pins = [
        line
        for line in lock.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    names = [pin.split("==", 1)[0].lower() for pin in pins]
    assert names == sorted(set(names))
    urllib_pins = [pin for pin in pins if pin.lower().startswith("urllib3==")]
    assert urllib_pins == ["urllib3==2.7.0"]


def test_parse_pin_lines_skips_comments_and_blanks() -> None:
    text = "# heading\n\nrequests==2.32.3\n  \n# skip\npackaging==24.2\n"
    assert parse_pin_lines(text) == ["requests==2.32.3", "packaging==24.2"]


def test_parse_galaxy_requirements_reads_name_and_version() -> None:
    text = (
        "---\ncollections:\n  - name: ansible.posix\n    version: \"1.6.2\"\n"
        "  - name: community.general\n"
    )
    cols = parse_galaxy_requirements(text)
    assert cols[0] == {"name": "ansible.posix", "version": "1.6.2"}
    assert cols[1]["name"] == "community.general"


def test_write_and_verify_sha256sums_round_trip(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    pkg = drop / "pypi" / "packages"
    pkg.mkdir(parents=True)
    artifact = pkg / "demo-1.0-py3-none-any.whl"
    artifact.write_bytes(b"not-a-real-wheel-just-bytes")
    write_sha256sums(drop)
    digest = sha256_file(artifact)
    sums = (drop / "SHA256SUMS").read_text(encoding="utf-8")
    assert digest in sums
    assert "pypi/packages/demo-1.0-py3-none-any.whl" in sums
    assert verify_sha256sums(drop) == []
    artifact.write_bytes(b"tampered")
    bad = verify_sha256sums(drop)
    assert any("mismatch" in item for item in bad)


def test_verify_drop_rejects_missing_file(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "MANIFEST.json").write_text("{}", encoding="utf-8")
    (drop / "SHA256SUMS").write_text(
        "0" * 64 + "  missing.bin\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="missing missing.bin"):
        verify_drop(drop)


def test_write_drop_manifest_names_pypi_rpm_galaxy(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    (drop / "pypi" / "packages").mkdir(parents=True)
    (drop / "galaxy" / "collections").mkdir(parents=True)
    (drop / "rpm" / "smoke-base").mkdir(parents=True)
    (drop / "oci").mkdir(parents=True)
    (drop / "pypi" / "packages" / "a.whl").write_bytes(b"a")
    (drop / "galaxy" / "collections" / "ns-col-1.0.0.tar.gz").write_bytes(b"g")
    (drop / "rpm" / "smoke-base" / "which-1.noarch.rpm").write_bytes(b"r")
    (drop / "oci" / "docker.io__library__busybox--1.36.1.tar").write_bytes(b"oci")
    write_images_json(
        drop / "oci" / "images.json",
        [
            {
                "ref": "docker.io/library/busybox:1.36.1",
                "archive": "docker.io__library__busybox--1.36.1.tar",
                "digest": "sha256:" + "a" * 64,
            }
        ],
    )
    manifest = write_drop_manifest(drop, "test-release")
    assert manifest["pypi"] == 1
    assert manifest["rpm"] == 1
    assert manifest["galaxy"] == 1
    assert manifest["oci"] == 1
    assert manifest["counts"]["pypi"] == 1
    assert manifest["counts"]["oci"] == 1
    assert collect_rpm_files(drop)[0][0] == "smoke-base"
    assert component_counts(drop)["rpm"] == 1
    assert component_counts(drop)["oci"] == 1


def test_parse_image_lines_and_dest_map(tmp_path: Path) -> None:
    text = (
        "# comment\n\n"
        "docker.io/library/busybox:1.36.1\n"
        "docker://ghcr.io/example/app@sha256:" + ("b" * 64) + "\n"
    )
    refs = parse_image_lines(text)
    assert refs[0] == "docker.io/library/busybox:1.36.1"
    assert refs[1].startswith("ghcr.io/example/app@sha256:")
    dest = pulp_dest_ref(refs[0], "http://127.0.0.1:18080", prefix="otk-oci")
    assert dest == "127.0.0.1:18080/otk-oci/library/busybox:1.36.1"
    row = dest_map_entry(
        {"ref": refs[0], "archive": "busybox.tar", "digest": "sha256:abc"},
        dest,
        "sha256:abc",
    )
    assert row["dest"] == dest
    assert row["dest_digest"] == "sha256:abc"
    assert_digest_match("sha256:abc", "sha256:abc")
    try:
        assert_digest_match("sha256:abc", "sha256:def")
    except ValueError as exc:
        assert "mismatch" in str(exc)
    else:
        raise AssertionError("expected digest mismatch")
    images_path = tmp_path / "images.json"
    write_images_json(images_path, [row])
    loaded = load_images_json(images_path)
    assert loaded[0]["digest"] == "sha256:abc"
    assert loaded[0]["dest"] == dest


def test_assert_registry_blobs_not_loopback_rejects_127() -> None:
    class _Reg(_PulpStub):
        def do_GET(self) -> None:  # noqa: N802
            if "/manifests/" in urlparse(self.path).path:
                self._send({"config": {"digest": "sha256:" + "a" * 64}})
                return
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:18080/pulp/container/x")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Reg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        with pytest.raises(RuntimeError, match="loopback"):
            assert_registry_blobs_not_loopback(f"{host}:{port}/otk-oci/library/busybox:1.36.1")
    finally:
        server.shutdown()
        server.server_close()


class _PulpStub(BaseHTTPRequestHandler):
    created = 0

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send(self, payload: object, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.endswith("/status/"):
            self._send({"online_workers": []})
            return
        self._send({"results": []})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        _PulpStub.created += 1
        href = f"/pulp/api/v3/fake/{_PulpStub.created}/"
        self._send({"pulp_href": href, "name": f"n{_PulpStub.created}"})

    def do_PATCH(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._send({})


@pytest.fixture
def pulp_stub() -> str:
    _PulpStub.created = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PulpStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def _tiny_drop(tmp_path: Path) -> Path:
    drop = tmp_path / "drop"
    (drop / "pypi" / "packages").mkdir(parents=True)
    (drop / "galaxy" / "collections").mkdir(parents=True)
    (drop / "rpm" / "smoke-base").mkdir(parents=True)
    (drop / "pypi" / "packages" / "packaging-24.2-py3-none-any.whl").write_bytes(b"whl")
    (drop / "galaxy" / "collections" / "ansible-posix-1.6.2.tar.gz").write_bytes(b"gal")
    (drop / "rpm" / "smoke-base" / "which-2.21-29.el9.x86_64.rpm").write_bytes(b"rpm")
    write_sha256sums(drop)
    write_drop_manifest(drop, "stub-release")
    return drop


def test_import_functions_publish_all_three_urls(tmp_path: Path, pulp_stub: str) -> None:
    drop = _tiny_drop(tmp_path)
    client = PulpClient(pulp_stub, "admin", "unused")
    pypi = import_pypi(client, drop, "otk")
    galaxy = import_galaxy(client, drop, "otk")
    rpm = import_rpm(client, drop, "otk")
    assert pypi is not None and pypi.endswith("/pypi/otk-pypi/simple/")
    assert galaxy is not None and "/pulp_ansible/galaxy/otk-galaxy/api/" in galaxy
    assert "smoke-base" in rpm
    assert rpm["smoke-base"].endswith("/pulp/content/otk-rpm-smoke-base/")


def test_foreman_upload_skips_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOREMAN_URL", raising=False)
    assert configured() is False


class _KatelloStub(_PulpStub):
    """Minimal Katello API for upload_drop unit tests."""

    posts: list[str] = []
    next_id = 10
    repos_by_name: dict[str, dict] = {}

    def _send_repo(self, repo_id: int, name: str) -> None:
        self._send(
            {
                "id": repo_id,
                "name": name,
                "content_type": "yum",
                "full_path": f"https://foreman.example/pulp/content/{name}/",
            }
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path.endswith("/organizations"):
            self._send({"results": [{"id": 1, "name": "Default Organization"}]})
            return
        if path.endswith("/products"):
            self._send({"results": [{"id": 2, "name": "OTK"}]})
            return
        if "/repositories/" in path and path.rstrip("/").split("/")[-1].isdigit():
            rid = int(path.rstrip("/").split("/")[-1])
            for repo in _KatelloStub.repos_by_name.values():
                if repo["id"] == rid:
                    self._send(repo)
                    return
            self._send_repo(rid, "smoke-base")
            return
        if path.endswith("/repositories"):
            name = (parse_qs(parsed.query).get("name") or [""])[0]
            if name == "smoke-base":
                self._send({"results": [{"id": 3, "name": name, "full_path": "https://foreman.example/pulp/content/otk/"}]})
                return
            if name in _KatelloStub.repos_by_name:
                self._send({"results": [_KatelloStub.repos_by_name[name]]})
                return
        self._send({"results": []})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        _KatelloStub.posts.append(parsed.path)
        if parsed.path.endswith("/repositories") and "upload_content" not in parsed.path:
            _KatelloStub.next_id += 1
            name = f"created-{_KatelloStub.next_id}"
            repo = {
                "id": _KatelloStub.next_id,
                "name": name,
                "full_path": f"https://foreman.example/pulp/content/{name}/",
            }
            _KatelloStub.repos_by_name[name] = repo
            self._send(repo)
            return
        super().do_POST()


def test_foreman_upload_posts_each_rpm(tmp_path: Path) -> None:
    drop = _tiny_drop(tmp_path)
    _KatelloStub.posts = []
    _KatelloStub.repos_by_name = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KatelloStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        result = upload_drop_rpms(
            drop,
            f"http://{host}:{port}",
            "admin",
            "unused",
            "Default Organization",
            "OTK",
            insecure=True,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result["uploaded"] == 1
    uploads = [path for path in _KatelloStub.posts if path.endswith("/upload_content")]
    assert len(uploads) >= 1


def test_otk_airgap_help_lists_modes_and_list_types() -> None:
    script = KIT_SCRIPTS / "otk-airgap.sh"
    assert script.is_file()
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR
    help_text = subprocess.check_output([str(script), "--help"], text=True)
    for token in ("pull", "ingest", "bundle", "pip", "rpm", "galaxy", "oci", "container"):
        assert token in help_text
    parsed = subprocess.run(
        ["bash", "-n", str(script)], check=False, capture_output=True, text=True
    )
    assert parsed.returncode == 0, parsed.stderr


def test_catalog_parse_cli_require_rejects_empty(tmp_path: Path) -> None:
    empty = tmp_path / "catalog"
    (empty / "pypi").mkdir(parents=True)
    (empty / "pypi" / "requirements.txt").write_text("# none\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(KIT_LIB / "catalog_parse.py"),
            "--catalog",
            str(empty),
            "--require",
            "pypi,rpm,galaxy,oci",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "missing required list entries" in proc.stderr
