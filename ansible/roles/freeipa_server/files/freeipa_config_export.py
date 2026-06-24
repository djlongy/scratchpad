#!/usr/bin/python3
"""Export live FreeIPA configuration into the freeipa_server role's flat
declarative contract (freeipa_idam_* / freeipa_server_*), emitted as JSON on
stdout.

Runs ON a FreeIPA server (uses the local ipalib + an existing admin Kerberos
ccache — kinit admin before invoking). Read-only: issues only *_find/_show
commands. The companion task (tasks/export_config.yml) renders the JSON into a
YAML snapshot that drops straight into an inventory group_vars to reapply the
captured state idempotently with this role.

Deliberately NOT captured:
  * user passwords / Kerberos keys     — unreadable, and realm-salted
  * POSIX uid/gid numbers              — let IPA assign on a rebuild (avoids
                                         ID-range collisions); membership, not
                                         numbering, is what matters
  * User Private Groups (mepManagedEntry) and `ipausers` — auto-managed
  * hostgroup host rosters             — populated by enrolment + automember
                                         (opt in with --include-host-membership)
  * global_policy pwpolicy             — owned by FreeIPA itself
"""
import argparse
import datetime
import json
import re
import sys

from ipalib import api, errors

# Groups that are auto-managed, role-owned, or FreeIPA built-ins — never emit as
# declarative config. The built-ins (admins/editors/trust admins) ship with every
# realm; capturing them would re-apply membership to core groups. Drop one from this
# set if you deliberately want to manage its membership declaratively.
GROUP_DENYLIST = {"ipausers", "idam-managed-users", "admins", "editors", "trust admins"}
# System users that must never be emitted as declarative config. `admin` lacks the
# inetOrgPerson objectClass, so a re-apply that sets givenName/sn on it fails with
# "attribute givenName not allowed" — it is a built-in account, not managed identity.
USER_DENYLIST = {"admin"}
# pwpolicy owned by FreeIPA itself.
PWPOLICY_DENYLIST = {"global_policy"}
DEFAULT_FALLBACK_GROUP = "ipausers"  # only used if a user has no other group
# Built-in fallback set of stock FreeIPA HBAC services — shipped on every server,
# so they are excluded from the snapshot (only CUSTOM services are captured, then
# seeded on a fresh server before the HBAC rule memberships that reference them).
# Overridable via --stock-hbacsvc (the role exposes freeipa_server_export_hbacsvc_stock)
# so a newer FreeIPA that ships extra stock services can extend it without code edits.
DEFAULT_HBACSVCS = {
    "crond", "ftp", "gdm", "gdm-password", "gssftp", "kdm", "login", "proftpd",
    "pure-ftpd", "sshd", "su", "su-l", "sudo", "sudo-i", "systemd-user", "vsftpd",
}
# Reused output-contract / LDAP-attribute key names — a single source for the
# repeated string literals. ("group"/"hostgroup" are deliberately NOT constants:
# they double as automember type values, which is a different meaning.)
_DESCRIPTION = "description"
_GROUPS, _ROLES = "groups", "roles"
_USERCATEGORY, _HOSTCATEGORY = "usercategory", "hostcategory"


def _one(entry, key):
    """First value of a (possibly multi-valued) LDAP attribute, or None."""
    v = entry.get(key)
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def _many(entry, key):
    """All values of an attribute as a plain list of stringified scalars."""
    v = entry.get(key)
    if v is None:
        return []
    if not isinstance(v, (list, tuple)):
        v = [v]
    return [x if isinstance(x, str) else str(x) for x in v]


def _str(entry, key):
    v = _one(entry, key)
    if v is None:
        return None
    return v if isinstance(v, str) else str(v)


