"""Unit tests for plugins/filter/network_catalog.py.

The engine must stay estate-agnostic: these tests deliberately use vendors,
field names and naming conventions that do NOT exist in this repo's own
networks.yml, so a change that quietly bakes the estate's vocabulary into the engine
fails here.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_PLUGIN = (
    pathlib.Path(__file__).resolve().parents[3] / "plugins" / "filter" / "network_catalog.py"
)
_spec = importlib.util.spec_from_file_location("network_catalog", _PLUGIN)
nc = importlib.util.module_from_spec(_spec)
sys.modules["network_catalog"] = nc
_spec.loader.exec_module(nc)


# ──────────────────────────────────────────────────────────────────────────
# fixtures — a fictional estate, nothing like this repo's
# ──────────────────────────────────────────────────────────────────────────
BASE_CONFIG = {
    "platforms": ["gamma", "alpha", "beta"],
    "name_default": "name",
    "names": {
        "name": {
            "parts": ["vlan", "bu", "stage", "purpose"],
            "case": "lower",
            "sep": "_",
            "vlan_prefix": "seg",
            "vlan_pad": 4,
        },
        "short": {"from": "name", "drop_tokens": ["vlan", "vlan_label", "vid"]},
    },
    "defaults": {"mtu": 9000},
    "partition_fields": ["bu", "stage"],
    "required": {"all": ["vlan_id", "platforms"], "by_platform": {"beta": ["gw"]}},
    "views": {},
}

SEGMENTS = {
    "web_prod": {
        "vlan_id": 1101,
        "subnet": "10.11.1.0/24",
        "gw": "10.11.1.1",
        "gateway": "10.11.1.1",
        "platforms": ["gamma", "alpha", "beta"],
        "bu": "retail",
        "stage": "prod",
        "purpose": "web",
        "operator_source": True,
    },
    "db_dev": {
        "vlan_id": 2101,
        "subnet": "10.21.1.0/24",
        "gw": "10.21.1.1",
        "gateway": "10.21.1.1",
        "platforms": ["gamma"],
        "bu": "retail",
        "stage": "dev",
        "purpose": "db",
    },
}


def build(segments=None, **overrides):
    cfg = dict(BASE_CONFIG)
    cfg.update(overrides)
    # A pool's `key:` is a template, so the generator needs an environment.
    return nc.network_catalog(SEGMENTS if segments is None else segments, cfg,
                              env=_env() if cfg.get("pools") else None)


# ──────────────────────────────────────────────────────────────────────────
# name recipes
# ──────────────────────────────────────────────────────────────────────────
def test_name_is_built_from_tokens_with_padding_and_case():
    result = build()
    assert result["name_by_key"]["web_prod"] == "seg1101_retail_prod_web"


def test_vlan_pad_widens_short_ids():
    segs = {"a": {"vlan_id": 7, "platforms": [], "bu": "x", "stage": "y", "purpose": "z"}}
    assert build(segs)["name_by_key"]["a"] == "seg0007_x_y_z"


def test_empty_tokens_are_dropped_without_doubling_the_separator():
    segs = {"a": {"vlan_id": 5, "platforms": [], "bu": "x", "purpose": "z"}}  # no stage
    assert build(segs)["name_by_key"]["a"] == "seg0005_x_z"


def test_from_inherits_the_recipe_as_resolved_for_that_segment():
    """A segment that overrides parts must drag `from:` recipes along with it."""
    segs = {
        "odd": {
            "vlan_id": 9,
            "platforms": [],
            "bu": "acme",
            "name_parts": ["vlan", "bu"],  # overrides the default recipe only
        }
    }
    row = build(segs)["by_key"]["odd"]
    assert row["name"] == "seg0009_acme"
    # short = the SEGMENT's parts minus the vlan token, not the estate default
    assert row["short"] == "acme"


def test_glue_group_joins_without_separator_and_drops_whole_on_drop_token():
    cfg_names = {
        "name": {"parts": ["vlan", ["purpose", "instance_nn"]], "case": "upper", "sep": "-",
                 "vlan_prefix": "V", "vlan_pad": 2},
        "short": {"from": "name", "drop_tokens": ["vlan"]},
    }
    segs = {"a": {"vlan_id": 8, "platforms": [], "purpose": "pod", "instance": 3}}
    row = build(segs, names=cfg_names)["by_key"]["a"]
    assert row["name"] == "V08-POD03"
    assert row["short"] == "POD03"


def test_prefix_and_suffix_auto_affix_but_are_not_doubled_when_positioned():
    names_auto = {"name": {"parts": ["purpose"], "prefix": "pre", "suffix": "post",
                           "sep": "-", "case": "lower"}}
    names_manual = {"name": {"parts": ["suffix", "purpose", "prefix"], "prefix": "pre",
                             "suffix": "post", "sep": "-", "case": "lower"}}
    segs = {"a": {"vlan_id": 1, "platforms": [], "purpose": "web"}}
    assert build(segs, names=names_auto)["name_by_key"]["a"] == "pre-web-post"
    assert build(segs, names=names_manual)["name_by_key"]["a"] == "post-web-pre"


def test_a_field_named_after_a_recipe_pins_that_name():
    segs = {
        "a": {"vlan_id": 1, "platforms": [], "bu": "x", "stage": "y", "purpose": "z",
              "name": "LEGACY-NAME"}
    }
    result = build(segs)
    assert result["name_by_key"]["a"] == "LEGACY-NAME"
    assert result["derived_names"]["a"] == "seg0001_x_y_z"
    assert result["name_overrides"] == [
        {"key": "a", "name": "LEGACY-NAME", "derived_name": "seg0001_x_y_z"}
    ]


def test_no_overrides_when_every_name_is_token_built():
    assert build()["name_overrides"] == []


# ──────────────────────────────────────────────────────────────────────────
# segment enrichment
# ──────────────────────────────────────────────────────────────────────────
def test_defaults_are_inherited_but_the_segment_wins():
    segs = {
        "a": {"vlan_id": 1, "platforms": []},
        "b": {"vlan_id": 2, "platforms": [], "mtu": 1500},
    }
    rows = build(segs)["by_key"]
    assert rows["a"]["mtu"] == 9000
    assert rows["b"]["mtu"] == 1500


def test_on_target_booleans_come_from_targets():
    row = build()["by_key"]["db_dev"]
    assert row["on_gamma"] is True
    assert row["on_alpha"] is False


def test_prefixlen_and_gateway_cidr_are_derived_from_subnet():
    row = build()["by_key"]["web_prod"]
    assert row["prefixlen"] == 24
    assert row["gateway_cidr"] == "10.11.1.1/24"


def test_unknown_user_fields_pass_through_untouched():
    segs = {"a": {"vlan_id": 1, "platforms": [], "my_weird_field": {"deep": [1, 2]}}}
    assert build(segs)["by_key"]["a"]["my_weird_field"] == {"deep": [1, 2]}


def test_vlan_zero_is_untagged():
    segs = {"a": {"vlan_id": 0, "platforms": []}}
    result = build(segs)
    assert result["by_key"]["a"]["tagged"] is False
    assert result["tagged_vlan_ids"] == []


# ──────────────────────────────────────────────────────────────────────────
# platform views
# ──────────────────────────────────────────────────────────────────────────
VIEWS = {
    "gamma": {
        "segments": {
            "platform": "gamma",
            "fields": {"display_name": "name", "vlan_ids": "vlan_id", "mtu": "mtu"},
            "append": [{"display_name": "legacy", "vlan_ids": 999, "mtu": 1500}],
        }
    },
    "alpha": {
        "epgs": {
            "platform": "alpha",
            "consts": {"tenant": "T1"},
            "fields": {
                "epg": "short",
                "encap": "vlan-{vlan_id}",
                "tn": "{tenant}",
                "kind": {"const": "epg"},
            },
        }
    },
    "beta": {
        "interfaces": {
            "platform": "beta",
            "where": {"tagged": True},
            "group_by": "stage",
            "fields": {"name": "port1.{vlan_id}", "seq": "index", "ip": "gw"},
        }
    },
}


# ──────────────────────────────────────────────────────────────────────────
# generated pools
# ──────────────────────────────────────────────────────────────────────────
POOL = {
    "tenant_x": {
        "vlan_base": 3000,
        "vlan_stride": 10,
        "instances": 2,
        "bu": "tenantx",
        "platforms": ["gamma"],
        "key": "{{ bu }}-{{ vid }}-{{ role }}",
        "roles": [
            {"app": {"purpose": "app"}},
            {"mon": {"offset": 5, "purpose": "mon", "platforms": ["alpha"]}},
        ],
    }
}


def test_pool_expands_sets_times_roles_with_stride_and_offsets():
    keys = sorted(build(pools=POOL)["by_key"])
    assert "tenantx-3000-app" in keys
    assert "tenantx-3005-mon" in keys
    assert "tenantx-3010-app" in keys
    assert "tenantx-3015-mon" in keys


def test_a_pool_key_that_collides_within_the_pool_errors_instead_of_vanishing():
    # key_parts naming only the pool makes every role render the same key.
    # Overwriting would drop a whole VLAN from every derived view while the
    # health check still read clean, so it has to surface as an error.
    pool = {"p": {"vlan_base": 100, "instances": 1, "roles": ["a", "b"],
                  "key": "{{ pool }}"}}
    result = build({}, pools=pool)
    assert len(result["errors"]) == 1
    assert "collides" in result["errors"][0]
    # The first member survives; the collider is rejected, not silently preferred.
    assert result["vlan_by_key"] == {"p": 100}


def test_a_pool_key_that_collides_across_pools_errors_too():
    pools = {
        "one": {"vlan_base": 100, "instances": 1, "roles": ["a"], "key": "{{ role }}"},
        "two": {"vlan_base": 200, "instances": 1, "roles": ["a"], "key": "{{ role }}"},
    }
    result = build({}, pools=pools)
    assert len(result["errors"]) == 1
    assert "collides" in result["errors"][0]
    assert result["vlan_by_key"] == {"a": 100}


def test_distinct_pool_keys_produce_no_collision_error():
    pool = {"p": {"vlan_base": 100, "instances": 2, "roles": ["a", "b"],
                  "key": "{{ pool }}-{{ vid }}-{{ role }}"}}
    result = build({}, pools=pool)
    assert result["errors"] == []
    assert len(result["segments"]) == 4


def test_pool_stride_defaults_to_the_role_count():
    pool = {"p": {"vlan_base": 100, "instances": 2, "roles": ["a", "b"],
                  "key": "{{ pool }}-{{ vid }}"}}
    vlans = sorted(build({}, pools=pool)["vlan_by_key"].values())
    assert vlans == [100, 101, 102, 103]


def test_pool_roles_as_a_list_use_positional_offsets():
    pool = {"p": {"vlan_base": 10, "instances": 1, "roles": ["a", "b", "c"],
                  "key": "{{ role }}"}}
    result = build({}, pools=pool)
    assert result["vlan_by_key"] == {"a": 10, "b": 11, "c": 12}


def test_pool_list_entry_may_be_a_single_key_map_with_per_role_fields():
    pool = {"p": {"vlan_base": 10, "instances": 1, "platforms": ["gamma"],
                  "roles": ["a", {"b": {"platforms": ["alpha"]}}],
                  "key": "{{ role }}"}}
    rows = build({}, pools=pool)["by_key"]
    assert rows["a"]["platforms"] == ["gamma"]
    assert rows["b"]["platforms"] == ["alpha"]


def test_per_role_fields_override_pool_level_ones():
    rows = build(pools=POOL)["by_key"]
    assert rows["tenantx-3000-app"]["platforms"] == ["gamma"]
    assert rows["tenantx-3005-mon"]["platforms"] == ["alpha"]


def test_pool_stamped_identical_subnet_repeats_without_any_duplicate_error():
    """Scenario: self-isolated test environments — every instance deliberately
    gets the SAME addressing (NAT'd behind its own router). Duplicate subnets
    must be legal; only names and tagged VLAN ids are uniqueness-checked."""
    pool = {"p": {"vlan_base": 100, "instances": 3, "roles": ["inside"],
                  "subnet": "192.168.1.0/24", "gateway": "192.168.1.1",
                  "netmask": "255.255.255.0", "key": "{{ role }}-{{ vid }}"}}
    result = build({}, pools=pool)
    subnets = [s["subnet"] for s in result["segments"]]
    assert subnets == ["192.168.1.0/24"] * 3
    assert all(s["gateway"] == "192.168.1.1" for s in result["segments"])
    assert result["errors"] == []
    assert result["duplicate_names"] == []


def test_pool_subnet_base_increments_one_network_per_segment():
    pool = {"p": {"vlan_base": 100, "instances": 2, "roles": ["a", "b"],
                  "subnet_base": "10.64.0.0/24", "gateway_offset": 1,
                  "key": "{{ role }}-{{ vid }}"}}
    rows = build({}, pools=pool)["by_key"]
    assert rows["a-100"]["subnet"] == "10.64.0.0/24"
    assert rows["b-101"]["subnet"] == "10.64.1.0/24"
    assert rows["a-102"]["subnet"] == "10.64.2.0/24"
    assert rows["b-103"]["subnet"] == "10.64.3.0/24"
    assert rows["a-100"]["gateway"] == "10.64.0.1"
    assert rows["b-103"]["gateway"] == "10.64.3.1"
    assert rows["a-100"]["netmask"] == "255.255.255.0"
    assert rows["a-100"]["gateway_cidr"] == "10.64.0.1/24"


def test_pool_subnet_stride_leaves_gaps_between_networks():
    pool = {"p": {"vlan_base": 1, "instances": 2, "roles": ["a"],
                  "subnet_base": "10.0.0.0/24", "subnet_stride": 4,
                  "key": "{{ role }}-{{ vid }}"}}
    rows = build({}, pools=pool)["by_key"]
    assert rows["a-1"]["subnet"] == "10.0.0.0/24"
    assert rows["a-2"]["subnet"] == "10.0.4.0/24"


def test_pool_subnet_index_vlan_mirrors_vlan_gaps():
    """subnet_index: vlan uses the VLAN's distance from vlan_base, so the
    subnet numbering mirrors the VLAN numbering, gaps included."""
    pool = {"p": {"vlan_base": 2000, "vlan_stride": 10, "instances": 2,
                  "roles": ["app", "db"], "subnet_base": "10.64.0.0/24",
                  "subnet_index": "vlan", "key": "{{ role }}-{{ vid }}"}}
    rows = build({}, pools=pool)["by_key"]
    assert rows["app-2000"]["subnet"] == "10.64.0.0/24"
    assert rows["db-2001"]["subnet"] == "10.64.1.0/24"
    assert rows["app-2010"]["subnet"] == "10.64.10.0/24"   # gap mirrored
    assert rows["db-2011"]["subnet"] == "10.64.11.0/24"


def test_pool_per_role_subnet_wins_and_neighbours_keep_their_index():
    pool = {"p": {"vlan_base": 1, "instances": 1,
                  "roles": ["a", {"b": {"subnet": "172.16.0.0/24"}}, "c"],
                  "subnet_base": "10.0.0.0/24", "key": "{{ role }}-{{ vid }}"}}
    rows = build({}, pools=pool)["by_key"]
    assert rows["a-1"]["subnet"] == "10.0.0.0/24"
    assert rows["b-2"]["subnet"] == "172.16.0.0/24"    # declared wins
    assert rows["c-3"]["subnet"] == "10.0.2.0/24"      # index 2, not 1


def test_pool_bad_subnet_base_is_reported_and_segments_still_emit():
    pool = {"p": {"vlan_base": 1, "instances": 1, "roles": ["a"],
                  "subnet_base": "not-a-network", "key": "{{ role }}-{{ vid }}"}}
    result = build({}, pools=pool)
    assert any("subnet_base 'not-a-network' is not a valid network" in e
               for e in result["errors"])
    assert "subnet" not in build({}, pools=pool)["by_key"]["a-1"] or \
           not result["by_key"]["a-1"].get("subnet")


def test_pool_subnet_overflow_is_reported_per_segment():
    pool = {"p": {"vlan_base": 1, "instances": 2, "roles": ["a"],
                  "subnet_base": "255.255.255.0/24", "key": "{{ role }}-{{ vid }}"}}
    result = build({}, pools=pool)
    assert result["by_key"]["a-1"]["subnet"] == "255.255.255.0/24"
    assert any("falls outside the address space" in e for e in result["errors"])
    assert not result["by_key"]["a-2"].get("subnet")


def test_hand_written_key_wins_over_a_pool_key_and_the_clash_is_reported():
    pool = {"p": {"vlan_base": 10, "instances": 1, "roles": ["a"], "key": "{{ role }}"}}
    segs = {"a": {"vlan_id": 999, "platforms": []}}
    result = nc.network_catalog(segs, {**BASE_CONFIG, "pools": pool}, env=_env())
    assert result["vlan_by_key"]["a"] == 999
    assert any("collides" in e for e in result["errors"])


# ──────────────────────────────────────────────────────────────────────────
# partitions and cidr groups
# ──────────────────────────────────────────────────────────────────────────
def test_partitions_are_keyed_by_field_then_value():
    result = build()
    assert sorted(result["by"]["stage"]) == ["dev", "prod"]
    assert result["by"]["bu"]["retail"][0]["key"] in SEGMENTS


def test_platform_partition_is_always_present_even_without_config():
    result = build(partition_fields=[])
    assert sorted(result["by"]["platform"]) == ["alpha", "beta", "gamma"]


def test_cidrs_by_and_vlan_ids_by_platform():
    result = build()
    assert result["cidrs_by"]["stage"]["prod"] == ["10.11.1.0/24"]
    assert result["vlan_ids_by_platform"]["gamma"] == [1101, 2101]


def test_operator_cidrs_only_lists_flagged_segments():
    assert build()["operator_cidrs"] == ["10.11.1.0/24"]


# ──────────────────────────────────────────────────────────────────────────
# validation diagnostics
# ──────────────────────────────────────────────────────────────────────────
def test_clean_catalog_reports_nothing():
    result = build()
    assert result["errors"] == []
    assert result["missing"] == []
    assert result["duplicate_names"] == []
    assert result["duplicate_vlans"] == []


def test_unknown_platform_is_named():
    segs = {"a": {"vlan_id": 1, "platforms": ["gamma", "nxs"]}}
    assert any("unknown platform 'nxs'" in e for e in build(segs)["errors"])


def test_platforms_as_a_string_is_rejected():
    segs = {"a": {"vlan_id": 1, "platforms": "gamma"}}
    assert any("must be a LIST" in e for e in build(segs)["errors"])


def test_missing_vlan_id_is_named_and_does_not_crash():
    segs = {"a": {"platforms": ["gamma"]}}
    result = build(segs)
    assert any("vlan_id is required" in e for e in result["errors"])
    assert result["by_key"]["a"]["vlan_id"] == 0  # degraded, not exploded


def test_required_fields_are_checked_per_target():
    segs = {"a": {"vlan_id": 1, "platforms": ["beta"]}}  # beta requires gw
    assert build(segs)["missing"] == ["a: missing or empty gw"]


def test_empty_string_and_empty_list_count_as_missing():
    segs = {"a": {"vlan_id": 1, "platforms": []}}
    assert any("platforms" in m for m in build(segs)["missing"])


def test_unknown_name_recipe_on_a_segment_is_named():
    segs = {"a": {"vlan_id": 1, "platforms": [], "names": {"nope": {"parts": []}}}}
    assert any("is not a recipe" in e for e in build(segs)["errors"])


def test_duplicate_names_and_vlans_are_reported():
    segs = {
        "a": {"vlan_id": 5, "platforms": [], "bu": "x", "stage": "y", "purpose": "z"},
        "b": {"vlan_id": 5, "platforms": [], "bu": "x", "stage": "y", "purpose": "z"},
    }
    result = build(segs)
    assert result["duplicate_vlans"] == [5]
    assert result["duplicate_names"] == ["seg0005_x_y_z"]


def test_untagged_ids_may_repeat_across_sites():
    segs = {"a": {"vlan_id": 0, "platforms": []}, "b": {"vlan_id": 0, "platforms": []}}
    assert build(segs)["duplicate_vlans"] == []


def test_empty_catalog_is_reported_not_crashed():
    result = nc.network_catalog({}, {})
    assert result["segments"] == []
    assert any("no segments" in e for e in result["errors"])


@pytest.mark.parametrize("bad", [None, "string", 42, []])
def test_garbage_inputs_degrade_instead_of_raising(bad):
    result = nc.network_catalog(bad, bad)
    assert result["segments"] == []
    assert result["errors"]


def test_non_dict_underlays_root_is_named_in_errors():
    result = nc.network_catalog(["not", "a", "dict"], BASE_CONFIG)
    assert any("must be a dict" in e for e in result["errors"])


def test_non_numeric_vlan_id_degrades_to_zero_and_is_reported():
    segs = {"a": {"vlan_id": "forty-two", "platforms": ["gamma"]}}
    result = build(segs)
    assert result["by_key"]["a"]["vlan_id"] == 0
    assert any("is not a number" in e for e in result["errors"])


# ──────────────────────────────────────────────────────────────────────────
# namespace vs platform filter — the two need not match
# ──────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────
# degrade-not-raise guarantees
# ──────────────────────────────────────────────────────────────────────────
def test_non_numeric_instance_degrades_to_the_raw_string():
    cfg_names = {"name": {"parts": [["purpose", "instance_nn"]], "sep": "-"}}
    segs = {"a": {"vlan_id": 1, "platforms": [], "purpose": "pod", "instance": "gold"}}
    assert build(segs, names=cfg_names)["name_by_key"]["a"] == "podgold"


def test_garbage_pool_int_knobs_degrade_to_their_defaults():
    pool = {"p": {"vlan_base": "junk", "vlan_stride": "junk", "instances": "junk",
                  "roles": [{"a": {"offset": "junk"}}], "key": "{{ role }}"}}
    result = build({}, pools=pool)  # must not raise
    assert result["vlan_by_key"] == {"a": 0}  # base 0 + offset 0, one set


def test_garbage_vlan_pad_degrades_to_no_padding():
    cfg_names = {"name": {"parts": ["vlan"], "vlan_prefix": "V", "vlan_pad": "wide"}}
    segs = {"a": {"vlan_id": 7, "platforms": []}}
    assert build(segs, names=cfg_names)["name_by_key"]["a"] == "V7"


def test_vlan_ranges_compress_contiguous_runs_per_platform():
    pool = {"p": {"vlan_base": 2000, "vlan_stride": 10, "instances": 3,
                  "roles": ["a", "b", "c", "d", "e"], "platforms": ["gamma"],
                  "key": "{{ pool }}-{{ vid }}"}}
    segs = {
        "one": {"vlan_id": 7, "platforms": ["gamma"]},
        "two": {"vlan_id": 8, "platforms": ["gamma"]},
    }
    result = build(segs, pools=pool)
    assert result["vlan_ranges_by_platform"]["gamma"] == [
        "7-8", "2000-2004", "2010-2014", "2020-2024",
    ]


def test_vlan_ranges_keep_singletons_single():
    segs = {"a": {"vlan_id": 5, "platforms": ["gamma"]},
            "b": {"vlan_id": 9, "platforms": ["gamma"]}}
    assert build(segs)["vlan_ranges_by_platform"]["gamma"] == ["5", "9"]


def test_vlan_ranges_cover_tagged_ids_only():
    segs = {"native": {"vlan_id": 0, "platforms": ["gamma"]},
            "tagged": {"vlan_id": 1, "platforms": ["gamma"]}}
    assert build(segs)["vlan_ranges_by_platform"]["gamma"] == ["1"]


# ──────────────────────────────────────────────────────────────────────────
# Robustness: partial data and mis-chained views must not crash or go quiet
# ──────────────────────────────────────────────────────────────────────────
PARTIAL = {
    "a": {"vlan_id": 11, "platforms": ["gamma"], "bu": "retail", "stage": "prod",
          "purpose": "web"},
    "b": {"vlan_id": 12, "platforms": ["gamma"], "bu": "retail", "stage": "dev",
          "purpose": "api"},
    "c": {"vlan_id": 13, "platforms": ["gamma"], "bu": "retail", "stage": "prod",
          "purpose": "db"},
}


def test_sort_by_naming_a_const_output_key_is_accepted():
    views = {"p": {"v": {"fields": {"id": "vlan_id"},
                         "consts": {"tier": "core"}, "sort_by": "tier"}}}
    assert build(views=views)["errors"] == []


def test_chaining_off_a_flat_source_stays_valid():
    """The fix must not flag legitimate chains."""
    views = {"p": {"rows": {"fields": {"zone": "stage"}},
                   "zones": {"source": "rows", "unique_by": "zone",
                             "fields": {"zone": "zone"}}}}
    assert build(views=views)["errors"] == []


# ──────────────────────────────────────────────────────────────────────────
# Addressing sanity and boolean coercion (external review, 2026-07-26)
# ──────────────────────────────────────────────────────────────────────────
def _seg(**over):
    base = {"vlan_id": 10, "subnet": "10.0.0.0/24", "gw": "10.0.0.1",
            "gateway": "10.0.0.1", "platforms": ["gamma"], "bu": "retail",
            "stage": "prod", "purpose": "web"}
    base.update(over)
    return {"s": base}


def test_operator_source_string_false_is_not_truthy():
    """A quoted false must not put the subnet in an operator allow-list."""
    result = build(_seg(operator_source="false"))
    assert result["operator_cidrs"] == []


def test_operator_source_string_true_still_counts():
    result = build(_seg(operator_source="true"))
    assert result["operator_cidrs"] == ["10.0.0.0/24"]


def test_operator_source_real_bool_still_counts():
    result = build(_seg(operator_source=True))
    assert result["operator_cidrs"] == ["10.0.0.0/24"]


def test_operator_source_unparseable_is_reported_and_excluded():
    result = build(_seg(operator_source="maybe"))
    assert result["operator_cidrs"] == []
    assert any("operator_source" in e for e in result["errors"]), result["errors"]


def test_reserved_vlan_4095_is_rejected():
    errs = build(_seg(vlan_id=4095))["errors"]
    assert any("4095" in e or "4094" in e for e in errs), errs


def test_vlan_id_above_the_range_is_rejected():
    assert any("vlan" in e.lower() for e in build(_seg(vlan_id=9999))["errors"])


def test_vlan_id_zero_is_allowed_as_untagged():
    assert build(_seg(vlan_id=0))["errors"] == []


def test_gateway_outside_its_subnet_is_rejected():
    errs = build(_seg(gateway="192.168.99.1", gw="192.168.99.1"))["errors"]
    assert any("gateway" in e.lower() for e in errs), errs


def test_gateway_inside_its_subnet_is_accepted():
    assert build(_seg(gateway="10.0.0.254", gw="10.0.0.254"))["errors"] == []


def test_netmask_inconsistent_with_the_subnet_prefix_is_rejected():
    errs = build(_seg(netmask="255.255.0.0"))["errors"]
    assert any("netmask" in e.lower() for e in errs), errs


def test_matching_netmask_is_accepted():
    assert build(_seg(netmask="255.255.255.0"))["errors"] == []


def test_unparseable_subnet_is_reported_not_crashed():
    errs = build(_seg(subnet="not-a-cidr"))["errors"]
    assert any("subnet" in e.lower() for e in errs), errs


# ──────────────────────────────────────────────────────────────────────────
# Uniqueness is per L2 domain, not per estate
# ──────────────────────────────────────────────────────────────────────────
def _sited(site_a, site_b, vlan=10):
    """Two segments carrying the same VLAN id, one per site."""
    common = {"vlan_id": vlan, "platforms": ["gamma"], "bu": "x", "stage": "y",
              "purpose": "z"}
    return {"a": {**common, "site": site_a}, "b": {**common, "site": site_b}}


def test_the_same_vlan_at_two_sites_is_not_a_duplicate():
    """A VLAN id is unique within an L2 domain, never estate-wide."""
    result = build(_sited("alpha", "beta"), uniqueness_scope=["site"])
    assert result["duplicate_vlans"] == []
    assert result["duplicate_names"] == []


def test_the_same_vlan_twice_at_one_site_is_still_a_duplicate():
    result = build(_sited("alpha", "alpha"), uniqueness_scope=["site"])
    assert result["duplicate_vlans"] == [10]


def test_an_unset_scope_compares_estate_wide():
    """No scope configured = the pre-scoping behaviour, for a lifted engine."""
    assert build(_sited("alpha", "beta"))["duplicate_vlans"] == [10]


def test_segments_without_a_site_still_share_one_scope():
    """Backward compatible: no site means one estate-wide domain."""
    segs = {
        "a": {"vlan_id": 5, "platforms": [], "bu": "x", "stage": "y", "purpose": "z"},
        "b": {"vlan_id": 5, "platforms": [], "bu": "x", "stage": "y", "purpose": "z"},
    }
    assert build(segs, uniqueness_scope=["site"])["duplicate_vlans"] == [5]


def test_the_uniqueness_scope_is_configurable():
    """A second fabric at one site needs a finer scope than site alone."""
    common = {"vlan_id": 10, "platforms": ["gamma"], "site": "alpha",
              "bu": "x", "stage": "y", "purpose": "z"}
    segs = {"a": {**common, "fabric": "core"}, "b": {**common, "fabric": "dmz"}}
    assert build(segs, uniqueness_scope=["site"])["duplicate_vlans"] == [10]
    assert build(segs, uniqueness_scope=["site", "fabric"])["duplicate_vlans"] == []


# ──────────────────────────────────────────────────────────────────────────
# Chaos round 2 — a bare string where a list belongs, and pool addressing
# ──────────────────────────────────────────────────────────────────────────
def test_a_bare_string_uniqueness_scope_is_rejected_not_ignored():
    """C-SCOPE-STRING: _as_list('site') is [], so the scope was dropped and
    two sites sharing a VLAN became a false-positive duplicate."""
    segs = {"a": {"vlan_id": 10, "platforms": ["gamma"], "site": "alpha",
                  "purpose": "a"},
            "b": {"vlan_id": 10, "platforms": ["gamma"], "site": "beta",
                  "purpose": "b"}}
    errs = build(segs, uniqueness_scope="site")["errors"]
    assert any("uniqueness_scope" in e and "LIST" in e for e in errs), errs


def test_a_bare_string_partition_field_is_rejected_not_ignored():
    errs = build(partition_fields="bu")["errors"]
    assert any("partition_fields" in e and "LIST" in e for e in errs), errs


def test_a_bare_string_pool_roles_is_rejected_not_ignored():
    """C-POOL-ROLES-STR: produced zero segments and only the generic
    'no segments' message, which points nowhere near the cause."""
    pool = {"p": {"vlan_base": 100, "instances": 3, "roles": "app",
                  "platforms": ["gamma"], "site": "s", "zone": "z"}}
    errs = build({}, pools=pool)["errors"]
    assert any("roles" in e and "LIST" in e for e in errs), errs


def test_a_vlan_indexed_subnet_below_the_base_is_rejected():
    """C-POOL-NEG-INDEX: a negative role offset wrapped the subnet under the
    base — 10.0.0.0/24 became 9.255.251.0/24 with no error at all."""
    pool = {"p": {"vlan_base": 100, "instances": 1, "subnet_base": "10.0.0.0/24",
                  "subnet_index": "vlan", "platforms": ["gamma"],
                  "site": "s", "zone": "z",
                  "key": "{{ role }}",
                  "roles": ["app", {"svc": {"offset": -5}}]}}
    result = build({}, pools=pool)
    assert any("below" in e or "negative" in e for e in result["errors"]), \
        result["errors"]
    assert "9.255.251.0/24" not in [s.get("subnet") for s in result["segments"]]


def test_a_gateway_on_the_broadcast_address_is_rejected():
    """C-POOL-GW-BCAST: inside the subnet, but not a usable host."""
    pool = {"p": {"vlan_base": 100, "instances": 1, "subnet_base": "10.0.0.0/24",
                  "gateway_offset": 255, "roles": ["app"], "platforms": ["gamma"],
                  "site": "s", "zone": "z", "key": "{{ role }}"}}
    errs = build({}, pools=pool)["errors"]
    assert any("gateway" in e.lower() for e in errs), errs


def test_a_normal_gateway_offset_is_still_accepted():
    pool = {"p": {"vlan_base": 100, "instances": 1, "subnet_base": "10.0.0.0/24",
                  "gateway_offset": 1, "roles": ["app"], "platforms": ["gamma"],
                  "site": "s", "zone": "z", "key": "{{ role }}"}}
    result = build({}, pools=pool)
    assert result["errors"] == []
    assert result["segments"][0]["gateway"] == "10.0.0.1"


# ──────────────────────────────────────────────────────────────────────────
# Chaos round 3 — name recipes must not raise
# ──────────────────────────────────────────────────────────────────────────
def test_a_negative_vlan_pad_is_reported_not_raised():
    """N-PAD-NEG: reached f-string as format spec '0-3d' → ValueError."""
    result = build({"a": {"vlan_id": 7, "platforms": ["gamma"], "bu": "x",
                          "stage": "y", "purpose": "web"}},
                   names={"name": {"parts": ["vlan"], "vlan_prefix": "V",
                                   "vlan_pad": -3}}, name_default="name")
    assert any("vlan_pad" in e for e in result["errors"]), result["errors"]


def test_a_glue_group_nested_more_than_one_deep_is_reported_not_raised():
    """N-GLUE-3DEEP: a list inside a glue group hit dict lookup → TypeError."""
    result = build({"a": {"vlan_id": 1, "platforms": ["gamma"], "bu": "x",
                          "stage": "y", "purpose": "web"}},
                   names={"name": {"parts": [[["purpose"]]], "case": "lower"}},
                   name_default="name")
    assert any("glue" in e or "nested" in e for e in result["errors"]), \
        result["errors"]


def test_a_normal_glue_group_still_works():
    result = build({"a": {"vlan_id": 1, "platforms": ["gamma"], "bu": "x",
                          "stage": "y", "purpose": "web"}},
                   names={"name": {"parts": [["bu", "purpose"]], "sep": "-",
                                   "case": "lower"}}, name_default="name")
    assert result["errors"] == []
    assert result["segments"][0]["name"] == "xweb"


# ──────────────────────────────────────────────────────────────────────────
# Pools: WYSIWYG key/name templates
# ──────────────────────────────────────────────────────────────────────────
def _env():
    from jinja2.nativetypes import NativeEnvironment
    return NativeEnvironment()


TEMPLATE_POOL = {"vlan_base": 100, "vlan_stride": 10, "instances": 2,
        "roles": ["app", "db"], "platforms": ["gamma"], "site": "s", "zone": "z",
        "tenant": "acme", "env": "dev",
        "key": "{{ pool }}-{{ instance_nn }}-{{ role }}"}


def _pooled(**over):
    cfg = dict(BASE_CONFIG)
    cfg.update({"platforms": ["gamma"], "required": {"all": []},
                "pools": {"p": {**TEMPLATE_POOL, **over}}})
    return nc.network_catalog({}, cfg, env=_env())


def test_a_key_template_shows_its_separators_and_order():
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}")
    assert sorted(result["keys"]) == ["p-01-app", "p-01-db",
                                      "p-02-app", "p-02-db"]


def test_a_name_template_can_mix_literal_text_and_tokens():
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     name="vlan{{ vid }}-{{ tenant }}-{{ role }}")
    assert result["name_by_key"]["p-01-app"] == "vlan100-acme-app"


def test_a_template_can_use_ordinary_filters():
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     name="{{ (tenant ~ '-' ~ role) | upper }}")
    assert result["name_by_key"]["p-01-app"] == "ACME-APP"


def test_a_template_without_an_environment_is_reported_not_raised():
    """Pools using {{ }} must be loaded via lookup+from_yaml."""
    cfg = dict(BASE_CONFIG)
    cfg.update({"platforms": ["gamma"], "required": {"all": []},
                "pools": {"p": {**TEMPLATE_POOL, "key": "{{ pool }}-{{ role }}"}}})
    result = nc.network_catalog({}, cfg)          # no env
    assert any("Jinja environment" in e for e in result["errors"]), \
        result["errors"]


def test_a_broken_template_names_the_pool_and_the_expression():
    result = _pooled(key="{{ pool }}-{{ no_such_token.deeper }}")
    assert any("pools.p.key" in e for e in result["errors"]), result["errors"]


def test_template_keys_are_not_stamped_onto_the_segments():
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     name="{{ role }}")
    assert "key" not in result["segments"][0] or \
        result["segments"][0]["key"] == "p-01-app"
    assert result["segments"][0].get("name") == "app"


# ──────────────────────────────────────────────────────────────────────────
# Pool guards: a typo should not generate an estate
# ──────────────────────────────────────────────────────────────────────────
def test_a_stride_smaller_than_the_role_span_is_rejected():
    """vlan_stride 0 (or any value <= the widest role offset) makes instance
    N+1 start inside instance N's block, so members silently share VLANs."""
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     vlan_stride=0)
    assert any("vlan_stride" in e for e in result["errors"]), result["errors"]


