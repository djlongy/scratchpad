# -*- coding: utf-8 -*-
"""FreeIPA IDAM helper filters (Ansible filter plugins).

Two small, general-purpose filters the role's reconcile uses:

  freeipa_idam_merge(base, extra, key="name", union_fields=None)
      Append `extra` onto `base`, deduped by `key`. Used to layer generated objects
      (e.g. the RBAC overlay from freeipa_rbac.py) onto a hand-written / exported native
      baseline without a separate var: baseline wins on a name collision, listed
      `union_fields` are unioned into the base item.

  freeipa_idam_orphans(found, desired, match, protected=None)
      Compute the orphan object names to delete per type, for the authoritative object
      reconcile: found names that contain the scope marker `match`, are NOT desired, and
      are NOT protected. A blank `match` yields nothing (fail-safe).

(The earlier access-matrix compilers were retired in favour of the thin RBAC overlay in
freeipa_rbac.py — the overlay generates only role groups + nesting + memberships, and every
other object stays native; see roles/freeipa_server/README.md.)
"""
from __future__ import annotations


# ── merge generated objects onto the baseline (native keys) ───────────────────
def _union_into(target, item, fields):
    """Union each of `fields` (list values) from `item` into `target` in place."""
    for field in fields:
        combined = list(target.get(field) or [])
        for value in (item.get(field) or []):
            if value not in combined:
                combined.append(value)
        target[field] = combined


def freeipa_idam_merge(base, extra, key="name", union_fields=None):
    """Append `extra` onto `base`, deduped by `key`.

    Order: every base item first (baseline is the base), then the genuinely-new extra
    items. On a `key` collision the base item is authoritative and the extra is dropped —
    UNLESS union_fields is given, in which case those list fields are unioned into the base
    item (e.g. a generated role group's `user`/`group` lists unioned onto a native group).
    """
    union_fields = union_fields or []
    merged = [dict(x) if isinstance(x, dict) else x for x in (base or [])]
    index = {x.get(key): i for i, x in enumerate(merged) if isinstance(x, dict)}
    for item in (extra or []):
        if not isinstance(item, dict):
            merged.append(item)
            continue
        name = item.get(key)
        if name not in index:
            index[name] = len(merged)
            merged.append(item)
            continue
        _union_into(merged[index[name]], item, union_fields)
    return merged


# ── orphan reconcile — what to DELETE (managed, in scope, no longer declared) ──
def _is_orphan(name, match, want, protected):
    """A name is an orphan iff it is in scope, not desired, and not protected.

    In scope means: the scope marker is a substring of the name, OR match == "*"
    (the all-undeclared mode — every found name is eligible). A blank match is
    handled by the caller (freeipa_idam_orphans) as a hard fail-safe (deletes
    nothing), so it never reaches here as "".
    """
    if not name:
        return False
    in_scope = (match == "*") or (match in name)
    return in_scope and name not in want and name not in protected


def freeipa_idam_orphans(found, desired, match, protected=None):
    """Compute the orphan object names to delete, per object type.

    `found`    : {type: [names currently in the realm]} (from `ipa <type>-find <match>`)
    `desired`  : {type: [names declared this run]}
    `match`    : the scope marker that EVERY managed name contains (e.g. "acme-prod") —
                 a name is only ever eligible for deletion if it CONTAINS this, so other
                 tenants/environments and unrelated objects are never touched.
    `protected`: names that must never be deleted (e.g. freeipa_idam_protected_groups).

    Returns {type: [orphan names]}. An empty/blank `match` yields NOTHING (fail-safe:
    never delete the whole realm because the scope marker was unset).
    """
    if not match:
        return {otype: [] for otype in (found or {})}
    protected = set(protected or [])
    out = {}
    for otype, names in (found or {}).items():
        want = set((desired or {}).get(otype) or [])
        out[otype] = [n for n in (names or []) if _is_orphan(n, match, want, protected)]
    return out


