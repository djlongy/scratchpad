# -*- coding: utf-8 -*-
"""FreeIPA RBAC overlay compiler (Ansible filter plugins).

A THIN, PURELY OPTIONAL overlay. It lets a human assign users to an abstract ROLE
instead of hand-adding them to many granular target groups. It compiles INTO the
role's native ``freeipa_idam_usergroups`` / ``freeipa_idam_users`` lists and
generates ONLY:

  * the role group itself (a plain usergroup, name declared literally)
  * its NESTING into the EXISTING groups listed in ``member_of`` (each target group
    carries ``group: [<role group>]``; a user in the role group is then an INDIRECT
    member of the target group, so the native HBAC/sudo rules that target it apply
    unchanged — proven on the live realm)
  * user -> role-group membership (from the entry's ``members`` list)
  * OPTIONAL role-scoped HBAC rules (the entry's ``hbac_rules`` list): each rule's
    name is declared EXPLICITLY (WYSIWYG); the compiler injects
    ``usergroup: [<the role group>]`` — binding the rule to the role is the point —
    and everything else (hostgroup/host/user/service/servicegroup) passes through verbatim

WYSIWYG: every name in the input is used VERBATIM. There are no naming templates —
scope (tenant/environment/service) lives in the names you declare, so a
``role-{tenant}-{env}-{name}`` convention is documentation, not code. Policy group
names are pasted straight from the ``--tags export`` snapshot, zero metamorphosis.

It generates NOTHING else: sudo rules/commands, hostgroups, DNS, automember, IPA
permissions/privileges/roles all stay plain native entries. Policy groups are NOT
invented — they must already exist natively (that is where the HBAC/sudo point);
the overlay only adds the role-group nesting onto them.

Input var (role-prefixed per ansible-lint var-naming) — a flat LIST with the same
visual shape as ``freeipa_idam_usergroups``:

  freeipa_server_rbac_roles:
    - name: role-acme-prod-platform-admin      # the role group, exactly as created
      description: "acme/prod platform admins"
      member_of:                           # EXISTING groups, pasted from the export
        - ug-acme-prod-gitlab-admins
        - ug-acme-prod-docker-operators
      members: [alice, bob]                    # users granted the role
      hbac_rules:                              # OPTIONAL role-scoped rules
        - name: hbac-acme-prod-platform-ssh    # EXPLICIT rule name (WYSIWYG)
          hostgroup: [hg-acme-prod]            # usergroup: [<role>] is injected
          service: [sshd]

Filters:
  freeipa_rbac_role_groups(roles)
      -> [ {name: <role>, description?}, {name: <target group>, group: [<role>]}, ... ]
  freeipa_rbac_memberships(roles)
      -> [ {name: <user>, groups: [<role>, ...]}, ... ]
  freeipa_rbac_hbac_rules(roles)
      -> [ {name: <rule>, usergroup: [<role>], hostgroup?, host?, service?, servicegroup?, ...}, ... ]
  freeipa_rbac_validate(roles, native_usergroups, native_users=..., native_hbac_rules=..., ...)
      -> True | raise AnsibleFilterError   (fail fast, before any apply)
"""
from __future__ import annotations

try:                                          # real Ansible at runtime …
    from ansible.errors import AnsibleFilterError
except ImportError:                           # … plain Python under pytest
    class AnsibleFilterError(Exception):
        pass


# FreeIPA built-ins the overlay must never generate, nest into, or collide with.
PROTECTED_GROUPS = frozenset({"admins", "editors", "ipausers", "trust admins"})

# The full public shape of one role entry — anything else is a typo, not an option.
ALLOWED_KEYS = frozenset({"name", "description", "member_of", "members", "hbac_rules"})

# The shape of one role-scoped HBAC rule. usergroup/group are FORBIDDEN — the compiler
# injects usergroup: [<the role group>]; binding the rule to the role is the point.
# `user` IS allowed: extra specific users on the rule beyond the role (edge case).
HBAC_RULE_KEYS = frozenset({"name", "description", "hostgroup", "host", "user",
                            "service", "servicegroup", "state"})


def _iter_roles(roles):
    """Yield ``(name, entry)`` per role in declared order; reject a malformed list,
    a malformed entry, an unknown key (typo trap: ``member`` vs ``members``), or a
    duplicate role name."""
    if roles is None:
        roles = []
    if isinstance(roles, dict):
        raise AnsibleFilterError(
            "freeipa_server_rbac_roles is now a flat LIST (WYSIWYG — one entry per role "
            "group with its literal name, member_of and members, same shape as "
            "freeipa_idam_usergroups). The nested tenant→environment tree was removed; "
            "migrate per the role README.")
    if not isinstance(roles, (list, tuple)):
        raise AnsibleFilterError(
            f"freeipa_server_rbac_roles must be a list of role entries, "
            f"got {type(roles).__name__}")
    seen = set()
    for idx, entry in enumerate(roles):
        name = _entry_name(entry, idx)
        if name in seen:
            raise AnsibleFilterError(
                f"rbac role '{name}' is declared more than once "
                f"(merge its member_of/members into one entry)")
        seen.add(name)
        yield name, entry


