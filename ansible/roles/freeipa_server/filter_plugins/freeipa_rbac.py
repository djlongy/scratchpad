# -*- coding: utf-8 -*-
"""FreeIPA RBAC overlay compiler (Ansible filter plugins).

A THIN overlay. It lets a human assign a user to an abstract ROLE, scoped to a single
``(tenant, environment)`` cell, instead of hand-adding them to many granular policy
groups. It compiles INTO the role's native ``freeipa_idam_usergroups`` /
``freeipa_idam_users`` lists and generates ONLY:

  * role groups          ``role-<tenant>-<environment>-<name>``
  * their NESTING into EXISTING ``ug-*`` policy groups (the policy group carries
    ``group: [role-*]``; a user in the role group is then an INDIRECT member of the
    policy group, so the native HBAC/sudo rules that target ``ug-*`` apply unchanged —
    proven on the live realm)
  * user -> role-group membership

TENANCY IS A HARD BOUNDARY. A role is DEFINED inside its ``(tenant, environment)`` cell
and ASSIGNED by naming that exact cell, so one grant can never resolve into a second
tenant or environment. There is no "same name fans across cells" behaviour: breadth is
opt-in only, by listing each cell explicitly. Think of a tenant as its own VPC — we
provision the boundary, the tenant owns what is inside it.

It generates NOTHING else: HBAC rules, sudo rules/commands, hostgroups, DNS, automember,
IPA permissions/privileges/roles all stay plain native entries in the exported
``freeipa_idam_*`` dicts. Policy groups are NOT invented — they must already exist
natively (that is where the HBAC/sudo point); the overlay only adds the role-group
nesting onto them.

Input vars (role-prefixed per ansible-lint var-naming):
  freeipa_server_rbac_naming             role_template + policy_group_template
  freeipa_server_rbac_roles              {tenant: {environment: {role: {description?,
                                          policy_groups: [...]}}}} — each entry is either
                                          a LITERAL existing group name (paste it straight
                                          from the export, zero metamorphosis) or a
                                          {service, privilege} dict fed to the naming
                                          template
  freeipa_server_rbac_user_assignments   {user: {tenant: {environment: [role, ...]}}}

Filters:
  freeipa_rbac_role_groups(roles, naming=None)
      -> [ {name: role-*, description?}, {name: ug-*, group: [role-*]}, ... ]
  freeipa_rbac_memberships(assignments, roles, naming=None)
      -> [ {name: <user>, groups: [role-*, ...]}, ... ]
  freeipa_rbac_validate(roles, assignments, native_usergroups, naming=None, ...)
      -> True | raise AnsibleFilterError   (fail fast, before any apply)
"""
from __future__ import annotations

try:                                          # real Ansible at runtime …
    from ansible.errors import AnsibleFilterError
except ImportError:                           # … plain Python under pytest
    class AnsibleFilterError(Exception):
        pass


# Name templates. role_template builds the grant group from its tree COORDINATES
# (tenant + environment + role name); policy_group_template builds each granular policy
# group the role nests into. Override via freeipa_server_rbac_naming.
DEFAULT_NAMING = {
    "role_prefix": "role",
    "usergroup_prefix": "ug",
    "role_template": "{role_prefix}-{tenant}-{environment}-{name}",
    "policy_group_template": "{usergroup_prefix}-{tenant}-{environment}-{service}-{privilege}",
}

# FreeIPA built-ins the overlay must never generate, nest into, or collide with.
PROTECTED_GROUPS = frozenset({"admins", "editors", "ipausers", "trust admins"})


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


def _role_group_name(tenant, environment, name, naming):
    n = _naming(naming)
    return _fmt(n["role_template"],
                role_prefix=n["role_prefix"],
                tenant=tenant, environment=environment, name=name)


def _policy_group_name(tenant, environment, policy_group, naming):
    # A literal string is used VERBATIM — the paste-from-export form: reference an
    # exported freeipa_idam_usergroups entry by its exact name, no naming template.
    if isinstance(policy_group, str):
        return policy_group
    n = _naming(naming)
    return _fmt(n["policy_group_template"],
                usergroup_prefix=n["usergroup_prefix"],
                tenant=tenant, environment=environment,
                service=policy_group["service"], privilege=policy_group["privilege"])


