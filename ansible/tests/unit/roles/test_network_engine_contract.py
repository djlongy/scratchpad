"""The contract BETWEEN the two network filter plugins.

`network_catalog.py` builds the segment model; `network_views.py` projects it.
Since the catalog's own view engine was deleted, that is no longer a dependency
at all: neither imports the other, and they communicate purely through data — a
list of enriched segment rows.

This file guards the BOUNDARY. Behaviour lives in test_network_catalog.py and
test_network_views.py.

HISTORY, because it explains why a boundary is worth a test. Both engines once
answered the same question — "is this the same KIND of thing?", asked when
comparing a `where:` value against the data — each from its own copy of the
rule. It diverged twice in two rounds, and the second time a `where` the
catalog accepted made the views engine abort a live play. The copy was replaced
by a path-import from the catalog, and now by nothing: the catalog's only
caller of that rule was its own view engine, so with Stage 3 gone it has one
home and one caller.

The lesson worth keeping is that the fix for drift was never a detector. It was
deleting the second copy.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

_PLUGINS = pathlib.Path(__file__).resolve().parents[3] / "plugins" / "filter"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _PLUGINS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


nc = _load("network_catalog")
nv = _load("network_views")


# ── the boundary ──────────────────────────────────────────────────────────


def test_neither_engine_reaches_into_the_other():
    """They communicate through data — enriched rows — and nothing else.

    Checked for the path-import form too, not just `import x`: this repo used
    `spec_from_file_location` to share code across plugins, because Ansible
    does not put plugins/filter/ on sys.path.
    """
    for name, other in (("network_catalog", "network_views"),
                        ("network_views", "network_catalog")):
        source = (_PLUGINS / f"{name}.py").read_text(encoding="utf-8")
        offending = [
            line.strip() for line in source.splitlines()
            if re.match(rf"\s*(import {other}\b|from {other} import)", line)
            or f'"{other}"' in line and "spec_from_file_location" in line
        ]
        assert not offending, f"{name} reaches into {other}: {offending}"


def test_the_catalog_needs_no_ansible_at_all():
    """Its tests run without Ansible installed — that is what lets the model be
    exercised in CI without a control node."""
    source = (_PLUGINS / "network_catalog.py").read_text(encoding="utf-8")
    assert "import ansible" not in source
    assert "from ansible" not in source


def test_the_catalog_no_longer_ships_a_view_engine():
    """A SECOND view engine in this repo is the condition that produced every
    drift bug this file was written for. If one returns, it returns
    deliberately."""
    source = (_PLUGINS / "network_catalog.py").read_text(encoding="utf-8")
    for gone in ("def build_view(", "def build_views(", "def _render_field(",
                 "def _build_row(", "def _validate_views("):
        assert gone not in source, f"{gone} is back in network_catalog"


def test_the_kind_rule_has_exactly_one_definition():
    """A second `def _value_kind` under plugins/filter/ means someone copied it
    again — which is how this started."""
    definitions = [
        path.name
        for path in sorted(_PLUGINS.glob("*.py"))
        if re.search(r"^def _value_kind\(", path.read_text(encoding="utf-8"), re.M)
    ]
    assert definitions == ["network_views.py"], (
        f"_value_kind is defined in {definitions}; it belongs only where it is "
        f"asked, which is network_views.py"
    )


# ── the data contract that boundary carries ───────────────────────────────


CATALOG_CONFIG = {
    "platforms": ["fw", "edge"],
    "name_default": "name",
    "names": {"name": {"parts": ["purpose"]}},
    "required": {"all": []},
}
SEGMENTS = {
    "web": {"vlan_id": 10, "platforms": ["fw"], "purpose": "web",
            "subnet": "10.0.0.0/24", "gateway": "10.0.0.1", "site": "alpha"},
    "db": {"vlan_id": 11, "platforms": ["fw", "edge"], "purpose": "db",
           "site": "beta"},
}


@pytest.fixture(scope="module")
def rows():
    catalog = nc.network_catalog(SEGMENTS, CATALOG_CONFIG)
    assert catalog["errors"] == [], catalog["errors"]
    return catalog["segments"]


def test_the_catalog_emits_the_membership_flags_the_views_engine_filters_on(rows):
    """`platform: fw` selects on `on_fw`, which the catalog derives from
    `platforms[]`. If it stopped, every view would build nothing — silently,
    because an empty list is a legal list."""
    for row in rows:
        for platform in CATALOG_CONFIG["platforms"]:
            assert isinstance(row[f"on_{platform}"], bool)
    assert [r["key"] for r in rows if r["on_fw"]] == ["web", "db"]
    assert [r["key"] for r in rows if r["on_edge"]] == ["db"]


def test_a_catalog_row_is_a_plain_mapping_the_views_engine_accepts(rows):
    """The whole interface: a list of mappings, projected without translation."""
    from jinja2.nativetypes import NativeEnvironment

    env = NativeEnvironment()
    built = nv.network_view(
        env.from_string("").new_context(vars={}), rows,
        {"platform": "fw", "fields": {"n": "{{ seg.key }}",
                                      "v": "{{ seg.vlan_id }}"}}, "contract")
    assert built == [{"n": "web", "v": 10}, {"n": "db", "v": 11}]


def test_the_enriched_fields_a_view_relies_on_are_present(rows):
    """Named explicitly, so deleting one from `enrich` fails HERE — next to the
    reason — rather than as an empty column on a device."""
    web = next(r for r in rows if r["key"] == "web")
    for field in ("key", "name", "vlan_id", "platforms", "tagged", "prefixlen",
                  "gateway_cidr", "names", "site"):
        assert field in web, f"enrich no longer emits {field!r}"


def test_grouping_and_chaining_work_on_real_catalog_rows(rows):
    """group_by and source exist to serve real segments, not fixtures."""
    from jinja2.nativetypes import NativeEnvironment

    env = NativeEnvironment()
    built = nv.network_views(
        env.from_string("").new_context(vars={}), rows,
        {"ns": {
            "flat": {"platform": "fw", "fields": {"n": "{{ seg.key }}",
                                                  "s": "{{ seg.site }}"}},
            "bysite": {"source": "flat", "group_by": "s",
                       "fields": {"n": "{{ seg.n }}"}}}})
    assert built["ns"]["bysite"] == {"alpha": [{"n": "web"}],
                                     "beta": [{"n": "db"}]}