def test_a_stride_exactly_the_role_count_is_accepted():
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     vlan_stride=2)          # 2 roles, offsets 0 and 1
    assert result["errors"] == []


def test_a_single_instance_pool_ignores_the_stride():
    """With one instance there is no next block to collide with."""
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     instances=1, vlan_stride=0)
    assert result["errors"] == []


def test_a_pool_larger_than_the_cap_is_refused_before_it_expands():
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     instances=5000)
    assert any("max" in e.lower() and "5000" in e or "10000" in e
               for e in result["errors"]), result["errors"]
    assert len(result["segments"]) < 10000


def test_the_cap_is_configurable():
    cfg = dict(BASE_CONFIG)
    cfg.update({"platforms": ["gamma"], "required": {"all": []},
                "pool_max_segments": 4,
                "pools": {"p": {**TEMPLATE_POOL,
                                "key": "{{ pool }}-{{ instance_nn }}-{{ role }}",
                                "instances": 3}}})       # 3 x 2 = 6 > 4
    result = nc.network_catalog({}, cfg, env=_env())
    assert any("pool_max_segments" in e or "max" in e.lower()
               for e in result["errors"]), result["errors"]


# ──────────────────────────────────────────────────────────────────────────
# Pools: a member with no usable identity is not generated
#
# A reported error used to be no protection at all — the segment was inserted
# anyway, under a placeholder key, carrying a real VLAN and real on_<platform>
# flags into every derived view.
# ──────────────────────────────────────────────────────────────────────────
def _cfg_with(pools, **extra):
    cfg = dict(BASE_CONFIG)
    cfg.update({"platforms": ["gamma"], "required": {"all": []}, "pools": pools})
    cfg.update(extra)
    return cfg


