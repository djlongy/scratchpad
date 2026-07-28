#!/usr/bin/libexec/platform-python
"""Dump a FreeIPA realm's IAM state to JSON — the test oracle for chaos runs.

Independent of this role's own export/reconcile code on purpose: a chaos test
that verifies the role with the role's own machinery proves nothing. Talks to
ipalib directly and emits raw server truth.

Runs ON the IPA server as root with a valid Kerberos ticket:

    kinit admin
    /usr/bin/python3 freeipa_realm_snapshot.py > /tmp/snap.json

Exit 0 with JSON on stdout; diagnostics go to stderr so stdout stays parseable.
"""

import json
import sys

from ipalib import api

# (command, kwargs) per object type. all=True pulls membership attributes —
# without it group_find returns names only and every membership diff is blind.
QUERIES = [
    ("users", "user_find", {"all": True, "sizelimit": 0}),
    # Preserved (archived) users live in a separate subtree; user_find does not
    # return them, so the soft-delete archival path is invisible without this.
    ("users_preserved", "user_find", {"all": True, "sizelimit": 0, "preserved": True}),
    ("groups", "group_find", {"all": True, "sizelimit": 0}),
    ("hostgroups", "hostgroup_find", {"all": True, "sizelimit": 0}),
    ("hosts", "host_find", {"all": True, "sizelimit": 0}),
    ("hbacrules", "hbacrule_find", {"all": True, "sizelimit": 0}),
    ("hbacsvcs", "hbacsvc_find", {"all": True, "sizelimit": 0}),
    ("hbacsvcgroups", "hbacsvcgroup_find", {"all": True, "sizelimit": 0}),
    ("sudorules", "sudorule_find", {"all": True, "sizelimit": 0}),
    ("sudocmds", "sudocmd_find", {"all": True, "sizelimit": 0}),
    ("sudocmdgroups", "sudocmdgroup_find", {"all": True, "sizelimit": 0}),
    ("pwpolicies", "pwpolicy_find", {"all": True, "sizelimit": 0}),
    ("roles", "role_find", {"all": True, "sizelimit": 0}),
    # automember_find is type-scoped; the two rule namespaces are disjoint and
    # a rule is keyed by its target group, so they must be captured separately.
    ("automember_group", "automember_find", {"type": "group", "all": True}),
    ("automember_hostgroup", "automember_find", {"type": "hostgroup", "all": True}),
]

# Attributes that change on every read or every write regardless of intent.
# Keeping them would make every snapshot diff non-empty and hide real drift.
NOISE_ATTRS = {
    "krblastsuccessfulauth", "krblastpwdchange", "krbextradata",
    "krblastadminunlock", "krbloginfailedcount", "krbticketflags",
    "modifytimestamp", "createtimestamp", "entryusn", "usercertificate",
    "krbprincipalkey", "ipasshpubkey", "randompassword", "serverhostname",
}


def scrub(value):
    """Make an ipalib result JSON-safe and diff-stable."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in sorted(value.items()) if k not in NOISE_ATTRS}
    if isinstance(value, (list, tuple)):
        # Multi-valued LDAP attributes come back in server order, which is not
        # stable across writes — sort so a diff shows content change only.
        return sorted((scrub(v) for v in value), key=lambda x: json.dumps(x, sort_keys=True))
    if isinstance(value, bytes):
        return "<bytes:%d>" % len(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def main():
    api.bootstrap(context="cli")
    api.finalize()
    api.Backend.rpcclient.connect()

    snapshot = {}
    for label, command, kwargs in QUERIES:
        try:
            result = api.Command[command](**kwargs)["result"]
        # Broad by design: report, never abort. Any failure of one query is
        # recorded and the sweep continues.
        except Exception as exc:  # noqa: BLE001
            # One unsupported query (e.g. no DNS installed) must not lose the
            # other fifteen; record the failure in-band so the diff shows it.
            snapshot[label] = {"__error__": "%s: %s" % (type(exc).__name__, exc)}
            print("WARN %s: %s" % (label, exc), file=sys.stderr)
            continue
        entries = {}
        for entry in result:
            scrubbed = scrub(entry)
            # Primary key attribute differs per type; take the first name-ish attr
            # present. Order matters: sudocmd entries carry BOTH `sudocmd` (the
            # command path, the real pkey) and `ipauniqueid`, so `sudocmd` must be
            # tried first or every command is keyed by an opaque UUID that changes
            # on recreate — which would make a diff report a delete plus an add
            # where nothing actually changed.
            for key_attr in ("uid", "cn", "sudocmd", "automemberrule", "ipauniqueid"):
                if key_attr in scrubbed:
                    values = scrubbed[key_attr]
                    name = values[0] if isinstance(values, list) else values
                    entries[str(name)] = scrubbed
                    break
        snapshot[label] = entries

    json.dump(snapshot, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