# ── tree walkers (the scope IS the address; flat fan-out is impossible) ────────
def _require_mapping(value, what):
    if not isinstance(value, dict):
        raise AnsibleFilterError(
            f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _iter_roles(roles):
    """Yield ``(tenant, environment, name, role_def)`` for every cell, in declared
    order. Raises on a structurally malformed tree."""
    tree = _require_mapping(roles or {}, "freeipa_server_rbac_roles")
    for tenant, envs in tree.items():
        envs = _require_mapping(envs, f"rbac_roles['{tenant}']")
        for environment, role_map in envs.items():
            role_map = _require_mapping(
                role_map, f"rbac_roles['{tenant}']['{environment}']")
            for name, role_def in role_map.items():
                yield tenant, environment, name, _require_mapping(
                    role_def, f"role '{tenant}/{environment}/{name}'")


def _iter_assignments(assignments):
    """Yield ``(user, tenant, environment, role_name)`` for every grant, in declared
    order. Raises on a malformed assignment tree."""
    tree = _require_mapping(
        assignments or {}, "freeipa_server_rbac_user_assignments")
    for user, tenants in tree.items():
        tenants = _require_mapping(tenants, f"assignment for user '{user}'")
        for tenant, envs in tenants.items():
            yield from _iter_user_tenant(user, tenant, envs)


def _iter_user_tenant(user, tenant, envs):
    envs = _require_mapping(envs, f"assignment '{user}'/'{tenant}'")
    for environment, role_names in envs.items():
        if isinstance(role_names, str) or not isinstance(role_names, (list, tuple)):
            raise AnsibleFilterError(
                f"assignment '{user}'/'{tenant}'/'{environment}' must be a LIST of role "
                f"names, got {role_names!r}")
        for role_name in role_names:
            yield user, tenant, environment, role_name


# ── filter 1: generated usergroups (role groups + their nesting) ──────────────
def _ensure_role_group(out, order, tenant, environment, name, role_def, naming):
    """Add the cell's role group (once), return its name."""
    role = _role_group_name(tenant, environment, name, naming)
    if role not in out:
        entry = {"name": role}
        if role_def.get("description"):
            entry["description"] = role_def["description"]
        out[role] = entry
        order.append(role)
    return role


def _nest_into_policy_groups(out, order, tenant, environment, role, policy_groups, naming):
    """Nest `role` into each of the cell's (existing) ug-* policy groups."""
    for policy_group in policy_groups or []:
        ug = _policy_group_name(tenant, environment, policy_group, naming)
        entry = out.get(ug)
        if entry is None:
            entry = {"name": ug, "group": []}
            out[ug] = entry
            order.append(ug)
        if role not in entry["group"]:
            entry["group"].append(role)


def freeipa_rbac_role_groups(roles, naming=None):
    """Generated native usergroup dicts, deterministic order, deduped by name: each role
    group (``role-*``) plus each policy group (``ug-*``) gaining the role group as a
    nested member (``group: [role-*]``)."""
    out, order = {}, []
    for tenant, environment, name, role_def in _iter_roles(roles):
        role = _ensure_role_group(out, order, tenant, environment, name, role_def, naming)
        _nest_into_policy_groups(out, order, tenant, environment, role,
                                 role_def.get("policy_groups"), naming)
    return [out[name] for name in order]


# ── filter 2: user -> role-group membership (as native user `groups` additions) ──
def _role_coords(roles, naming):
    """Map ``(tenant, environment, name)`` -> role-group name for every defined cell."""
    return {(tenant, environment, name): _role_group_name(tenant, environment, name, naming)
            for tenant, environment, name, _role_def in _iter_roles(roles)}


def freeipa_rbac_memberships(assignments, roles, naming=None):
    """``[{name: <user>, groups: [role-*, ...]}]`` — the role groups each user joins,
    shaped as additions to the native ``freeipa_idam_users`` entries (merge with
    union_fields=['groups']). Each ``(tenant, environment, role)`` coordinate resolves to
    EXACTLY ONE role group, so a grant can never reach another tenant or environment.
    freeipa_rbac_validate is the gate that rejects an assignment to an undefined cell;
    this projection defensively skips one so a bypassed validate can't crash the run."""
    coords = _role_coords(roles, naming)
    per_user, order = {}, []
    for user, tenant, environment, role_name in _iter_assignments(assignments):
        role = coords.get((tenant, environment, role_name))
        if role is None:
            continue
        groups = per_user.get(user)
        if groups is None:
            groups = []
            per_user[user] = groups
            order.append(user)
        if role not in groups:
            groups.append(role)
    return [{"name": user, "groups": per_user[user]} for user in order if per_user[user]]


# ── filter 3: validate (fail fast, before any apply) ──────────────────────────
def _validate_policy_groups_shape(tenant, environment, name, role_def):
    policy_groups = role_def.get("policy_groups")
    if not policy_groups:
        raise AnsibleFilterError(
            f"role '{tenant}/{environment}/{name}' declares no policy_groups; a role must "
            f"grant at least one (it would otherwise grant nothing)")
    for policy_group in policy_groups:
        if isinstance(policy_group, str) and policy_group.strip():
            continue  # literal existing-group name (paste-from-export form)
        if not (isinstance(policy_group, dict)
                and policy_group.get("service") and policy_group.get("privilege")):
            raise AnsibleFilterError(
                f"role '{tenant}/{environment}/{name}': each policy_group must be either a "
                f"LITERAL existing group name (paste it from the export) or a dict with "
                f"'service' and 'privilege' for the naming template (got {policy_group!r})")


def _check_group_name(name, prefix, kind, enforce_prefix=True):
    """Reject a generated name with the wrong prefix or that collides with a built-in.
    Literal (paste-from-export) policy names skip the prefix check — they are existing
    groups with whatever name the realm uses — but never a protected built-in."""
    if enforce_prefix and not name.startswith(prefix):
        raise AnsibleFilterError(
            f"{kind} group '{name}' must start with the prefix '{prefix}' "
            f"(check the naming templates)")
    if name in PROTECTED_GROUPS:
        raise AnsibleFilterError(f"{kind} group '{name}' collides with a protected built-in")
    return name


def _validate_cell_policy_groups(tenant, environment, name, role_def, naming,
                                 native_names, ug_prefix, allow_missing, policy_group_names):
    for policy_group in role_def["policy_groups"]:
        ug = _check_group_name(
            _policy_group_name(tenant, environment, policy_group, naming), ug_prefix,
            "policy", enforce_prefix=not isinstance(policy_group, str))
        policy_group_names.add(ug)
        if not allow_missing and ug not in native_names:
            raise AnsibleFilterError(
                f"role '{tenant}/{environment}/{name}' nests into policy group '{ug}', which "
                f"is not declared in freeipa_idam_usergroups. Declare it (with its HBAC/sudo) "
                f"natively first, or set allow_missing_policy_groups.")


def _validate_roles(roles, naming, native_names, allow_missing_policy_groups):
    """Validate naming + policy-group existence for every cell; return
    (role_group_names, policy_group_names, coords)."""
    n = _naming(naming)
    role_prefix, ug_prefix = n["role_prefix"] + "-", n["usergroup_prefix"] + "-"
    role_group_names, policy_group_names, coords = set(), set(), set()
    for tenant, environment, name, role_def in _iter_roles(roles):
        coords.add((tenant, environment, name))
        role_group_names.add(
            _check_group_name(_role_group_name(tenant, environment, name, naming),
                              role_prefix, "role"))
        _validate_policy_groups_shape(tenant, environment, name, role_def)
        _validate_cell_policy_groups(tenant, environment, name, role_def, naming,
                                     native_names, ug_prefix, allow_missing_policy_groups,
                                     policy_group_names)
    return role_group_names, policy_group_names, coords


def _validate_assignments(assignments, coords, known_users, allow_unknown_users):
    for user, tenant, environment, role_name in _iter_assignments(assignments):
        if not allow_unknown_users and known_users and user not in known_users:
            raise AnsibleFilterError(
                f"user '{user}' in rbac_user_assignments is not in freeipa_idam_users "
                f"(set allow_unknown_users to permit)")
        if (tenant, environment, role_name) not in coords:
            raise AnsibleFilterError(
                f"user '{user}' is assigned role '{role_name}' in '{tenant}/{environment}', "
                f"which is not a defined role "
                f"(freeipa_server_rbac_roles['{tenant}']['{environment}']['{role_name}'] is "
                f"missing). A grant must name an existing tenant/environment/role cell — "
                f"that is what keeps tenancies isolated.")


def freeipa_rbac_validate(roles, assignments=None, native_usergroups=None,
                          naming=None, native_users=None,
                          allow_unknown_users=False, allow_missing_policy_groups=False):
    """Raise AnsibleFilterError on any rule break; return True when the overlay is sound.
    Checks tree shape + naming, that every referenced policy group exists natively, that no
    role group name equals a policy group name (would cycle), that nothing collides with a
    protected built-in, and that every assignment targets an EXISTING tenant/environment/role
    cell (and a known user) — the path check is what makes cross-tenant grants impossible."""
    native_names = {g.get("name") for g in (native_usergroups or []) if isinstance(g, dict)}
    role_group_names, policy_group_names, coords = _validate_roles(
        roles, naming, native_names, allow_missing_policy_groups)
    clash = role_group_names & policy_group_names
    if clash:
        raise AnsibleFilterError(
            f"role group name(s) collide with policy group name(s): {sorted(clash)} "
            f"(a role group can never also be a policy group)")
    known_users = {u.get("name") for u in (native_users or []) if isinstance(u, dict)}
    _validate_assignments(assignments, coords, known_users, allow_unknown_users)
    return True


class FilterModule:
    def filters(self):
        return {
            "freeipa_rbac_role_groups": freeipa_rbac_role_groups,
            "freeipa_rbac_memberships": freeipa_rbac_memberships,
            "freeipa_rbac_validate": freeipa_rbac_validate,
        }