# ── normalize name-only object lists (accept bare-string shorthand) ───────────
def freeipa_idam_named(items):
    """Normalize a name-only object list to dicts: a bare string ``s`` becomes
    ``{'name': s}``; a mapping is passed through unchanged. Lets terse shorthand
    (e.g. ``freeipa_idam_hbacsvcs: [cockpit]``) work alongside the full
    ``[{name: cockpit, description: ...}]`` form, instead of crashing the
    downstream ``map(attribute='name')`` with 'str object has no attribute name'."""
    out = []
    for item in items or []:
        out.append({"name": item} if isinstance(item, str) else item)
    return out


# ── scope a captured snapshot to a tenant/env slice (export) ─────────────────
_SCOPE_ID_FIELDS = ("name", "zone_name")


def _scope_identifier(item):
    """The string a scope substring is matched against for one captured object:
    its ``name``, or ``zone_name`` for a DNS-records group."""
    if isinstance(item, dict):
        for field in _SCOPE_ID_FIELDS:
            value = item.get(field)
            if value:
                return str(value)
    return ""


def freeipa_export_scope(export, scopes, mode="include"):
    """Slice a captured FreeIPA snapshot by object-name substring, so one realm
    can be carved into per-tenant/env inventories.

    ``export`` : the parsed snapshot dict (meta + server_* scalars + object lists).
    ``scopes`` : a substring or list of substrings (e.g. ``acme-prod-``).
    ``mode``   : ``include`` keeps objects whose identifier CONTAINS any scope (the
                 tenant/env slice); ``exclude`` keeps objects whose identifier
                 contains NONE of them (the global 'outliers' — users, DNS,
                 ``platform-*``, built-ins — for the shared/auth inventory).

    Only object lists (lists of dicts) are filtered, matched on each item's
    ``name`` (or ``zone_name`` for DNS records). Scalar keys and non-object lists
    (``meta``, ``realm``, ``domain``, ``forwarders``) pass through unchanged.
    An empty ``scopes`` returns the snapshot untouched (no filtering)."""
    if isinstance(scopes, str):
        scopes = [scopes]
    scopes = [s for s in (scopes or []) if s]
    if not scopes:
        return export
    exclude = (mode == "exclude")

    def keep(item):
        ident = _scope_identifier(item)
        hit = any(s in ident for s in scopes)
        return (not hit) if exclude else hit

    out = {}
    for key, value in (export or {}).items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            out[key] = [item for item in value if keep(item)]
        else:
            out[key] = value
    return out


# ── unified per-realm membership model (declarative, user-centric) ────────────
# Short, hand-friendly object keys a tenant file may use -> the role var they feed.
# (Full freeipa_idam_* / freeipa_server_* keys, e.g. straight from a --tags export
# snapshot, pass through unchanged — so migrating an export is just adding a header.)
_FREEIPA_IDENTITY_ALIASES = {
    "users": "freeipa_idam_users",
    "groups": "freeipa_idam_usergroups",
    "roles": "freeipa_idam_roles",
    "hostgroups": "freeipa_idam_hostgroups",
    "hbacsvcs": "freeipa_idam_hbacsvcs",
    "hbacsvcgroups": "freeipa_idam_hbacsvcgroups",
    "hbac_rules": "freeipa_idam_hbac_rules",
    "sudo_commands": "freeipa_idam_sudo_commands",
    "sudocmdgroups": "freeipa_idam_sudocmdgroups",
    "sudo_rules": "freeipa_idam_sudo_rules",
    "permissions": "freeipa_idam_permissions",
    "privileges": "freeipa_idam_privileges",
    "iparoles": "freeipa_idam_iparoles",
    "pwpolicies": "freeipa_idam_pwpolicies",
    "service_accounts": "freeipa_idam_service_accounts",
    "automember": "freeipa_server_automember_rules",
    "dns_zones": "freeipa_server_dns_zones",
    "dns_records": "freeipa_server_dns_records",
}
_FREEIPA_IDENTITY_META = {"tenant", "shared"}
_FREEIPA_USERS_VAR = "freeipa_idam_users"
_FREEIPA_GROUPS_VAR = "freeipa_idam_usergroups"


