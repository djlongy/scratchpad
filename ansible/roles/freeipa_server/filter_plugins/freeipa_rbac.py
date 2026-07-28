# -*- coding: utf-8 -*-
"""FreeIPA RBAC overlay compiler (Ansible filter plugins).

A THIN, PURELY OPTIONAL overlay. It lets a human assign users to an abstract ROLE
instead of hand-adding them to many granular target groups. It compiles INTO the
role's native ``freeipa_iam_usergroups`` / ``freeipa_iam_users`` lists and
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
visual shape as ``freeipa_iam_usergroups``:

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
        - name: hbac-acme-prod-platform-any    # hostcategory/servicecategory: "all"
          hostcategory: all                    #   open that axis (usercategory is
          servicecategory: all                 #   forbidden — the role IS the users)

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


def _fold(name):
    """Fold a group/user name for COMPARISON only — never for output.

    Same reason as _protected() below: FreeIPA cn is caseIgnoreMatch, so `Role-X` and
    `role-x` are ONE realm group. _protected() already folds; nothing else did, which let
    four distinct escalations through — most seriously `member_of: [Role-X]` against a
    role named `role-x`, where the "a role group can never also be a member_of target"
    guard misses and every member of the nesting role inherits the target role's
    HBAC/sudo grants. Declared spellings are still emitted verbatim.
    """
    return name.strip().lower() if isinstance(name, str) else name


def _protected(name):
    """True when `name` resolves to a protected built-in ON THE REALM.

    Compared case-insensitively and whitespace-trimmed because FreeIPA group cn
    uses caseIgnoreMatch: `ipa group-show Admins` returns the real `admins`
    group, and `ipa group-add Admins` fails with 'group with name "admins"
    already exists' (both verified live 2026-07-28). A verbatim `in` check
    therefore let `member_of: [Admins]` past this guard while the generated
    ipagroup call targeted the genuine admins group — silently making every
    member of that role a realm administrator.
    """
    return isinstance(name, str) and name.strip().lower() in PROTECTED_GROUPS

# The full public shape of one role entry — anything else is a typo, not an option.
ALLOWED_KEYS = frozenset({"name", "description", "member_of", "members", "hbac_rules",
                          "sudo_rules"})

# The shape of one role-scoped HBAC rule. usergroup/group are FORBIDDEN — the compiler
# injects usergroup: [<the role group>]; binding the rule to the role is the point.
# `user` IS allowed: extra specific users on the rule beyond the role (edge case).
# hostcategory/servicecategory pass through to ipahbacrule (value "all" opens that
# axis; "" clears it). usercategory is FORBIDDEN too: IPA rejects member users/groups
# alongside usercategory=all, and every role-scoped rule carries the injected role
# usergroup — an all-users rule belongs in baseline freeipa_iam_hbac_rules instead.
HBAC_RULE_KEYS = frozenset({"name", "description", "hostgroup", "host", "user",
                            "service", "servicegroup", "state",
                            "hostcategory", "servicecategory"})

# category key -> the member keys IPA rejects alongside <category>=all.
_HBAC_CATEGORY_CONFLICTS = {
    "hostcategory": ("host", "hostgroup"),
    "servicecategory": ("service", "servicegroup"),
}


# The shape of one role-scoped SUDO rule. Mirrors HBAC_RULE_KEYS for the same reason:
# a sudo rule BINDS to the role group, and the role group does not exist until this
# overlay creates it, so the rule cannot pre-exist natively — the overlay owns it and
# injects the binding. Keys are the INVENTORY names from freeipa_iam's _SUDORULE_KEYMAP
# (the native contract), minus the two that would fight the injection:
#   usergroup — the compiler injects usergroup: [<role>] (maps to ipasudorule `group`)
#   usercategory — IPA rejects member users/groups alongside usercategory=all, and every
#                  role-scoped rule carries the injected role usergroup
SUDO_RULE_KEYS = frozenset({
    "name", "description", "state", "order",
    "hostcategory", "cmdcategory", "runasusercategory", "runasgroupcategory",
    "host", "hostgroup", "hostmask", "user",
    "cmd", "deny_cmd", "cmdgroup", "deny_cmdgroup",
    "sudoopt", "runasuser", "runasgroup", "runasuser_group",
})

# category key -> the member keys IPA rejects alongside <category>=all.
_SUDO_CATEGORY_CONFLICTS = {
    "hostcategory": ("host", "hostgroup", "hostmask"),
    "cmdcategory": ("cmd", "cmdgroup", "deny_cmd", "deny_cmdgroup"),
}


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
            "freeipa_iam_usergroups). The nested tenant→environment tree was removed; "
            "migrate per the role README.")
    if not isinstance(roles, (list, tuple)):
        raise AnsibleFilterError(
            f"freeipa_server_rbac_roles must be a list of role entries, "
            f"got {type(roles).__name__}")
    seen = set()
    for idx, entry in enumerate(roles):
        name = _entry_name(entry, idx)
        if _fold(name) in seen:
            raise AnsibleFilterError(
                f"rbac role '{name}' is declared more than once "
                f"(merge its member_of/members into one entry)")
        seen.add(_fold(name))
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
        out[_fold(name)] = rec
        order.append(_fold(name))
    for name, entry in _iter_roles(roles):
        _nest_into_member_of(out, order, name, entry)
    return [out[n] for n in order]


def _nest_into_member_of(out, order, role, entry):
    # `out` is keyed by FOLDED name (see _fold): `ug-x` and `UG-X` are one realm group, so
    # they must land on one entry rather than emitting two ipagroup records for it.
    for ug in _string_list(entry.get("member_of"), f"role '{role}'", required=True):
        rec = out.get(_fold(ug))
        if rec is not None and "group" not in rec:
            raise AnsibleFilterError(
                f"role '{role}' nests into '{ug}', which is itself declared as a role "
                f"(a role group can never also be a member_of target)")
        if rec is None:
            rec = {"name": ug, "group": []}
            out[_fold(ug)] = rec
            order.append(_fold(ug))
        if role not in rec["group"]:
            rec["group"].append(role)


# ── filter 2: user -> role-group membership (as native user `groups` additions) ──
def freeipa_rbac_memberships(roles):
    """``[{name: <user>, groups: [<role>, ...]}]`` — the role groups each user joins,
    shaped as additions to the native ``freeipa_iam_users`` entries (merge with
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
def _unknown_key_hint(unknown):
    """Targeted guidance for the two known-dangerous hbac rule key mistakes."""
    if unknown & {"usergroup", "group"}:
        return " (the compiler injects usergroup: [<the role group>] itself)"
    if "usercategory" in unknown:
        return (" (usercategory is incompatible with a role-scoped rule: IPA "
                "rejects member users/groups alongside usercategory=all, and "
                "the compiler injects the role usergroup — declare an all-users "
                "rule in baseline freeipa_iam_hbac_rules instead)")
    return ""


def _check_rule_keys(role, idx, rule):
    """Reject a non-mapping rule or any key outside HBAC_RULE_KEYS."""
    if not isinstance(rule, dict):
        raise AnsibleFilterError(
            f"role '{role}' hbac_rules #{idx + 1} must be a mapping, got {rule!r}")
    unknown = set(rule) - HBAC_RULE_KEYS
    if unknown:
        raise AnsibleFilterError(
            f"role '{role}' hbac_rules #{idx + 1} ({rule.get('name', '?')}): unknown "
            f"key(s) {sorted(unknown)}; allowed: {sorted(HBAC_RULE_KEYS)}"
            f"{_unknown_key_hint(unknown)}")


def _check_rule_categories(role, name, rule):
    """Enforce the ipahbacrule category contract: value all|"" and no explicit
    members on an axis opened with =all (IPA rejects that server-side)."""
    for cat, member_keys in _HBAC_CATEGORY_CONFLICTS.items():
        if cat not in rule:
            continue
        if rule[cat] not in ("all", ""):
            raise AnsibleFilterError(
                f"role '{role}' hbac rule '{name}': {cat} must be 'all' (or '' to "
                f"clear it), got {rule[cat]!r} — that is the ipahbacrule contract")
        conflicts = [k for k in member_keys if rule.get(k)]
        if rule[cat] == "all" and conflicts:
            raise AnsibleFilterError(
                f"role '{role}' hbac rule '{name}': {cat}=all cannot be combined "
                f"with {conflicts} — IPA rejects explicit members on an "
                f"all-category axis; drop the member key(s) or the category")


def _iter_role_hbac_rules(role, entry):
    """Yield validated ``(rule_name, rule_dict)`` for one role's ``hbac_rules``."""
    rules = entry.get("hbac_rules") or []
    if isinstance(rules, (str, dict)) or not isinstance(rules, (list, tuple)):
        raise AnsibleFilterError(
            f"role '{role}' hbac_rules must be a LIST of rule mappings, got {rules!r}")
    for idx, rule in enumerate(rules):
        _check_rule_keys(role, idx, rule)
        name = rule.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AnsibleFilterError(
                f"role '{role}' hbac_rules #{idx + 1} has no usable 'name' — the rule "
                f"name is declared explicitly (WYSIWYG), got {name!r}")
        _check_rule_categories(role, name, rule)
        yield name, rule


def freeipa_rbac_hbac_rules(roles):
    """Generated native HBAC-rule dicts, declared order: each role's ``hbac_rules``
    with ``usergroup: [<the role group>]`` injected and every other declared field
    passed through verbatim. A rule name may appear under only ONE role."""
    out, owner = [], {}
    for role, entry in _iter_roles(roles):
        for name, rule in _iter_role_hbac_rules(role, entry):
            if _fold(name) in owner:
                raise AnsibleFilterError(
                    f"hbac rule '{name}' is declared under role '{role}' AND role "
                    f"'{owner[_fold(name)]}' — a rule belongs to exactly one role")
            owner[_fold(name)] = role
            out.append(dict(rule) | {"usergroup": [role]})
    return out


# ── filter 4: role-scoped sudo rules (usergroup: [<role>] injected) ───────────
def _sudo_unknown_key_hint(unknown):
    """Targeted guidance for the key mistakes that fight the injected binding."""
    if unknown & {"usergroup", "group"}:
        return (" (the compiler injects usergroup: [<the role group>] itself; note "
                "`group` is ipasudorule's own name for the same field)")
    if "usercategory" in unknown:
        return (" (usercategory is incompatible with a role-scoped rule: IPA rejects "
                "member users/groups alongside usercategory=all, and the compiler "
                "injects the role usergroup — declare an all-users rule in baseline "
                "freeipa_iam_sudo_rules instead)")
    return ""


def _check_sudo_rule_keys(role, idx, rule):
    """Reject a non-mapping rule or any key outside SUDO_RULE_KEYS."""
    if not isinstance(rule, dict):
        raise AnsibleFilterError(
            f"role '{role}' sudo_rules #{idx + 1} must be a mapping, got {rule!r}")
    unknown = set(rule) - SUDO_RULE_KEYS
    if unknown:
        raise AnsibleFilterError(
            f"role '{role}' sudo_rules #{idx + 1} ({rule.get('name', '?')}): unknown "
            f"key(s) {sorted(unknown)}; allowed: {sorted(SUDO_RULE_KEYS)}"
            f"{_sudo_unknown_key_hint(unknown)}")


def _check_sudo_categories(role, name, rule):
    """Enforce the ipasudorule category contract: value all|"" and no explicit
    members on an axis opened with =all (IPA rejects that server-side)."""
    for cat, member_keys in _SUDO_CATEGORY_CONFLICTS.items():
        if cat not in rule:
            continue
        if rule[cat] not in ("all", ""):
            raise AnsibleFilterError(
                f"role '{role}' sudo rule '{name}': {cat} must be 'all' (or '' to "
                f"clear it), got {rule[cat]!r} — that is the ipasudorule contract")
        conflicts = [k for k in member_keys if rule.get(k)]
        if rule[cat] == "all" and conflicts:
            raise AnsibleFilterError(
                f"role '{role}' sudo rule '{name}': {cat}=all cannot be combined "
                f"with {conflicts} — IPA rejects explicit members on an "
                f"all-category axis; drop the member key(s) or the category")


def _iter_role_sudo_rules(role, entry):
    """Yield validated ``(rule_name, rule_dict)`` for one role's ``sudo_rules``."""
    rules = entry.get("sudo_rules") or []
    if isinstance(rules, (str, dict)) or not isinstance(rules, (list, tuple)):
        raise AnsibleFilterError(
            f"role '{role}' sudo_rules must be a LIST of rule mappings, got {rules!r}")
    for idx, rule in enumerate(rules):
        _check_sudo_rule_keys(role, idx, rule)
        name = rule.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AnsibleFilterError(
                f"role '{role}' sudo_rules #{idx + 1} has no usable 'name' — the rule "
                f"name is declared explicitly (WYSIWYG), got {name!r}")
        _check_sudo_categories(role, name, rule)
        yield name, rule


def freeipa_rbac_sudo_rules(roles):
    """Generated native sudo-rule dicts, declared order: each role's ``sudo_rules``
    with ``usergroup: [<the role group>]`` injected and every other declared field
    passed through verbatim. A rule name may appear under only ONE role."""
    out, owner = [], {}
    for role, entry in _iter_roles(roles):
        for name, rule in _iter_role_sudo_rules(role, entry):
            if _fold(name) in owner:
                raise AnsibleFilterError(
                    f"sudo rule '{name}' is declared under role '{role}' AND role "
                    f"'{owner[_fold(name)]}' — a rule belongs to exactly one role")
            owner[_fold(name)] = role
            out.append(dict(rule) | {"usergroup": [role]})
    return out


# ── filter 5: validate (fail fast, before any apply) ──────────────────────────
def _validate_role(name, entry, native_names, known_users_verbatim, allow):
    """Validate one role entry; return its member_of target-name set."""
    if _protected(name):
        raise AnsibleFilterError(
            f"role group '{name}' collides with a protected FreeIPA built-in "
            f"(FreeIPA group names are case-insensitive)")
    if _fold(name) in native_names:
        raise AnsibleFilterError(
            f"role group '{name}' is also declared in freeipa_iam_usergroups — the "
            f"overlay owns the role group; declare it in exactly one place")
    member_of = set()
    for ug in _string_list(entry.get("member_of"), f"role '{name}'", required=True):
        if _protected(ug):
            raise AnsibleFilterError(
                f"role '{name}' nests into protected built-in group '{ug}' "
                f"(FreeIPA group names are case-insensitive)")
        if not allow["missing_member_of"] and _fold(ug) not in native_names:
            raise AnsibleFilterError(
                f"role '{name}' is member_of group '{ug}', which is not declared "
                f"in freeipa_iam_usergroups. Paste/declare it (with its HBAC/sudo) "
                f"natively first, or set allow_missing_member_of.")
        member_of.add(ug)
    for user in _string_list(entry.get("members"), f"role '{name}' members"):
        # Members are matched EXACTLY, unlike group names above. Folding here
        # made validate accept `Alice` against a declared `alice` — and then
        # `freeipa_rbac_memberships` emitted the verbatim `Alice`, which
        # `freeipa_iam_merge` (a verbatim index) could not match, so it appended
        # a brand-new user with no first/last and the run died much later in
        # shape validation blaming the user rather than the reference. Two
        # halves of the pipeline disagreeing about identity is worse than either
        # rule on its own, and the whole point of this round was that a case
        # difference is an operator error to fix, not something to guess at.
        if allow["unknown_users"] or user in known_users_verbatim:
            continue
        near = sorted(k for k in known_users_verbatim if _fold(k) == _fold(user))
        hint = (f" Declared as {near[0]!r} — names are compared exactly here, "
                f"so use that spelling." if near else
                " (set allow_unknown_users to permit)")
        raise AnsibleFilterError(
            f"role '{name}' member '{user}' is not in freeipa_iam_users.{hint}")
    return member_of


def freeipa_rbac_validate(roles, native_usergroups=None, native_users=None,
                          native_hbac_rules=None, native_sudo_rules=None,
                          allow_unknown_users=False,
                          allow_missing_member_of=False):
    """Raise AnsibleFilterError on any rule break; return True when the overlay is
    sound. Checks list shape, duplicate/unknown keys, that every member_of target
    group exists natively (typo trap for pasted names), that no role group name is
    also a member_of target (would cycle) or a native/protected group, that every
    member is a declared user, and that a role-scoped HBAC rule name is not also
    declared natively (the overlay owns its rules; declare in exactly one place)."""
    # Folded, because FreeIPA resolves these names case-insensitively — a verbatim set
    # lets `Admins`/`UG-Shared`/`Rule-A` walk past every ownership and collision guard.
    native_names = {_fold(g.get("name")) for g in (native_usergroups or []) if isinstance(g, dict)}
    # Users are indexed VERBATIM only. There is deliberately no folded user index:
    # members are compared exactly, so a folded one would only invite a caller to
    # reintroduce the case-insensitive match that _validate_role documents as a bug.
    known_users_verbatim = {u.get("name") for u in (native_users or [])
                            if isinstance(u, dict) and u.get("name")}
    native_rules = {_fold(r.get("name")) for r in (native_hbac_rules or []) if isinstance(r, dict)}
    native_sudo = {_fold(r.get("name")) for r in (native_sudo_rules or []) if isinstance(r, dict)}
    allow = {"unknown_users": allow_unknown_users,
             "missing_member_of": allow_missing_member_of}
    role_names, target_names = set(), set()
    for name, entry in _iter_roles(roles):
        role_names.add(name)
        target_names |= _validate_role(name, entry, native_names,
                                       known_users_verbatim, allow)
        for rule_name, _rule in _iter_role_hbac_rules(name, entry):
            if _fold(rule_name) in native_rules:
                raise AnsibleFilterError(
                    f"role '{name}' hbac rule '{rule_name}' is also declared in "
                    f"freeipa_iam_hbac_rules — the overlay owns its role-scoped "
                    f"rules; declare it in exactly one place")
        for rule_name, _rule in _iter_role_sudo_rules(name, entry):
            if _fold(rule_name) in native_sudo:
                raise AnsibleFilterError(
                    f"role '{name}' sudo rule '{rule_name}' is also declared in "
                    f"freeipa_iam_sudo_rules — the overlay owns its role-scoped "
                    f"rules; declare it in exactly one place")
    clash = {n for n in role_names if _fold(n) in {_fold(t) for t in target_names}}
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
            "freeipa_rbac_sudo_rules": freeipa_rbac_sudo_rules,
            "freeipa_rbac_validate": freeipa_rbac_validate,
        }
