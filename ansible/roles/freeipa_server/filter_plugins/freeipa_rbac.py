# -*- coding: utf-8 -*-
"""FreeIPA RBAC overlay compiler (Ansible filter plugins).

A THIN overlay. It lets a human assign a user to an abstract ROLE instead of
hand-adding them to many granular policy groups. It compiles INTO the role's
native ``freeipa_idam_usergroups`` list and generates ONLY:

  * role groups          ``role-<tenant>-<environment>-<name>``
  * their NESTING into EXISTING ``ug-*`` policy groups (the policy group carries
    ``group: [role-*]``; a user in the role group is then an INDIRECT member of
    the policy group, so the native HBAC/sudo rules that target ``ug-*`` apply
    unchanged — proven on the live realm)
  * user -> role-group membership

It generates NOTHING else: HBAC rules, sudo rules/commands, hostgroups, DNS,
automember, IPA permissions/privileges/roles all stay plain native entries in the
exported ``freeipa_idam_*`` dicts. Policy groups are NOT invented — they must
already exist natively (that is where the HBAC/sudo point); the overlay only adds
the role-group nesting onto them.

Filters:
  freeipa_rbac_role_groups(role_sets, naming=None)
      -> [ {name: role-*, description?}, {name: ug-*, group: [role-*]}, ... ]
  freeipa_rbac_memberships(assignments, role_sets, naming=None)
      -> [ {name: role-*, user: [<users>]}, ... ]   (batched per role group)
  freeipa_rbac_validate(role_sets, assignments, native_usergroups, naming=None, ...)
      -> True | raise AnsibleFilterError   (fail fast, before any apply)

Input vars (role-prefixed per ansible-lint var-naming):
  freeipa_server_rbac_naming            role_template + policy_group_template
  freeipa_server_rbac_role_sets         [{name, tenant, environment, description?,
                                          policy_groups: [{service, privilege}, ...]}]
  freeipa_server_rbac_user_assignments  {user: {roles: [role_name, ...]}}
"""
from __future__ import annotations

try:                                          # real Ansible at runtime …
    from ansible.errors import AnsibleFilterError
except ImportError:                           # … plain Python under pytest
    class AnsibleFilterError(Exception):
        pass


# Name templates. role_template builds the grant group; policy_group_template builds
# each granular policy group the role nests into. Override via freeipa_server_rbac_naming.
DEFAULT_NAMING = {
    "role_prefix": "role",
    "usergroup_prefix": "ug",
    "role_template": "{role_prefix}-{tenant}-{environment}-{name}",
    "policy_group_template": "{usergroup_prefix}-{tenant}-{environment}-{service}-{privilege}",
}

# FreeIPA built-ins the overlay must never generate, nest into, or collide with.
PROTECTED_GROUPS = frozenset({"admins", "editors", "ipausers", "trust admins"})

# Required fields on every role_set entry.
ROLE_SET_REQUIRED = ("name", "tenant", "environment")


def _naming(naming):
    return {**DEFAULT_NAMING, **(naming or {})}


def _fmt(template, **tokens):
    try:
        return template.format(**tokens)
    except KeyError as exc:
        raise AnsibleFilterError(
            f"RBAC naming template {template!r} references unknown placeholder {exc}; "
            f"valid placeholders: {sorted(tokens)}") from exc
    except (IndexError, ValueError) as exc:
        raise AnsibleFilterError(
            f"RBAC naming template {template!r} is malformed: {exc}") from exc


def _role_group_name(role_set, naming):
    n = _naming(naming)
    return _fmt(n["role_template"],
                role_prefix=n["role_prefix"],
                tenant=role_set["tenant"],
                environment=role_set["environment"],
                name=role_set["name"])


def _policy_group_name(role_set, policy_group, naming):
    n = _naming(naming)
    return _fmt(n["policy_group_template"],
                usergroup_prefix=n["usergroup_prefix"],
                tenant=role_set["tenant"],
                environment=role_set["environment"],
                service=policy_group["service"],
                privilege=policy_group["privilege"])


# ── filter 1: generated usergroups (role groups + their nesting) ──────────────
def _ensure_role_group(out, order, role_set, naming):
    """Add the role_set's role group (once), return its name."""
    role = _role_group_name(role_set, naming)
    if role not in out:
        entry = {"name": role}
        if role_set.get("description"):
            entry["description"] = role_set["description"]
        out[role] = entry
        order.append(role)
    return role


def _nest_role_into_policy_groups(out, order, role_set, role, naming):
    """Nest `role` into each of the role_set's (existing) ug-* policy groups."""
    for policy_group in role_set.get("policy_groups") or []:
        ug = _policy_group_name(role_set, policy_group, naming)
        entry = out.get(ug)
        if entry is None:
            entry = {"name": ug, "group": []}
            out[ug] = entry
            order.append(ug)
        if role not in entry["group"]:
            entry["group"].append(role)


def freeipa_rbac_role_groups(role_sets, naming=None):
    """Generated native usergroup dicts, deterministic order, deduped by name:
    each role group (``role-*``) plus each policy group (``ug-*``) gaining the role
    group as a nested member (``group: [role-*]``)."""
    out, order = {}, []
    for role_set in role_sets or []:
        role = _ensure_role_group(out, order, role_set, naming)
        _nest_role_into_policy_groups(out, order, role_set, role, naming)
    return [out[name] for name in order]


# ── filter 2: user -> role-group membership (as native user `groups` additions) ──
def _index_role_sets_by_name(role_sets):
    by_name = {}
    for role_set in role_sets or []:
        by_name.setdefault(role_set["name"], []).append(role_set)
    return by_name


