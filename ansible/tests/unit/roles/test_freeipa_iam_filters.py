"""Unit tests for roles/freeipa_server/filter_plugins/freeipa_iam.py.

Covers all five filter groups (composition, deletion safety, bulk-payload compilers,
raw-output prechecks, validation) — see the plugin's module docstring for the map.
Pure Python — no live FreeIPA, no Ansible runtime (the plugin falls back to a stub
AnsibleFilterError under pytest). These tests are the safety net for the raw-output
regexes: when a FreeIPA upgrade changes `ipa *-find --all --raw` formatting, paste a
sample of the NEW output into the RAW fixtures here and fix the parsers against it.

The thin RBAC overlay compiler is tested separately in test_freeipa_rbac_filters.py.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
MODULE_PATH = (REPO_ROOT / "ansible" / "roles" / "freeipa_server"
               / "filter_plugins" / "freeipa_iam.py")


@pytest.fixture(scope="module")
def fp():
    spec = importlib.util.spec_from_file_location("freeipa_iam", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ── merge: generated objects appended onto the native baseline key ────────────
def test_merge_appends_new_keeps_order(fp):
    base = [{"name": "baseline-grp", "description": "from snapshot"}]
    extra = [{"name": "role-x", "description": "generated"}]
    out = fp.freeipa_iam_merge(base, extra)
    assert [g["name"] for g in out] == ["baseline-grp", "role-x"]


def test_merge_baseline_wins_on_name_collision(fp):
    base = [{"name": "admins", "description": "BASELINE"}]
    extra = [{"name": "admins", "description": "GENERATED"}]
    out = fp.freeipa_iam_merge(base, extra)
    assert len(out) == 1
    assert out[0]["description"] == "BASELINE"


def test_merge_unions_user_groups(fp):
    base = [{"name": "alice", "first": "A", "groups": ["snapshot-grp"]}]
    extra = [{"name": "alice", "groups": ["role-matrix-grp"]},
             {"name": "bob", "groups": ["role-bob"]}]
    out = fp.freeipa_iam_merge(base, extra, union_fields=["groups"])
    by = {u["name"]: u for u in out}
    assert by["alice"]["groups"] == ["snapshot-grp", "role-matrix-grp"]
    assert by["alice"]["first"] == "A"
    assert by["bob"]["groups"] == ["role-bob"]


def test_merge_unions_group_and_user_fields(fp):
    # The RBAC overlay merges role groups (carrying user:) + policy groups (carrying
    # group: nesting) onto native groups; both list fields must union, not clobber.
    base = [{"name": "ug-x", "description": "native", "group": ["existing-nested"]}]
    extra = [{"name": "ug-x", "group": ["role-new"]},
             {"name": "role-new", "user": ["alice"]}]
    out = fp.freeipa_iam_merge(base, extra, union_fields=["group", "user"])
    by = {g["name"]: g for g in out}
    assert by["ug-x"]["group"] == ["existing-nested", "role-new"]
    assert by["ug-x"]["description"] == "native"            # baseline wins
    assert by["role-new"]["user"] == ["alice"]


def test_merge_empty_base_is_just_extra(fp):
    extra = [{"name": "role-x"}]
    assert fp.freeipa_iam_merge([], extra) == extra
    assert fp.freeipa_iam_merge(None, extra) == extra


# ── orphan reconcile ─────────────────────────────────────────────────────────
def test_orphans_deletes_only_undeclared_scoped(fp):
    """Only in-scope, undeclared names are orphans: hg-acme-mgt-ztest-old and
    ug-acme-mgt-stale. hg-acme-infra-gitlab-admins is a different env (lacks the
    'acme-mgt' marker) so it is never touched; the declared names survive."""
    found = {
        "hostgroup": ["hg-acme-mgt-gitlab-admins", "hg-acme-mgt-ztest-old",
                      "hg-acme-infra-gitlab-admins"],
        "group": ["role-acme-mgt-gitlab-admins", "ug-acme-mgt-stale"],
    }
    desired = {
        "hostgroup": ["hg-acme-mgt-gitlab-admins"],
        "group": ["role-acme-mgt-gitlab-admins"],
    }
    out = fp.freeipa_iam_orphans(found, desired, "acme-mgt")
    assert out["hostgroup"] == ["hg-acme-mgt-ztest-old"]
    assert out["group"] == ["ug-acme-mgt-stale"]


def test_orphans_respects_protected(fp):
    found = {"group": ["role-acme-mgt-foo-admins", "admins"]}   # 'admins' lacks the marker anyway
    out = fp.freeipa_iam_orphans(found, {"group": []}, "acme-mgt", protected=["role-acme-mgt-foo-admins"])
    assert out["group"] == []        # protected excluded; 'admins' lacks marker → also excluded


def test_orphans_blank_match_deletes_nothing(fp):
    found = {"group": ["anything", "everything"]}
    assert fp.freeipa_iam_orphans(found, {}, "") == {"group": []}   # fail-safe: no match → no deletes


# ── regression: REG-002 (marker containers must never be pruned) ──────────────
# A marker container whose name shares the scope substring (e.g. a "test-" scope vs the
# "test-iam-managed-*" markers) was deleted as an orphan, wiping the role's own
# bookkeeping. The fix passes the markers in `protected`. This pins that contract.
def test_orphans_excludes_marker_containers_when_protected(fp):
    found = {"group": ["test-iam-managed-users", "test-iam-managed-groups"]}
    # Without protection the markers look like orphans (this is the bug being guarded):
    assert fp.freeipa_iam_orphans(found, {"group": []}, "test-")["group"] == [
        "test-iam-managed-users", "test-iam-managed-groups"]
    # With the markers protected (the fix, as reconcile.yml now wires them) → never deleted:
    out = fp.freeipa_iam_orphans(
        found, {"group": []}, "test-",
        protected=["test-iam-managed-users", "test-iam-managed-groups"])
    assert out["group"] == []


# ── freeipa_iam_named: accept bare-string shorthand for name-only lists ───────
def test_named_coerces_bare_strings_to_dicts(fp):
    # e.g. freeipa_iam_hbacsvcs: [cockpit] → [{name: cockpit}], so the downstream
    # map(attribute='name') validation can't crash with "str has no attribute name".
    assert fp.freeipa_iam_named(["cockpit", "docker"]) == [
        {"name": "cockpit"}, {"name": "docker"}]


def test_named_passes_dicts_through_unchanged(fp):
    full = [{"name": "cockpit", "description": "Cockpit"}, {"name": "x", "state": "absent"}]
    assert fp.freeipa_iam_named(full) == full


def test_named_mixed_and_empty(fp):
    assert fp.freeipa_iam_named(["cockpit", {"name": "docker"}]) == [
        {"name": "cockpit"}, {"name": "docker"}]
    assert fp.freeipa_iam_named([]) == []
    assert fp.freeipa_iam_named(None) == []


# ── freeipa_export_scope: carve a snapshot into per-tenant/env slices ──────────
def _sample_export():
    return {
        "meta": {"realm": "IPA.EXAMPLE.COM"},
        "freeipa_server_domain": "ipa.example.com",
        "freeipa_server_forwarders": ["10.0.0.1"],
        "freeipa_iam_usergroups": [
            {"name": "ug-acme-prod-db"}, {"name": "ug-globex-dev-app"},
            {"name": "platform-admin-all"},
        ],
        "freeipa_iam_users": [{"name": "alice.fox"}],
        "freeipa_server_dns_records": [
            {"zone_name": "ipa.example.com.", "records": [{"record_name": "a"}]},
        ],
        "freeipa_iam_hostgroups": [],
    }


def _names(result, key):
    return [i.get("name") or i.get("zone_name") for i in result[key]]


def test_export_scope_include_keeps_only_matching(fp):
    out = fp.freeipa_export_scope(_sample_export(), ["acme-prod-"], "include")
    assert _names(out, "freeipa_iam_usergroups") == ["ug-acme-prod-db"]
    assert out["freeipa_iam_users"] == []          # uid has no tenant-env → dropped
    assert out["freeipa_server_dns_records"] == []  # zone_name doesn't match


def test_export_scope_exclude_catches_outliers(fp):
    out = fp.freeipa_export_scope(
        _sample_export(), ["acme-prod-", "globex-dev-"], "exclude")
    assert _names(out, "freeipa_iam_usergroups") == ["platform-admin-all"]
    assert _names(out, "freeipa_iam_users") == ["alice.fox"]
    assert _names(out, "freeipa_server_dns_records") == ["ipa.example.com."]


def test_export_scope_accepts_string_or_list(fp):
    src = _sample_export()
    assert (fp.freeipa_export_scope(src, "acme-prod-", "include")
            == fp.freeipa_export_scope(src, ["acme-prod-"], "include"))


def test_export_scope_passes_scalars_and_str_lists_through(fp):
    out = fp.freeipa_export_scope(_sample_export(), ["acme-prod-"], "include")
    assert out["freeipa_server_domain"] == "ipa.example.com"
    assert out["freeipa_server_forwarders"] == ["10.0.0.1"]  # list[str] untouched
    assert out["meta"] == {"realm": "IPA.EXAMPLE.COM"}
    assert out["freeipa_iam_hostgroups"] == []                  # empty list stays empty


def test_export_scope_empty_returns_unchanged(fp):
    src = _sample_export()
    assert fp.freeipa_export_scope(src, [], "include") == src
    assert fp.freeipa_export_scope(src, "", "include") == src
    assert fp.freeipa_export_scope(src, ["", None], "include") == src


# ── unified per-realm membership model ────────────────────────────────────────
def test_identity_merge_flattens_and_maps_ownership(fp):
    files = [
        {"tenant": "acme",
         "users": [{"name": "acme.dave", "groups": ["acme-viewers", "admins"]}],
         "groups": [{"name": "acme-viewers"}]},
        {"tenant": "global", "shared": True,
         "users": [{"name": "ops.root", "groups": ["admins"]}],
         "groups": [{"name": "admins"}]},
    ]
    out = fp.freeipa_iam_identity_merge(files)
    obj = out["objects"]
    # objects keyed by the role var, passed through UNCHANGED (no _owner stamp)
    assert [u["name"] for u in obj["freeipa_iam_users"]] == ["acme.dave", "ops.root"]
    assert "_owner" not in obj["freeipa_iam_users"][0]
    assert [g["name"] for g in obj["freeipa_iam_usergroups"]] == ["acme-viewers", "admins"]
    assert "_owner" not in obj["freeipa_iam_usergroups"][0]
    # ownership lives in separate maps
    assert out["user_owner"] == {"acme.dave": "acme", "ops.root": "global"}
    assert out["group_owner"] == {"acme-viewers": "acme", "admins": "global"}
    assert out["group_shared"] == {"acme-viewers": False, "admins": True}


def test_identity_merge_carries_every_object_type(fp):
    # short hand-keys AND full snapshot keys both flatten into the right role var
    files = [
        {"tenant": "acme",
         "hbac_rules": [{"name": "acme-ssh"}],
         "sudo_rules": [{"name": "acme-sudo"}],
         "freeipa_iam_hostgroups": [{"name": "acme-hosts"}]},   # full snapshot key passes through
        {"tenant": "global", "shared": True,
         "hbac_rules": [{"name": "allow-all"}],
         "freeipa_server_dns_zones": [{"name": "example.com"}]},
    ]
    obj = fp.freeipa_iam_identity_merge(files)["objects"]
    assert [r["name"] for r in obj["freeipa_iam_hbac_rules"]] == ["acme-ssh", "allow-all"]
    assert [r["name"] for r in obj["freeipa_iam_sudo_rules"]] == ["acme-sudo"]
    assert [h["name"] for h in obj["freeipa_iam_hostgroups"]] == ["acme-hosts"]
    assert [z["name"] for z in obj["freeipa_server_dns_zones"]] == ["example.com"]


def test_identity_merge_concatenates_rbac_overlay_across_tenants(fp):
    # freeipa_server_rbac_roles (WYSIWYG flat LIST) rides the tenant loader like any
    # other freeipa_* list key — each tenant contributes its own overlay slice.
    files = [
        {"tenant": "acme",
         "freeipa_server_rbac_roles": [
             {"name": "role-acme-prod-admin", "policy_groups": ["ug-a"],
              "members": ["alice"]}]},
        {"tenant": "globex",
         "freeipa_server_rbac_roles": [
             {"name": "role-globex-prod-viewer", "policy_groups": ["ug-g"],
              "members": ["bob"]}]},
    ]
    obj = fp.freeipa_iam_identity_merge(files)["objects"]
    assert [r["name"] for r in obj["freeipa_server_rbac_roles"]] == [
        "role-acme-prod-admin", "role-globex-prod-viewer"]


def test_evictions_only_managed_droppers(fp):
    current = ["admin", "ops.root", "acme.dave", "acme.erin", "contractor.bob"]
    managed = ["ops.root", "acme.dave", "acme.erin"]
    desired = ["ops.root"]
    # acme.dave + acme.erin are managed & no longer desired; admin/contractor.bob unmanaged
    assert fp.freeipa_iam_evictions(current, managed, desired) == ["acme.dave", "acme.erin"]


# ── validate: shape + reference problems in one pass ───────────────────────────
def _valid_data(**over):
    data = {
        "users": [{"name": "alice", "givenname": "Alice", "sn": "A", "groups": ["devs"]}],
        "usergroups": [{"name": "devs"}],
        "roles": [],
        "hostgroups": [{"name": "hg-app"}],
        "hbacsvcs": [],
        "hbacsvcgroups": [],
        "hbac_rules": [{"name": "hbac-dev", "usergroup": ["devs"], "hostgroup": ["hg-app"],
                        "service": ["sshd"]}],
        "sudo_commands": [],
        "sudocmdgroups": [],
        "sudo_rules": [],
        "iparoles": [],
        "pwpolicies": [],
        "automember_rules": [],
        "unmodifiable_users": ["admin"],
        "protected_groups": ["admins", "ipausers"],
        "builtin_groups": ["admins", "ipausers"],
        "builtin_hostgroups": ["ipaservers"],
        "stock_hbacsvcs": ["sshd", "sudo"],
        "live": {},
    }
    data.update(over)
    return data


def test_validate_clean_dataset_has_no_problems(fp):
    out = fp.freeipa_iam_validate(_valid_data())
    assert out == {"shape": [], "refs": []}


def test_validate_shape_catches_missing_fields_and_duplicates(fp):
    users = [
        {"givenname": "No", "sn": "Name", "groups": ["devs"]},          # missing name
        {"name": "bob"},                                                # no first/last/groups
        {"name": "carol", "givenname": "C", "sn": "C", "groups": ["devs"]},
        {"name": "carol", "givenname": "C", "sn": "C", "groups": ["devs"]},  # duplicate
        {"name": "admin"},                                              # unmodifiable: exempt f/l
    ]
    out = fp.freeipa_iam_validate(_valid_data(users=users))
    assert "a user entry is missing 'name'" in out["shape"]
    assert "user 'bob' is missing a first name (first/givenname)" in out["shape"]
    assert "user 'bob' is missing a surname (last/sn)" in out["shape"]
    assert "user 'bob' has no groups and no roles (must belong to at least one)" in out["shape"]
    assert "duplicate username 'carol' (2 entries)" in out["shape"]
    # admin is unmodifiable → no first/last complaints, but still needs groups/roles
    assert not any("admin' is missing" in p for p in out["shape"])


def test_validate_shape_refuses_protected_group_absent(fp):
    groups = [{"name": "admins", "state": "absent"}, {"name": "devs"}]
    out = fp.freeipa_iam_validate(_valid_data(usergroups=groups))
    assert any(p.startswith("group 'admins' is a protected FreeIPA built-in") for p in out["shape"])


def test_validate_refs_catch_unknowns_across_object_types(fp):
    out = fp.freeipa_iam_validate(_valid_data(
        users=[{"name": "alice", "givenname": "A", "sn": "A",
                "groups": ["ghost-group"], "roles": ["ghost-role"]}],
        hbac_rules=[{"name": "hbac-x", "usergroup": ["ghost-group"], "hostgroup": ["ghost-hg"],
                     "service": ["ghost-svc"], "servicegroup": ["ghost-svcgrp"]}],
        sudo_rules=[{"name": "sudo-x", "cmd": ["/ghost"], "cmdgroup": ["ghost-cmds"],
                     "usergroup": ["ghost-group"], "hostgroup": ["ghost-hg"]}],
        usergroups=[{"name": "devs", "group": ["ghost-nested"]}],
        hostgroups=[{"name": "hg-app", "hostgroup": ["ghost-hg-nest"]}],
        pwpolicies=[{"name": "ghost-pol"}],
        automember_rules=[{"name": "ghost-am", "automember_type": "group"}],
    ))
    refs = out["refs"]
    assert "user 'alice' references unknown role 'ghost-role'" in refs
    assert "user 'alice' references unknown group 'ghost-group'" in refs
    assert ("HBAC rule 'hbac-x' references HBAC service 'ghost-svc' "
            "that is not stock, declared, or on the realm") in refs
    assert "HBAC rule 'hbac-x' references service group 'ghost-svcgrp' not declared or on the realm" in refs
    assert "sudo rule 'sudo-x' references sudo command '/ghost' not declared or on the realm" in refs
    assert "sudo rule 'sudo-x' references sudo command group 'ghost-cmds' not declared or on the realm" in refs
    assert "HBAC rule 'hbac-x' references unknown user group 'ghost-group'" in refs
    assert "HBAC rule 'hbac-x' references unknown host group 'ghost-hg'" in refs
    assert "sudo rule 'sudo-x' references unknown user group 'ghost-group'" in refs
    assert "sudo rule 'sudo-x' references unknown host group 'ghost-hg'" in refs
    assert "usergroup 'devs' nests unknown group 'ghost-nested'" in refs
    assert "hostgroup 'hg-app' nests unknown hostgroup 'ghost-hg-nest'" in refs
    assert "automember rule 'ghost-am' (group) targets unknown group 'ghost-am'" in refs
    assert "password policy 'ghost-pol' targets unknown group 'ghost-pol'" in refs


def test_validate_live_names_satisfy_references(fp):
    out = fp.freeipa_iam_validate(_valid_data(
        users=[{"name": "alice", "givenname": "A", "sn": "A", "groups": ["realm-only-group"]}],
        live={"groups": ["realm-only-group"]},
    ))
    assert out["refs"] == []


# ── bulk-call payload compilers ────────────────────────────────────────────────
def test_sudorules_payload_compiles_bulk_lists(fp):
    rules = [
        {"name": "sudo-a", "description": "d", "usergroup": ["g1"], "cmdgroup": ["cg"],
         "sudoopt": ["!authenticate"], "cmdcategory": "all"},
        {"name": "sudo-b", "state": "disabled", "user": ["alice"]},
        {"name": "sudo-c", "state": "absent"},
        {"name": "sudo-d", "state": "enabled", "cmd": ["/usr/bin/id"], "deny_cmd": ["/usr/bin/rm"]},
    ]
    got = fp.freeipa_iam_sudorules_payload(rules)
    a = got["present"][0]
    # inventory keys are renamed to the module's option names
    assert a == {"name": "sudo-a", "description": "d", "group": ["g1"],
                 "allow_sudocmdgroup": ["cg"], "sudooption": ["!authenticate"],
                 "cmdcategory": "all"}
    d = got["present"][2]
    assert d["allow_sudocmd"] == ["/usr/bin/id"] and d["deny_sudocmd"] == ["/usr/bin/rm"]
    # state never forwarded into present entries; routed to its own bulk list
    assert all("state" not in e for e in got["present"])
    assert got["absent"] == [{"name": "sudo-c"}]
    assert got["disabled"] == [{"name": "sudo-b"}]
    assert got["enabled"] == [{"name": "sudo-d"}]


def test_sudorules_payload_rejects_unknown_key(fp):
    with pytest.raises(Exception, match="sudo rule 'sudo-x': unknown key"):
        fp.freeipa_iam_sudorules_payload([{"name": "sudo-x", "usergroups": ["g"]}])


def test_sudorules_payload_rejects_missing_name(fp):
    with pytest.raises(Exception, match="without a usable 'name'"):
        fp.freeipa_iam_sudorules_payload([{"description": "no name"}])


GROUP_FIND_RAW = """  dn: cn=devs,cn=groups,cn=accounts,dc=x
  cn: devs
  member: uid=alice,cn=users,cn=accounts,dc=x
  member: uid=mallory,cn=users,cn=accounts,dc=x
  member: cn=nested,cn=groups,cn=accounts,dc=x

  dn: cn=ops,cn=groups,cn=accounts,dc=x
  cn: ops
  member: uid=bob,cn=users,cn=accounts,dc=x