def test_a_failed_key_template_generates_no_segment_at_all():
    result = _pooled(key="{{ pool }}-{{ no_such_token.deeper }}")
    assert any("pools.p.key" in e for e in result["errors"]), result["errors"]
    assert result["keys"] == [], result["keys"]
    assert result["segments"] == []


def test_a_failed_name_template_generates_no_segment_either():
    """The key rendered fine; the NAME did not. Neither ships a segment."""
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     name="{{ missing.attr }}")
    assert any("pools.p.name" in e for e in result["errors"]), result["errors"]
    assert result["keys"] == [], result["keys"]
    assert not any("error" in k for k in result["keys"])


def test_a_failed_render_leaves_no_placeholder_key_behind():
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     name="{{ missing.attr }}")
    assert not [k for k in result["by_key"] if "-error-" in k], result["by_key"]


def test_a_key_that_renders_blank_is_refused():
    """An empty key addresses nothing, and every member collapses onto it."""
    result = _pooled(key="{{ '' }}")
    assert any("BLANK key" in e for e in result["errors"]), result["errors"]
    assert "" not in result["by_key"]
    assert result["keys"] == []


def test_a_whitespace_only_key_counts_as_blank():
    result = _pooled(key="   ")
    assert any("BLANK key" in e for e in result["errors"]), result["errors"]
    assert result["keys"] == []


