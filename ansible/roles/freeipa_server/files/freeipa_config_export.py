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

from ipalib import api, errors

# INTERNAL / auto-managed objects that are NOT declarative config and would be wrong
# to re-declare — so they are skipped on export. This is NOT "protection": protected
# objects (admin, admins, ipausers, trust admins) ARE exported (state stays complete
# + auditable); protection is purely an APPLY-time delete/modify guard. Skipped here:
#   * the role's own marker group (re-created by the role; circular to capture)
#   * User Private Groups (one per user, mepManagedEntry — handled by _is_upg below)
GROUP_EXPORT_SKIP = {"idam-managed-users"}
# Built-in FreeIPA delegation roles (ipa role) — recreated on every install, so only
# CUSTOM roles are exported. Extend if a newer FreeIPA ships more built-ins.
ROLE_DENYLIST = {
    "helpdesk", "User Administrator", "Enrollment Administrator", "IT Specialist",
    "IT Security Specialist", "Security Architect", "Subordinate ID Selfservice User",
}
# pwpolicy owned by FreeIPA itself.
PWPOLICY_DENYLIST = {"global_policy"}
# Stock HBAC service groups shipped on every server — only CUSTOM ones are captured.
STOCK_HBACSVCGROUPS = {"Sudo", "ftp"}
DEFAULT_FALLBACK_GROUP = "ipausers"  # only used if a user has no other group
# Login shells that mean "no interactive login" → the account is a SERVICE account.
NOLOGIN_SHELLS = {"/sbin/nologin", "/usr/sbin/nologin", "/bin/false", "/usr/bin/false"}
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
_GROUPS = "groups"
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


# Sections skipped because their capability is not available on THIS server
# (recorded into meta["skipped"] so a partial snapshot is self-documenting).
_SKIPPED = []

# Errors that mean "this plugin/command isn't installed or configured here" — skip the
# section rather than aborting the whole export. The common case is a server built
# WITHOUT the integrated DNS plugin: dnszone_find / dnsrecord_find / dnsserver_show then
# raise NotFound ("DNS is not configured") or CommandError, or the command isn't even
# registered client-side (KeyError on api.Command[cmd] / AttributeError on api.Command.x).
# errors.PublicError is the base class for every IPA error returned over the wire, so it
# covers NotFound/CommandError without masking genuine local bugs (TypeError etc. still
# propagate — those are not in this tuple).
_UNAVAILABLE = (errors.PublicError, KeyError, AttributeError)


def _safe(label, fn, default):
    """Run an export section; if its capability is unavailable on this server, log a
    skip to stderr, record it in meta["skipped"], and substitute `default` so the rest
    of the export still succeeds (partial config beats no config)."""
    try:
        return fn()
    except _UNAVAILABLE as exc:
        reason = "%s: %s" % (type(exc).__name__, exc)
        sys.stderr.write("[freeipa-export] skipping '%s' — %s\n" % (label, reason))
        _SKIPPED.append({"section": label, "reason": reason})
        return default


def export_groups():
    groups, names = [], set()
    for e in _find("group_find"):
        name = _str(e, "cn")
        if not name or name in GROUP_EXPORT_SKIP or _is_upg(e):
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
            # Capture the GID so a DR rebuild recreates the SAME gidnumber — group
            # GIDs drive /etc/group + SSSD/NSS and must stay consistent across hosts
            # (unlike user UIDs, which IPA may re-assign without cross-host impact).
            "gidnumber": _int(e, "gidnumber"),
            "group": nested,  # nested member groups only; user membership lives on users
            # Member managers — users/groups allowed to manage THIS group's
            # membership without being admins (e.g. a PAM/JIT toggler service).
            "membermanager_user": _many(e, "membermanager_user"),
            "membermanager_group": [g for g in _many(e, "membermanager_group") if g in names],
        }))
    return out, names


def export_users(group_names, include_sshkeys):
    """Split active accounts into human users vs SERVICE accounts (login disabled).
    A user whose loginshell is a nologin shell is emitted under
    freeipa_idam_service_accounts (so a re-apply forces shell=nologin), everyone
    else under freeipa_idam_users. Returns (users, service_accounts, used_fallback)."""
    used_fallback = False
    users, service_accounts = [], []
    for e in _find("user_find"):
        if str(_one(e, "preserved")).lower() == "true":
            continue  # staged/deleted-preserved entry, not active config
        name = _str(e, "uid")
        if not name:
            continue
        # Keep ALL of the user's real memberships (admin, ipausers, admins, ...): the
        # group set already excludes only internal/auto-managed groups, so this never
        # strips a real, access-granting membership. "Protected" is an apply-time
        # guard, NOT an export filter — admin and built-in groups are exported too.
        groups = [g for g in _many(e, "memberof_group") if g in group_names]
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
        # Login disabled (nologin shell) -> service account. The role forces the
        # nologin shell on apply, so it is not re-emitted on the item.
        if _str(e, "loginshell") in NOLOGIN_SHELLS:
            service_accounts.append(_prune(item))
        else:
            users.append(_prune(item))
    return users, service_accounts, used_fallback