"""


def test_evict_payload_from_single_group_find(fp):
    candidates = [{"name": "devs", "user": ["alice"]},          # mallory must go
                  {"name": "ops", "user": ["bob"]},             # nothing to evict
                  {"name": "not-created-yet", "user": ["x"]}]   # absent from output -> skipped
    managed = ["alice", "mallory", "bob"]
    got = fp.freeipa_iam_evict_payload(GROUP_FIND_RAW, candidates, managed)
    assert got == [{"name": "devs", "user": ["mallory"]}]


def test_evict_payload_never_touches_unmanaged(fp):
    candidates = [{"name": "devs", "user": []}]
    # mallory & alice current but NOT managed -> untouchable
    got = fp.freeipa_iam_evict_payload(GROUP_FIND_RAW, candidates, ["bob"])
    assert got == []


def test_evict_payload_empty_raw_is_noop(fp):
    # --check mode skips the group-find command, so the filter receives '' via
    # default('') — that must stay a silent no-op, never trip the canary
    assert fp.freeipa_iam_evict_payload("", [{"name": "devs", "user": []}], ["bob"]) == []


def test_evict_payload_canary_on_unparseable_output(fp):
    # NON-empty output in which no `cn:` block parses = the --raw format drifted
    # (FreeIPA upgrade). Eviction would silently stop enforcing — must fail LOUDLY.
    drifted = "Group name: devs\nMember users: alice, mallory\n"
    with pytest.raises(Exception, match="no group entries could be parsed"):
        fp.freeipa_iam_evict_payload(drifted, [{"name": "devs", "user": []}], ["bob"])


# ── precheck: changed-subset gating (invocation saver) ─────────────────────────
HG_RAW = """\
  dn: cn=hg-all,cn=hostgroups,cn=accounts,dc=x
  cn: hg-all
  description: all hosts
  member: cn=hg-base,cn=hostgroups,cn=accounts,dc=x

  dn: cn=hg-base,cn=hostgroups,cn=accounts,dc=x
  cn: hg-base
  description: base hosts