def test_an_undefined_token_in_a_key_template_names_the_missing_token():
    """Jinja's default Undefined renders '', so `{{ nope }}` silently
    truncated the key instead of failing."""
    result = _pooled(key="{{ pool }}-{{ nope }}")
    assert any("nope" in e and "undefined" in e for e in result["errors"]), \
        result["errors"]
    assert "p-" not in result["by_key"]
    assert result["keys"] == []


def test_an_undefined_token_in_a_name_template_is_named():
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     name="vlan{{ vid }}-{{ nope }}")
    assert any("nope" in e and "undefined" in e for e in result["errors"]), \
        result["errors"]
    assert result["keys"] == []


def test_a_default_filter_still_covers_an_optional_token():
    """Strict undefined must not break the documented `| default(...)`."""
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     name="{{ nickname | default('anon') }}-{{ role }}")
    assert result["errors"] == [], result["errors"]
    assert result["name_by_key"]["p-01-app"] == "anon-app"


def test_a_declared_token_still_renders_under_strict_undefined():
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     name="{{ tenant }}-{{ env }}-{{ vid }}")
    assert result["errors"] == [], result["errors"]
    assert result["name_by_key"]["p-01-app"] == "acme-dev-100"


# ──────────────────────────────────────────────────────────────────────────
# Pools: the root shape
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [["a", "b"], "just-a-string", 7])
def test_a_pools_root_that_is_not_a_mapping_is_rejected(bad):
    """A pools FILE written as a list parsed clean, coerced to {} and left a
    catalog reporting zero pools and zero errors."""
    result = nc.network_catalog(SEGMENTS, _cfg_with(bad))
    assert any("pools must be a mapping" in e for e in result["errors"]), \
        result["errors"]


