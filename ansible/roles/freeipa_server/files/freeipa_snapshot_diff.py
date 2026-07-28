#!/usr/bin/env python3
"""Diff two realm snapshots — the pass/fail oracle for a chaos scenario.

Usage: snapdiff.py BEFORE.json AFTER.json [--only-names]

Reports per object type: objects added, objects REMOVED, and membership deltas.
Removals are what a chaos scenario is actually testing, so they print first and
loudest; a scenario that expected to remove nothing and shows removals is a fail.
"""
import json
import sys

# Attributes whose change is a real membership/state delta worth reporting.
# Anything not listed is structural noise (dn, objectclass, ipauniqueid, gid...).
INTERESTING = [
    "member_user", "member_group", "memberindirect_user", "member_host",
    "memberof_group", "memberuser_group", "memberuser_user",
    "memberhost_hostgroup", "memberhost_host", "memberservice_hbacsvc",
    "servicecategory", "usercategory", "hostcategory", "cmdcategory",
    "memberallowcmd_sudocmd", "memberallowcmd_sudocmdgroup",
    "member_hbacsvc", "member_sudocmd", "member_hostgroup",
    "nsaccountlock", "ipaenabledflag", "description", "loginshell",
    "ipauserauthtype", "automemberinclusiveregex", "gidnumber",
]


def load(path):
    with open(path) as handle:
        return json.load(handle)


def norm(value):
    if isinstance(value, list):
        return sorted(str(v) for v in value)
    return value


def main() -> None:
    """Print the diff. Deliberately does NOT adjudicate — see the module docstring.

    Reporting and verdict are split on purpose: the exit status is always success
    (barring a crash), and the caller decides pass/fail from the REPORT. A removal
    is not universally a failure — re-declaring an archived user reactivates it,
    which drops a key from `users_preserved` legitimately — so a blanket
    "removals => non-zero exit" would fail runs that are correct. The consumers in
    `scripts/freeipa-chaos-regress.sh` therefore count `^  REMOVED [<kind>]` lines
    and filter by object type; they pipe stdout into grep, so this process's own
    status is not even observable to them.
    """
    before, after = load(sys.argv[1]), load(sys.argv[2])
    verdict_removed = 0
    lines = []

    for kind in sorted(set(before) | set(after)):
        b, a = before.get(kind, {}), after.get(kind, {})
        if not isinstance(b, dict) or not isinstance(a, dict):
            continue
        added = sorted(set(a) - set(b))
        removed = sorted(set(b) - set(a))
        changed = []
        for name in sorted(set(a) & set(b)):
            if not isinstance(a[name], dict) or not isinstance(b[name], dict):
                continue
            for attr in INTERESTING:
                bv, av = norm(b[name].get(attr)), norm(a[name].get(attr))
                if bv != av:
                    changed.append((name, attr, bv, av))
        if not (added or removed or changed):
            continue
        lines.append("\n## %s" % kind)
        if removed:
            verdict_removed += len(removed)
            # The object TYPE is repeated on the removal line, not left implicit on the
            # preceding "## <kind>" header. A caller filtering removals by type (e.g.
            # "ignore users_preserved, since re-declaring an archived user reactivates
            # it and that is not a deletion") otherwise has to track section state, and
            # a naive `grep -v <kind>` silently matches nothing and counts every row.
            lines.append("  REMOVED [%s] (%d): %s" % (kind, len(removed), ", ".join(removed)))
        if added:
            lines.append("  added   (%d): %s" % (len(added), ", ".join(added)))
        for name, attr, bv, av in changed:
            lines.append("  ~ %-28s %-24s %s -> %s" % (name, attr, bv, av))

    if not lines:
        print("NO DIFF — snapshots are identical on all tracked attributes.")
        return
    print("\n".join(lines))
    print("\n=== %d object(s) REMOVED across all types ===" % verdict_removed)


if __name__ == "__main__":
    main()