"""

HBAC_RAW = """\
  dn: ipaUniqueID=1,cn=hbac,dc=x
  cn: hbac-admin
  description: admin access
  ipaenabledflag: TRUE
  memberhost: cn=hg-all,cn=hostgroups,cn=accounts,dc=x
  memberuser: cn=admins-g,cn=groups,cn=accounts,dc=x
  memberservice: cn=sshd,cn=hbacservices,cn=hbac,dc=x
  memberservice: cn=sudo,cn=hbacservices,cn=hbac,dc=x

  dn: ipaUniqueID=2,cn=hbac,dc=x
  cn: allow_all
  usercategory: all
  hostcategory: all
  servicecategory: all
  ipaenabledflag: FALSE
"""

AM_RAW = """\
  dn: cn=viewers,cn=group,cn=automember,cn=etc,dc=x
  cn: viewers
  description: viewer auto-enrolment
  automemberinclusiveregex: uid=^beta-
"""


class TestChangedSubset:
    def test_in_sync_entries_are_skipped(self, fp):
        declared = [{"name": "hg-all", "description": "all hosts", "hostgroup": ["hg-base"]},
                    {"name": "hg-base", "description": "base hosts"}]
        assert fp.freeipa_iam_changed_subset(declared, HG_RAW, "hostgroup") == []

    def test_description_and_nesting_drift_included(self, fp):
        declared = [{"name": "hg-all", "description": "CHANGED", "hostgroup": ["hg-base"]},
                    {"name": "hg-base", "description": "base hosts", "hostgroup": ["hg-new"]}]
        got = fp.freeipa_iam_changed_subset(declared, HG_RAW, "hostgroup")
        assert [e["name"] for e in got] == ["hg-all", "hg-base"]

    def test_missing_entry_and_empty_raw_fail_open(self, fp):
        declared = [{"name": "hg-new", "description": "brand new"}]
        assert fp.freeipa_iam_changed_subset(declared, HG_RAW, "hostgroup") == declared
        # failed find -> raw "" -> everything runs (old behaviour)
        all_declared = [{"name": "hg-all", "description": "all hosts"}]
        assert fp.freeipa_iam_changed_subset(all_declared, "", "hostgroup") == all_declared

    def test_absent_entry_costs_nothing_once_gone(self, fp):
        declared = [{"name": "hg-gone", "state": "absent"},
                    {"name": "hg-base", "state": "absent"}]
        got = fp.freeipa_iam_changed_subset(declared, HG_RAW, "hostgroup")
        assert [e["name"] for e in got] == ["hg-base"]     # still exists -> delete runs

    def test_unmodeled_key_is_conservative(self, fp):
        declared = [{"name": "hg-all", "description": "all hosts", "mystery": 1}]
        got = fp.freeipa_iam_changed_subset(declared, HG_RAW, "hostgroup")
        assert got == declared                             # can't verify -> include

    def test_hbac_members_and_categories(self, fp):
        in_sync = [{"name": "hbac-admin", "description": "admin access",
                    "usergroup": ["admins-g"], "hostgroup": ["hg-all"],
                    "service": ["sshd", "sudo"]},
                   {"name": "allow_all", "usercategory": "all", "hostcategory": "all",
                    "servicecategory": "all", "state": "disabled"}]
        assert fp.freeipa_iam_changed_subset(in_sync, HBAC_RAW, "hbacrule") == []
        drift = [{"name": "hbac-admin", "usergroup": ["admins-g", "extra-g"]}]
        assert fp.freeipa_iam_changed_subset(drift, HBAC_RAW, "hbacrule") == drift

    def test_hbac_servicegroup_not_confused_with_service(self, fp):
        # cn=hbacservices is a substring of cn=hbacservicegroups — must not cross-match
        declared = [{"name": "hbac-admin", "servicegroup": ["sshd"]}]
        got = fp.freeipa_iam_changed_subset(declared, HBAC_RAW, "hbacrule")
        assert got == declared     # sshd is a SERVICE member, not a servicegroup member

    def test_automember_regex_comparison(self, fp):
        in_sync = [{"name": "viewers", "automember_type": "group",
                    "description": "viewer auto-enrolment",
                    "inclusive": [{"key": "uid", "expression": "^beta-"}]}]
        assert fp.freeipa_iam_changed_subset(in_sync, AM_RAW, "automember") == []
        drift = [{"name": "viewers", "automember_type": "group",
                  "inclusive": [{"key": "uid", "expression": "^gamma-"}]}]
        assert fp.freeipa_iam_changed_subset(drift, AM_RAW, "automember") == drift

    def test_unknown_kind_fails_fast(self, fp):
        with pytest.raises(Exception, match="unknown kind"):
            fp.freeipa_iam_changed_subset([], "", "sudoers")


# Raw fixtures below mirror live `ipa <type>-find --all --raw` output shapes
# (verified against the idm realm 2026/07/02): sudocmds key on the sudocmd
# attribute and are referenced from groups by ipaUniqueID; pwpolicy stores
# maxlife/minlife in SECONDS; privilege/role links live in memberof DNs.
SUDOCMD_RAW = """\
  dn: ipaUniqueID=aaa-1,cn=sudocmds,cn=sudo,dc=x
  sudocmd: /usr/bin/systemctl
  ipaUniqueID: aaa-1

  dn: ipaUniqueID=bbb-2,cn=sudocmds,cn=sudo,dc=x
  sudocmd: /usr/bin/journalctl
  description: read logs
  ipaUniqueID: bbb-2