def test_a_pool_value_that_is_not_a_mapping_is_named():
    result = nc.network_catalog(SEGMENTS, _cfg_with({"p": ["not", "a", "spec"]}))
    assert any("pools.p must be a mapping" in e for e in result["errors"]), \
        result["errors"]


def test_an_empty_pools_mapping_is_still_silent():
    result = nc.network_catalog(SEGMENTS, _cfg_with({}))
    assert not any("pools" in e for e in result["errors"]), result["errors"]


# ──────────────────────────────────────────────────────────────────────────
# Pools: the segment cap
# ──────────────────────────────────────────────────────────────────────────
def test_pool_max_segments_zero_means_the_default():
    """The wiring passes 0 for an undeclared setting, so 0 == unset."""
    result = nc.network_catalog(
        {}, _cfg_with({"p": {**TEMPLATE_POOL,
                             "key": "{{ pool }}-{{ instance_nn }}-{{ role }}"}},
                      pool_max_segments=0), env=_env())
    assert result["errors"] == [], result["errors"]
    assert len(result["segments"]) == 4


def test_a_negative_pool_max_segments_is_refused_not_used_as_a_limit():
    """A negative cap made every pool trip the over-limit guard, blaming the
    estate's size for what is a typo in the cap."""
    result = nc.network_catalog(
        {}, _cfg_with({"p": {**TEMPLATE_POOL,
                             "key": "{{ pool }}-{{ instance_nn }}-{{ role }}"}},
                      pool_max_segments=-5), env=_env())
    assert any("POSITIVE integer" in e for e in result["errors"]), \
        result["errors"]
    assert not any("over the" in e for e in result["errors"]), result["errors"]
    assert len(result["segments"]) == 4