def freeipa_idam_identity_merge(files: list[dict]) -> dict:
    """Flatten per-tenant identity files into one realm-wide dataset — EVERY object
    type, not just users/groups.

    Each file is ``{tenant, shared?, <object lists>}`` where an object list is either
    a hand-friendly short key (``users``, ``groups``, ``hbac_rules``, ``sudo_rules``,
    ``roles`` …) or the full role var (``freeipa_idam_*`` / ``freeipa_server_*``, e.g.
    straight from an export snapshot). Returns:

      ``objects``       {role_var: concatenated list across all files} — the lists are
                        passed through UNCHANGED (so they go straight to the ipa modules
                        with no stray keys), keyed by the role var each feeds.
      ``user_owner`` / ``group_owner``  name -> tenant
      ``group_shared``  name -> bool (the group's own ``shared`` if set, else the file's)

    Seeing every tenant in one run is the precondition for a fully declarative reconcile.
    """
    result = {"objects": {}, "user_owner": {}, "group_owner": {}, "group_shared": {}}
    for entry in files or []:
        _merge_identity_entry(entry, result)
    return result


def _merge_identity_entry(entry: dict, result: dict) -> None:
    """Fold one tenant file's object lists into the realm-wide result (in place)."""
    tenant = entry.get("tenant", "")
    file_shared = bool(entry.get("shared", False))
    for key, value in (entry or {}).items():
        if key in _FREEIPA_IDENTITY_META or not isinstance(value, list):
            continue
        target = _FREEIPA_IDENTITY_ALIASES.get(key, key)
        result["objects"].setdefault(target, []).extend(value)
        if target == _FREEIPA_USERS_VAR:
            _stamp_user_owner(value, tenant, result["user_owner"])
        elif target == _FREEIPA_GROUPS_VAR:
            _stamp_group_owner(value, tenant, file_shared, result)


def _stamp_user_owner(users: list, tenant: str, user_owner: dict) -> None:
    """Record name -> owning tenant for each named user in the list."""
    for user in users:
        if isinstance(user, dict) and user.get("name"):
            user_owner[user["name"]] = tenant


def _stamp_group_owner(groups: list, tenant: str, file_shared: bool, result: dict) -> None:
    """Record name -> owning tenant + shared flag for each named group in the list."""
    for group in groups:
        obj = group if isinstance(group, dict) else {"name": group}
        name = obj.get("name")
        if name:
            result["group_owner"][name] = tenant
            result["group_shared"][name] = bool(obj.get("shared", file_shared))


def freeipa_idam_evictions(current: list[str], managed: list[str], desired: list[str]) -> list[str]:
    """Managed members of a group no longer desired -> eviction list.

    ``(current ∩ managed) − desired``. Members not in ``managed`` (the built-in
    ``admin``, service accounts, anything created out-of-band) are NEVER returned,
    so eviction can only remove accounts the role owns.
    """
    managed_set = set(managed or [])
    desired_set = set(desired or [])
    return sorted(m for m in (current or []) if m in managed_set and m not in desired_set)


# ── desired-state validation (shape + references) ─────────────────────────────
# Python port of the (formerly inline-Jinja) validation engine in idam_desired.yml:
# collect EVERY structural + referential problem in one pass so the operator gets a
# complete bullet list, not one assert at a time. Messages are kept identical to the
# original engine; within a check block, problems are grouped per check rather than
# per item (content-equal, order may differ from the old per-item interleave).

def _names(items: list | None) -> list[str]:
    """The `name` of every dict item that has one."""
    return [i["name"] for i in (items or []) if isinstance(i, dict) and i.get("name")]


def _lst(data: dict, key: str) -> list:
    """data[key] as a list (missing/None -> [])."""
    return data.get(key) or []


def _as_set(*sources) -> set:
    """Union of any mix of lists/None into one set."""
    out: set = set()
    for src in sources:
        out.update(src or [])
    return out


def _known_name_sets(data: dict) -> dict[str, set[str]]:
    """Known names per type = declared + built-in allow-lists + live realm names."""
    live = data.get("live") or {}
    return {
        "groups": _as_set(_names(data.get("usergroups")),
                          data.get("builtin_groups"), live.get("groups")),
        "hostgroups": _as_set(_names(data.get("hostgroups")),
                              data.get("builtin_hostgroups"), live.get("hostgroups")),
        "roles": _as_set(_names(data.get("roles")), live.get("roles")),
        "hbacsvcs": _as_set(data.get("stock_hbacsvcs"),
                            _names(data.get("hbacsvcs")), live.get("hbacsvcs")),
        "hbacsvcgroups": _as_set(_names(data.get("hbacsvcgroups")), live.get("hbacsvcgroups")),
        "sudocmds": _as_set(_names(data.get("sudo_commands")), live.get("sudocmds")),
        "sudocmdgroups": _as_set(_names(data.get("sudocmdgroups")), live.get("sudocmdgroups")),
    }