def _entry_name(entry, idx):
    if not isinstance(entry, dict):
        raise AnsibleFilterError(
            f"rbac role #{idx + 1} must be a mapping with a 'name', got {entry!r}")
    unknown = set(entry) - ALLOWED_KEYS
    if unknown:
        hint = (" (renamed 2026/07: policy_groups -> member_of)"
                if "policy_groups" in unknown else "")
        raise AnsibleFilterError(
            f"rbac role #{idx + 1} ({entry.get('name', '?')}): unknown key(s) "
            f"{sorted(unknown)}; allowed: {sorted(ALLOWED_KEYS)}{hint}")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AnsibleFilterError(
            f"rbac role #{idx + 1} has no usable 'name' (got {name!r})")
    return name


def _string_list(value, what, required=False):
    """Return ``value`` as a validated list of non-empty strings."""
    if not value:
        if required:
            raise AnsibleFilterError(
                f"{what} declares no member_of groups; a role must grant at least one "
                f"(it would otherwise grant nothing)")
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise AnsibleFilterError(f"{what} must be a LIST of names, got {value!r}")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise AnsibleFilterError(
                f"{what}: each entry must be a non-empty group/user name — paste it "
                f"verbatim from the export (got {item!r})")
    return list(value)


# ── filter 1: generated usergroups (role groups + their nesting) ──────────────
def freeipa_rbac_role_groups(roles):
    """Generated native usergroup dicts, deterministic order, deduped by name: each
    role group plus each member_of target group gaining the role group as a nested
    member (``group: [<role>]``). Two roles nesting into the same target group share one
    entry with both roles in its ``group`` list."""
    out, order = {}, []
    for name, entry in _iter_roles(roles):
        rec = {"name": name}
        if entry.get("description"):
            rec["description"] = entry["description"]
        out[name] = rec
        order.append(name)
    for name, entry in _iter_roles(roles):
        _nest_into_member_of(out, order, name, entry)
    return [out[n] for n in order]


def _nest_into_member_of(out, order, role, entry):
    for ug in _string_list(entry.get("member_of"), f"role '{role}'", required=True):
        rec = out.get(ug)
        if rec is not None and "group" not in rec:
            raise AnsibleFilterError(
                f"role '{role}' nests into '{ug}', which is itself declared as a role "
                f"(a role group can never also be a member_of target)")
        if rec is None:
            rec = {"name": ug, "group": []}
            out[ug] = rec
            order.append(ug)
        if role not in rec["group"]:
            rec["group"].append(role)


# ── filter 2: user -> role-group membership (as native user `groups` additions) ──
def freeipa_rbac_memberships(roles):
    """``[{name: <user>, groups: [<role>, ...]}]`` — the role groups each user joins,
    shaped as additions to the native ``freeipa_idam_users`` entries (merge with
    union_fields=['groups']). Derived from each role's ``members`` list, so granting
    a role is a one-line diff on the role entry — the user's own ``groups:`` list is
    never touched."""
    per_user, order = {}, []
    for name, entry in _iter_roles(roles):
        for user in _string_list(entry.get("members"), f"role '{name}' members"):
            groups = per_user.get(user)
            if groups is None:
                groups = []
                per_user[user] = groups
                order.append(user)
            if name not in groups:
                groups.append(name)
    return [{"name": user, "groups": per_user[user]} for user in order]


# ── filter 3: role-scoped HBAC rules (usergroup: [<role>] injected) ───────────
def _iter_role_hbac_rules(role, entry):
    """Yield validated ``(rule_name, rule_dict)`` for one role's ``hbac_rules``."""
    rules = entry.get("hbac_rules") or []
    if isinstance(rules, (str, dict)) or not isinstance(rules, (list, tuple)):
        raise AnsibleFilterError(
            f"role '{role}' hbac_rules must be a LIST of rule mappings, got {rules!r}")
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise AnsibleFilterError(
                f"role '{role}' hbac_rules #{idx + 1} must be a mapping, got {rule!r}")
        unknown = set(rule) - HBAC_RULE_KEYS
        if unknown:
            hint = (" (the compiler injects usergroup: [<the role group>] itself)"
                    if unknown & {"usergroup", "group"} else "")
            raise AnsibleFilterError(
                f"role '{role}' hbac_rules #{idx + 1} ({rule.get('name', '?')}): unknown "
                f"key(s) {sorted(unknown)}; allowed: {sorted(HBAC_RULE_KEYS)}{hint}")
        name = rule.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AnsibleFilterError(
                f"role '{role}' hbac_rules #{idx + 1} has no usable 'name' — the rule "
                f"name is declared explicitly (WYSIWYG), got {name!r}")
        yield name, rule