def test_a_non_numeric_cap_is_reported_and_the_default_used():
    result = nc.network_catalog(
        {}, _cfg_with({"p": {**TEMPLATE_POOL,
                             "key": "{{ pool }}-{{ instance_nn }}-{{ role }}"}},
                      pool_max_segments="lots"), env=_env())
    assert any("pool_max_segments" in e and "not a number" in e
               for e in result["errors"]), result["errors"]
    assert len(result["segments"]) == 4


def test_a_positive_cap_is_still_enforced():
    result = nc.network_catalog(
        {}, _cfg_with({"p": {**TEMPLATE_POOL,
                             "key": "{{ pool }}-{{ instance_nn }}-{{ role }}",
                             "instances": 3}},
                      pool_max_segments=4), env=_env())
    assert any("over the 4 limit" in e for e in result["errors"]), \
        result["errors"]


# ──────────────────────────────────────────────────────────────────────────
# Pools: key vs key_parts precedence
# ──────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────
# Pools: loaded from a file, so their `{{ }}` never meets Ansible's templar
# ──────────────────────────────────────────────────────────────────────────
POOLS_FILE = """---
p:
  vlan_base: 100
  vlan_stride: 10
  instances: 2
  roles: [app, db]
  platforms: [gamma]
  site: s
  tenant: acme
  key: "{{ pool }}-{{ instance_nn }}-{{ role }}"
  name: "vlan{{ vid }}-{{ tenant }}-{{ role }}"
"""