def _user_shape_problems(users: list, unmodifiable: set[str]) -> list[str]:
    """Per-user shape checks: name present, first/last present, has groups or roles."""
    out: list[str] = []
    for user in users or []:
        name = user.get("name")
        if name is None:
            out.append("a user entry is missing 'name'")
            continue
        if name not in unmodifiable and "first" not in user and "givenname" not in user:
            out.append(f"user '{name}' is missing a first name (first/givenname)")
        if name not in unmodifiable and "last" not in user and "sn" not in user:
            out.append(f"user '{name}' is missing a surname (last/sn)")
        if not (user.get("groups") or []) and not (user.get("roles") or []):
            out.append(f"user '{name}' has no groups and no roles (must belong to at least one)")
    return out


def _duplicate_user_problems(users: list) -> list[str]:
    """Duplicate usernames across the whole assembled user set."""
    unames = [u.get("name", "(unnamed)") for u in (users or [])]
    out: list[str] = []
    for name in dict.fromkeys(unames):  # unique, first-occurrence order
        count = unames.count(name)
        if count > 1:
            out.append(f"duplicate username '{name}' ({count} entries)")
    return out


def _protected_group_problems(usergroups: list, protected: set[str]) -> list[str]:
    """A protected built-in group declared state: absent is refused."""
    return [
        f"group '{g.get('name')}' is a protected FreeIPA built-in — "
        "refusing state: absent (would delete core schema)"
        for g in (usergroups or [])
        if g.get("state", "present") == "absent" and g.get("name") in protected
    ]


def _ref_missing(items: list, fields: list[str], known: set[str], tmpl: str) -> list[str]:
    """Problems for refs in any of `fields` of each item that are not in `known`."""
    out: list[str] = []
    for item in items or []:
        for field in fields:
            for ref in item.get(field) or []:
                if ref not in known:
                    out.append(tmpl.format(name=item.get("name"), ref=ref))
    return out


def _one_user_ref_problems(user: dict, rnames: set[str], gnames: set[str]) -> list[str]:
    """Role + group reference problems for a single (named) user."""
    name = user.get("name")
    if name is None:
        return []
    bad_roles = [r for r in user.get("roles") or [] if r not in rnames]
    bad_groups = [g for g in user.get("groups") or [] if g not in gnames]
    return ([f"user '{name}' references unknown role '{r}'" for r in bad_roles]
            + [f"user '{name}' references unknown group '{g}'" for g in bad_groups])


def _user_ref_problems(users: list, rnames: set[str], gnames: set[str]) -> list[str]:
    """Per-user role + group reference checks (unnamed users are skipped)."""
    return [p for user in users or [] for p in _one_user_ref_problems(user, rnames, gnames)]


def _automember_target_problems(rules: list, gnames: set[str], hgnames: set[str]) -> list[str]:
    """An automember rule's NAME must be a known group/hostgroup (it targets itself)."""
    out: list[str] = []
    for rule in rules or []:
        name = rule.get("name")
        atype = rule.get("automember_type", "")
        if atype == "group" and name not in gnames:
            out.append(f"automember rule '{name}' (group) targets unknown group '{name}'")
        if atype == "hostgroup" and name not in hgnames:
            out.append(f"automember rule '{name}' (hostgroup) targets unknown hostgroup '{name}'")
    return out


def _pwpolicy_target_problems(pwpolicies: list, gnames: set[str]) -> list[str]:
    """A password policy's NAME must be a known group (it targets itself)."""
    return [f"password policy '{p.get('name')}' targets unknown group '{p.get('name')}'"
            for p in pwpolicies if p.get("name") not in gnames]