def _role_groups_for_user(spec, by_name, naming):
    """The deduped role-group names a user joins, across every role_set of each role name."""
    roles = []
    for role_name in (spec or {}).get("roles") or []:
        for role_set in by_name.get(role_name, []):
            role = _role_group_name(role_set, naming)
            if role not in roles:
                roles.append(role)
    return roles


def freeipa_rbac_memberships(assignments, role_sets, naming=None):
    """``[{name: <user>, groups: [role-*, ...]}]`` — the role groups each user joins,
    shaped as additions to the native ``freeipa_idam_users`` entries (merge with
    union_fields=['groups']). A user's role name resolves to EVERY role_set entry of that
    name, so a role defined across several tenant/envs grants membership in each. Keeping
    membership user-side (vs a separate role-group `user:` list) means overlay users flow
    through the role's existing user validation + membership pipeline unchanged."""
    by_name = _index_role_sets_by_name(role_sets)
    out = []
    for user, spec in (assignments or {}).items():
        roles = _role_groups_for_user(spec, by_name, naming)
        if roles:
            out.append({"name": user, "groups": roles})
    return out


# ── filter 3: validate (fail fast, before any apply) ──────────────────────────
def _validate_role_set_shape(role_set, idx):
    if not isinstance(role_set, dict):
        raise AnsibleFilterError(
            f"role_set #{idx} must be a mapping, got {type(role_set).__name__}")
    for field in ROLE_SET_REQUIRED:
        if not role_set.get(field):
            raise AnsibleFilterError(
                f"role_set #{idx} is missing required field '{field}'")
    for policy_group in role_set.get("policy_groups") or []:
        if not (isinstance(policy_group, dict)
                and policy_group.get("service") and policy_group.get("privilege")):
            raise AnsibleFilterError(
                f"role_set '{role_set.get('name')}': each policy_group needs both "
                f"'service' and 'privilege' (got {policy_group!r})")


def _check_group_name(name, prefix, kind):
    """Reject a generated name with the wrong prefix or that collides with a built-in."""
    if not name.startswith(prefix):
        raise AnsibleFilterError(
            f"{kind} group '{name}' must start with the prefix '{prefix}' "
            f"(check the naming templates)")
    if name in PROTECTED_GROUPS:
        raise AnsibleFilterError(f"{kind} group '{name}' collides with a protected built-in")
    return name


def _validate_policy_groups(role_set, naming, native_names, ug_prefix,
                            allow_missing, policy_group_names):
    for policy_group in role_set.get("policy_groups") or []:
        ug = _check_group_name(
            _policy_group_name(role_set, policy_group, naming), ug_prefix, "policy")
        policy_group_names.add(ug)
        if not allow_missing and ug not in native_names:
            raise AnsibleFilterError(
                f"role_set '{role_set['name']}' nests into policy group '{ug}', which is not "
                f"declared in freeipa_idam_usergroups. Declare it (with its HBAC/sudo) natively "
                f"first, or set allow_missing_policy_groups.")


def _validate_role_sets(role_sets, naming, native_names, allow_missing_policy_groups):
    """Validate naming + policy-group existence; return (role_group_names, policy_group_names)."""
    n = _naming(naming)
    role_prefix, ug_prefix = n["role_prefix"] + "-", n["usergroup_prefix"] + "-"
    role_group_names, policy_group_names = set(), set()
    for idx, role_set in enumerate(role_sets):
        _validate_role_set_shape(role_set, idx)
        role_group_names.add(
            _check_group_name(_role_group_name(role_set, naming), role_prefix, "role"))
        _validate_policy_groups(role_set, naming, native_names, ug_prefix,
                                allow_missing_policy_groups, policy_group_names)
    return role_group_names, policy_group_names


def _validate_assignments(assignments, role_sets, known_users, allow_unknown_users):
    known_roles = {role_set["name"] for role_set in role_sets}
    for user, spec in (assignments or {}).items():
        if not allow_unknown_users and known_users and user not in known_users:
            raise AnsibleFilterError(
                f"user '{user}' in rbac_user_assignments is not in freeipa_idam_users "
                f"(set allow_unknown_users to permit)")
        for role_name in (spec or {}).get("roles") or []:
            if role_name not in known_roles:
                raise AnsibleFilterError(
                    f"user '{user}': role '{role_name}' is not a defined role_set")


def freeipa_rbac_validate(role_sets, assignments=None, native_usergroups=None,
                          naming=None, native_users=None,
                          allow_unknown_users=False, allow_missing_policy_groups=False):
    """Raise AnsibleFilterError on any rule break; return True when the overlay is sound.
    Checks role_set shape + naming, that referenced policy groups exist natively, that no
    role group name equals a policy group name (would cycle), that nothing collides with a
    protected built-in, and that assignments reference real roles (and known users)."""
    role_sets = role_sets or []
    native_names = {g.get("name") for g in (native_usergroups or []) if isinstance(g, dict)}
    role_group_names, policy_group_names = _validate_role_sets(
        role_sets, naming, native_names, allow_missing_policy_groups)
    clash = role_group_names & policy_group_names
    if clash:
        raise AnsibleFilterError(
            f"role group name(s) collide with policy group name(s): {sorted(clash)} "
            f"(a role group can never also be a policy group)")
    known_users = {u.get("name") for u in (native_users or []) if isinstance(u, dict)}
    _validate_assignments(assignments, role_sets, known_users, allow_unknown_users)
    return True


class FilterModule:
    def filters(self):
        return {
            "freeipa_rbac_role_groups": freeipa_rbac_role_groups,
            "freeipa_rbac_memberships": freeipa_rbac_memberships,
            "freeipa_rbac_validate": freeipa_rbac_validate,
        }
