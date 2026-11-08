"""Archive completion depends on upstream hashes and safe regular-file paths."""
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "roles/model_archive/files/archive_model.py"
SPEC = importlib.util.spec_from_file_location("archive_model", SOURCE)
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


def test_source_hash_is_required(tmp_path):
    payload = b"model weights"
    (tmp_path / "model.bin").write_bytes(payload)
    item = {"path": "model.bin", "size": len(payload),
            "lfs_sha256": hashlib.sha256(payload).hexdigest()}
    assert archive.verify_file(tmp_path, item) == item["lfs_sha256"]
    (tmp_path / "model.bin").write_bytes(b"MODEL WEIGHTS")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        archive.verify_file(tmp_path, item)


def test_original_readme_git_blob_verified(tmp_path):
    payload = b"# Original upstream model card\n"
    (tmp_path / "README.md").write_bytes(payload)
    item = {"path": "README.md", "size": len(payload),
            "git_blob_id": hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()}
    assert archive.verify_file(tmp_path, item) == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside"])
def test_path_escape_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        archive.safe_path(tmp_path, name)


def test_external_symlink_rejected(tmp_path):
    (tmp_path / "model.bin").symlink_to("/tmp/outside-model-archive")
    with pytest.raises(ValueError):
        archive.safe_path(tmp_path, "model.bin")


@pytest.mark.parametrize("corrupt", [True, False])
def test_missing_readme_is_documented_but_corruption_never_completes(tmp_path, monkeypatch, corrupt):
    payload = b"weights"
    revision = "a" * 40
    sibling = SimpleNamespace(rfilename="model.bin", size=len(payload), blob_id=None,
                              lfs=SimpleNamespace(sha256=hashlib.sha256(payload).hexdigest()))
    info = SimpleNamespace(sha=revision, siblings=[sibling])

    def download(_repo, revision, local_dir, max_workers):
        local_dir.mkdir(parents=True)
        (local_dir / "model.bin").write_bytes(b"corrupt" if corrupt else payload)

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(
        HfApi=lambda: SimpleNamespace(model_info=lambda *a, **kw: info),
        snapshot_download=download))
    monkeypatch.setattr(sys, "argv", ["archive", "--root", str(tmp_path),
                                     "--repo", "owner/model", "--revision", revision])
    metadata = tmp_path / "huggingface/owner/model" / revision / "archive-metadata"
    if corrupt:
        with pytest.raises(ValueError, match="Checksum mismatch"):
            archive.main()
        assert not (metadata / "VERIFIED.json").exists()
    else:
        archive.main()
        assert (metadata / "VERIFIED.json").is_file()
        assert "no README/model card" in (metadata / "ABOUT.txt").read_text()
        assert not (metadata.parent / "snapshot/README.md").exists()


@pytest.mark.parametrize("readme", [True, False])
def test_local_description_preserves_card_availability_without_touching_snapshots(readme):
    import base64
    import json
    import yaml
    from _templating import render

    tasks = yaml.safe_load((SOURCE.parents[1] / "tasks/main.yml").read_text())
    task = next(t for t in tasks[1]["block"] if t["name"] == "Publish separate local model descriptions")
    model = {"repo": "owner/model", "revision": "a" * 40}
    manifest = {"files": [{"path": "README.md"}] if readme else [{"path": "model.bin"}]}
    item = {"item": model, "content": base64.b64encode(json.dumps(manifest).encode()).decode()}
    scope = {"item": item, "model_archive_root": "/srv/archive", **task["vars"]}
    copy = task["ansible.builtin.copy"]
    target = render(copy["dest"], scope)
    assert target.endswith("/archive-metadata/ABOUT.txt")
    assert "/snapshot/" not in target
    content = render(copy["content"], scope)
    assert "not an upstream model card" in content
    assert ("Upstream provides no README/model card" in content) is not readme
    assert ("Original upstream README preserved unchanged" in content) is readme
