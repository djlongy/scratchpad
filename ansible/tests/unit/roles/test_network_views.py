"""Unit tests for plugins/filter/network_views.py.

Two jobs. First, that ordinary Ansible template syntax in a views file
produces the right rows with the right TYPES. Second — the larger half — that
every malformed input fails with a message naming the view and the offending
key, because the alternative is a wrong list reaching a device in silence.

Runs without ansible installed, like the sibling network_catalog tests.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import sys

import pytest
from jinja2 import Environment
from jinja2.nativetypes import NativeEnvironment

_PLUGIN = (
    pathlib.Path(__file__).resolve().parents[3] / "plugins" / "filter" / "network_views.py"
)

# A real Templar finds `network_view` the way a play does — by loading the
# filter plugin directory. Set before ansible is imported anywhere below.
os.environ.setdefault("ANSIBLE_FILTER_PLUGINS", str(_PLUGIN.parent))
_spec = importlib.util.spec_from_file_location("network_views", _PLUGIN)
nv = importlib.util.module_from_spec(_spec)
sys.modules["network_views"] = nv
_spec.loader.exec_module(nv)

AnsibleFilterError = nv.AnsibleFilterError

# A fictional estate — nothing like this repo's, so a change that bakes in
# the estate's vocabulary fails here.
SEGMENTS = [
    {"key": "web", "on_gamma": True, "on_edge": True, "name": "SEG1101-WEB",
     "short_name": "WEB", "vlan_id": 1101, "vswitch": "dvs-a", "ports": 8,
     "site": "alpha", "tagged": True, "relaxed": True},
    {"key": "db", "on_gamma": True, "on_edge": False, "name": "SEG1102-DB",
     "short_name": "DB", "vlan_id": 1102, "vswitch": "dvs-a",
     "site": "alpha", "tagged": True},
    {"key": "mgmt", "on_gamma": False, "on_edge": True, "name": "SEG0-MGMT",
     "short_name": "MGMT", "vlan_id": 0, "vswitch": "dvs-b",
     "site": "beta", "tagged": False},
]


def ctx(scope=None, native=True):
    env = NativeEnvironment() if native else Environment()
    return env.from_string("").new_context(vars=dict(scope or {}))


def view(spec, scope=None, native=True, rows=SEGMENTS):
    return nv.network_view(ctx(scope, native), rows, spec, "test.view")


def refused(spec, *fragments, rows=SEGMENTS, scope=None):
    """The error must be an AnsibleFilterError that names the problem."""
    try:
        view(spec, scope=scope, rows=rows)
    except AnsibleFilterError as err:
        message = str(err)
        for fragment in fragments:
            assert fragment in message, f"message lacks {fragment!r}: {message}"
        return message
    except Exception as exc:  # noqa: BLE001 - precisely what must not happen
        pytest.fail(f"raised {type(exc).__name__} instead of "
                    f"AnsibleFilterError: {exc}")
    pytest.fail("no error raised — the input was silently accepted")


# ── it does the job ───────────────────────────────────────────────────────


def test_projects_rows_and_keeps_types():
    rows = view({"platform": "gamma", "fields": {
        "name": "{{ seg.name }}",
        "vlan": "{{ seg.vlan_id }}",
        "ports": "{{ seg.ports | default(0) }}",
        "relaxed": "{{ seg.relaxed | default(false) }}",
    }})
    assert [r["name"] for r in rows] == ["SEG1101-WEB", "SEG1102-DB"]
    assert isinstance(rows[0]["vlan"], int)
    assert isinstance(rows[0]["ports"], int)
    assert isinstance(rows[0]["relaxed"], bool)
    assert rows[1]["ports"] == 0        # the default fired


def test_interpolation_and_tilde_concatenation():
    rows = view({"platform": "gamma", "fields": {
        "fqdn": "{{ seg.key }}.{{ seg.site }}.example.net",
        "label": "{{ seg.site ~ '/' ~ seg.key }}",
    }})
    assert rows[0] == {"fqdn": "web.alpha.example.net", "label": "alpha/web"}


def test_where_filters_before_projection():
    rows = view({"platform": "edge", "where": {"tagged": True},
                 "fields": {"n": "{{ seg.key }}"}})
    assert [r["n"] for r in rows] == ["web"]


def test_consts_and_append():
    rows = view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                 "consts": {"kind": "vlan"}, "append": [{"n": "HAND"}]})
    assert all(r.get("kind") == "vlan" for r in rows[:2])
    assert rows[-1] == {"n": "HAND"}


def test_spec_level_templates_resolve_from_the_caller_scope():
    rows = view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                 "where": {"site": "{{ target }}"},
                 "consts": {"owner": "{{ who }}"},
                 "append": "{{ extras }}"},
                scope={"target": "alpha", "who": "netops",
                       "extras": [{"n": "HAND"}]})
    assert [r["n"] for r in rows] == ["web", "db", "HAND"]
    assert rows[0]["owner"] == "netops"


def test_scope_resolution_never_enumerates_the_context():
    """Enumerating an Ansible context forces every lazy lookup to evaluate."""
    class Landmine:
        def __iter__(self):
            raise AssertionError("the context was enumerated")

    rows = view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                 "append": "{{ extras }}"},
                scope={"extras": [{"n": "OK"}], "landmine": Landmine()})
    assert rows[-1] == {"n": "OK"}


def test_row_values_are_not_re_templated():
    rows = [dict(SEGMENTS[0], key="{{ 7 * 6 }}")]
    out = view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"}}, rows=rows)
    assert out[0]["n"] == "{{ 7 * 6 }}"


def test_network_views_namespaces_the_output():
    built = nv.network_views(ctx(), SEGMENTS, {
        "gamma": {"a": {"platform": "gamma", "fields": {"n": "{{ seg.key }}"}}},
        "edge": {"b": {"platform": "edge", "fields": {"n": "{{ seg.key }}"}}},
    })
    assert sorted(built) == ["edge", "gamma"]
    assert [r["n"] for r in built["gamma"]["a"]] == ["web", "db"]


# ── it refuses everything else ────────────────────────────────────────────


def test_unknown_spec_key_is_rejected_with_a_suggestion():
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
             "wheree": {"site": "alpha"}}, "wheree", "Did you mean 'where'?")


def test_missing_platform_is_rejected():
    refused({"fields": {"n": "{{ seg.key }}"}}, "platform")


def test_platform_no_segment_carries_is_rejected():
    refused({"platform": "wifi", "fields": {"n": "{{ seg.key }}"}},
            "wifi", "Declared:")


def test_platform_typo_suggests_the_real_one():
    refused({"platform": "gammax", "fields": {"n": "{{ seg.key }}"}},
            "Did you mean 'gamma'?")


def test_missing_or_malformed_fields_is_rejected():
    refused({"platform": "gamma"}, "fields")
    refused({"platform": "gamma", "fields": "{{ seg.key }}"}, "fields")
    refused({"platform": "gamma", "fields": {"n": ["{{ seg.key }}"]}}, "fields.n")


def test_consts_clashing_with_a_field_is_rejected():
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
             "consts": {"n": "clobbered"}}, "consts", "n")


def test_where_on_an_unknown_field_is_rejected():
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
             "where": {"sitte": "alpha"}}, "sitte", "Did you mean 'site'?")


def test_where_with_a_mistyped_value_is_rejected():
    # Kinds are named coarsely ("text", "a number") rather than by Python
    # class, because Ansible's str subclasses are not a type mismatch.
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
             "where": {"vlan_id": "1101"}}, "vlan_id", "text", "a number")


def test_where_that_legitimately_matches_nothing_is_allowed():
    assert view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                 "where": {"site": "gamma"}}) == []


def test_append_of_the_wrong_shape_is_rejected():
    for bad in ({"n": "x"}, ["scalar"], 7):
        refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                 "append": bad}, "append")


def test_rows_of_the_wrong_shape_are_rejected():
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"}},
            "rows", rows=None)
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"}},
            "rows", rows={"a": 1})
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"}},
            "rows[0]", rows=["nope"])


def test_a_broken_expression_names_the_field_and_the_segment():
    refused({"platform": "gamma", "fields": {"n": "{{ seg.vlan_id / 0 }}"}},
            "fields.n", "web")


def test_undefined_source_without_a_default_is_fatal():
    refused({"platform": "gamma", "fields": {"n": "{{ seg.nope.deeper }}"}},
            "fields.n")


def test_a_non_native_caller_still_gets_typed_rows():
    """Replaces the old "a non-native environment is refused" test.

    That test guarded a real hazard — fields rendering to strings and losing
    their type — but it guarded it by inspecting the CALLER's environment and
    refusing. Fields are now rendered on the plugin's own NativeEnvironment,
    so the caller's environment cannot cost us types at all. The hazard is
    designed out rather than detected, and the assertion worth making is the
    outcome, not the refusal: a plain (non-native) caller still gets an int.
    """
    rows = view({"platform": "gamma", "fields": {"v": "{{ seg.vlan_id }}",
                                               "t": "{{ seg.tagged }}"}},
                native=False)
    assert isinstance(rows[0]["v"], int)
    assert isinstance(rows[0]["t"], bool)


def test_views_catalog_of_the_wrong_shape_is_rejected():
    with pytest.raises(AnsibleFilterError):
        nv.network_views(ctx(), SEGMENTS, ["not", "a", "mapping"])
    with pytest.raises(AnsibleFilterError) as err:
        nv.network_views(ctx(), SEGMENTS, {"gamma": ["nope"]})
    assert "gamma" in str(err.value)


def test_segments_are_never_mutated():
    before = [dict(r) for r in SEGMENTS]
    view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
          "consts": {"extra": 1}})
    assert SEGMENTS == before


# ──────────────────────────────────────────────────────────────────────────
# Production fidelity — the REAL Ansible environment
# ──────────────────────────────────────────────────────────────────────────
# Bare Jinja and Ansible disagree on undefined: jinja2.Undefined renders to
# nothing, AnsibleUndefined raises when used. Every test above runs on the
# former, so none of them prove what a typo does in a play. These do, and
# skip cleanly when ansible is not installed.


def ansible_view(spec, scope=None, rows=SEGMENTS):
    """Build a view the way a play does — through a real Templar.

    These used to mint a context out of band:
        env.from_string("").new_context(vars=...)
    ansible-core 2.19+ cannot support that. Lazy container creation asks for
    the ambient TemplateContext, which only exists inside a real templating
    call, so every one of these tests died in setup with "A required
    TemplateContext context is not active" — before the filter under test ran
    at all. Going through Templar.template() is both the fix and an
    improvement: the filter is now reached the same way a play reaches it,
    which is what "production fidelity" was supposed to mean.
    """
    pytest.importorskip("ansible", reason="production-fidelity tests")
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template

    variables = dict(scope or {})
    variables.update({"_rows": rows, "_spec": spec})
    return Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template("{{ _rows | network_view(_spec, 'test.view') }}"))


def test_ansible_env_is_native_so_types_survive():
    rows = ansible_view({"platform": "gamma", "fields": {
        "vlan": "{{ seg.vlan_id }}", "on": "{{ seg.tagged }}"}})
    assert isinstance(rows[0]["vlan"], int)
    assert isinstance(rows[0]["on"], bool)


def test_one_level_typo_is_fatal_under_ansible():
    """The gap: bare Jinja returns Undefined here and says nothing."""
    with pytest.raises(AnsibleFilterError) as err:
        ansible_view({"platform": "gamma", "fields": {"s": "{{ seg.vswich }}"}})
    message = str(err.value)
    assert "fields.s" in message
    assert "vswich" in message


def test_a_play_variable_in_fields_resolves():
    rows = ansible_view({"platform": "gamma",
                         "fields": {"s": "{{ site_default_switch }}"}},
                        scope={"site_default_switch": "dvs-a"})
    assert all(r["s"] == "dvs-a" for r in rows)


def test_a_field_can_fall_back_from_the_segment_to_a_play_variable():
    """The obvious thing to write, which used to fail for no good reason."""
    rows = ansible_view({"platform": "gamma", "fields": {
        "switch": "{{ seg.vswitch | default(site_default_switch) }}",
        "missing": "{{ seg.nothing | default(site_default_switch) }}"}},
        scope={"site_default_switch": "dvs-fallback"})
    assert rows[0]["switch"] == "dvs-a"            # the segment wins
    assert rows[0]["missing"] == "dvs-fallback"    # the play var fills in


def test_a_name_in_neither_the_segment_nor_the_play_is_still_fatal():
    """Widening the scope must not reintroduce the silent-typo hole."""
    with pytest.raises(AnsibleFilterError) as err:
        ansible_view({"platform": "gamma", "fields": {"s": "{{ no_such_var }}"}})
    message = str(err.value)
    assert "no_such_var" in message
    assert "is in scope" in message


def test_play_variables_are_resolved_by_name_never_by_enumeration():
    """Enumerating a real context forces every lazy lookup, Vault included."""
    class Landmine:
        def __iter__(self):
            raise AssertionError("the context was enumerated")

    rows = ansible_view({"platform": "gamma",
                         "fields": {"s": "{{ site_default_switch }}"}},
                        scope={"site_default_switch": "dvs-a",
                               "landmine": Landmine()})
    assert rows[0]["s"] == "dvs-a"


def test_a_play_variable_in_consts_resolves():
    rows = ansible_view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                         "consts": {"switch": "{{ site_default_switch }}"}},
                        scope={"site_default_switch": "dvs-a"})
    assert all(r["switch"] == "dvs-a" for r in rows)


def test_a_default_still_absorbs_a_missing_field_under_ansible():
    rows = ansible_view({"platform": "gamma", "fields": {
        "ports": "{{ seg.ports | default(0) }}"}})
    assert [r["ports"] for r in rows] == [8, 0]


# ──────────────────────────────────────────────────────────────────────────
# unique_by — one row per distinct combination of OUTPUT keys
# ──────────────────────────────────────────────────────────────────────────
# Same VLAN id at two sites: two L2 domains sharing a tag. A per-device view
# wants them separate; a trunk allow-list across both wants one row.
MULTISITE = [
    {"key": "a", "on_gamma": True, "vlan_id": 10, "site": "alpha", "tagged": True},
    {"key": "b", "on_gamma": True, "vlan_id": 10, "site": "beta", "tagged": True},
    {"key": "c", "on_gamma": True, "vlan_id": 20, "site": "alpha", "tagged": True},
]


def test_without_unique_by_every_row_is_kept():
    rows = view({"platform": "gamma", "fields": {"id": "{{ seg.vlan_id }}"}},
                rows=MULTISITE)
    assert [r["id"] for r in rows] == [10, 10, 20]


def test_unique_by_one_key_collapses_the_repeat():
    rows = view({"platform": "gamma", "unique_by": ["id"],
                 "fields": {"id": "{{ seg.vlan_id }}"}}, rows=MULTISITE)
    assert [r["id"] for r in rows] == [10, 20]


def test_unique_by_two_keys_keeps_what_is_genuinely_distinct():
    rows = view({"platform": "gamma", "unique_by": ["id", "site"],
                 "fields": {"id": "{{ seg.vlan_id }}",
                            "site": "{{ seg.site }}"}}, rows=MULTISITE)
    assert [(r["id"], r["site"]) for r in rows] == [
        (10, "alpha"), (10, "beta"), (20, "alpha")]


def test_unique_by_keeps_the_first_occurrence():
    rows = view({"platform": "gamma", "unique_by": ["id"],
                 "fields": {"id": "{{ seg.vlan_id }}",
                            "key": "{{ seg.key }}"}}, rows=MULTISITE)
    assert [r["key"] for r in rows] == ["a", "c"]


def test_unique_by_may_name_a_const():
    rows = view({"platform": "gamma", "unique_by": ["kind"],
                 "fields": {"id": "{{ seg.vlan_id }}"},
                 "consts": {"kind": "vlan"}}, rows=MULTISITE)
    assert len(rows) == 1


def test_unique_by_as_a_bare_string_is_rejected():
    """One shape only: a list. The error says how to fix it."""
    refused({"platform": "gamma", "unique_by": "id",
             "fields": {"id": "{{ seg.vlan_id }}"}},
            "unique_by", "[id]", rows=MULTISITE)


def test_unique_by_naming_a_source_field_is_rejected():
    """Dedup runs on the projected rows, so it names OUTPUT keys."""
    refused({"platform": "gamma", "unique_by": ["vlan_id"],
             "fields": {"id": "{{ seg.vlan_id }}"}},
            "vlan_id", "output key", rows=MULTISITE)


def test_unique_by_typo_suggests_the_real_output_key():
    refused({"platform": "gamma", "unique_by": ["idd"],
             "fields": {"id": "{{ seg.vlan_id }}"}},
            "Did you mean 'id'?", rows=MULTISITE)


def test_append_rows_are_not_deduped():
    """Hand-written rows pass through untouched, as append promises."""
    rows = view({"platform": "gamma", "unique_by": ["id"],
                 "fields": {"id": "{{ seg.vlan_id }}"},
                 "append": [{"id": 10}]}, rows=MULTISITE)
    assert [r["id"] for r in rows] == [10, 20, 10]


# ──────────────────────────────────────────────────────────────────────────
# The people who edit views are not Python developers
# ──────────────────────────────────────────────────────────────────────────
# Everything a view can do has to be discoverable from the YAML and the
# README. A key added to the engine without documenting it is invisible to
# the only people who use it, so these tests fail on that drift.

_ROLE = pathlib.Path(__file__).resolve().parents[3] / "roles" / "network_views"


def test_the_editable_views_file_documents_every_spec_key():
    header = (_ROLE.parents[1] / "playbooks" / "networks_views.yml").read_text()
    undocumented = [k for k in nv.SPEC_KEYS if f"{k}:" not in header]
    assert not undocumented, (
        f"views.yml does not mention {undocumented} — someone editing it "
        f"cannot discover those keys")


def test_the_readme_documents_every_spec_key():
    readme = (_ROLE / "README.md").read_text()
    undocumented = [k for k in nv.SPEC_KEYS if k not in readme]
    assert not undocumented, f"README.md does not mention {undocumented}"


def test_the_loop_variable_is_named_in_the_editable_file():
    """`seg` is the one piece of vocabulary a view author must know."""
    header = (_ROLE.parents[1] / "playbooks" / "networks_views.yml").read_text()
    assert f"`{nv.LOOP_VAR}`" in header


def test_the_role_declares_every_knob_it_reads():
    """A knob with no entry in defaults/ is undiscoverable and unsettable."""
    defaults = (_ROLE / "defaults" / "main.yml").read_text()
    tasks = "".join(p.read_text() for p in (_ROLE / "tasks").glob("*.yml"))
    knobs = {m for m in re.findall(r"network_views_[a-z_]+", tasks)}
    # facts the role SETS are outputs, not knobs
    outputs = {"network_views_built", "network_views_refs",
               "network_views_chosen"}
    missing = sorted(k for k in knobs - outputs if f"{k}:" not in defaults)
    assert not missing, f"knobs read but never declared in defaults: {missing}"


# ──────────────────────────────────────────────────────────────────────────
# Chaos round 2 — findings from an independent adversarial pass
# ──────────────────────────────────────────────────────────────────────────
def test_an_undefined_field_never_reaches_the_output_row():
    """V-UNDEF-LEAK: a native env returns the Undefined OBJECT for a
    single-expression template, so it rode into the row and only exploded
    later at yaml.safe_dump — far from the view that caused it."""
    refused({"platform": "gamma", "fields": {"n": "{{ seg.no_such_field }}"}},
            "fields.n", "no_such_field")


def test_an_undefined_field_is_reported_with_the_segment_that_lacked_it():
    rows = [dict(SEGMENTS[0]), {"key": "gap", "on_gamma": True, "vlan_id": 99}]
    message = refused({"platform": "gamma", "fields": {"n": "{{ seg.name }}"}},
                      "gap", rows=rows)
    assert "name" in message


def test_a_default_still_absorbs_the_same_missing_field():
    rows = view({"platform": "gamma",
                 "fields": {"n": "{{ seg.no_such_field | default('-') }}"}})
    assert all(r["n"] == "-" for r in rows)


def test_unique_by_on_a_key_missing_from_a_row_is_refused():
    """V-UB-NONE-COLLAPSE: rows with no `site` all deduped to one, silently
    dropping distinct VLANs. Cannot honour the requested key, so say so."""
    rows = [{"key": "a", "on_gamma": True, "vlan_id": 10, "site": "alpha"},
            {"key": "b", "on_gamma": True, "vlan_id": 20},
            {"key": "c", "on_gamma": True, "vlan_id": 30}]
    refused({"platform": "gamma", "unique_by": ["site"],
             "fields": {"id": "{{ seg.vlan_id }}",
                        "site": "{{ seg.site | default(none) }}"}},
            "unique_by", "site", rows=rows)


def test_unique_by_still_works_when_every_row_has_the_key():
    rows = [{"key": "a", "on_gamma": True, "vlan_id": 10, "site": "alpha"},
            {"key": "b", "on_gamma": True, "vlan_id": 20, "site": "alpha"}]
    out = view({"platform": "gamma", "unique_by": ["site"],
                "fields": {"id": "{{ seg.vlan_id }}",
                           "site": "{{ seg.site }}"}}, rows=rows)
    assert len(out) == 1


def test_each_field_template_is_compiled_once_per_view_not_per_row():
    """V-NO-TMPL-CACHE: from_string ran per field PER ROW.

    Instruments the PLUGIN's environment, not the caller's. Field templates
    are compiled on `nv._ENV` now, so counting `from_string` on a caller env
    would count zero either way and the test would pass without proving
    anything.
    """
    env = NativeEnvironment()
    compiled = []
    original = nv._ENV.from_string
    nv._ENV.from_string = lambda src, *a, **k: (compiled.append(src),
                                                original(src, *a, **k))[1]
    ctx = env.from_string("").new_context(vars={})
    compiled.clear()
    try:
        nv.network_view(ctx, SEGMENTS * 10, {"platform": "gamma", "fields": {
            "a": "{{ seg.key }}", "b": "{{ seg.vlan_id }}"}}, "t")
    finally:
        # _ENV is module-global: without this the instrumented lambda leaks
        # into every test that runs after this one.
        nv._ENV.from_string = original
    field_compiles = [c for c in compiled if "seg." in c]
    assert len(field_compiles) == 2, (
        f"compiled {len(field_compiles)} times for 2 fields over 30 rows")


# ──────────────────────────────────────────────────────────────────────────
# Chaos round 3
# ──────────────────────────────────────────────────────────────────────────
def test_a_dot_in_a_namespace_or_list_name_is_refused():
    """R-NS-DOTTED: the role addresses a list as 'ns.name' and splits on the
    first dot, so a dotted namespace resolved to the wrong keys and printed
    VARIABLE IS NOT DEFINED while the play exited 0."""
    with pytest.raises(AnsibleFilterError) as err:
        nv.network_views(ctx(), SEGMENTS, {"ns.with.dots": {
            "x": {"platform": "gamma", "fields": {"n": "{{ seg.key }}"}}}})
    assert "ns.with.dots" in str(err.value)

    with pytest.raises(AnsibleFilterError) as err:
        nv.network_views(ctx(), SEGMENTS, {"gamma": {
            "list.with.dots": {"platform": "gamma",
                               "fields": {"n": "{{ seg.key }}"}}}})
    assert "list.with.dots" in str(err.value)


def test_ordinary_names_are_unaffected():
    built = nv.network_views(ctx(), SEGMENTS, {"gamma_extra": {
        "port_groups-v2": {"platform": "gamma", "fields": {"n": "{{ seg.key }}"}}}})
    assert built["gamma_extra"]["port_groups-v2"]


def test_unique_by_on_a_blank_key_is_refused_like_a_null_one():
    """V-UB-EMPTY-STR: '' is as unusable a dedup key as None — every blank row
    shares a signature and all but the first vanish."""
    rows = [{"key": "a", "on_gamma": True, "vlan_id": 1, "site": ""},
            {"key": "b", "on_gamma": True, "vlan_id": 2, "site": ""},
            {"key": "c", "on_gamma": True, "vlan_id": 3, "site": "x"}]
    refused({"platform": "gamma", "unique_by": ["site"],
             "fields": {"id": "{{ seg.vlan_id }}", "site": "{{ seg.site }}"}},
            "unique_by", "site", rows=rows)


# ──────────────────────────────────────────────────────────────────────────
# where: kinds are COARSE, so an Ansible string subclass is not a mismatch
#
# Segments are built in Python and carry plain `str`; a views file loaded via
# from_yaml carries AnsibleUnicode. Comparing type().__name__ called every
# correct string `where` a type mismatch and aborted the play.
# ──────────────────────────────────────────────────────────────────────────
class _AnsibleUnicode(str):
    """Stands in for ansible.parsing.yaml.objects.AnsibleUnicode."""


class _AnsibleUnsafeText(str):
    """Stands in for ansible.utils.unsafe_proxy.AnsibleUnsafeText."""


@pytest.mark.parametrize("subclass", [_AnsibleUnicode, _AnsibleUnsafeText])
def test_a_string_subclass_where_value_is_not_a_type_mismatch(subclass):
    assert view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                 "where": {"site": subclass("alpha")}}) == [{"n": "web"},
                                                            {"n": "db"}]


def test_a_string_subclass_on_the_SEGMENT_side_is_also_fine():
    """The mismatch can arrive from either direction — hand-built rows carry
    the subclass while a Python-built where value is a plain str."""
    rows = [{"key": "web", "on_gamma": True, "site": _AnsibleUnicode("alpha")}]
    assert view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                 "where": {"site": "alpha"}}, rows=rows) == [{"n": "web"}]


def test_a_genuine_text_vs_number_mismatch_is_still_refused():
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
             "where": {"vlan_id": "1101"}}, "vlan_id", "text", "a number")


def test_a_genuine_text_vs_boolean_mismatch_is_still_refused():
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
             "where": {"tagged": "true"}}, "tagged", "text", "a boolean")


def test_a_list_where_value_against_a_text_column_is_no_longer_skipped():
    """Lists and dicts used to bypass the type check entirely, so this went
    silently empty instead of saying why."""
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
             "where": {"site": ["alpha", "beta"]}}, "site", "a list")


def test_a_list_where_value_against_a_list_column_is_allowed():
    """Exact-match on a list-valued field is legitimate, so the check must
    not become a blanket refusal of lists."""
    rows = [{"key": "web", "on_gamma": True, "zones": ["a", "b"]},
            {"key": "db", "on_gamma": True, "zones": ["c"]}]
    assert view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                 "where": {"zones": ["a", "b"]}}, rows=rows) == [{"n": "web"}]


# ──────────────────────────────────────────────────────────────────────────
# platform inference reads BOOLEAN on_* keys only
# ──────────────────────────────────────────────────────────────────────────
def test_a_non_boolean_on_prefixed_field_is_not_a_platform():
    """`on_call_team: ops` is data. It used to make `platform: call_team`
    look like a declared platform."""
    rows = [{"key": "web", "on_gamma": True, "on_call_team": "ops"}]
    refused({"platform": "call_team", "fields": {"n": "{{ seg.key }}"}},
            "call_team", "not a platform this catalog knows", rows=rows)


def test_a_real_boolean_platform_still_resolves():
    rows = [{"key": "web", "on_gamma": True, "on_call_team": "ops"}]
    assert view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"}},
                rows=rows) == [{"n": "web"}]


# ──────────────────────────────────────────────────────────────────────────
# the native-detection cache lives on the environment object
# ──────────────────────────────────────────────────────────────────────────
def test_a_poisoned_native_cache_is_re_probed_not_trusted():
    """The cache is an attribute on a shared environment. A value of the wrong
    shape used to decide how every view in the process rendered."""
    env = NativeEnvironment()
    assert nv._is_native(env) is True
    env._network_views_native = "garbage"
    assert nv._is_native(env) is True


def test_the_cache_still_answers_false_for_a_plain_environment():
    env = Environment()
    assert nv._is_native(env) is False
    assert nv._is_native(env) is False          # second call uses the cache


# ──────────────────────────────────────────────────────────────────────────
# _render_scope: tuples, mapping keys, and a play var holding a template
# ──────────────────────────────────────────────────────────────────────────
def test_a_tuple_of_templates_is_walked_like_a_list():
    env = NativeEnvironment()
    context = ctx({"v": "resolved"})
    assert nv._render_scope(env, context, ("{{ v }}", "plain")) == \
        ["resolved", "plain"]


def test_a_template_in_a_mapping_KEY_is_left_alone():
    """Ansible does not template module-arg keys either; rendering one here
    would make this engine disagree with everywhere else a key is written."""
    env = NativeEnvironment()
    context = ctx({"v": "resolved"})
    assert nv._render_scope(env, context, {"{{ v }}": "x"}) == {"{{ v }}": "x"}


def test_a_play_var_holding_a_template_fails_with_a_named_error():
    """A native environment literal_evals the render, and `{{ 1 }}` parses as
    a set containing a set — the resulting TypeError escaped raw, with no
    view and no key attached."""
    message = refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                       "consts": {"c": "{{ v }}"}},
                      "test.view", "failed to render",
                      scope={"v": "{{ 1 }}"})
    assert "unhashable" in message or "set" in message, message


def test_a_play_var_holding_a_plain_value_still_renders():
    assert view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                 "consts": {"c": "{{ v }}"}},
                scope={"v": "ordinary"})[0]["c"] == "ordinary"


# ──────────────────────────────────────────────────────────────────────────
# every broken list is reported in ONE run, not one per run
# ──────────────────────────────────────────────────────────────────────────
def test_three_broken_lists_are_all_reported_together():
    """One _fail used to abort the whole filter, so a file with three
    mistakes took three runs — each re-validating the two not yet seen."""
    views = {"ns": {
        "bad_unknown_key": {"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
                            "nonsense": 1},
        "bad_no_fields": {"platform": "gamma"},
        "bad_platform": {"platform": "nope", "fields": {"n": "{{ seg.key }}"}},
        "good": {"platform": "gamma", "fields": {"n": "{{ seg.key }}"}},
    }}
    with pytest.raises(AnsibleFilterError) as caught:
        nv.network_views(ctx(), SEGMENTS, views)
    message = str(caught.value)
    assert "3 of the declared lists" in message, message
    for name in ("bad_unknown_key", "bad_no_fields", "bad_platform"):
        assert f"ns.{name}" in message, f"{name} missing from: {message}"


def test_a_file_with_no_broken_lists_still_builds_them_all():
    views = {"ns": {"a": {"platform": "gamma", "fields": {"n": "{{ seg.key }}"}},
                    "b": {"platform": "edge", "fields": {"n": "{{ seg.key }}"}}}}
    built = nv.network_views(ctx(), SEGMENTS, views)
    assert sorted(built["ns"]) == ["a", "b"]
    assert built["ns"]["a"] == [{"n": "web"}, {"n": "db"}]


def test_a_dotted_name_still_raises_immediately():
    """Structural problems break addressing, so nothing below them can be
    trusted — those keep failing fast rather than accumulating."""
    with pytest.raises(AnsibleFilterError, match="contains a dot"):
        nv.network_views(ctx(), SEGMENTS,
                         {"ns": {"has.dot": {"platform": "gamma",
                                             "fields": {"n": "{{ seg.key }}"}}}})


@pytest.mark.parametrize("bad", ["{{ seg.key", "{{ seg.key | }}", "{% for x in %}"])
def test_an_unparseable_field_template_names_the_view_and_the_field(bad):
    """It escaped as a raw TemplateSyntaxError naming neither — for a file of
    a dozen fields, that is a hunt."""
    refused({"platform": "gamma", "fields": {"n": bad}},
            "test.view", "fields.n", "not valid template syntax")


# ──────────────────────────────────────────────────────────────────────────
# `| default(omit)` drops the key, on BOTH templating paths
#
# Ansible strips omit placeholders when it post-validates a task's arguments,
# so this appeared to work through set_fact. `_net` is built in group_vars —
# templated lazily, never post-validated — and there the placeholder survived
# into the row and would have shipped to a device as a real value.
# ──────────────────────────────────────────────────────────────────────────
OMIT_TOKEN = "__omit_place_holder__84fcd962c31d46c35fbb0dc5b0a4a01a4b9a1f2e"


def test_a_field_defaulting_to_omit_is_dropped_from_the_row():
    rows = [{"key": "web", "on_gamma": True, "trunk": ""},
            {"key": "db", "on_gamma": True, "trunk": "10-20"}]
    out = view({"platform": "gamma", "fields": {
        "n": "{{ seg.key }}",
        "trunk": "{{ seg.trunk | default(omit, true) }}"}},
        scope={"omit": OMIT_TOKEN}, rows=rows)
    assert out[0] == {"n": "web"}, out[0]
    assert "trunk" not in out[0]
    assert out[1] == {"n": "db", "trunk": "10-20"}


def test_an_omit_marker_arriving_any_other_way_is_still_dropped():
    """A play variable that already holds the marker never names `omit`, so
    the token is not in scope — the well-known prefix has to catch it."""
    rows = [{"key": "web", "on_gamma": True}]
    out = view({"platform": "gamma", "fields": {"n": "{{ seg.key }}",
                                              "x": "{{ carried }}"}},
               scope={"carried": OMIT_TOKEN}, rows=rows)
    assert out[0] == {"n": "web"}, out[0]


def test_a_value_that_merely_looks_similar_is_not_dropped():
    rows = [{"key": "web", "on_gamma": True, "trunk": "omit"}]
    out = view({"platform": "gamma", "fields": {"n": "{{ seg.key }}",
                                              "trunk": "{{ seg.trunk }}"}},
               scope={"omit": OMIT_TOKEN}, rows=rows)
    assert out[0] == {"n": "web", "trunk": "omit"}


def test_a_non_string_value_is_never_mistaken_for_omit():
    rows = [{"key": "web", "on_gamma": True, "n_ports": 0, "flag": False}]
    out = view({"platform": "gamma", "fields": {
        "ports": "{{ seg.n_ports }}", "flag": "{{ seg.flag }}"}},
        scope={"omit": OMIT_TOKEN}, rows=rows)
    assert out[0] == {"ports": 0, "flag": False}


# ──────────────────────────────────────────────────────────────────────────
# group_by — one view, many buckets
#
# The feature that blocked migrating _net.firewall.* and _net.wifi.* off the
# catalog's embedded engine. Semantics deliberately match that engine's, so a
# migrated view produces byte-identical output.
# ──────────────────────────────────────────────────────────────────────────
GROUPED = [
    {"key": "web", "on_gamma": True, "site": "alpha", "vlan_id": 10},
    {"key": "db", "on_gamma": True, "site": "alpha", "vlan_id": 11},
    {"key": "app", "on_gamma": True, "site": "beta", "vlan_id": 10},
    {"key": "off", "on_gamma": False, "site": "beta", "vlan_id": 99},
]


def _grouped(**over):
    spec = {"platform": "gamma", "group_by": "site",
            "fields": {"n": "{{ seg.key }}", "vlan": "{{ seg.vlan_id }}"}}
    spec.update(over)
    return view(spec, rows=GROUPED, scope=over.pop("_scope", None))


def test_group_by_emits_a_mapping_of_buckets_not_a_list():
    out = _grouped()
    assert isinstance(out, dict), out
    assert sorted(out) == ["alpha", "beta"]
    assert [r["n"] for r in out["alpha"]] == ["web", "db"]
    assert [r["n"] for r in out["beta"]] == ["app"]


def test_a_view_without_group_by_still_returns_a_flat_list():
    out = view({"platform": "gamma", "fields": {"n": "{{ seg.key }}"}}, rows=GROUPED)
    assert isinstance(out, list)
    assert [r["n"] for r in out] == ["web", "db", "app"]


def test_platform_and_where_still_filter_before_grouping():
    out = _grouped(where={"site": "alpha"})
    assert sorted(out) == ["alpha"], out
    assert len(out["alpha"]) == 2


def test_bucket_keys_are_stringified():
    """So `group_by: vlan_id` gives '10', matching an append keyed '10'."""
    out = view({"platform": "gamma", "group_by": "vlan_id",
                "fields": {"n": "{{ seg.key }}"}}, rows=GROUPED)
    assert sorted(out) == ["10", "11"]


def test_unique_by_dedupes_WITHIN_each_bucket_not_across_them():
    """VLAN 10 exists at both sites; grouping by site must keep both."""
    out = view({"platform": "gamma", "group_by": "site", "unique_by": ["vlan"],
                "fields": {"vlan": "{{ seg.vlan_id }}"}}, rows=GROUPED)
    assert out["alpha"] == [{"vlan": 10}, {"vlan": 11}]
    assert out["beta"] == [{"vlan": 10}]


def test_append_on_a_grouped_view_is_a_mapping_keyed_by_bucket():
    out = _grouped(append={"alpha": [{"n": "HAND"}]})
    assert [r["n"] for r in out["alpha"]] == ["web", "db", "HAND"]
    assert [r["n"] for r in out["beta"]] == ["app"]


def test_a_bucket_that_exists_only_in_append_is_still_emitted():
    """A wholly hand-maintained site must not vanish from a grouped view."""
    out = _grouped(append={"gamma": [{"n": "HAND"}]})
    assert "gamma" in out, sorted(out)
    assert out["gamma"] == [{"n": "HAND"}]


def test_an_append_bucket_key_is_stringified_too():
    out = view({"platform": "gamma", "group_by": "vlan_id",
                "fields": {"n": "{{ seg.key }}"},
                "append": {10: [{"n": "HAND"}]}}, rows=GROUPED)
    assert [r["n"] for r in out["10"]] == ["web", "app", "HAND"]


def test_a_LIST_append_on_a_grouped_view_is_rejected():
    """The catalog's engine ignored this in silence — the hand-written rows
    simply never appeared."""
    refused({"platform": "gamma", "group_by": "site",
             "fields": {"n": "{{ seg.key }}"}, "append": [{"n": "HAND"}]},
            "group_by", "MAPPING of bucket name", rows=GROUPED)


def test_a_MAPPING_append_on_a_flat_view_is_still_rejected():
    refused({"platform": "gamma", "fields": {"n": "{{ seg.key }}"},
             "append": {"alpha": [{"n": "HAND"}]}}, "LIST of rows",
            rows=GROUPED)


def test_group_by_naming_a_field_no_segment_carries_is_rejected():
    """Otherwise every row lands in the bucket "" and the consumer's
    `<view>.<site>` fails somewhere far away."""
    refused({"platform": "gamma", "group_by": "sitte",
             "fields": {"n": "{{ seg.key }}"}},
            "sitte", "no segment carries", "Did you mean 'site'?", rows=GROUPED)


@pytest.mark.parametrize("bad", [["site"], 7, {"a": 1}, ""])
def test_group_by_must_be_one_field_name(bad):
    refused({"platform": "gamma", "group_by": bad,
             "fields": {"n": "{{ seg.key }}"}},
            "group_by", rows=GROUPED)


def test_a_segment_missing_the_group_field_lands_in_the_blank_bucket():
    """Matches the catalog engine, which used str(row.get(field, ''))."""
    rows = GROUPED + [{"key": "orphan", "on_gamma": True, "vlan_id": 12}]
    out = view({"platform": "gamma", "group_by": "site",
                "fields": {"n": "{{ seg.key }}"}}, rows=rows)
    assert out[""] == [{"n": "orphan"}], out


def test_group_by_is_reported_as_an_unknown_key_nowhere():
    """It is a real spec key now, so it must not trip the unknown-key check."""
    out = _grouped()
    assert isinstance(out, dict)


# ──────────────────────────────────────────────────────────────────────────
# source — chaining one view off another
#
# The last gap before _net.hypervisor.hv_mgr_portgroups and _net.edge.zones can
# leave the catalog's embedded engine. A chained view projects the SOURCE
# VIEW'S OUTPUT ROWS, so `seg.<x>` reads an output key, not a segment field.
# ──────────────────────────────────────────────────────────────────────────
def _chain(views):
    return nv.network_views(ctx(), SEGMENTS, views)


BASE_LIST = {"platform": "gamma",
             "fields": {"n": "{{ seg.key }}", "switch": "{{ seg.vswitch }}"}}


def test_a_chained_view_projects_the_sources_output_keys():
    built = _chain({"ns": {
        "base": BASE_LIST,
        "refined": {"source": "base",
                    "fields": {"name": "{{ seg.n }}",
                               "vds": "{{ seg.switch }}"}}}})
    assert built["ns"]["refined"] == [{"name": "web", "vds": "dvs-a"},
                                      {"name": "db", "vds": "dvs-a"}]


def test_a_chained_view_may_be_referenced_across_namespaces_with_a_dot():
    built = _chain({"a": {"base": BASE_LIST},
                    "b": {"refined": {"source": "a.base",
                                      "fields": {"name": "{{ seg.n }}"}}}})
    assert built["b"]["refined"] == [{"name": "web"}, {"name": "db"}]


def test_where_and_unique_by_run_on_the_projected_rows():
    built = _chain({"ns": {
        "base": BASE_LIST,
        "refined": {"source": "base", "unique_by": ["vds"],
                    "where": {"switch": "dvs-a"},
                    "fields": {"vds": "{{ seg.switch }}"}}}})
    assert built["ns"]["refined"] == [{"vds": "dvs-a"}]


def test_a_forward_source_reference_is_rejected():
    """Views chain in FILE ORDER, so the source has to appear above."""
    with pytest.raises(AnsibleFilterError, match="declared earlier"):
        _chain({"ns": {"refined": {"source": "base",
                                   "fields": {"n": "{{ seg.n }}"}},
                       "base": BASE_LIST}})


def test_an_unknown_source_is_rejected_by_name():
    with pytest.raises(AnsibleFilterError, match="nope"):
        _chain({"ns": {"base": BASE_LIST,
                       "refined": {"source": "nope",
                                   "fields": {"n": "{{ seg.n }}"}}}})


def test_chaining_off_a_GROUPED_view_is_rejected():
    """A grouped view emits buckets, not rows. In the catalog this silently
    produced an empty list; group_by brought the same trap here."""
    with pytest.raises(AnsibleFilterError, match="GROUPED view"):
        _chain({"ns": {
            "grouped": {"platform": "gamma", "group_by": "site",
                        "fields": {"n": "{{ seg.key }}"}},
            "refined": {"source": "grouped", "fields": {"n": "{{ seg.n }}"}}}})


def test_source_and_platform_together_are_rejected():
    """Projected rows carry no on_<platform> flag, so the filter would match
    nothing and the view would come out empty in silence."""
    with pytest.raises(AnsibleFilterError, match="cannot both be set"):
        _chain({"ns": {"base": BASE_LIST,
                       "refined": {"source": "base", "platform": "gamma",
                                   "fields": {"n": "{{ seg.n }}"}}}})


def test_a_chained_view_needs_no_platform():
    built = _chain({"ns": {"base": BASE_LIST,
                           "refined": {"source": "base",
                                       "fields": {"n": "{{ seg.n }}"}}}})
    assert len(built["ns"]["refined"]) == 2


def test_source_on_a_single_view_call_is_rejected():
    """`network_view` builds one list and has nothing to chain from."""
    refused({"source": "base", "fields": {"n": "{{ seg.n }}"}},
            "only works through the `network_views` filter")


@pytest.mark.parametrize("bad", [["base"], 7, "", {"a": 1}])
def test_source_must_name_one_view(bad):
    with pytest.raises(AnsibleFilterError, match="source"):
        _chain({"ns": {"base": BASE_LIST,
                       "refined": {"source": bad,
                                   "fields": {"n": "{{ seg.n }}"}}}})


def test_a_chain_three_deep_works():
    built = _chain({"ns": {
        "one": BASE_LIST,
        "two": {"source": "one", "fields": {"a": "{{ seg.n }}"}},
        "three": {"source": "two", "fields": {"b": "{{ seg.a }}"}}}})
    assert built["ns"]["three"] == [{"b": "web"}, {"b": "db"}]


def test_a_chained_view_may_itself_be_grouped():
    built = _chain({"ns": {
        "base": {"platform": "gamma",
                 "fields": {"n": "{{ seg.key }}", "site": "{{ seg.site }}"}},
        "bysite": {"source": "base", "group_by": "site",
                   "fields": {"n": "{{ seg.n }}"}}}})
    assert built["ns"]["bysite"] == {"alpha": [{"n": "web"}, {"n": "db"}]}
