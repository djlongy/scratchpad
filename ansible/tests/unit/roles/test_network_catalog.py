"""Unit tests for plugins/filter/network_catalog.py.

The engine must stay estate-agnostic: these tests deliberately use vendors,
field names and naming conventions that do NOT exist in this repo's own
networks.yml, so a change that quietly bakes one estate's vocabulary into the engine
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
    "platforms": ["alpha", "beta", "gamma"],
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
    "required": {"all": ["vlan_id", "platforms"], "by_platform": {"gamma": ["gw"]}},
    "views": {},
}

SEGMENTS = {
    "web_prod": {
        "vlan_id": 1101,
        "subnet": "10.11.1.0/24",
        "gw": "10.11.1.1",
        "gateway": "10.11.1.1",
        "platforms": ["alpha", "beta", "gamma"],
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
        "platforms": ["alpha"],
        "bu": "retail",
        "stage": "dev",
        "purpose": "db",
    },
}


def build(segments=None, **overrides):
    cfg = dict(BASE_CONFIG)
    cfg.update(overrides)
    return nc.network_catalog(SEGMENTS if segments is None else segments, cfg)


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
    assert row["on_alpha"] is True
    assert row["on_beta"] is False


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


def test_description_falls_back_to_the_template():
    segs = {"a": {"vlan_id": 3, "platforms": [], "bu": "acme"}}
    row = build(segs, desc_template="{bu} segment on VLAN {vid}")["by_key"]["a"]
    assert row["description"] == "acme segment on VLAN 3"


# ──────────────────────────────────────────────────────────────────────────
# platform views
# ──────────────────────────────────────────────────────────────────────────
VIEWS = {
    "alpha": {
        "segments": {
            "platform": "alpha",
            "fields": {"display_name": "name", "vlan_ids": "vlan_id", "mtu": "mtu"},
            "append": [{"display_name": "legacy", "vlan_ids": 999, "mtu": 1500}],
        }
    },
    "beta": {
        "epgs": {
            "platform": "beta",
            "consts": {"tenant": "T1"},
            "fields": {
                "epg": "short",
                "encap": "vlan-{vlan_id}",
                "tn": "{tenant}",
                "kind": {"const": "epg"},
            },
        }
    },
    "gamma": {
        "interfaces": {
            "platform": "gamma",
            "where": {"tagged": True},
            "group_by": "stage",
            "fields": {"name": "port1.{vlan_id}", "seq": "index", "ip": "gw"},
        }
    },
}


def test_view_is_namespaced_by_platform():
    views = build(views=VIEWS)["views"]
    assert set(views) == {"alpha", "beta", "gamma"}
    assert [r["display_name"] for r in views["alpha"]["segments"]][:2] == [
        "seg1101_retail_prod_web",
        "seg2101_retail_dev_db",
    ]


def test_platform_filters_which_segments_reach_a_view():
    views = build(views=VIEWS)["views"]
    assert len(views["beta"]["epgs"]) == 1  # only web_prod targets beta


def test_append_bolts_an_existing_hand_written_list_on_untouched():
    rows = build(views=VIEWS)["views"]["alpha"]["segments"]
    assert rows[-1] == {"display_name": "legacy", "vlan_ids": 999, "mtu": 1500}


def test_field_spec_forms_template_const_and_consts():
    row = build(views=VIEWS)["views"]["beta"]["epgs"][0]
    assert row == {
        "epg": "retail_prod_web",
        "encap": "vlan-1101",
        "tn": "T1",
        "kind": "epg",
    }


def test_missing_source_field_is_omitted_rather_than_nulled():
    views = {"p": {"v": {"fields": {"nope": "does_not_exist", "ok": "vlan_id"}}}}
    rows = build(views=views)["views"]["p"]["v"]
    assert all("nope" not in r for r in rows)
    assert all("ok" in r for r in rows)


def test_group_by_produces_a_dict_and_index_counts_within_the_group():
    grouped = build(views=VIEWS)["views"]["gamma"]["interfaces"]
    assert set(grouped) == {"prod"}
    assert grouped["prod"][0]["seq"] == 1


def test_group_by_append_is_keyed_by_group():
    views = {
        "p": {
            "v": {
                "group_by": "stage",
                "fields": {"n": "name"},
                "append": {"prod": [{"n": "extra-prod"}]},
            }
        }
    }
    grouped = build(views=views)["views"]["p"]["v"]
    assert grouped["prod"][-1] == {"n": "extra-prod"}
    assert all(r["n"] != "extra-prod" for r in grouped["dev"])


def test_nested_group_is_emitted_only_when_a_watched_field_is_truthy():
    segs = {
        "relaxed": {"vlan_id": 1, "platforms": ["alpha"], "promisc": True},
        "strict": {"vlan_id": 2, "platforms": ["alpha"], "promisc": False},
    }
    views = {
        "p": {
            "v": {
                "platform": "alpha",
                "fields": {
                    "id": "vlan_id",
                    "sec": {"emit_when_any": ["promisc"],
                            "group": {"allow": "promisc"}},
                },
            }
        }
    }
    rows = {r["id"]: r for r in build(segs, views=views)["views"]["p"]["v"]}
    assert rows[1]["sec"] == {"allow": True}
    assert "sec" not in rows[2]


def test_omit_if_falsy_drops_the_key_entirely():
    segs = {"a": {"vlan_id": 1, "platforms": ["alpha"], "trunk": False}}
    views = {"p": {"v": {"platform": "alpha",
                         "fields": {"id": "vlan_id", "trunk": "trunk"},
                         "omit_if_falsy": ["trunk"]}}}
    assert "trunk" not in build(segs, views=views)["views"]["p"]["v"][0]


def test_source_chains_one_view_off_another_and_unique_by_dedupes():
    views = {
        "p": {
            "rows": {"fields": {"zone": "stage"}},
            "zones": {"source": "rows", "unique_by": "zone", "fields": {"zone": "zone"}},
        }
    }
    built = build(views=views)["views"]["p"]
    assert sorted(r["zone"] for r in built["zones"]) == ["dev", "prod"]


def test_sort_by_orders_rows():
    views = {"p": {"v": {"fields": {"id": "vlan_id"}, "sort_by": "id"}}}
    rows = build(views=views)["views"]["p"]["v"]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)


# ──────────────────────────────────────────────────────────────────────────
# generated pools
# ──────────────────────────────────────────────────────────────────────────
POOL = {
    "tenant_x": {
        "vlan_base": 3000,
        "vlan_stride": 10,
        "instances": 2,
        "bu": "tenantx",
        "platforms": ["alpha"],
        "key_parts": ["bu", "vid", "role"],
        "roles": {
            "app": {"offset": 0, "purpose": "app"},
            "mon": {"offset": 5, "purpose": "mon", "platforms": ["beta"]},
        },
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
                  "key_parts": ["pool"]}}
    result = build({}, pools=pool)
    assert len(result["errors"]) == 1
    assert "collides" in result["errors"][0]
    # The first member survives; the collider is rejected, not silently preferred.
    assert result["vlan_by_key"] == {"p": 100}


def test_a_pool_key_that_collides_across_pools_errors_too():
    pools = {
        "one": {"vlan_base": 100, "instances": 1, "roles": ["a"], "key_parts": ["role"]},
        "two": {"vlan_base": 200, "instances": 1, "roles": ["a"], "key_parts": ["role"]},
    }
    result = build({}, pools=pools)
    assert len(result["errors"]) == 1
    assert "collides" in result["errors"][0]
    assert result["vlan_by_key"] == {"a": 100}


def test_distinct_pool_keys_produce_no_collision_error():
    pool = {"p": {"vlan_base": 100, "instances": 2, "roles": ["a", "b"],
                  "key_parts": ["pool", "vid", "role"]}}
    result = build({}, pools=pool)
    assert result["errors"] == []
    assert len(result["segments"]) == 4


def test_pool_stride_defaults_to_the_role_count():
    pool = {"p": {"vlan_base": 100, "instances": 2, "roles": ["a", "b"],
                  "key_parts": ["pool", "vid"]}}
    vlans = sorted(build({}, pools=pool)["vlan_by_key"].values())
    assert vlans == [100, 101, 102, 103]


def test_pool_roles_as_a_list_use_positional_offsets():
    pool = {"p": {"vlan_base": 10, "instances": 1, "roles": ["a", "b", "c"],
                  "key_parts": ["role"]}}
    result = build({}, pools=pool)
    assert result["vlan_by_key"] == {"a": 10, "b": 11, "c": 12}


def test_pool_list_entry_may_be_a_single_key_map_with_per_role_fields():
    pool = {"p": {"vlan_base": 10, "instances": 1, "platforms": ["alpha"],
                  "roles": ["a", {"b": {"platforms": ["beta"]}}],
                  "key_parts": ["role"]}}
    rows = build({}, pools=pool)["by_key"]
    assert rows["a"]["platforms"] == ["alpha"]
    assert rows["b"]["platforms"] == ["beta"]


def test_per_role_fields_override_pool_level_ones():
    rows = build(pools=POOL)["by_key"]
    assert rows["tenantx-3000-app"]["platforms"] == ["alpha"]
    assert rows["tenantx-3005-mon"]["platforms"] == ["beta"]


def test_generated_segments_flow_through_names_and_views():
    result = build(pools=POOL, views=VIEWS)
    assert result["name_by_key"]["tenantx-3000-app"] == "seg3000_tenantx_app"
    alpha = [r["display_name"] for r in result["views"]["alpha"]["segments"]]
    assert "seg3000_tenantx_app" in alpha


def test_pool_stamped_identical_subnet_repeats_without_any_duplicate_error():
    """Scenario: self-isolated test environments — every instance deliberately
    gets the SAME addressing (NAT'd behind its own router). Duplicate subnets
    must be legal; only names and tagged VLAN ids are uniqueness-checked."""
    pool = {"p": {"vlan_base": 100, "instances": 3, "roles": ["inside"],
                  "subnet": "192.168.1.0/24", "gateway": "192.168.1.1",
                  "netmask": "255.255.255.0", "key_parts": ["role", "vid"]}}
    result = build({}, pools=pool)
    subnets = [s["subnet"] for s in result["segments"]]
    assert subnets == ["192.168.1.0/24"] * 3
    assert all(s["gateway"] == "192.168.1.1" for s in result["segments"])
    assert result["errors"] == []
    assert result["duplicate_names"] == []


def test_pool_subnet_base_increments_one_network_per_segment():
    pool = {"p": {"vlan_base": 100, "instances": 2, "roles": ["a", "b"],
                  "subnet_base": "10.64.0.0/24", "gateway_offset": 1,
                  "key_parts": ["role", "vid"]}}
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
                  "key_parts": ["role", "vid"]}}
    rows = build({}, pools=pool)["by_key"]
    assert rows["a-1"]["subnet"] == "10.0.0.0/24"
    assert rows["a-2"]["subnet"] == "10.0.4.0/24"


def test_pool_subnet_index_vlan_mirrors_vlan_gaps():
    """subnet_index: vlan uses the VLAN's distance from vlan_base, so the
    subnet numbering mirrors the VLAN numbering, gaps included."""
    pool = {"p": {"vlan_base": 2000, "vlan_stride": 10, "instances": 2,
                  "roles": ["app", "db"], "subnet_base": "10.64.0.0/24",
                  "subnet_index": "vlan", "key_parts": ["role", "vid"]}}
    rows = build({}, pools=pool)["by_key"]
    assert rows["app-2000"]["subnet"] == "10.64.0.0/24"
    assert rows["db-2001"]["subnet"] == "10.64.1.0/24"
    assert rows["app-2010"]["subnet"] == "10.64.10.0/24"   # gap mirrored
    assert rows["db-2011"]["subnet"] == "10.64.11.0/24"


def test_pool_per_role_subnet_wins_and_neighbours_keep_their_index():
    pool = {"p": {"vlan_base": 1, "instances": 1,
                  "roles": ["a", {"b": {"subnet": "172.16.0.0/24"}}, "c"],
                  "subnet_base": "10.0.0.0/24", "key_parts": ["role", "vid"]}}
    rows = build({}, pools=pool)["by_key"]
    assert rows["a-1"]["subnet"] == "10.0.0.0/24"
    assert rows["b-2"]["subnet"] == "172.16.0.0/24"    # declared wins
    assert rows["c-3"]["subnet"] == "10.0.2.0/24"      # index 2, not 1


def test_pool_bad_subnet_base_is_reported_and_segments_still_emit():
    pool = {"p": {"vlan_base": 1, "instances": 1, "roles": ["a"],
                  "subnet_base": "not-a-network", "key_parts": ["role", "vid"]}}
    result = build({}, pools=pool)
    assert any("subnet_base 'not-a-network' is not a valid network" in e
               for e in result["errors"])
    assert "subnet" not in build({}, pools=pool)["by_key"]["a-1"] or \
           not result["by_key"]["a-1"].get("subnet")


def test_pool_subnet_overflow_is_reported_per_segment():
    pool = {"p": {"vlan_base": 1, "instances": 2, "roles": ["a"],
                  "subnet_base": "255.255.255.0/24", "key_parts": ["role", "vid"]}}
    result = build({}, pools=pool)
    assert result["by_key"]["a-1"]["subnet"] == "255.255.255.0/24"
    assert any("falls outside the address space" in e for e in result["errors"])
    assert not result["by_key"]["a-2"].get("subnet")


def test_hand_written_key_wins_over_a_pool_key_and_the_clash_is_reported():
    pool = {"p": {"vlan_base": 10, "instances": 1, "roles": ["a"], "key_parts": ["role"]}}
    segs = {"a": {"vlan_id": 999, "platforms": []}}
    result = nc.network_catalog(segs, {**BASE_CONFIG, "pools": pool})
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
    assert result["vlan_ids_by_platform"]["alpha"] == [1101, 2101]


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
    segs = {"a": {"vlan_id": 1, "platforms": ["alpha", "nxs"]}}
    assert any("unknown platform 'nxs'" in e for e in build(segs)["errors"])


def test_platforms_as_a_string_is_rejected():
    segs = {"a": {"vlan_id": 1, "platforms": "alpha"}}
    assert any("must be a LIST" in e for e in build(segs)["errors"])


def test_missing_vlan_id_is_named_and_does_not_crash():
    segs = {"a": {"platforms": ["alpha"]}}
    result = build(segs)
    assert any("vlan_id is required" in e for e in result["errors"])
    assert result["by_key"]["a"]["vlan_id"] == 0  # degraded, not exploded


def test_required_fields_are_checked_per_target():
    segs = {"a": {"vlan_id": 1, "platforms": ["gamma"]}}  # gamma requires gw
    assert build(segs)["missing"] == ["a: missing or empty gw"]


def test_empty_string_and_empty_list_count_as_missing():
    segs = {"a": {"vlan_id": 1, "platforms": []}}
    assert any("platforms" in m for m in build(segs)["missing"])


def test_unknown_name_recipe_on_a_segment_is_named():
    segs = {"a": {"vlan_id": 1, "platforms": [], "names": {"nope": {"parts": []}}}}
    assert any("is not a recipe" in e for e in build(segs)["errors"])


def test_view_referencing_a_later_source_is_rejected():
    views = {"p": {"first": {"source": "second", "fields": {"a": "key"}},
                   "second": {"fields": {"a": "key"}}}}
    assert any("declared earlier" in e for e in build(views=views)["errors"])


def test_view_without_fields_is_rejected():
    assert any("fields is required" in e for e in build(views={"p": {"v": {}}})["errors"])


def test_view_platform_outside_the_declared_platforms_is_rejected():
    views = {"p": {"v": {"platform": "wat", "fields": {"a": "key"}}}}
    assert any("not in the declared platforms" in e for e in build(views=views)["errors"])


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
    segs = {"a": {"vlan_id": "forty-two", "platforms": ["alpha"]}}
    result = build(segs)
    assert result["by_key"]["a"]["vlan_id"] == 0
    assert any("is not a number" in e for e in result["errors"])


# ──────────────────────────────────────────────────────────────────────────
# namespace vs platform filter — the two need not match
# ──────────────────────────────────────────────────────────────────────────
def test_namespace_may_differ_from_the_platform_filter():
    """The outer views key is a free output label; only `platform:` filters."""
    views = {"cisco": {"vlans": {"platform": "alpha", "fields": {"id": "vlan_id"}}}}
    result = build(views=views)
    assert result["errors"] == []
    assert len(result["views"]["cisco"]["vlans"]) == 2


def test_source_chaining_validates_against_the_namespace_not_the_filter():
    """A chained view under a namespace unlike its filter must not be a
    forward-reference error, and its rows must resolve."""
    views = {
        "cisco": {
            "vlans": {"platform": "alpha", "fields": {"id": "vlan_id", "z": "stage"}},
            "zones": {"source": "vlans", "unique_by": "z", "fields": {"z": "z"}},
        }
    }
    result = build(views=views)
    assert result["errors"] == []
    assert sorted(r["z"] for r in result["views"]["cisco"]["zones"]) == ["dev", "prod"]


def test_view_error_labels_use_the_namespace():
    """The second view's error must name its own namespace, not the previous
    view's platform filter."""
    views = {
        "cisco": {
            "vlans": {"platform": "alpha", "fields": {"id": "vlan_id"}},
            "broken": {"platform": "alpha"},  # no fields
        }
    }
    errors = build(views=views)["errors"]
    assert any(e.startswith("views.cisco.broken:") for e in errors)


# ──────────────────────────────────────────────────────────────────────────
# degrade-not-raise guarantees
# ──────────────────────────────────────────────────────────────────────────
def test_non_numeric_instance_degrades_to_the_raw_string():
    cfg_names = {"name": {"parts": [["purpose", "instance_nn"]], "sep": "-"}}
    segs = {"a": {"vlan_id": 1, "platforms": [], "purpose": "pod", "instance": "gold"}}
    assert build(segs, names=cfg_names)["name_by_key"]["a"] == "podgold"


def test_garbage_pool_int_knobs_degrade_to_their_defaults():
    pool = {"p": {"vlan_base": "junk", "vlan_stride": "junk", "instances": "junk",
                  "roles": [{"a": {"offset": "junk"}}], "key_parts": ["role"]}}
    result = build({}, pools=pool)  # must not raise
    assert result["vlan_by_key"] == {"a": 0}  # base 0 + offset 0, one set


def test_garbage_vlan_pad_degrades_to_no_padding():
    cfg_names = {"name": {"parts": ["vlan"], "vlan_prefix": "V", "vlan_pad": "wide"}}
    segs = {"a": {"vlan_id": 7, "platforms": []}}
    assert build(segs, names=cfg_names)["name_by_key"]["a"] == "V7"


def test_unique_by_on_unhashable_values_does_not_raise():
    segs = {
        "a": {"vlan_id": 1, "platforms": ["alpha"], "members": [1, 2]},
        "b": {"vlan_id": 2, "platforms": ["alpha"], "members": [1, 2]},
        "c": {"vlan_id": 3, "platforms": ["alpha"], "members": [9]},
    }
    views = {"p": {"v": {"platform": "alpha", "unique_by": "m",
                         "fields": {"m": "members"}}}}
    rows = build(segs, views=views)["views"]["p"]["v"]
    assert [r["m"] for r in rows] == [[1, 2], [9]]


def test_vlan_ranges_compress_contiguous_runs_per_platform():
    pool = {"p": {"vlan_base": 2000, "vlan_stride": 10, "instances": 3,
                  "roles": ["a", "b", "c", "d", "e"], "platforms": ["alpha"],
                  "key_parts": ["pool", "vid"]}}
    segs = {
        "one": {"vlan_id": 7, "platforms": ["alpha"]},
        "two": {"vlan_id": 8, "platforms": ["alpha"]},
    }
    result = build(segs, pools=pool)
    assert result["vlan_ranges_by_platform"]["alpha"] == [
        "7-8", "2000-2004", "2010-2014", "2020-2024",
    ]


def test_vlan_ranges_keep_singletons_single():
    segs = {"a": {"vlan_id": 5, "platforms": ["alpha"]},
            "b": {"vlan_id": 9, "platforms": ["alpha"]}}
    assert build(segs)["vlan_ranges_by_platform"]["alpha"] == ["5", "9"]


def test_vlan_ranges_cover_tagged_ids_only():
    segs = {"native": {"vlan_id": 0, "platforms": ["alpha"]},
            "tagged": {"vlan_id": 1, "platforms": ["alpha"]}}
    assert build(segs)["vlan_ranges_by_platform"]["alpha"] == ["1"]


def test_grouped_append_creates_a_bucket_for_an_append_only_group():
    views = {
        "p": {
            "v": {
                "group_by": "stage",
                "fields": {"n": "name"},
                "append": {"legacy": [{"n": "hand-maintained"}]},
            }
        }
    }
    grouped = build(views=views)["views"]["p"]["v"]
    assert grouped["legacy"] == [{"n": "hand-maintained"}]
    assert set(grouped) == {"prod", "dev", "legacy"}