def export_hostgroups(include_host_membership):
    out = []
    names = {_str(e, "cn") for e in _find("hostgroup_find")}
    for e in _find("hostgroup_find"):
        name = _str(e, "cn")
        if not name:
            continue
        item = {"name": name, _DESCRIPTION: _str(e, _DESCRIPTION)}
        # Exclude SELF — a hostgroup must never be a member of itself (bad state /
        # would create a self-reference on re-apply). Only real nested hostgroups.
        nested = [g for g in _many(e, "member_hostgroup") if g in names and g != name]
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
            "usergroup": _many(e, "memberuser_group"),
            "host": _many(e, "memberhost_host"),
            "hostgroup": _many(e, "memberhost_hostgroup"),
            "service": _many(e, "memberservice_hbacsvc"),
            "servicegroup": _many(e, "memberservice_hbacsvcgroup"),
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
            "usergroup": _many(e, "memberuser_group"),
            "host": _many(e, "memberhost_host"),
            "hostgroup": _many(e, "memberhost_hostgroup"),
            "cmd": _many(e, "memberallowcmd_sudocmd"),
            "deny_cmd": _many(e, "memberdenycmd_sudocmd"),
            "cmdgroup": _many(e, "memberallowcmd_sudocmdgroup"),
            "deny_cmdgroup": _many(e, "memberdenycmd_sudocmdgroup"),
            "sudoopt": _many(e, "ipasudoopt"),
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


def export_iparoles():
    """CUSTOM native IPA delegation roles (ipa role) — name + privileges + members.
    Built-in roles (ROLE_DENYLIST) are skipped; they recreate on install."""
    out = []
    for e in _find("role_find"):
        name = _str(e, "cn")
        if not name or name in ROLE_DENYLIST:
            continue
        out.append(_prune({
            "name": name,
            _DESCRIPTION: _str(e, _DESCRIPTION),
            "privilege": _many(e, "memberof_privilege"),
            "user": _many(e, "member_user"),
            "usergroup": _many(e, "member_group"),
            "host": _many(e, "member_host"),
            "hostgroup": _many(e, "member_hostgroup"),
            "service": _many(e, "member_service"),
        }))
    return out


def export_hbacsvcgroups():
    """Custom HBAC service GROUPS — name + description + member services. Stock
    groups that ship on every server ('Sudo', 'ftp') are skipped."""
    out = []
    for e in _find("hbacsvcgroup_find"):
        name = _str(e, "cn")
        if not name or name in STOCK_HBACSVCGROUPS:
            continue
        out.append(_prune({
            "name": name,
            _DESCRIPTION: _str(e, _DESCRIPTION),
            "hbacsvc": _many(e, "member_hbacsvc"),
        }))
    return out


def export_sudocmdgroups():
    """Sudo command GROUPS — name + description + member sudo commands."""
    out = []
    for e in _find("sudocmdgroup_find"):
        name = _str(e, "cn")
        if not name:
            continue
        out.append(_prune({
            "name": name,
            _DESCRIPTION: _str(e, _DESCRIPTION),
            "sudocmd": _many(e, "member_sudocmd"),
        }))
    return out


def _system_permission_names():
    """Names of FreeIPA-managed (built-in) permissions — ipapermissiontype contains
    'MANAGED'. (Every permission carries SYSTEM+V2; only the built-in/default ones
    are additionally MANAGED — user-created permissions are not.) These can't be
    recreated on a rebuild, so they're excluded from the snapshot and used to detect
    built-in privileges (a privilege holding ONLY managed permissions is built-in)."""
    out = set()
    for e in _find("permission_find"):
        name = _str(e, "cn")
        ptype = " ".join(_many(e, "ipapermissiontype")).upper()
        if name and "MANAGED" in ptype:
            out.add(name)
    return out


def export_privileges(wanted_names, system_perms):
    """CUSTOM privileges referenced by exported roles. A privilege holding only
    built-in (SYSTEM) permissions is itself built-in (e.g. 'Group Administrators')
    and is skipped — the role still references it by name, but we never recreate it.
    A kept privilege keeps its FULL permission list (system perms exist built-in;
    only custom ones are emitted under freeipa_idam_permissions). Returns
    (privileges, set_of_member_permission_names)."""
    out, perm_names = [], set()
    for e in _find("privilege_find"):
        name = _str(e, "cn")
        if not name or name not in wanted_names:
            continue
        perms = _many(e, "memberof_permission")
        if not [p for p in perms if p not in system_perms]:
            continue  # built-in privilege (only system permissions) — don't recreate
        perm_names.update(perms)
        out.append(_prune({
            "name": name,
            _DESCRIPTION: _str(e, _DESCRIPTION),
            "permission": perms,
        }))
    return out, perm_names


