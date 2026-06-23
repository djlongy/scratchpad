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
import sys

from ipalib import api

# Groups that are auto-managed or role-owned — never emit as declarative config.
GROUP_DENYLIST = {"ipausers", "idam-managed-users"}
# pwpolicy owned by FreeIPA itself.
PWPOLICY_DENYLIST = {"global_policy"}
DEFAULT_FALLBACK_GROUP = "ipausers"  # only used if a user has no other group
# Stock FreeIPA HBAC services — shipped on every server, so they are excluded
# from the snapshot. Only CUSTOM services are captured (and later seeded on a
# fresh server before HBAC rule memberships that reference them).
DEFAULT_HBACSVCS = {
    "crond", "ftp", "gdm", "gdm-password", "gssftp", "kdm", "login", "proftpd",
    "pure-ftpd", "sshd", "su", "su-l", "sudo", "sudo-i", "systemd-user", "vsftpd",
}


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
    return None if v is None else (v if isinstance(v, str) else str(v))


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
            "description": _str(e, "description"),
            "group": nested,  # nested member groups only; user membership lives on users
        }))
    return out, names


def export_users(group_names, include_sshkeys):
    used_fallback = False
    out = []
    for e in _find("user_find"):
        if str(_one(e, "preserved")).lower() == "true":
            continue  # staged/deleted-preserved entry, not active config
        name = _str(e, "uid")
        if not name:
            continue
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
            "groups": groups,
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
        item = {"name": name, "description": _str(e, "description")}
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
            "description": _str(e, "description"),
            "usercategory": _str(e, "usercategory"),
            "hostcategory": _str(e, "hostcategory"),
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


def export_hbacsvcs():
    """Custom HBAC services only — the stock services already exist everywhere."""
    out = []
    for e in _find("hbacsvc_find"):
        name = _str(e, "cn")
        if not name or name in DEFAULT_HBACSVCS:
            continue
        out.append(_prune({"name": name, "description": _str(e, "description")}))
    return out


def export_sudo_commands():
    out = []
    for e in _find("sudocmd_find"):
        name = _str(e, "sudocmd")
        if not name:
            continue
        out.append(_prune({"name": name, "description": _str(e, "description")}))
    return out


def export_sudo_rules():
    out = []
    for e in _find("sudorule_find"):
        name = _str(e, "cn")
        if not name:
            continue
        item = {
            "name": name,
            "description": _str(e, "description"),
            "usercategory": _str(e, "usercategory"),
            "hostcategory": _str(e, "hostcategory"),
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


def export_automember_rules():
    out = []
    for atype in ("group", "hostgroup"):
        for e in _find("automember_find", type=atype):
            name = _str(e, "cn")
            if not name:
                continue
            inclusive, exclusive = [], []
            for raw in _many(e, "automemberinclusiveregex"):
                k, _, expr = raw.partition("=")
                if expr:
                    inclusive.append({"key": k, "expression": expr})
            for raw in _many(e, "automemberexclusiveregex"):
                k, _, expr = raw.partition("=")
                if expr:
                    exclusive.append({"key": k, "expression": expr})
            if not inclusive and not exclusive:
                continue  # a target group with no regex is not a useful rule
            out.append(_prune({
                "name": name,
                "automember_type": atype,
                "description": _str(e, "description"),
                "inclusive": inclusive,
                "exclusive": exclusive,
            }))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-host-membership", action="store_true",
                    help="emit static hostgroup host rosters (default: rely on "
                         "enrolment + automember)")
    ap.add_argument("--include-sshkeys", action="store_true",
                    help="emit per-user ipasshpubkey values")
    args = ap.parse_args()

    api.bootstrap(context="cli", log=None)
    api.finalize()
    api.Backend.rpcclient.connect()

    groups, group_names = export_groups()
    users, used_fallback = export_users(group_names, args.include_sshkeys)

    # Some accounts (typically service users) belong to no group other than the
    # default ipausers. The role requires every user to reference a declared
    # group, so when that fallback is hit we surface ipausers as a declared group
    # — it already exists, so applying it is idempotent.
    # No description — ipausers already exists with its own; omitting it means a
    # re-apply leaves the system group's description untouched (idempotent).
    if used_fallback and not any(g["name"] == DEFAULT_FALLBACK_GROUP for g in groups):
        groups.append({"name": DEFAULT_FALLBACK_GROUP})

    doc = {
        "meta": {
            "source": api.env.host,
            "domain": api.env.domain,
            "realm": api.env.realm,
            "captured_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host_membership_included": args.include_host_membership,
            "fallback_group_used": used_fallback,
        },
        "freeipa_server_domain": api.env.domain,
        "freeipa_server_realm": api.env.realm,
        "freeipa_idam_groups": groups,
        "freeipa_idam_users": users,
        "freeipa_idam_hostgroups": export_hostgroups(args.include_host_membership),
        "freeipa_idam_hbacsvcs": export_hbacsvcs(),
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
