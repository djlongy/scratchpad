#!/usr/bin/env python3
"""High side: make the registry match the desired state the low side sent.

A transfer that only ever says "add these images" can never express a deletion,
so an additive mirror drifts: tags removed upstream live forever, and a floating
tag that moves leaves its old manifest behind referenced by nothing. The fix is
for every transfer to carry the complete intended tag -> digest map (state.json)
and for this script to make the far side match it: add what is missing, retarget
what moved, delete what is no longer wanted.

  usage: reconcile.py STATE_JSON OCI_STORE REGISTRY USER PASS TOKEN_FILE [APPLIED_FILE]

Scope and safety, both deliberate:

* Only repositories named in state.json are touched. A repository that vanished
  upstream is left alone rather than deleted on the strength of its absence,
  because absence is also what a truncated transfer looks like.
* A state document older than the last one applied is refused. Without this a
  replayed or out-of-order transfer would quietly revert the mirror to an earlier
  world, and nothing downstream would notice.
* A repository whose desired set is EMPTY is refused. That is never a legitimate
  instruction - a repository with no wanted tags simply would not appear.
* A run that would delete more than RECONCILE_MAX_DELETE_PCT (default 90) of a
  repository's tags refuses, unless RECONCILE_FORCE=1. This is a backstop for a
  well-formed but badly truncated document, not the primary guard: withdrawing one
  version upstream legitimately removes it plus every floating tag that pointed at
  it, which is easily most of a small repository's tags.

The primary guard is none of these - it is that import.sh only calls this script
at all when the transfer passed its completeness check.
* Tags are reconciled. Blobs in the OCI store are NOT pruned here - see
  prune-store.py for why that cannot be automatic.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

MAX_DELETE_PCT = int(os.environ.get("RECONCILE_MAX_DELETE_PCT", "90"))
FORCE = os.environ.get("RECONCILE_FORCE") == "1"


def api(reg, path, token, method="GET"):
    # Quay's API v1 wants the superuser's OAuth token. Basic auth with the same
    # credentials authenticates for registry pull/push but is rejected here (401).
    url = f"http://{reg}/api/v1{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def live_tags(reg, repo, token):
    """Current tag -> digest on the high side, newest revision of each name."""
    got = api(reg, f"/repository/{repo}/tag/?limit=100&onlyActiveTags=true", token)
    if got is None:
        return {}
    out = {}
    for t in got["tags"]:
        out.setdefault(t["name"], t["manifest_digest"])
    return out


def main():
    state_path, store, reg, user, password, token_file = sys.argv[1:7]
    applied_file = sys.argv[7] if len(sys.argv) > 7 else None
    token = open(token_file).read().strip()
    state = json.load(open(state_path))
    stamp = state.get("transfer", "")

    # Transfers can arrive out of order, or be replayed from imported/. Applying an
    # older desired state would silently roll the mirror back.
    if applied_file and os.path.exists(applied_file):
        last = open(applied_file).read().strip()
        if stamp and last and stamp <= last:
            print(f"  skipping {stamp}: not newer than the last applied state ({last})")
            return

    added = retargeted = deleted = 0

    for repo, desired in state["repos"].items():
        if not desired:
            print(f"  REFUSING {repo}: the desired set is empty, which is never a "
                  f"legitimate instruction", file=sys.stderr)
            continue
        live = live_tags(reg, repo, token)
        extra = [t for t in live if t not in desired]

        if extra and not FORCE and live and len(extra) * 100 > len(live) * MAX_DELETE_PCT:
            print(f"  REFUSING {repo}: {len(extra)} of {len(live)} tags would be deleted "
                  f"(> {MAX_DELETE_PCT}%). Set RECONCILE_FORCE=1 if that is really intended.",
                  file=sys.stderr)
            continue

        for tag, digest in desired.items():
            if live.get(tag) == digest:
                continue
            ref = f"{repo}:{tag}"
            try:
                subprocess.run(
                    ["skopeo", "copy", "-q", "--dest-tls-verify=false",
                     "--dest-creds", f"{user}:{password}",
                     f"oci:{store}:{ref}", f"docker://{reg}/{ref}"],
                    check=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                # The store cannot hold content that never crossed. Say so and carry on;
                # the next transfer that includes this digest will settle it.
                print(f"  SKIP {ref}: not in the OCI store ({e.stderr.decode().strip()[:120]})",
                      file=sys.stderr)
                continue
            if tag in live:
                print(f"  retargeted {ref} -> {digest[:19]}")
                retargeted += 1
            else:
                print(f"  added {ref}")
                added += 1

        for tag in extra:
            api(reg, f"/repository/{repo}/tag/{tag}", token, method="DELETE")
            print(f"  deleted {repo}:{tag} (not in desired state)")
            deleted += 1

    print(f"reconciled: {added} added, {retargeted} retargeted, {deleted} deleted")
    if applied_file and stamp:
        with open(applied_file, "w") as fh:
            fh.write(stamp)


if __name__ == "__main__":
    main()
