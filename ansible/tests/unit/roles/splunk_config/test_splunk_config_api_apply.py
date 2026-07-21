"""Unit tests for roles/splunk_config/files/splunk_config_api_apply.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
MODULE_PATH = (
    REPO_ROOT / "ansible" / "roles" / "splunk_config" / "files" / "splunk_config_api_apply.py"
)


@pytest.fixture(scope="module")
def saa():
    spec = importlib.util.spec_from_file_location("splunk_config_api_apply", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parse_conf_basic(saa):
    text = "[default]\nfoo = 1\n[bar]\nkey = value\n# c\nempty =\n"
    st = saa.parse_conf(text)
    assert st["default"]["foo"] == "1"
    assert st["bar"]["key"] == "value"


def test_filter_props_skips_scrubbed_and_forbidden(saa):
    props = {
        "token": "<SCRUBBED:secrets>",
        "password": "plaintext",
        "SHOULD_LINEMERGE": "true",
        "blank": "",
    }
    kept, skipped = saa.filter_props(
        props, skip_forbidden=True, forbidden_keys={"password", "token"},
    )
    assert kept == {"SHOULD_LINEMERGE": "true"}
    assert skipped == 3


def test_is_stock_app(saa):
    assert saa.is_stock_app("search")
    assert saa.is_stock_app("splunk_httpinput")
    assert not saa.is_stock_app("ansible_audit")


def test_surface_loader_accepts_extra_conf(tmp_path):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[5] / "ansible" / "roles" / "splunk_config" / "files" / "splunk_config_surface.py"
    spec = importlib.util.spec_from_file_location("surface", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    surface_path = tmp_path / "s.json"
    surface_path.write_text(
        '{"conf_files": ["server", "multikv"], "stock_conf_files": ["props"]}',
        encoding="utf-8",
    )
    s = mod.load_surface(str(surface_path))
    assert "multikv" in s["conf_files"]
    assert s["stock_conf_files"] == ["props"]
    # missing keys filled from default
    assert "apply_forbidden_keys" in s
