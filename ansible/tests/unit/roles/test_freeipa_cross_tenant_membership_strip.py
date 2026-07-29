"""Cross-tenant membership strip — reproduction for the 2026/07/29 bug report.

Report: "user `long` (tenant nwn) dropped tenant acme's groups from his
`groups:` list, but the converge does not remove him from those groups —
removal only works when the group is in the SAME tenant file. Is that by
design?"

Verdict, pinned by these tests: the TENANT-FILE BOUNDARY IS NOT THE AXIS. The
merge (freeipa_iam_identity_merge) flattens every tenant file before any
membership logic runs, so the strip cannot see file boundaries at all. The
axis is whether the group RETAINS AT LEAST ONE DECLARED MEMBER after the edit:

  * group still has a declared member (either channel: its own `user:` or any
    user's `groups:`) -> the declarative payload carries its `user` list ->
    gen_add_del_lists removes the dropper. Cross-tenant works.
  * group left with ZERO declared members -> the `user` key is OMITTED (by
    design: absent != empty; emitting `user: []` for every memberless group is
    the exact 3d2bceb1 outage — 82 live groups including `admins` emptied) ->
    NO strip, dropper keeps the membership.
  * the managed-subset EVICTION pass — whose whole mandate is "remove MANAGED
    users who dropped a group" and whose (current ∩ managed) − desired math is
    safe for out-of-band members — used to ALSO skip the memberless group,
    because its candidate list was built from the membership PAYLOAD (groups
    carrying at least one member key). FIXED on the operator's GO (2026/07/29):
    candidates now come from the DECLARED effective set, so managed droppers
    are evicted from memberless declared groups too — the final test pins it.

Replays the role's real machinery: the two-channel enrichment from
tasks/iam.yml (`Build effective FreeIPA groups with merged memberships`), the
shipped `_mdecl_yaml` strip payload (sync-pinned by
test_freeipa_membership_strip_undeclared), and the `_fm_yaml` membership
payload that feeds the eviction candidate build (iam.yml `Eviction | Build
candidate groups`).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from typing import Any

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:
    from ansible.template import trust_as_template
except ImportError:  # <= 2.18 has no trust model; strings are templated as-is
    def trust_as_template(value: Any) -> Any:
        return value


def render(expr: str, variables: dict) -> Any:
    scope = {k: trust_as_template(v) if isinstance(v, str) else v
             for k, v in variables.items()}
    return Templar(loader=DataLoader(), variables=scope).template(
        trust_as_template(expr))

REPO_ROOT = Path(__file__).resolve().parents[4]
ROLE = REPO_ROOT / "ansible" / "roles" / "freeipa_server"


@pytest.fixture(scope="module")
def fp():
    spec = importlib.util.spec_from_file_location(
        "freeipa_iam_xtenant", ROLE / "filter_plugins" / "freeipa_iam.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ── the role's machinery, replayed ────────────────────────────────────────────

def _pairs(users):
    """iam.yml `Build requested user->group memberships from user entries`."""
    return [{"user": u["name"], "group": g}
            for u in users for g in u.get("groups", [])]


def _effective(groups, users):
    """iam.yml `Build effective FreeIPA groups with merged memberships`:
    merged = group's own `user:` + inverted user-side pairs; the `user` key is
    attached ONLY when the merged list is non-empty."""
    pairs = _pairs(users)
    out = []
    for g in groups:
        merged = list(dict.fromkeys(
            list(g.get("user") or [])
            + [p["user"] for p in pairs if p["group"] == g["name"]]))
        eff = dict(g)
        eff.pop("user", None)
        if merged:
            eff["user"] = merged
        out.append(eff)
    return out


# The shipped strip-payload expression — identical to the block
# test_freeipa_membership_strip_undeclared sync-pins against tasks/iam.yml.
STRIP_PAYLOAD = (
    '{% for g in (freeipa_iam_usergroups_effective | default([], true)) %}\n'
    "{% if (g.state | default('present')) != 'absent' "
    'and (g.name | trim | lower) not in '
    "(_freeipa_iam_strip_exempt_groups | map('trim') | map('lower') | list) %}\n"
    '- {name: "{{ g.name }}"'
    "{% if 'user' in g %}, user: {{ g.user | default([], true) | to_json }}{% endif %}"
    ', group: {{ g.group | default([], true) | to_json }}}\n'
    '{% endif %}\n'
    '{% endfor %}'
)


def _strip_payload(effective, exempt=()):
    import yaml as _yaml
    out = render(STRIP_PAYLOAD, {
        "freeipa_iam_usergroups_effective": effective,
        "_freeipa_iam_strip_exempt_groups": list(exempt),
    })
    return _yaml.safe_load(out) or []


def _members_payload_names(effective):
    """iam.yml `_fm_yaml`: a group enters the MEMBERSHIP payload only when it
    carries user/group/membermanager entries — this list (minus exemptions) is
    what the eviction pass uses as its candidate set."""
    return [g["name"] for g in effective
            if (g.get("user") or g.get("group")
                or g.get("membermanager_user") or g.get("membermanager_group"))]


# ── the two-tenant model of the live report ───────────────────────────────────

def _merged_scenario(fp, long_groups):
    """Tenant nwn declares `long`; tenant acme declares its own users+groups.
    Returns the effective group list after the real tenant merge."""
    merged = fp.freeipa_iam_identity_merge([
        {"tenant": "acme",
         "users": [{"name": "acme-admin-1", "groups": ["acme-admins"]}],
         "groups": [{"name": "acme-admins"}, {"name": "acme-empty"}]},
        {"tenant": "nwn",
         "users": [{"name": "long", "groups": long_groups}],
         "groups": [{"name": "nwn-admins"}]},
    ])["objects"]
    return _effective(merged["freeipa_iam_usergroups"],
                      merged["freeipa_iam_users"])


def test_cross_tenant_drop_IS_stripped_when_the_group_keeps_a_member(fp):
    """long drops acme-admins (declared in the OTHER tenant file). acme-admins
    still has acme-admin-1, so its `user` list rides the declarative payload
    WITHOUT long — gen_add_del_lists removes him. Cross-tenant removal works;
    the tenant boundary is not the axis."""
    effective = _merged_scenario(fp, long_groups=["nwn-admins"])
    payload = {g["name"]: g for g in _strip_payload(effective)}
    assert payload["acme-admins"]["user"] == ["acme-admin-1"], (
        "acme-admins must carry its remaining member and NOT long — the module "
        "then strips long as an undeclared current member")
    assert payload["nwn-admins"]["user"] == ["long"]


def test_drop_is_NOT_stripped_when_the_group_is_left_memberless(fp):
    """long was acme-empty's ONLY member (via his own groups: list). Dropping
    it leaves acme-empty with zero declared members -> `user` key omitted ->
    no strip. BY DESIGN: absent != empty (emitting `user: []` for memberless
    groups is the 3d2bceb1 outage). The same-file/cross-file perception in the
    bug report reduces to this: it is about remaining members, not files."""
    before = _merged_scenario(fp, long_groups=["nwn-admins", "acme-empty"])
    payload_before = {g["name"]: g for g in _strip_payload(before)}
    assert payload_before["acme-empty"]["user"] == ["long"]

    after = _merged_scenario(fp, long_groups=["nwn-admins"])
    payload_after = {g["name"]: g for g in _strip_payload(after)}
    assert "user" not in payload_after["acme-empty"], (
        "memberless group must OMIT the user key (absent != empty) — so the "
        "stale membership survives the declarative strip")


def test_FIXED_eviction_covers_the_memberless_group_and_spares_outofband(fp):
    """The gap closed on the operator's GO (2026/07/29): eviction candidates
    now come from the DECLARED effective set, so a declared group whose last
    declared member was dropped still gets its managed droppers evicted —
    while out-of-band members survive, because the eviction math is
    (current ∩ managed) − desired and only role-managed accounts are in
    `managed`. This is the merge-on-delete half of the two-channel model: the
    union of both channels is the truth, and eviction now enforces it even
    when the union is empty.

    The membership PAYLOAD still excludes the memberless group (that half is
    by design — the declarative strip must not see `user: []` it was never
    given), which is exactly why eviction had to stop keying off it."""
    after = _merged_scenario(fp, long_groups=["nwn-admins"])
    assert "acme-empty" not in _members_payload_names(after), (
        "membership payload behaviour changed — the by-design half moved")

    # End-to-end through the role's own filter: long (managed) is a current
    # member of the now-memberless acme-empty; svc-backup (out-of-band) too.
    group_find_raw = (
        "  cn: acme-empty\n"
        "  member: uid=long,cn=users,cn=accounts,dc=example,dc=test\n"
        "  member: uid=svc-backup,cn=users,cn=accounts,dc=example,dc=test\n"
        "\n"
        "  cn: nwn-admins\n"
        "  member: uid=long,cn=users,cn=accounts,dc=example,dc=test\n"
    )
    candidates = [g if g["name"] != "acme-empty" else dict(g)
                  for g in after]                      # acme-empty carries no user key
    payload = fp.freeipa_iam_evict_payload(
        group_find_raw, candidates, managed=["long"])
    assert payload == [{"name": "acme-empty", "user": ["long"]}], (
        "managed dropper must be evicted from the memberless declared group; "
        "nwn-admins keeps long (still desired) and svc-backup (unmanaged) is "
        "never touched")


# ── group-deleter scope: marker-scope default in tenants-dir mode ─────────────

def _scope_re(**vars_in):
    """Replay the REAL `_freeipa_iam_group_scope_re` expression from tasks/iam.yml."""
    import yaml as _yaml
    tasks = _yaml.safe_load((ROLE / "tasks" / "iam.yml").read_text(encoding="utf-8"))
    task = next(t for t in tasks
                if "Build declarative group delete list" in (t.get("name") or ""))
    expr = task["vars"]["_freeipa_iam_group_scope_re"]
    base = {"freeipa_iam_tenants_dir": "", "freeipa_iam_reconcile_scope": "",
            "freeipa_iam_reconcile_all": False, "freeipa_iam_reconcile_all_confirm": False}
    base.update(vars_in)
    return render(expr, base).strip()


def test_group_deleter_scope_quadrant():
    """Tenants-dir mode + blank scope = marker-scope (match-all: the candidate
    set is already container − declared − shields, positive ownership evidence,
    and the unified load sees every tenant). Legacy + blank keeps the
    multi-slice fail-safe (match-nothing). A set scope narrows in both modes;
    reconcile_all still needs its confirm to widen."""
    assert _scope_re(freeipa_iam_tenants_dir="/repo/tenants") == ""          # marker-scope
    assert _scope_re() == "(?!)"                                             # legacy fail-safe
    assert _scope_re(freeipa_iam_tenants_dir="/repo/tenants",
                     freeipa_iam_reconcile_scope="gamma") == "gamma"         # staged vacate
    assert _scope_re(freeipa_iam_reconcile_all=True) == "(?!)"               # no confirm, no widen
    assert _scope_re(freeipa_iam_reconcile_all=True,
                     freeipa_iam_reconcile_all_confirm=True) == ""           # ceremony still works