def export_permissions(wanted_names, system_perms):
    """CUSTOM permissions held by the exported privileges. SYSTEM (FreeIPA-managed)
    permissions are skipped — they exist on every server and cannot be recreated."""
    out = []
    for e in _find("permission_find"):
        name = _str(e, "cn")
        if not name or name not in wanted_names or name in system_perms:
            continue
        out.append(_prune({
            "name": name,
            "right": _many(e, "ipapermright"),
            "attrs": _many(e, "attrs"),
            "object_type": _str(e, "type"),
            "subtree": _str(e, "subtree"),
            "target": _str(e, "ipapermtargetto") or _str(e, "target"),
            "extra_target_filter": _many(e, "extratargetfilter"),
            "memberof": _many(e, "memberof"),
        }))
    return out


def export_dns_zones():
    """All DNS zones managed by this server (forward AND reverse). Includes the
    realm's own primary zone — harmless to re-declare idempotently."""
    out = []
    for e in _find("dnszone_find"):
        name = _str(e, "idnsname")
        if not name:
            continue
        # idnsallowtransfer comes back as the raw BIND ACL string ("none;" = the
        # default no-transfer). ipadnszone's allow_transfer param expects a LIST of
        # IPs, NOT the "none;" keyword — feeding it back fails ("Invalid ip_address").
        # So only emit allow_transfer when it's a real, non-default ACL; otherwise
        # omit it (default = no transfer), which makes the zone round-trip cleanly.
        xfer = (_str(e, "idnsallowtransfer") or "").strip()
        allow_transfer = [] if xfer.rstrip(";").strip().lower() in ("", "none") \
            else [a.strip() for a in xfer.rstrip(";").split(";") if a.strip()]
        out.append(_prune({
            "name": name,
            "dynamic_update": _str(e, "idnsallowdynupdate") in ("TRUE", "True", "true"),
            "allow_sync_ptr": _str(e, "idnsallowsyncptr") in ("TRUE", "True", "true"),
            "allow_transfer": allow_transfer,
        }))
    return out


def export_dns_forward_zones():
    """Conditional forward zones (idnsforwardzone) — the role applies these via
    freeipa_server_dns_forward_zones, so capture them for symmetry. Field names =
    ipadnsforwardzone params (name, forwarders, forwardpolicy)."""
    out = []
    for e in _find("dnsforwardzone_find"):
        name = _str(e, "idnsname")
        if not name:
            continue
        out.append(_prune({
            "name": name,
            "forwarders": _many(e, "idnsforwarders"),
            "forwardpolicy": _str(e, "idnsforwardpolicy"),
        }))
    return out


def _dns_record_entry(e):
    """One dnsrecord_find entry → a declarative record dict, or None to skip it.
    Skips the apex '@' and SOA/NS-only records (zone-owned, not record state)."""
    rn = _str(e, "idnsname")
    if rn in ("@",):
        return None
    a = _many(e, "arecord")
    aaaa = _many(e, "aaaarecord")
    cname = _many(e, "cnamerecord")
    ptr = _many(e, "ptrrecord")
    if (_many(e, "nsrecord") or _many(e, "soarecord")) and not (a or aaaa or cname or ptr):
        return None
    rec = _prune({
        "record_name": rn,
        "a_record": a,
        "aaaa_record": aaaa,
        "cname_record": cname,
        "ptr_record": ptr,
        "mx_record": _many(e, "mxrecord"),
        "txt_record": _many(e, "txtrecord"),
        "srv_record": _many(e, "srvrecord"),
    })
    return rec if (rec.get("record_name") and len(rec) > 1) else None