def freeipa_rbac_hbac_rules(roles):
    """Generated native HBAC-rule dicts, declared order: each role's ``hbac_rules``
    with ``usergroup: [<the role group>]`` injected and every other declared field
    passed through verbatim. A rule name may appear under only ONE role."""
    out, owner = [], {}
    for role, entry in _iter_roles(roles):
        for name, rule in _iter_role_hbac_rules(role, entry):
            if name in owner:
                raise AnsibleFilterError(
                    f"hbac rule '{name}' is declared under role '{role}' AND role "
                    f"'{owner[name]}' — a rule belongs to exactly one role")
            owner[name] = role
            out.append(dict(rule) | {"usergroup": [role]})
    return out


# ── filter 4: validate (fail fast, before any apply) ──────────────────────────
def _validate_role(name, entry, native_names, known_users, allow):
    """Validate one role entry; return its member_of target-name set."""
    if name in PROTECTED_GROUPS:
        raise AnsibleFilterError(
            f"role group '{name}' collides with a protected FreeIPA built-in")
    if name in native_names:
        raise AnsibleFilterError(
            f"role group '{name}' is also declared in freeipa_idam_usergroups — the "
            f"overlay owns the role group; declare it in exactly one place")
    member_of = set()
    for ug in _string_list(entry.get("member_of"), f"role '{name}'", required=True):
        if ug in PROTECTED_GROUPS:
            raise AnsibleFilterError(
                f"role '{name}' nests into protected built-in group '{ug}'")
        if not allow["missing_member_of"] and ug not in native_names:
            raise AnsibleFilterError(
                f"role '{name}' is member_of group '{ug}', which is not declared "
                f"in freeipa_idam_usergroups. Paste/declare it (with its HBAC/sudo) "
                f"natively first, or set allow_missing_member_of.")
        member_of.add(ug)
    for user in _string_list(entry.get("members"), f"role '{name}' members"):
        if not allow["unknown_users"] and known_users and user not in known_users:
            raise AnsibleFilterError(
                f"role '{name}' member '{user}' is not in freeipa_idam_users "
                f"(set allow_unknown_users to permit)")
    return member_of


def freeipa_rbac_validate(roles, native_usergroups=None, native_users=None,
                          native_hbac_rules=None, allow_unknown_users=False,
                          allow_missing_member_of=False):
    """Raise AnsibleFilterError on any rule break; return True when the overlay is
    sound. Checks list shape, duplicate/unknown keys, that every member_of target
    group exists natively (typo trap for pasted names), that no role group name is
    also a member_of target (would cycle) or a native/protected group, that every
    member is a declared user, and that a role-scoped HBAC rule name is not also
    declared natively (the overlay owns its rules; declare in exactly one place)."""
    native_names = {g.get("name") for g in (native_usergroups or []) if isinstance(g, dict)}
    known_users = {u.get("name") for u in (native_users or []) if isinstance(u, dict)}
    native_rules = {r.get("name") for r in (native_hbac_rules or []) if isinstance(r, dict)}
    allow = {"unknown_users": allow_unknown_users,
             "missing_member_of": allow_missing_member_of}
    role_names, target_names = set(), set()
    for name, entry in _iter_roles(roles):
        role_names.add(name)
        target_names |= _validate_role(name, entry, native_names, known_users, allow)
        for rule_name, _rule in _iter_role_hbac_rules(name, entry):
            if rule_name in native_rules:
                raise AnsibleFilterError(
                    f"role '{name}' hbac rule '{rule_name}' is also declared in "
                    f"freeipa_idam_hbac_rules — the overlay owns its role-scoped "
                    f"rules; declare it in exactly one place")
    clash = role_names & target_names
    if clash:
        raise AnsibleFilterError(
            f"role group name(s) collide with member_of target(s): {sorted(clash)} "
            f"(a role group can never also be a member_of target)")
    return True


class FilterModule:
    def filters(self):
        return {
            "freeipa_rbac_role_groups": freeipa_rbac_role_groups,
            "freeipa_rbac_memberships": freeipa_rbac_memberships,
            "freeipa_rbac_hbac_rules": freeipa_rbac_hbac_rules,
            "freeipa_rbac_validate": freeipa_rbac_validate,
        }