def _int(entry, key):
    v = _one(entry, key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _enabled(entry):
    """ipaenabledflag may come back as a real bool or a 'TRUE'/'FALSE' string."""
    v = _one(entry, "ipaenabledflag")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1")


def _is_upg(entry):
    """User Private Group — auto-created managed entry, one per user."""
    ocs = [str(o).lower() for o in _many(entry, "objectclass")]
    return "mepmanagedentry" in ocs


def _prune(d):
    """Drop keys whose value is None or an empty list/str — keep the YAML lean."""
    return {k: v for k, v in d.items() if v not in (None, [], "")}


def _find(cmd, **kw):
    kw.setdefault("all", True)
    # automember_find does not accept sizelimit; everything else does.
    if not cmd.startswith("automember"):
        kw.setdefault("sizelimit", 0)
    return api.Command[cmd](**kw)["result"]


def export_groups():
    groups, names = [], set()
    for e in _find("group_find"):
        name = _str(e, "cn")
        if not name or name in GROUP_DENYLIST or _is_upg(e):
            continue
        names.add(name)
        groups.append(e)
    out = []
    for e in groups:
        name = _str(e, "cn")
        nested = [g for g in _many(e, "member_group") if g in names]
        out.append(_prune({
            "name": name,
            _DESCRIPTION: _str(e, _DESCRIPTION),
            "group": nested,  # nested member groups only; user membership lives on users
            # Member managers — users/groups allowed to manage THIS group's
            # membership without being admins (e.g. a PAM/JIT toggler service).
            "membermanager_user": _many(e, "membermanager_user"),
            "membermanager_group": [g for g in _many(e, "membermanager_group") if g in names],
        }))
    return out, names


def export_users(group_names, include_sshkeys):
    used_fallback = False
    out = []
    for e in _find("user_find"):
        if str(_one(e, "preserved")).lower() == "true":
            continue  # staged/deleted-preserved entry, not active config
        name = _str(e, "uid")
        if not name or name in USER_DENYLIST:
            continue  # absent uid, or a built-in system account (not declarative)
        groups = [g for g in _many(e, "memberof_group")
                  if g in group_names and g not in GROUP_DENYLIST]
        if not groups:
            groups = [DEFAULT_FALLBACK_GROUP]
            used_fallback = True
        item = {
            "name": name,
            "first": _str(e, "givenname") or name,
            "last": _str(e, "sn") or name,
            "email": _many(e, "mail"),
            _GROUPS: groups,
        }
        if include_sshkeys:
            item["sshpubkey"] = _many(e, "ipasshpubkey")
        out.append(_prune(item))
    return out, used_fallback


def export_hostgroups(include_host_membership):
    out = []
    names = {_str(e, "cn") for e in _find("hostgroup_find")}
    for e in _find("hostgroup_find"):
        name = _str(e, "cn")
        if not name:
            continue
        item = {"name": name, _DESCRIPTION: _str(e, _DESCRIPTION)}
        nested = [g for g in _many(e, "member_hostgroup") if g in names]
        if nested:
            item["hostgroup"] = nested
        if include_host_membership:
            item["host"] = _many(e, "member_host")
        out.append(_prune(item))
    return out


def export_hbac_rules():
    out = []
    for e in _find("hbacrule_find"):
        name = _str(e, "cn")
        if not name:
            continue
        item = {
            "name": name,
            _DESCRIPTION: _str(e, _DESCRIPTION),
            _USERCATEGORY: _str(e, _USERCATEGORY),
            _HOSTCATEGORY: _str(e, _HOSTCATEGORY),
            "servicecategory": _str(e, "servicecategory"),
            "user": _many(e, "memberuser_user"),
            "group": _many(e, "memberuser_group"),
            "host": _many(e, "memberhost_host"),
            "hostgroup": _many(e, "memberhost_hostgroup"),
            "hbacsvc": _many(e, "memberservice_hbacsvc"),
            "hbacsvcgroup": _many(e, "memberservice_hbacsvcgroup"),
        }
        # Capture operational state only when disabled (present-by-default = enabled).
        if not _enabled(e):
            item["state"] = "disabled"
        out.append(_prune(item))
    return out


def export_hbacsvcs(stock):
    """Custom HBAC services only — services named in `stock` already exist on a
    fresh server, so they are skipped. `stock` is caller-supplied (default the
    built-in set) so newer FreeIPA versions can extend it without code changes."""
    out = []
    for e in _find("hbacsvc_find"):
        name = _str(e, "cn")
        if not name or name in stock:
            continue
        out.append(_prune({"name": name, _DESCRIPTION: _str(e, _DESCRIPTION)}))
    return out


def export_sudo_commands():
    out = []
    for e in _find("sudocmd_find"):
        name = _str(e, "sudocmd")
        if not name:
            continue
        out.append(_prune({"name": name, _DESCRIPTION: _str(e, _DESCRIPTION)}))
    return out


def export_sudo_rules():
    out = []
    for e in _find("sudorule_find"):
        name = _str(e, "cn")
        if not name:
            continue
        item = {
            "name": name,
            _DESCRIPTION: _str(e, _DESCRIPTION),
            _USERCATEGORY: _str(e, _USERCATEGORY),
            _HOSTCATEGORY: _str(e, _HOSTCATEGORY),
            "cmdcategory": _str(e, "cmdcategory"),
            "runasusercategory": _str(e, "ipasudorunasusercategory"),
            "runasgroupcategory": _str(e, "ipasudorunasgroupcategory"),
            "user": _many(e, "memberuser_user"),
            "group": _many(e, "memberuser_group"),
            "host": _many(e, "memberhost_host"),
            "hostgroup": _many(e, "memberhost_hostgroup"),
            "allow_sudocmd": _many(e, "memberallowcmd_sudocmd"),
            "deny_sudocmd": _many(e, "memberdenycmd_sudocmd"),
            "allow_sudocmdgroup": _many(e, "memberallowcmd_sudocmdgroup"),
            "deny_sudocmdgroup": _many(e, "memberdenycmd_sudocmdgroup"),
            "sudooption": _many(e, "ipasudoopt"),
            "runasuser": _many(e, "ipasudorunas_user"),
            "runasgroup": _many(e, "ipasudorunasgroup_group"),
            "order": _int(e, "sudoorder"),
        }
        if not _enabled(e):
            item["state"] = "disabled"
        out.append(_prune(item))
    return out


def export_pwpolicies():
    out = []
    for e in _find("pwpolicy_find"):
        name = _str(e, "cn")
        if not name or name in PWPOLICY_DENYLIST:
            continue
        item = {
            "name": name,
            "maxlife": _int(e, "krbmaxpwdlife"),
            "minlife": _int(e, "krbminpwdlife"),
            "history": _int(e, "krbpwdhistorylength"),
            "minclasses": _int(e, "krbpwdmindiffchars"),
            "minlength": _int(e, "krbpwdminlength"),
            "priority": _int(e, "cospriority"),
            "maxfail": _int(e, "krbpwdmaxfailure"),
            "failinterval": _int(e, "krbpwdfailurecountinterval"),
            "lockouttime": _int(e, "krbpwdlockoutduration"),
        }
        grace = _int(e, "passwordgracelimit")
        if grace is not None and grace >= 0:
            item["gracelimit"] = grace
        out.append(_prune(item))
    return out


def _automember_regex(entry, attr):
    """Parse FreeIPA automember '<key>=<expr>' regex strings into the role's
    [{key, expression}] inclusive/exclusive form (skipping malformed entries)."""
    out = []
    for raw in _many(entry, attr):
        key, _, expr = raw.partition("=")
        if expr:
            out.append({"key": key, "expression": expr})
    return out


def export_automember_rules():
    out = []
    for atype in ("group", "hostgroup"):
        for e in _find("automember_find", type=atype):
            name = _str(e, "cn")
            if not name:
                continue
            inclusive = _automember_regex(e, "automemberinclusiveregex")
            exclusive = _automember_regex(e, "automemberexclusiveregex")
            if not inclusive and not exclusive:
                continue  # a target group with no regex is not a useful rule
            out.append(_prune({
                "name": name,
                "automember_type": atype,
                _DESCRIPTION: _str(e, _DESCRIPTION),
                "inclusive": inclusive,
                "exclusive": exclusive,
            }))
    return out


def _mineable(group, pairs):
    return not any(rx.search(group) for _, rx in pairs)


def _cooccurrence_roles(users, pairs, min_groups):
    """Bundle mineable groups held by the same user-set into role-NN, keeping
    only bundles of >= min_groups groups. Returns (roles, user_to_roles)."""
    group_users = {}                      # mineable group -> set(users holding it)
    for u in users:
        for g in u.get(_GROUPS, []):
            if _mineable(g, pairs):
                group_users.setdefault(g, set()).add(u["name"])
    bundles = {}                          # frozenset(users) -> [groups]
    for g, us in group_users.items():
        bundles.setdefault(frozenset(us), []).append(g)
    qualifying = sorted(
        (kv for kv in bundles.items() if len(kv[1]) >= max(1, min_groups)),
        key=lambda kv: sorted(kv[1]),
    )
    pad = max(2, len(str(len(qualifying) or 1)))
    roles, user_to_roles = [], {}
    for i, (uset, groups) in enumerate(qualifying, 1):
        name = f"role-{i:0{pad}d}"
        roles.append({"name": name, _GROUPS: sorted(groups)})
        for un in uset:
            user_to_roles.setdefault(un, []).append(name)
    return roles, user_to_roles


def _native_users(users):
    """Copy each user verbatim with an empty `roles: []` placeholder inserted
    before `groups` — full real membership preserved, no auto-assigned roles."""
    out = []
    for u in users:
        nu = {}
        for key, value in u.items():
            if key == _GROUPS and _ROLES not in nu:
                nu[_ROLES] = []
            nu[key] = value
        nu.setdefault(_ROLES, [])
        out.append(nu)
    return out


def _excluded_by_pattern(users, pairs):
    """Diagnostic: the groups each exclude-pattern held out of mining."""
    excluded = {}
    for g in sorted({g for u in users for g in u.get(_GROUPS, [])}):
        for pat, rx in pairs:
            if rx.search(g):
                excluded.setdefault(pat, []).append(g)
                break
    return excluded


def mine_roles(users, exclude_patterns=None, min_groups=2):
    """Bridge users <-> groups with a roles matrix via co-occurrence bundling.

    Groups held by the EXACT same set of (managed) users always travel together,
    so they collapse into one role and a user references that role instead of a
    long group list. Two refinements keep the matrix honest:

      * Groups whose name matches ANY of `exclude_patterns` (a list of case-
        insensitive regexes, default ['role']) are NOT mined — they belong to a
        layer that already owns them (your role-* groups, or an external PAM/JIT
        that manages its own groups). Folding them into a synthetic
        bundle just makes a "role made of roles", so they stay as direct `groups`
        on the user. Pass [] to mine everything.
      * A bundle becomes a role only when it has >= `min_groups` groups; smaller
        bundles stay as direct groups, so you never get a 1-group "role" that is
        just a renamed group.

    Users are left NATIVE — each keeps its full real `groups` plus an empty
    `roles: []` placeholder. The matrix is a non-applied SUGGESTION only (the user
    opts in by renaming it and wiring users' `roles:` themselves). Returns
    (roles, users, excluded, user_role_map): `excluded` maps each matched pattern
    -> the groups it held out of mining (diagnostic); `user_role_map` maps each
    user -> the roles it WOULD get if the suggestion were adopted (also advisory).
    """
    patterns = ["role"] if exclude_patterns is None else exclude_patterns
    pairs = [(p, re.compile(p, re.IGNORECASE)) for p in patterns if p]
    roles, user_to_roles = _cooccurrence_roles(users, pairs, min_groups)
    user_role_map = {u["name"]: sorted(user_to_roles[u["name"]])
                     for u in users if user_to_roles.get(u["name"])}
    return (roles, _native_users(users),
            _excluded_by_pattern(users, pairs), user_role_map)


def export_dns_forwarders():
    """This server's DNS forwarders + forward policy. idnsforwarders live PER DNS
    server (dnsserver-show), NOT in the global dnsconfig — so this reads the local
    host's dnsserver entry. Returns ([], None) when this server runs no integrated
    DNS (dnsserver entry absent)."""
    try:
        entry = api.Command.dnsserver_show(api.env.host)["result"]
    except errors.NotFound:
        return [], None
    return _many(entry, "idnsforwarders"), _one(entry, "idnsforwardpolicy")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-host-membership", action="store_true",
                    help="emit static hostgroup host rosters (default: rely on "
                         "enrolment + automember)")
    ap.add_argument("--include-sshkeys", action="store_true",
                    help="emit per-user ipasshpubkey values")
    ap.add_argument("--flat-groups", action="store_true",
                    help="emit users with a flat `groups` list instead of the "
                         "roles matrix (default: derive roles)")
    ap.add_argument("--role-exclude", default='["role"]', metavar="JSON",
                    help="JSON array of case-insensitive regexes; groups whose "
                         "name matches ANY are kept as direct groups, not mined "
                         "into roles (default: '[\"role\"]'; '[]' mines everything)")
    ap.add_argument("--role-min-groups", type=int, default=2, metavar="N",
                    help="a co-occurrence bundle becomes a role only with >= N "
                         "groups; smaller bundles stay direct (default: 2)")
    ap.add_argument("--stock-hbacsvc", default=None, metavar="JSON",
                    help="JSON array of stock HBAC service names to skip (already "
                         "exist on a fresh server); extend it for newer FreeIPA "
                         "versions (default: the built-in set)")
    args = ap.parse_args()

    stock_hbacsvc = (set(json.loads(args.stock_hbacsvc))
                     if args.stock_hbacsvc else DEFAULT_HBACSVCS)

    api.bootstrap(context="cli", log=None)
    api.finalize()
    api.Backend.rpcclient.connect()

    groups, group_names = export_groups()
    users, used_fallback = export_users(group_names, args.include_sshkeys)
    dns_forwarders, dns_forward_policy = export_dns_forwarders()

    # Some accounts (typically service users) belong to no group other than the
    # default ipausers. The role requires every user to reference a declared
    # group, so when that fallback is hit we surface ipausers as a declared group
    # — it already exists, so applying it is idempotent.
    # No description — ipausers already exists with its own; omitting it means a
    # re-apply leaves the system group's description untouched (idempotent).
    if used_fallback and not any(g["name"] == DEFAULT_FALLBACK_GROUP for g in groups):
        groups.append({"name": DEFAULT_FALLBACK_GROUP})

    # Suggest a roles matrix (unless --flat-groups). Users stay NATIVE; the matrix
    # is advisory only and emitted under a key the role does NOT read.
    roles_suggested, roles_excluded, roles_user_map = [], {}, {}
    if not args.flat_groups:
        exclude = json.loads(args.role_exclude)
        roles_suggested, users, roles_excluded, roles_user_map = mine_roles(
            users, exclude, args.role_min_groups)

    doc = {
        "meta": {
            "source": api.env.host,
            "domain": api.env.domain,
            "realm": api.env.realm,
            "captured_at": datetime.datetime.now(
                datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host_membership_included": args.include_host_membership,
            "fallback_group_used": used_fallback,
            "roles_excluded_from_mining": roles_excluded,
            "roles_suggested_user_map": roles_user_map,
        },
        "freeipa_server_domain": api.env.domain,
        "freeipa_server_realm": api.env.realm,
        # DNS forwarders for the IPA-managed zone (per-server; "" if no integrated
        # DNS). NOTE: connection_name (the NetworkManager connection, default eth0)
        # is host-level networking, NOT FreeIPA state — it cannot be exported; set
        # it per host in inventory. The snapshot template carries it as a comment.
        "freeipa_server_forwarders": dns_forwarders,
        "freeipa_server_forward_policy": dns_forward_policy or "only",
        "freeipa_idam_groups": groups,
        # Advisory only — the role does NOT read this key (see template header).
        "freeipa_idam_roles_suggested": roles_suggested,
        "freeipa_idam_users": users,
        "freeipa_idam_hostgroups": export_hostgroups(args.include_host_membership),
        "freeipa_idam_hbacsvcs": export_hbacsvcs(stock_hbacsvc),
        "freeipa_idam_hbac_rules": export_hbac_rules(),
        "freeipa_idam_sudo_commands": export_sudo_commands(),
        "freeipa_idam_sudo_rules": export_sudo_rules(),
        "freeipa_idam_pwpolicies": export_pwpolicies(),
        "freeipa_server_automember_rules": export_automember_rules(),
    }
    counts = {k: len(v) for k, v in doc.items() if isinstance(v, list)}
    doc["meta"]["counts"] = counts
    json.dump(doc, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