"""

SUDOCMDGROUP_RAW = """\
  dn: cn=ops-cmds,cn=sudocmdgroups,cn=sudo,dc=x
  cn: ops-cmds
  description: ops bundle
  member: ipaUniqueID=aaa-1,cn=sudocmds,cn=sudo,dc=x
  member: ipaUniqueID=bbb-2,cn=sudocmds,cn=sudo,dc=x

  dn: cn=ghost-cmds,cn=sudocmdgroups,cn=sudo,dc=x
  cn: ghost-cmds
  member: ipaUniqueID=zzz-9,cn=sudocmds,cn=sudo,dc=x
"""

HBACSVC_RAW = """\
  dn: cn=docker,cn=hbacservices,cn=hbac,dc=x
  cn: docker
  description: Container services
"""

HBACSVCGROUP_RAW = """\
  dn: cn=remote,cn=hbacservicegroups,cn=hbac,dc=x
  cn: remote
  description: Interactive remote login
  member: cn=sshd,cn=hbacservices,cn=hbac,dc=x
  member: cn=xrdp-sesman,cn=hbacservices,cn=hbac,dc=x
"""

PWPOLICY_RAW = """\
  dn: cn=app-users,cn=REALM,cn=kerberos,dc=x
  cn: app-users
  krbmaxpwdlife: 7776000
  krbminpwdlife: 3600
  krbpwdhistorylength: 5
  krbpwdmindiffchars: 3
  krbpwdminlength: 12
  cospriority: 137
  passwordgracelimit: -1