def _from_file(tmp_path, body, name="pools.yml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    cfg = dict(BASE_CONFIG)
    cfg.update({"platforms": ["gamma"], "required": {"all": []},
                "pools_file": str(path)})
    return nc.network_catalog({}, cfg, env=_env())


def test_pools_are_read_from_a_file_with_their_templates_intact(tmp_path):
    result = _from_file(tmp_path, POOLS_FILE)
    assert result["errors"] == [], result["errors"]
    assert sorted(result["keys"]) == ["p-01-app", "p-01-db",
                                      "p-02-app", "p-02-db"]
    assert result["name_by_key"]["p-01-app"] == "vlan100-acme-app"


def test_a_missing_pools_file_is_one_named_error(tmp_path):
    cfg = dict(BASE_CONFIG)
    cfg.update({"platforms": ["gamma"], "required": {"all": []},
                "pools_file": str(tmp_path / "nope.yml")})
    result = nc.network_catalog(SEGMENTS, cfg, env=_env())
    named = [e for e in result["errors"] if "pools_file" in e]
    assert len(named) == 1, result["errors"]
    assert "nope.yml" in named[0]


def test_a_pools_file_that_is_a_directory_is_one_named_error(tmp_path):
    cfg = dict(BASE_CONFIG)
    cfg.update({"platforms": ["gamma"], "required": {"all": []},
                "pools_file": str(tmp_path)})
    result = nc.network_catalog(SEGMENTS, cfg, env=_env())
    assert any("pools_file" in e for e in result["errors"]), result["errors"]


def test_an_unset_pools_file_is_not_an_error():
    """No pools is a legitimate catalog, not a misconfiguration."""
    for empty in (None, "", "   "):
        cfg = dict(BASE_CONFIG)
        cfg.update({"pools_file": empty})
        result = nc.network_catalog(SEGMENTS, cfg)
        assert not any("pools_file" in e for e in result["errors"]), \
            (empty, result["errors"])


def test_an_unparseable_pools_file_is_named_not_raised(tmp_path):
    result = _from_file(tmp_path, "p: [unclosed\n")
    assert any("pools_file" in e and "valid YAML" in e
               for e in result["errors"]), result["errors"]


def test_a_pools_file_holding_a_list_is_rejected_by_the_shape_check(tmp_path):
    result = _from_file(tmp_path, "---\n- a\n- b\n")
    assert any("pools must be a mapping" in e for e in result["errors"]), \
        result["errors"]


def test_an_inline_pools_dict_still_wins_over_the_file(tmp_path):
    """The unit suite and any direct caller keep handing pools in directly."""
    path = tmp_path / "pools.yml"
    path.write_text(POOLS_FILE, encoding="utf-8")
    cfg = dict(BASE_CONFIG)
    cfg.update({"platforms": ["gamma"], "required": {"all": []},
                "pools_file": str(path),
                "pools": {"inline": {**TEMPLATE_POOL,
                                     "key": "{{ pool }}-{{ role }}"}}})
    result = nc.network_catalog({}, cfg, env=_env())
    assert sorted(result["keys"]) == ["inline-app", "inline-db"]


# ──────────────────────────────────────────────────────────────────────────
# Addressing: never materialise a host set
#
# `set(subnet.hosts())` on an IPv6 /64 is 2**64 address objects and never
# returns — an IPv6 subnet_base with any gateway_offset hung the whole play.
# ──────────────────────────────────────────────────────────────────────────
import ipaddress                                          # noqa: E402


@pytest.mark.parametrize(
    "cidr",
    ["10.0.0.0/24", "10.0.0.0/30", "10.0.0.0/31", "10.0.0.0/32", "10.0.0.0/29",
     "fd00::/120", "fd00::/126", "fd00::/127", "fd00::/128", "fd00::/125"],
)
def test_the_usable_host_check_matches_hosts_exactly(cidr):
    """The O(1) check must agree with hosts() on both families, including the
    IPv4-only broadcast reservation."""
    net = ipaddress.ip_network(cidr)
    expected = set(net.hosts()) if net.num_addresses > 2 else set(net)
    actual = {a for a in net if nc._is_usable_host(net, a)}
    assert actual == expected, cidr


def test_a_gateway_on_a_huge_subnet_is_answered_without_enumerating_it():
    """A /64 has 2**64 addresses; this must return, not hang."""
    net = ipaddress.ip_network("fd00::/64")
    assert nc._is_usable_host(net, ipaddress.ip_address("fd00::1")) is True
    assert nc._is_usable_host(net, ipaddress.ip_address("fd00::")) is False
    assert nc._is_usable_host(net, ipaddress.ip_address("fd01::1")) is False


def test_an_ipv6_pool_with_a_gateway_offset_builds(monkeypatch):
    """The live symptom: an IPv6 subnet_base plus gateway_offset hung."""
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     subnet_base="fd00::/64", gateway_offset=1)
    assert result["errors"] == [], result["errors"]
    assert result["gateway_by_key"]["p-01-app"] == "fd00::1"


def test_a_large_ipv4_pool_subnet_builds_too():
    """Same bug, smaller exponent: a /8 is 16M address objects."""
    result = _pooled(key="{{ pool }}-{{ instance_nn }}-{{ role }}",
                     subnet_base="10.0.0.0/8", gateway_offset=1)
    assert result["errors"] == [], result["errors"]
    assert result["gateway_by_key"]["p-01-app"] == "10.0.0.1"


# ──────────────────────────────────────────────────────────────────────────
# Addressing: the hand-written path judges a gateway like the pool path
# ──────────────────────────────────────────────────────────────────────────
def _row(**kw):
    return {"a": {"vlan_id": 10, "platforms": ["gamma"], "purpose": "web", **kw}}


def _plain(segments, **over):
    cfg = dict(BASE_CONFIG)
    cfg.update({"platforms": ["gamma"], "required": {"all": []},
                "names": {"name": {"parts": ["purpose"]}}, "name_default": "name"})
    cfg.update(over)
    return nc.network_catalog(segments, cfg)


@pytest.mark.parametrize("gateway,role", [("10.0.0.0", "network"),
                                          ("10.0.0.3", "broadcast")])
def test_a_hand_written_gateway_on_the_network_or_broadcast_is_rejected(gateway, role):
    """The pool path has refused these since round 3; the hand-written path
    only checked `in subnet`, which is true for both."""
    result = _plain(_row(subnet="10.0.0.0/30", gateway=gateway))
    assert any(role in e and "no interface can hold" in e
               for e in result["errors"]), result["errors"]


def test_a_normal_hand_written_gateway_is_still_accepted():
    result = _plain(_row(subnet="10.0.0.0/30", gateway="10.0.0.1"))
    assert result["errors"] == [], result["errors"]


def test_a_hand_written_gateway_on_a_31_is_accepted():
    """A /31 has no network/broadcast convention — both addresses are usable."""
    result = _plain(_row(subnet="10.0.0.0/31", gateway="10.0.0.0"))
    assert result["errors"] == [], result["errors"]


def test_an_ipv6_gateway_on_the_last_address_is_accepted():
    """IPv6 does not reserve a broadcast address; the last one is ordinary."""
    result = _plain(_row(subnet="fd00::/126", gateway="fd00::3"))
    assert result["errors"] == [], result["errors"]


def test_a_gateway_outside_its_subnet_is_still_named_that_way():
    result = _plain(_row(subnet="10.0.0.0/24", gateway="10.9.9.1"))
    assert any("outside its subnet" in e for e in result["errors"]), \
        result["errors"]


def test_a_mixed_family_gateway_builds_no_gateway_cidr():
    """'fd00::1/24' is not an address in any family; it used to be built and
    handed to whatever templates an interface line."""
    result = _plain(_row(subnet="10.0.0.0/24", gateway="fd00::1"))
    assert result["by_key"]["a"]["gateway_cidr"] == ""
    assert result["errors"], "the mismatch must still be reported"


def test_a_matching_family_gateway_still_builds_its_cidr():
    result = _plain(_row(subnet="10.0.0.0/24", gateway="10.0.0.1"))
    assert result["by_key"]["a"]["gateway_cidr"] == "10.0.0.1/24"


# ──────────────────────────────────────────────────────────────────────────
# enrich: a segment may not declare what the engine computes
# ──────────────────────────────────────────────────────────────────────────
def test_a_declared_tagged_flag_is_reported_not_silently_overwritten():
    """`tagged: false` on a tagged VLAN read like a way to keep a segment off
    a trunk. It was not one — enrich recomputes it from vlan_id."""
    result = _plain(_row(tagged=False))
    assert any("'tagged' is computed" in e for e in result["errors"]), \
        result["errors"]
    assert result["by_key"]["a"]["tagged"] is True