def freeipa_idam_validate(data: dict) -> dict[str, list[str]]:
    """All shape + reference problems for an assembled IDAM desired state.

    `data` carries the assembled object lists plus the allow-lists:
      users, usergroups, roles, hostgroups, hbacsvcs, hbacsvcgroups, hbac_rules,
      sudo_commands, sudocmdgroups, sudo_rules, iparoles, pwpolicies,
      automember_rules, unmodifiable_users, protected_groups, builtin_groups,
      builtin_hostgroups, stock_hbacsvcs, live ({type: [names]} from live mode).

    Returns {"shape": [...], "refs": [...]}: shape problems always hard-fail in the
    role; reference problems obey freeipa_server_idam_reference_validation.
    """
    known = _known_name_sets(data)
    users = _lst(data, "users")
    usergroups = _lst(data, "usergroups")
    hbac_rules = _lst(data, "hbac_rules")
    sudo_rules = _lst(data, "sudo_rules")

    shape = (_user_shape_problems(users, set(_lst(data, "unmodifiable_users")))
             + _duplicate_user_problems(users)
             + _protected_group_problems(usergroups, set(_lst(data, "protected_groups"))))

    refs = (
        _ref_missing(_lst(data, "roles"), ["groups"], known["groups"],
                     "role '{name}' references unknown group '{ref}'")
        + _user_ref_problems(users, known["roles"], known["groups"])
        + _ref_missing(hbac_rules, ["service"], known["hbacsvcs"],
                       "HBAC rule '{name}' references HBAC service '{ref}' "
                       "that is not stock, declared, or on the realm")
        + _ref_missing(hbac_rules, ["servicegroup"], known["hbacsvcgroups"],
                       "HBAC rule '{name}' references service group '{ref}' not declared or on the realm")
        + _ref_missing(sudo_rules, ["cmd", "deny_cmd"], known["sudocmds"],
                       "sudo rule '{name}' references sudo command '{ref}' not declared or on the realm")
        + _ref_missing(sudo_rules, ["cmdgroup", "deny_cmdgroup"], known["sudocmdgroups"],
                       "sudo rule '{name}' references sudo command group '{ref}' "
                       "not declared or on the realm")
        + _ref_missing(hbac_rules, ["usergroup"], known["groups"],
                       "HBAC rule '{name}' references unknown user group '{ref}'")
        + _ref_missing(hbac_rules, ["hostgroup"], known["hostgroups"],
                       "HBAC rule '{name}' references unknown host group '{ref}'")
        + _ref_missing(sudo_rules, ["usergroup"], known["groups"],
                       "sudo rule '{name}' references unknown user group '{ref}'")
        + _ref_missing(sudo_rules, ["hostgroup"], known["hostgroups"],
                       "sudo rule '{name}' references unknown host group '{ref}'")
        + _ref_missing(_lst(data, "iparoles"), ["usergroup"], known["groups"],
                       "iparole '{name}' references unknown user group '{ref}'")
        + _ref_missing(_lst(data, "hbacsvcgroups"), ["hbacsvc"], known["hbacsvcs"],
                       "HBAC service group '{name}' references unknown HBAC service '{ref}'")
        + _ref_missing(_lst(data, "sudocmdgroups"), ["sudocmd"], known["sudocmds"],
                       "sudo command group '{name}' references unknown sudo command '{ref}'")
        + _ref_missing(usergroups, ["group"], known["groups"],
                       "usergroup '{name}' nests unknown group '{ref}'")
        + _ref_missing(_lst(data, "hostgroups"), ["hostgroup"], known["hostgroups"],
                       "hostgroup '{name}' nests unknown hostgroup '{ref}'")
        + _automember_target_problems(_lst(data, "automember_rules"),
                                      known["groups"], known["hostgroups"])
        + _pwpolicy_target_problems(_lst(data, "pwpolicies"), known["groups"])
    )
    return {"shape": shape, "refs": refs}


class FilterModule:
    def filters(self):
        return {
            "freeipa_idam_merge": freeipa_idam_merge,
            "freeipa_idam_orphans": freeipa_idam_orphans,
            "freeipa_idam_named": freeipa_idam_named,
            "freeipa_export_scope": freeipa_export_scope,
            "freeipa_idam_identity_merge": freeipa_idam_identity_merge,
            "freeipa_idam_evictions": freeipa_idam_evictions,
            "freeipa_idam_validate": freeipa_idam_validate,
        }