"""

PERMISSION_RAW = """\
  dn: cn=Read App Hostgroups,cn=permissions,cn=pbac,dc=x
  cn: Read App Hostgroups
  ipapermright: read
  ipapermright: search
  ipapermincludedattr: cn
  ipapermincludedattr: member
  ipapermbindruletype: permission
  ipapermlocation: cn=hostgroups,cn=accounts,dc=x
  ipapermtargetfilter: (objectclass=ipahostgroup)
  member: cn=App Ops,cn=privileges,cn=pbac,dc=x

  dn: cn=Read Extended Groups,cn=permissions,cn=pbac,dc=x
  cn: Read Extended Groups
  ipapermright: read
  ipapermlocation: cn=groups,cn=accounts,dc=x
  ipapermtargetfilter: (|(objectClass=egGroup)(cn=pam_*))
  ipapermtargetfilter: (|(objectclass=ipausergroup)(objectclass=posixgroup))
"""

PRIVILEGE_RAW = """\
  dn: cn=App Ops,cn=privileges,cn=pbac,dc=x
  cn: App Ops
  description: Manage app hostgroups
  member: cn=app-reader,cn=roles,cn=accounts,dc=x
  memberof: cn=Read App Hostgroups,cn=permissions,cn=pbac,dc=x
"""

ROLE_RAW = """\
  dn: cn=app-reader,cn=roles,cn=accounts,dc=x
  cn: app-reader
  member: cn=app-admins,cn=groups,cn=accounts,dc=x
  memberof: cn=App Ops,cn=privileges,cn=pbac,dc=x
  memberof: cn=Read App Hostgroups,cn=permissions,cn=pbac,dc=x