def export_dns_records():
    """Per-zone DNS records. SOA/NS-only and the apex '@' record are skipped —
    they are owned by the zone definition itself, not declarative record state.
    Returns [{zone_name, records: [...]}] for zones with at least one record."""
    out = []
    for z in _find("dnszone_find"):
        zone = _str(z, "idnsname")
        if not zone:
            continue
        records = [r for r in (_dns_record_entry(e)
                               for e in _find("dnsrecord_find", dnszoneidnsname=zone)) if r]
        if records:
            out.append({"zone_name": zone, "records": records})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-host-membership", action="store_true",
                    help="emit static hostgroup host rosters (default: rely on "
                         "enrolment + automember)")
    ap.add_argument("--include-sshkeys", action="store_true",
                    help="emit per-user ipasshpubkey values")
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

    _SKIPPED.clear()
    groups, group_names = _safe("usergroups", export_groups, ([], set()))
    users, service_accounts, used_fallback = _safe(
        "users", lambda: export_users(group_names, args.include_sshkeys), ([], [], False))
    dns_forwarders, dns_forward_policy = _safe(
        "dns_forwarders", export_dns_forwarders, ([], None))

    # Some accounts (typically service users) belong to no group other than the
    # default ipausers. The role requires every user to reference a declared
    # group, so when that fallback is hit we surface ipausers as a declared group
    # — it already exists, so applying it is idempotent.
    # No description — ipausers already exists with its own; omitting it means a
    # re-apply leaves the system group's description untouched (idempotent).
    if used_fallback and not any(g["name"] == DEFAULT_FALLBACK_GROUP for g in groups):
        groups.append({"name": DEFAULT_FALLBACK_GROUP})

    # Custom delegation roles, plus exactly the privileges they reference and the
    # permissions those privileges hold (built-ins are skipped to avoid dumping the
    # ~80 stock privileges/permissions a fresh server already ships).
    iparoles = _safe("iparoles", export_iparoles, [])
    wanted_privs = {p for r in iparoles for p in r.get("privilege", [])}
    system_perms = _safe("system_permissions", _system_permission_names, set())
    privs, perm_names = _safe(
        "privileges", lambda: export_privileges(wanted_privs, system_perms), ([], set()))
    perms = _safe("permissions", lambda: export_permissions(perm_names, system_perms), [])

    doc = {
        "meta": {
            "source": api.env.host,
            "domain": api.env.domain,
            "realm": api.env.realm,
            "captured_at": datetime.datetime.now(
                datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host_membership_included": args.include_host_membership,
            "fallback_group_used": used_fallback,
        },
        "freeipa_server_domain": api.env.domain,
        "freeipa_server_realm": api.env.realm,
        # This server's own FQDN (captured), emitted as an overridable var. Useful
        # as module/DNS input — e.g. the IPA host a DNS record points at, or
        # community.general's ipa_host. Override in inventory for a different target.
        "freeipa_server_fqdn": api.env.host,
        # DNS forwarders for the IPA-managed zone (per-server; "" if no integrated
        # DNS). NOTE: connection_name (the NetworkManager connection, default eth0)
        # is host-level networking, NOT FreeIPA state — it cannot be exported; set
        # it per host in inventory. The snapshot template carries it as a comment.
        "freeipa_server_forwarders": dns_forwarders,
        "freeipa_server_forward_policy": dns_forward_policy or "only",
        # DNS zones (forward + reverse) and their records — DR-recreatable state.
        # All three skip gracefully (→ []) on a server without the integrated DNS plugin.
        "freeipa_server_dns_zones": _safe("dns_zones", export_dns_zones, []),
        "freeipa_server_dns_forward_zones": _safe("dns_forward_zones", export_dns_forward_zones, []),
        "freeipa_server_dns_records": _safe("dns_records", export_dns_records, []),
        "freeipa_idam_usergroups": groups,
        # Users are exported VERBATIM — each with its real, literal `groups` list
        # exactly as held in FreeIPA. No roles matrix is synthesised (the snapshot is
        # a faithful mirror of the directory, not an inferred refactor).
        "freeipa_idam_users": users,
        # Accounts with a nologin shell — re-applied as service accounts (shell forced).
        "freeipa_idam_service_accounts": service_accounts,
        "freeipa_idam_hostgroups": _safe(
            "hostgroups", lambda: export_hostgroups(args.include_host_membership), []),
        "freeipa_idam_hbacsvcs": _safe("hbacsvcs", lambda: export_hbacsvcs(stock_hbacsvc), []),
        "freeipa_idam_hbacsvcgroups": _safe("hbacsvcgroups", export_hbacsvcgroups, []),
        "freeipa_idam_hbac_rules": _safe("hbac_rules", export_hbac_rules, []),
        "freeipa_idam_sudo_commands": _safe("sudo_commands", export_sudo_commands, []),
        "freeipa_idam_sudocmdgroups": _safe("sudocmdgroups", export_sudocmdgroups, []),
        "freeipa_idam_sudo_rules": _safe("sudo_rules", export_sudo_rules, []),
        "freeipa_idam_pwpolicies": _safe("pwpolicies", export_pwpolicies, []),
        # CUSTOM native delegation roles (built-ins skipped) for DR.
        "freeipa_idam_iparoles": iparoles,
        # Privileges referenced by those roles + the permissions they hold.
        "freeipa_idam_privileges": privs,
        "freeipa_idam_permissions": perms,
        "freeipa_server_automember_rules": _safe("automember_rules", export_automember_rules, []),
    }
    # Record which sections were skipped (empty list = a complete capture).
    doc["meta"]["skipped"] = list(_SKIPPED)
    counts = {k: len(v) for k, v in doc.items() if isinstance(v, list)}
    doc["meta"]["counts"] = counts
    json.dump(doc, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