def test_a_declared_on_platform_flag_is_reported():
    result = _plain(_row(on_gamma=False))
    assert any("'on_gamma' is computed" in e and "platforms" in e
               for e in result["errors"]), result["errors"]


def test_a_declared_on_platform_for_a_platform_not_listed_is_reported():
    result = _plain(_row(on_gamma=True), platforms=["gamma", "alpha"])
    assert any("'on_gamma' is computed" in e for e in result["errors"]), \
        result["errors"]


@pytest.mark.parametrize("field", ["gateway_cidr", "derived_name", "key"])
def test_other_computed_fields_are_reported_too(field):
    result = _plain(_row(**{field: "anything"}))
    assert any(f"'{field}' is computed" in e for e in result["errors"]), \
        result["errors"]


def test_prefixlen_is_reported_only_when_a_subnet_supplies_it():
    with_subnet = _plain(_row(subnet="10.0.0.0/24", prefixlen=99))
    assert any("'prefixlen' is computed" in e for e in with_subnet["errors"]), \
        with_subnet["errors"]
    # Without a subnet, prefixlen is a legitimate INPUT and stays one.
    without = _plain(_row(prefixlen=29))
    assert without["errors"] == [], without["errors"]
    assert without["by_key"]["a"]["prefixlen"] == 29


def test_a_clean_segment_declares_none_of_them():
    result = _plain(_row(subnet="10.0.0.0/24", gateway="10.0.0.1",
                         operator_source=True, description="ordinary data"))
    assert result["errors"] == [], result["errors"]


# ──────────────────────────────────────────────────────────────────────────
# Names: recipes, the `from` graph, and blank primaries
# ──────────────────────────────────────────────────────────────────────────
def test_a_recipe_named_after_an_engine_field_is_refused():
    """`names.gateway` + `gateway: 10.0.10.1` made the primary name an IP
    address, because a field named after a recipe pins that name."""
    result = _plain(_row(subnet="10.0.10.0/24", gateway="10.0.10.1"),
                    names={"gateway": {"parts": ["purpose"]}},
                    name_default="gateway")
    assert any("names.gateway" in e and "may not be named after" in e
               for e in result["errors"]), result["errors"]


@pytest.mark.parametrize("reserved", ["subnet", "vlan_id", "platforms",
                                      "tagged", "netmask", "role", "pool"])
def test_every_engine_field_is_refused_as_a_recipe_name(reserved):
    result = _plain(_row(), names={reserved: {"parts": ["purpose"]}},
                    name_default=reserved)
    assert any(f"names.{reserved}" in e and "may not be named after" in e
               for e in result["errors"]), result["errors"]


def test_a_recipe_called_name_is_still_allowed():
    """Pinning `name:` IS the documented mechanism — it must not be refused."""
    result = _plain(_row())
    assert not any("may not be named after" in e for e in result["errors"]), \
        result["errors"]


def test_a_missing_name_default_is_reported():
    result = _plain(_row(), name_default="nope")
    assert any("name_default 'nope'" in e for e in result["errors"]), \
        result["errors"]


def test_an_unknown_from_target_is_reported():
    result = _plain(_row(), names={"name": {"parts": ["purpose"]},
                                   "alias": {"from": "nope"}})
    assert any("names.alias" in e and "not a declared recipe" in e
               for e in result["errors"]), result["errors"]


def test_a_self_referencing_from_is_reported():
    result = _plain(_row(), names={"name": {"from": "name",
                                            "parts": ["purpose"]}})
    assert any("refers to itself" in e for e in result["errors"]), \
        result["errors"]


def test_a_circular_from_is_reported_via_its_forward_edge():
    """Every cycle contains at least one recipe inheriting a LATER one."""
    result = _plain(_row(), names={"a": {"from": "b", "parts": ["purpose"]},
                                   "b": {"from": "a", "parts": ["purpose"]}},
                    name_default="a")
    assert any("declared LATER" in e for e in result["errors"]), \
        result["errors"]


def test_a_forward_from_reference_is_reported():
    """`from` inherits the parent AS RESOLVED FOR THIS SEGMENT, which only
    exists once the parent has run."""
    result = _plain(_row(), names={"early": {"from": "late", "parts": ["purpose"]},
                                   "late": {"parts": ["purpose"]}},
                    name_default="late")
    assert any("names.early" in e and "declared LATER" in e
               for e in result["errors"]), result["errors"]


def test_a_backward_from_reference_is_still_fine():
    result = _plain(_row(), names={"base": {"parts": ["purpose"]},
                                   "derived": {"from": "base"}},
                    name_default="base")
    assert result["errors"] == [], result["errors"]


def test_a_blank_primary_name_is_reported():
    """The gate checks errors, missing fields and duplicates; one blank name
    is none of those, so a nameless port group shipped."""
    result = _plain(_row(), names={"name": {"parts": ["purpose"],
                                            "drop_tokens": ["purpose"]}})
    assert any("primary name is blank" in e for e in result["errors"]), \
        result["errors"]


def test_a_blank_name_is_not_reported_twice_for_a_missing_default():
    """A broken name_default already names the cause; do not also report every
    segment for the effect."""
    result = _plain(_row(), name_default="nope")
    assert not any("primary name is blank" in e for e in result["errors"]), \
        result["errors"]


def test_a_catalog_with_no_recipes_at_all_is_not_flagged_per_segment():
    result = _plain(_row(), names={}, name_default="")
    assert not any("primary name is blank" in e for e in result["errors"]), \
        result["errors"]


def test_a_normal_name_is_not_flagged():
    result = _plain(_row())
    assert result["errors"] == [], result["errors"]
    assert result["by_key"]["a"]["name"] == "web"


def test_a_list_valued_primary_name_is_reported_not_raised():
    """A recipe sharing its name with a list-valued field pinned `name` to a
    LIST, and the duplicate scan then raised TypeError while building the
    return dict — the whole catalog aborted, unreadable."""
    result = _plain(_row(), names={"platforms": {"parts": ["purpose"]}},
                    name_default="platforms")
    assert any("not a name" in e for e in result["errors"]), result["errors"]
    assert any("may not be named after" in e for e in result["errors"]), \
        result["errors"]


def test_duplicate_detection_survives_unhashable_names():
    """The scan must be total even when a name is not a string."""
    assert nc._duplicates([["a"], ["a"], ["b"]]) == [["a"]]
    assert nc._duplicates(["x", "x", "y"]) == ["x"]
    assert nc._duplicates([{"k": 1}, {"k": 1}]) == [{"k": 1}]


# ──────────────────────────────────────────────────────────────────────────
# Views: a `where:` that can never match
# ──────────────────────────────────────────────────────────────────────────
def _viewed(where, **over):
    cfg = dict(BASE_CONFIG)
    cfg.update({"platforms": ["gamma"], "required": {"all": []},
                "names": {"name": {"parts": ["purpose"]}}, "name_default": "name",
                "views": {"ns": {"v": {"platform": "gamma", "where": where,
                                       "fields": {"n": "name"}}}}})
    cfg.update(over)
    return nc.network_catalog(_row(), cfg)


def test_a_where_on_a_field_no_segment_carries_is_left_alone():
    """A view may legitimately be written before the segments it selects."""
    result = _viewed({"not_a_field_anywhere": "x"})
    assert result["errors"] == [], result["errors"]


def test_a_declared_description_passes_through_and_an_absent_one_is_blank():
    """The `desc_template` str.format fallback is gone — a third string
    sublanguage beside the name recipes and the view fields, with an empty
    estate template and no segment ever setting its own."""
    result = build({"a": {"vlan_id": 3, "platforms": ["gamma"],
                          "description": "acme segment on VLAN 3"},
                    "b": {"vlan_id": 4, "platforms": ["gamma"]}})
    assert result["by_key"]["a"]["description"] == "acme segment on VLAN 3"
    assert result["by_key"]["b"]["description"] == ""