"""


class TestChangedSubsetNewKinds:
    def test_sudocmd_keys_on_sudocmd_attr(self, fp):
        in_sync = [{"name": "/usr/bin/systemctl"},
                   {"name": "/usr/bin/journalctl", "description": "read logs"}]
        assert fp.freeipa_iam_changed_subset(in_sync, SUDOCMD_RAW, "sudocmd") == []
        drift = [{"name": "/usr/bin/journalctl", "description": "CHANGED"},
                 {"name": "/usr/bin/new-tool"}]
        got = fp.freeipa_iam_changed_subset(drift, SUDOCMD_RAW, "sudocmd")
        assert [e["name"] for e in got] == ["/usr/bin/journalctl", "/usr/bin/new-tool"]

    def test_sudocmdgroup_members_resolve_through_aux(self, fp):
        in_sync = [{"name": "ops-cmds", "description": "ops bundle",
                    "sudocmd": ["/usr/bin/systemctl", "/usr/bin/journalctl"]}]
        assert fp.freeipa_iam_changed_subset(
            in_sync, SUDOCMDGROUP_RAW, "sudocmdgroup", SUDOCMD_RAW) == []
        drift = [{"name": "ops-cmds", "sudocmd": ["/usr/bin/systemctl"]}]
        assert fp.freeipa_iam_changed_subset(
            drift, SUDOCMDGROUP_RAW, "sudocmdgroup", SUDOCMD_RAW) == drift

    def test_sudocmdgroup_unresolvable_member_is_conservative(self, fp):
        # zzz-9 is not in the sudocmd output -> cannot verify -> include
        declared = [{"name": "ghost-cmds", "sudocmd": ["/usr/bin/mystery"]}]
        assert fp.freeipa_iam_changed_subset(
            declared, SUDOCMDGROUP_RAW, "sudocmdgroup", SUDOCMD_RAW) == declared
        # empty aux (sudocmd find failed) -> members unresolvable -> include
        in_sync = [{"name": "ops-cmds",
                    "sudocmd": ["/usr/bin/systemctl", "/usr/bin/journalctl"]}]
        assert fp.freeipa_iam_changed_subset(
            in_sync, SUDOCMDGROUP_RAW, "sudocmdgroup", "") == in_sync

    def test_hbacsvc_and_group(self, fp):
        in_sync = [{"name": "docker", "description": "Container services"}]
        assert fp.freeipa_iam_changed_subset(in_sync, HBACSVC_RAW, "hbacsvc") == []
        grp_sync = [{"name": "remote", "description": "Interactive remote login",
                     "hbacsvc": ["xrdp-sesman", "sshd"]}]
        assert fp.freeipa_iam_changed_subset(
            grp_sync, HBACSVCGROUP_RAW, "hbacsvcgroup") == []
        grp_drift = [{"name": "remote", "hbacsvc": ["sshd"]}]
        assert fp.freeipa_iam_changed_subset(
            grp_drift, HBACSVCGROUP_RAW, "hbacsvcgroup") == grp_drift

    def test_pwpolicy_unit_conversions(self, fp):
        # maxlife declared in DAYS (x86400), minlife in HOURS (x3600)
        in_sync = [{"name": "app-users", "maxlife": 90, "minlife": 1, "history": 5,
                    "minclasses": 3, "minlength": 12, "priority": 137}]
        assert fp.freeipa_iam_changed_subset(in_sync, PWPOLICY_RAW, "pwpolicy") == []
        drift = [{"name": "app-users", "maxlife": 60}]
        assert fp.freeipa_iam_changed_subset(drift, PWPOLICY_RAW, "pwpolicy") == drift
        junk = [{"name": "app-users", "maxlife": "not-a-number"}]
        assert fp.freeipa_iam_changed_subset(junk, PWPOLICY_RAW, "pwpolicy") == junk

    def test_permission_rights_attrs_and_type(self, fp):
        in_sync = [{"name": "Read App Hostgroups", "right": ["read", "search"],
                    "attrs": ["cn", "member"], "object_type": "hostgroup"}]
        assert fp.freeipa_iam_changed_subset(
            in_sync, PERMISSION_RAW, "permission") == []
        drift = [{"name": "Read App Hostgroups", "right": ["read", "search", "write"]}]
        assert fp.freeipa_iam_changed_subset(
            drift, PERMISSION_RAW, "permission") == drift

    def test_permission_extra_target_filter_joint_with_type(self, fp):
        in_sync = [{"name": "Read Extended Groups", "right": ["read"],
                    "object_type": "group",
                    "extra_target_filter": ["(|(objectClass=egGroup)(cn=pam_*))"]}]
        assert fp.freeipa_iam_changed_subset(
            in_sync, PERMISSION_RAW, "permission") == []
        drift = [{"name": "Read Extended Groups", "object_type": "group",
                  "extra_target_filter": ["(objectClass=other)"]}]
        assert fp.freeipa_iam_changed_subset(
            drift, PERMISSION_RAW, "permission") == drift
        # unmapped object_type -> cannot verify -> include
        unmapped = [{"name": "Read App Hostgroups", "object_type": "sudorule"}]
        assert fp.freeipa_iam_changed_subset(
            unmapped, PERMISSION_RAW, "permission") == unmapped

    def test_privilege_permission_links(self, fp):
        in_sync = [{"name": "App Ops", "description": "Manage app hostgroups",
                    "permission": ["Read App Hostgroups"]}]
        assert fp.freeipa_iam_changed_subset(in_sync, PRIVILEGE_RAW, "privilege") == []
        drift = [{"name": "App Ops", "permission": ["Other Permission"]}]
        assert fp.freeipa_iam_changed_subset(drift, PRIVILEGE_RAW, "privilege") == drift

    def test_role_privilege_and_member_links(self, fp):
        # memberof carries BOTH privilege and (nested) permission DNs — the
        # privilege comparator must only read the cn=privileges container
        in_sync = [{"name": "app-reader", "privilege": ["App Ops"],
                    "usergroup": ["app-admins"]}]
        assert fp.freeipa_iam_changed_subset(in_sync, ROLE_RAW, "role") == []
        drift = [{"name": "app-reader", "privilege": ["App Ops", "More Ops"]}]
        assert fp.freeipa_iam_changed_subset(drift, ROLE_RAW, "role") == drift
        member_drift = [{"name": "app-reader", "usergroup": ["other-group"]}]
        assert fp.freeipa_iam_changed_subset(member_drift, ROLE_RAW, "role") == member_drift


class TestHbacStateMismatch:
    def test_only_mismatched_states_returned(self, fp):
        declared = [
            {"name": "hbac-admin", "state": "enabled"},     # realm TRUE  -> in sync
            {"name": "allow_all", "state": "disabled"},     # realm FALSE -> in sync
            {"name": "hbac-new", "state": "disabled"},      # not in realm -> fresh rules start enabled
        ]
        got = fp.freeipa_iam_hbac_state_mismatch(declared, HBAC_RAW)
        assert [r["name"] for r in got] == ["hbac-new"]

    def test_flip_detected_and_stateless_ignored(self, fp):
        declared = [{"name": "allow_all", "state": "enabled"},
                    {"name": "hbac-admin"}]                 # no state key -> never in this pass
        got = fp.freeipa_iam_hbac_state_mismatch(declared, HBAC_RAW)
        assert [r["name"] for r in got] == ["allow_all"]


# ── password-expiration floor bumps (native bulk payload) ──────────────────────
USER_FIND_RAW = """\
  dn: uid=old.pw,cn=users,cn=accounts,dc=x
  uid: old.pw
  cn: Old Password
  krbPasswordExpiration: 19700101000000Z

  dn: uid=fresh.pw,cn=users,cn=accounts,dc=x
  uid: fresh.pw
  cn: Fresh Password
  krbPasswordExpiration: 20990101000000Z

  dn: uid=no.expiry,cn=users,cn=accounts,dc=x
  uid: no.expiry
  cn: No Expiry
