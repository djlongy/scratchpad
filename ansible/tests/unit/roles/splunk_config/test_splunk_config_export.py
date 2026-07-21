"""Unit tests for roles/splunk_config/files/splunk_config_export.py.

Pure-stdlib script (no Splunk libs), so we build fake `etc/` capture trees under
tmp_path and assert the scrub/classify/capture behaviour end to end.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
MODULE_PATH = (REPO_ROOT / "ansible" / "roles" / "splunk_config"
               / "files" / "splunk_config_export.py")


@pytest.fixture(scope="module")
def sce():
    spec = importlib.util.spec_from_file_location("splunk_config_export", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ── fixture-tree helpers ─────────────────────────────────────────────────────
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _instance(root: Path, label: str, *, service: str, role_hint: str = "",
              server_conf: str | None = None) -> Path:
    inst = root / label
    etc = inst / "etc"
    (etc).mkdir(parents=True, exist_ok=True)
    _write(inst / "_capture.json",
           json.dumps({"service": service, "node": "wkr-01", "role_hint": role_hint}))
    if server_conf is not None:
        _write(etc / "system" / "local" / "server.conf", server_conf)
    return etc


# ── scope rule ───────────────────────────────────────────────────────────────
def test_is_stock_app(sce):
    assert sce.is_stock_app("search")
    assert sce.is_stock_app("splunk_secure_gateway")
    assert sce.is_stock_app("TA-nix")
    assert not sce.is_stock_app("org_dashboards")
    assert not sce.is_stock_app("acme_app")


def test_custom_app_captures_default_stock_app_does_not(sce, tmp_path):
    etc = _instance(tmp_path / "raw", "sh01", service="splunk_search", role_hint="search_head")
    # custom app: has local + default → both captured
    _write(etc / "apps" / "org_app" / "local" / "props.conf", "[x]\nk = v\n")
    _write(etc / "apps" / "org_app" / "default" / "app.conf", "[ui]\nlabel = Org\n")
    # stock app: local override kept, default skipped
    _write(etc / "apps" / "search" / "local" / "savedsearches.conf", "[my search]\nsearch = *\n")
    _write(etc / "apps" / "search" / "default" / "savedsearches.conf", "[stock]\nsearch = index=_internal\n")

    out = tmp_path / "out"
    sce.run(str(tmp_path / "raw"), str(out), "splunk", "9.4")

    key = "instance-search_head-splunk_search"
    assert (out / key / "apps" / "org_app" / "default" / "app.conf").is_file()
    assert (out / key / "apps" / "org_app" / "local" / "props.conf").is_file()
    assert (out / key / "apps" / "search" / "local" / "savedsearches.conf").is_file()
    # stock default must NOT be captured
    assert not (out / key / "apps" / "search" / "default").exists()


# ── secret scrubbing ─────────────────────────────────────────────────────────
def test_scrub_conf_text_redacts_secret_keys_and_encrypted_values(sce):
    text = (
        "[sslConfig]\n"
        "sslPassword = $7$abcdEF==\n"
        "enableSplunkdSSL = true\n"
        "[general]\n"
        "pass4SymmKey = plaintextkey\n"
        "serverName = idx1\n"
        "someToken = $1$deadbeef\n"
    )
    scrubbed: list[dict] = []
    result = sce.scrub_conf_text(text, "system/local/server.conf", scrubbed)
    assert sce.SCRUBBED_TOKEN in result
    assert "$7$abcdEF==" not in result
    assert "plaintextkey" not in result
    assert "$1$deadbeef" not in result
    # non-secret values preserved
    assert "enableSplunkdSSL = true" in result
    assert "serverName = idx1" in result
    # records carry stanza context
    keys = {(r["stanza"], r["key"]) for r in scrubbed}
    assert ("sslConfig", "sslPassword") in keys
    assert ("general", "pass4SymmKey") in keys
    assert ("general", "someToken") in keys
    assert all(r["source"] == "secrets" for r in scrubbed)


def test_never_copy_files_are_recorded_not_written(sce, tmp_path):
    etc = _instance(tmp_path / "raw", "sh01", service="s", role_hint="search_head")
    _write(etc / "passwd", ":admin:$6$hash:::Administrator:admin:changeme\n")
    _write(etc / "auth" / "splunk.secret", "SUPERSECRETKEY\n")
    _write(etc / "apps" / "org" / "local" / "passwords.conf",
           "[credential:foo]\npassword = $7$enc\n")
    _write(etc / "apps" / "org" / "local" / "props.conf", "[x]\nk = v\n")

    out = tmp_path / "out"
    manifest = sce.run(str(tmp_path / "raw"), str(out), "splunk", "9.4")

    # No secret file content anywhere in the snapshot.
    for path in out.rglob("*"):
        if path.is_file():
            body = path.read_text(encoding="utf-8", errors="replace")
            assert "SUPERSECRETKEY" not in body
            assert "$6$hash" not in body
            assert "$7$enc" not in body
    names = {r["key"] for r in manifest["scrubbed_secrets"]}
    assert "splunk.secret" in names
    assert "passwd" in names
    assert "passwords.conf" in names


def test_run_leaves_no_secret_pattern_in_output(sce, tmp_path):
    etc = _instance(tmp_path / "raw", "idx01", service="splunk-idx", role_hint="indexer")
    _write(etc / "system" / "local" / "server.conf",
           "[general]\nserverName = idx1\n[sslConfig]\nsslPassword = $7$leaked\n")
    out = tmp_path / "out"
    sce.run(str(tmp_path / "raw"), str(out), "splunk", "9.4")
    for path in out.rglob("*.conf"):
        assert "$7$leaked" not in path.read_text(encoding="utf-8")


# ── tier / role classification ───────────────────────────────────────────────
@pytest.mark.parametrize("mode,expected", [
    ("manager", "cluster_manager"),
    ("master", "cluster_manager"),
    ("peer", "indexer"),
    ("slave", "indexer"),
    ("searchhead", "search_head"),
])
def test_classify_role_from_clustering_mode(sce, tmp_path, mode, expected):
    etc = _instance(tmp_path / "raw", "n", service="s",
                    server_conf=f"[clustering]\nmode = {mode}\n")
    assert sce.classify_role(str(etc), "") == expected


def test_classify_role_shclustering(sce, tmp_path):
    etc = _instance(tmp_path / "raw", "n", service="s",
                    server_conf="[shclustering]\nid = ABC\n")
    assert sce.classify_role(str(etc), "") == "search_head"


def test_classify_role_falls_back_to_hint(sce, tmp_path):
    etc = _instance(tmp_path / "raw", "n", service="s")  # no server.conf
    assert sce.classify_role(str(etc), "indexer") == "indexer"
    assert sce.classify_role(str(etc), "") == "standalone"


# ── control-plane staging capture ────────────────────────────────────────────
def test_control_plane_capture_marks_tier_present(sce, tmp_path):
    etc = _instance(tmp_path / "raw", "cm01", service="splunk-cm", role_hint="cluster_manager")
    _write(etc / "manager-apps" / "org_indexes" / "local" / "indexes.conf",
           "[main]\nhomePath = $SPLUNK_DB/main\n")
    out = tmp_path / "out"
    manifest = sce.run(str(tmp_path / "raw"), str(out), "splunk", "9.4")
    assert manifest["tiers"]["cluster_manager"]["present"] is True
    assert "org_indexes" in manifest["tiers"]["cluster_manager"]["apps"]
    assert (out / "manager-apps" / "org_indexes" / "local" / "indexes.conf").is_file()
    detected = manifest["meta"]["tiers_detected"]
    assert "cluster_manager" in detected
    # tiers_detected must be de-duplicated even when a tier flag and an instance
    # role name coincide (CM is both a present tier and an instance role).
    assert len(detected) == len(set(detected))


def test_deployment_server_serverclasses_parsed(sce, tmp_path):
    etc = _instance(tmp_path / "raw", "ds01", service="splunk-ds", role_hint="deployment_server")
    _write(etc / "deployment-apps" / "uf_base" / "local" / "outputs.conf", "[x]\nk = v\n")
    _write(etc / "system" / "local" / "serverclass.conf",
           "[serverClass:all_forwarders]\nwhitelist.0 = *\n"
           "[serverClass:all_forwarders:app:uf_base]\nrestartSplunkd = true\n"
           "[serverClass:linux:app:TA-nix]\nstateOnClient = enabled\n")
    out = tmp_path / "out"
    manifest = sce.run(str(tmp_path / "raw"), str(out), "splunk", "9.4")
    scs = manifest["tiers"]["deployment_server"]["serverclasses"]
    assert set(scs) == {"all_forwarders", "linux"}


# ── manifest + counts + secrets index ────────────────────────────────────────
def test_manifest_counts_and_secrets_index(sce, tmp_path):
    etc = _instance(tmp_path / "raw", "sh01", service="splunk-search", role_hint="search_head")
    _write(etc / "apps" / "org_dash" / "local" / "data" / "ui" / "views" / "overview.xml",
           "<dashboard><label>Overview</label></dashboard>")
    _write(etc / "apps" / "org_dash" / "local" / "data" / "ui" / "views" / "traffic.xml",
           "<dashboard/>")
    _write(etc / "apps" / "org_dash" / "local" / "savedsearches.conf",
           "[Errors]\nsearch = error\n[Warnings]\nsearch = warn\n")
    _write(etc / "system" / "local" / "web.conf", "[settings]\nsslPassword = $7$x\n")

    out = tmp_path / "out"
    manifest = sce.run(str(tmp_path / "raw"), str(out), "splunk", "9.4")

    counts = manifest["meta"]["counts"]
    assert counts["instances"] == 1
    assert counts["apps"] == 1
    assert counts["dashboards"] == 2
    assert counts["scrubbed_secrets"] >= 1
    assert (out / "SECRETS-SCRUBBED.md").is_file()
    index = (out / "SECRETS-SCRUBBED.md").read_text(encoding="utf-8")
    assert "web.conf" in index
    # app_detail reflects saved-search stanza count
    detail = manifest["tiers"]["instances"][0]["app_detail"][0]
    assert detail["saved_searches"] == 2
    assert detail["dashboards"] == 2


def test_shared_control_plane_readonly_file_recopies(sce, tmp_path):
    # Two indexer peers each carry the SAME manager-apps bundle with a read-only
    # (0444) file. The second copy must overwrite cleanly (regression: copy2
    # preserved 0444 → PermissionError on the second write).
    import os
    import stat
    raw = tmp_path / "raw"
    for label in ("splunk-idx-01", "splunk-idx-02"):
        etc = _instance(raw, label, service=label, role_hint="indexer")
        ro = etc / "manager-apps" / "_cluster" / "local" / "README"
        _write(ro, "shared read-only content\n")
        # Owner-read-only source is enough to exercise the re-copy path; world
        # bits are not part of the regression (and trip Sonar S2612).
        os.chmod(ro, stat.S_IRUSR)  # 0400

    out = tmp_path / "out"
    manifest = sce.run(str(raw), str(out), "splunk", "9.4")  # must not raise
    dst = out / "manager-apps" / "_cluster" / "local" / "README"
    assert dst.is_file()
    assert os.access(dst, os.W_OK)  # committed snapshot files stay writable
    assert manifest["tiers"]["cluster_manager"]["present"] is True


def test_out_of_scope_flagged_in_manifest(sce, tmp_path):
    _instance(tmp_path / "raw", "sh01", service="s", role_hint="search_head")
    out = tmp_path / "out"
    manifest = sce.run(str(tmp_path / "raw"), str(out), "splunk", "9.4")
    assert "kvstore_data" in manifest["out_of_scope_v1"]
    assert "index_data" in manifest["out_of_scope_v1"]


def test_empty_raw_dir_still_emits_manifest(sce, tmp_path):
    """Bare-minimum retrieve: scrub an empty capture without raising."""
    raw = tmp_path / "raw"
    raw.mkdir()
    out = tmp_path / "out"
    manifest = sce.run(str(raw), str(out), "splunk", "9.4")
    assert manifest["meta"]["counts"]["instances"] == 0
    assert manifest["tiers"]["instances"] == []
    assert (out / "SECRETS-SCRUBBED.md").is_file()


def test_resolve_etc_host_prefers_etc_destination(tmp_path):
    """Host-side fallback: pick /opt/splunk/etc volume under /var/lib/docker."""
    import importlib.util

    path = (REPO_ROOT / "ansible" / "roles" / "splunk_config"
            / "files" / "resolve_etc_host.py")
    spec = importlib.util.spec_from_file_location("resolve_etc_host", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    mounts = [
        {
            "Type": "volume",
            "Name": "splunk_var",
            "Source": "/var/lib/docker/volumes/splunk_var/_data",
            "Destination": "/opt/splunk/var",
            "Driver": "local",
        },
        {
            "Type": "volume",
            "Name": "splunk_etc",
            "Source": "/var/lib/docker/volumes/splunk_etc/_data",
            "Destination": "/opt/splunk/etc",
            "Driver": "local",
        },
    ]
    assert mod.resolve(mounts, "/var/lib/docker") == (
        "/var/lib/docker/volumes/splunk_etc/_data"
    )


def test_resolve_etc_host_reconstructs_volume_path_when_source_empty(tmp_path):
    import importlib.util

    path = (REPO_ROOT / "ansible" / "roles" / "splunk_config"
            / "files" / "resolve_etc_host.py")
    spec = importlib.util.spec_from_file_location("resolve_etc_host", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    mounts = [
        {
            "Type": "volume",
            "Name": "splunk_etc",
            "Destination": "/opt/splunk/etc",
            "Driver": "local",
        },
    ]
    assert mod.resolve(mounts, "/var/lib/docker") == (
        "/var/lib/docker/volumes/splunk_etc/_data"
    )


def test_mount_metadata_recorded_in_instance_entry(sce, tmp_path):
    """NFS / bind forensics: mounts from _capture.json land in the manifest."""
    inst = tmp_path / "raw" / "splunk-search"
    etc = inst / "etc"
    etc.mkdir(parents=True)
    _write(
        inst / "_capture.json",
        json.dumps({
            "service": "splunk_splunk-search",
            "node": "wkr-03",
            "role_hint": "search_head",
            "image": "splunk/splunk:9.4",
            "container_name": "splunk_splunk-search.1.abc",
            "capture_via": "host_volume",
            "docker_root": "/var/lib/docker",
            "etc_host": "/var/lib/docker/volumes/splunk_etc/_data",
            "mounts": [
                {
                    "Type": "volume",
                    "Name": "splunk_apps",
                    "Source": "/var/lib/docker/volumes/splunk_apps/_data",
                    "Destination": "/opt/splunk/etc/apps",
                    "Driver": "local",
                },
                {
                    "Type": "bind",
                    "Source": "/opt/splunk-search",
                    "Destination": "/opt/splunk/var",
                },
            ],
        }),
    )
    _write(etc / "system" / "local" / "web.conf", "[settings]\nhttpport = 8000\n")

    out = tmp_path / "out"
    manifest = sce.run(str(tmp_path / "raw"), str(out), "splunk", "9.4")
    entry = manifest["tiers"]["instances"][0]
    assert entry["image"] == "splunk/splunk:9.4"
    assert entry["container_name"] == "splunk_splunk-search.1.abc"
    assert entry["capture_via"] == "host_volume"
    assert entry["docker_root"] == "/var/lib/docker"
    assert entry["etc_host"] == "/var/lib/docker/volumes/splunk_etc/_data"
    assert len(entry["mounts"]) == 2
    by_dest = {m["destination"]: m for m in entry["mounts"]}
    assert by_dest["/opt/splunk/etc/apps"]["type"] == "volume"
    assert by_dest["/opt/splunk/etc/apps"]["name"] == "splunk_apps"
    assert by_dest["/opt/splunk/etc/apps"]["source"].startswith("/var/lib/docker/volumes/")
    assert by_dest["/opt/splunk/var"]["type"] == "bind"


def test_version_promoted_from_capture_meta(sce, tmp_path):
    etc = _instance(tmp_path / "raw", "api01", service="standalone", role_hint="standalone")
    # overwrite capture with API-style version field
    _write(
        tmp_path / "raw" / "api01" / "_capture.json",
        json.dumps({
            "service": "standalone",
            "node": "api",
            "capture_via": "api",
            "splunk_version": "9.4.13",
        }),
    )
    _write(etc / "system" / "local" / "web.conf", "[settings]\nhttpport = 8000\n")
    out = tmp_path / "out"
    manifest = sce.run(str(tmp_path / "raw"), str(out), "work", "unknown")
    assert manifest["meta"]["splunk_version"] == "9.4.13"