"""


class TestPwexpBumps:
    def test_only_below_floor_users_bumped(self, fp):
        floors = [{"user": "old.pw", "min_days_remaining": 14, "bump_to_days": 90},
                  {"user": "fresh.pw", "min_days_remaining": 14, "bump_to_days": 90}]
        got = fp.freeipa_iam_pwexp_bumps(floors, USER_FIND_RAW)
        assert [b["name"] for b in got] == ["old.pw"]
        # target = now + bump_to_days, GeneralizedTime format
        from datetime import datetime, timezone
        # bare GeneralizedTime WITHOUT trailing Z — ipauser appends it itself
        target = datetime.strptime(got[0]["passwordexpiration"], "%Y%m%d%H%M%S")
        days = (target.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
        assert 88 <= days <= 90

    def test_missing_user_and_missing_expiry_skipped(self, fp):
        floors = [{"user": "no.expiry", "min_days_remaining": 14, "bump_to_days": 90},
                  {"user": "not.a.user", "min_days_remaining": 14, "bump_to_days": 90}]
        assert fp.freeipa_iam_pwexp_bumps(floors, USER_FIND_RAW) == []

    def test_malformed_floor_fails_fast(self, fp):
        with pytest.raises(Exception, match="missing required key.*bump_to_days"):
            fp.freeipa_iam_pwexp_bumps([{"user": "old.pw", "min_days_remaining": 1}], USER_FIND_RAW)

    def test_unparseable_expiry_fails_fast(self, fp):
        raw = "  uid: bad.stamp\n  krbPasswordExpiration: NOT-A-TIME\n"
        with pytest.raises(Exception, match="cannot parse krbPasswordExpiration"):
            fp.freeipa_iam_pwexp_bumps(
                [{"user": "bad.stamp", "min_days_remaining": 1, "bump_to_days": 9}], raw)


# ── validate: type-correct known sets (builtin servicegroups + user refs) ───────
def test_validate_servicegroup_checks_builtin_hbacsvcgroups_not_groups(fp):
    # ftp is a BUILTIN service group (never a user group): must pass via
    # builtin_hbacsvcgroups, and must NOT leak in via builtin_groups.
    rule = [{"name": "hbac-x", "usergroup": ["devs"], "servicegroup": ["ftp"]}]
    ok = fp.freeipa_iam_validate(_valid_data(
        hbac_rules=rule, builtin_hbacsvcgroups=["Sudo", "ftp"]))
    assert ok["refs"] == []
    bad = fp.freeipa_iam_validate(_valid_data(
        hbac_rules=rule, builtin_hbacsvcgroups=[], builtin_groups=["ftp"]))
    assert any("service group 'ftp'" in p for p in bad["refs"])


def test_validate_user_refs_on_hbac_sudo_iparole(fp):
    # declared user + unmodifiable (admin) pass; unknown users are flagged per type
    ok = fp.freeipa_iam_validate(_valid_data(
        hbac_rules=[{"name": "hbac-x", "user": ["alice", "admin"], "service": ["sshd"]}],
        sudo_rules=[{"name": "sudo-x", "user": ["alice"]}],
        iparoles=[{"name": "role-x", "user": ["admin"]}]))
    assert ok["refs"] == []
    bad = fp.freeipa_iam_validate(_valid_data(
        hbac_rules=[{"name": "hbac-x", "user": ["ghost"]}],
        sudo_rules=[{"name": "sudo-x", "user": ["ghost"]}],
        iparoles=[{"name": "role-x", "user": ["ghost"]}]))
    msgs = "\n".join(bad["refs"])
    assert "HBAC rule 'hbac-x' references unknown user 'ghost'" in msgs
    assert "sudo rule 'sudo-x' references unknown user 'ghost'" in msgs
    assert "iparole 'role-x' references unknown user 'ghost'" in msgs


class TestDnsReverseDefaulted:
    RECORDS = [
        {"record_name": "esx01", "a_record": ["10.0.0.31"]},
        {"record_name": "esx02", "a_record": ["10.0.0.32"], "create_reverse": False},
        {"record_name": "v6", "aaaa_record": ["fd00::1"]},
        {"record_name": "alias", "cname_record": ["esx01.internal.example.com."]},
    ]

    def test_flag_off_returns_input_verbatim(self, fp):
        assert fp.freeipa_dns_reverse_defaulted(self.RECORDS, False) is self.RECORDS

    def test_flag_on_defaults_only_address_records_without_explicit_value(self, fp):
        got = fp.freeipa_dns_reverse_defaulted(self.RECORDS, True)
        assert got[0]["create_reverse"] is True          # a_record, no explicit -> defaulted
        assert got[1]["create_reverse"] is False         # explicit per-record value wins
        assert got[2]["create_reverse"] is True          # aaaa_record -> defaulted
        assert "create_reverse" not in got[3]            # cname untouched (A/AAAA-only)

    def test_flag_on_does_not_mutate_input(self, fp):
        fp.freeipa_dns_reverse_defaulted(self.RECORDS, True)
        assert "create_reverse" not in self.RECORDS[0]

    def test_empty_and_none_pass_through(self, fp):
        assert fp.freeipa_dns_reverse_defaulted([], True) == []
        assert fp.freeipa_dns_reverse_defaulted(None, True) is None


class TestIdentityMergeTypeGuard:
    """A KNOWN object key carrying a non-list must fail loud, never drop silently."""

    def test_dict_valued_users_raises(self, fp):
        files = [{"tenant": "acme", "users": {"name": "alice", "first": "A", "last": "B"}}]
        with pytest.raises(fp.AnsibleFilterError, match="'users' must be a LIST"):
            fp.freeipa_iam_identity_merge(files)

    def test_scalar_valued_full_var_raises(self, fp):
        files = [{"tenant": "acme", "freeipa_iam_usergroups": "acme-admins"}]
        with pytest.raises(fp.AnsibleFilterError, match="freeipa_iam_usergroups"):
            fp.freeipa_iam_identity_merge(files)

    def test_helper_scalars_and_empty_keys_still_skip(self, fp):
        files = [{"tenant": "acme", "local_env": "dev", "hg_prefix": "hg-acme",
                  "users": None,
                  "groups": [{"name": "acme-admins"}]}]
        out = fp.freeipa_iam_identity_merge(files)
        assert out["objects"]["freeipa_iam_usergroups"] == [{"name": "acme-admins"}]
        assert "freeipa_iam_users" not in out["objects"]


class TestValidateShapeGuards:
    def test_bare_string_user_is_a_shape_problem_not_a_crash(self, fp):
        out = fp.freeipa_iam_validate({"users": ["alice", {"name": "bob", "first": "B",
                                                           "last": "B", "groups": ["g"]}],
                                       "usergroups": [{"name": "g"}]})
        assert any("not a mapping" in p for p in out["shape"])

    def test_duplicate_non_user_objects_are_shape_problems(self, fp):
        out = fp.freeipa_iam_validate({
            "users": [],
            "usergroups": [{"name": "devs"}, {"name": "devs"}],
            "hostgroups": [{"name": "hg-a"}, {"name": "hg-a"}],
            "hbac_rules": [{"name": "r1"}, {"name": "r1"}],
            "sudo_rules": [{"name": "s1"}, {"name": "s1"}],
        })
        for needle in ("duplicate usergroup 'devs' (2 entries)",
                       "duplicate hostgroup 'hg-a' (2 entries)",
                       "duplicate HBAC rule 'r1' (2 entries)",
                       "duplicate sudo rule 's1' (2 entries)"):
            assert needle in out["shape"]


class TestExportScopeExcludeScrubsMembers:
    SNAP = {
        "meta": {"realm": "EXAMPLE.TEST"},
        "freeipa_iam_usergroups": [
            {"name": "app-harbor-admin", "group": ["pam_marvel_prod_harbor", "ops-core"]},
            {"name": "pam_marvel_prod_harbor", "membermanager_user": ["svc-extapp-toggler-marvel"]},
        ],
        "freeipa_iam_iparoles": [
            {"name": "Fleet Ops", "user": ["svc-extapp", "svc-fleet"], "usergroup": ["ops-core"]},
        ],
    }

    def test_exclude_drops_objects_and_scrubs_member_references(self, fp):
        got = fp.freeipa_export_scope(self.SNAP, ["extapp", "pam_"], mode="exclude")
        assert [g["name"] for g in got["freeipa_iam_usergroups"]] == ["app-harbor-admin"]
        assert got["freeipa_iam_usergroups"][0]["group"] == ["ops-core"]
        assert got["freeipa_iam_iparoles"][0]["user"] == ["svc-fleet"]
        assert got["freeipa_iam_iparoles"][0]["usergroup"] == ["ops-core"]

    def test_include_mode_leaves_member_lists_untouched(self, fp):
        got = fp.freeipa_export_scope(self.SNAP, ["app-"], mode="include")
        assert got["freeipa_iam_usergroups"][0]["group"] == ["pam_marvel_prod_harbor", "ops-core"]
