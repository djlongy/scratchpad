#!/usr/bin/env python3
"""Low side: decide what the high side should hold, and say so completely.

Prints one JSON document:

  {"selected": ["demo/app:1.2.0", ...],          # refs to copy into the OCI layout
   "repos": {"demo/app": {"1.2.0": "sha256:..",  # the COMPLETE desired tag -> digest map
                          "latest": "sha256:.."}}}

The two halves answer different questions. `selected` is the *content* filter —
newest KEEP semver versions per repository, the expensive part, deduped against
the Redis ledger before it crosses. `repos` is the *desired state* — every tag
the high side should end up with, sent in full on every transfer because a tag
is a few hundred bytes and a layer is megabytes.

A tag is an independent name -> digest row in each registry. It is not an alias:
copying 1.2.0 does not make `latest` resolve on the far side even when both point
at the same digest. So any floating tag that points at content we are already
sending is picked up here and carried along for nearly nothing.

  usage: select-tags.py API_BASE ORG KEEP TOKEN_FILE
"""
import json
import re
import sys
import urllib.error
import urllib.request

SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def api(base, path, token):
    req = urllib.request.Request(f"{base}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def check_auth(base, token):
    """Fail loudly on a dead token.

    Quay answers `/repository?namespace=...` with an empty list rather than 401
    when the token is invalid, because private repositories are simply not
    visible to an unauthenticated caller. An expired token therefore looks
    exactly like "this organisation has no repositories", and the mirror would
    ship a desired state of nothing. Probe an endpoint that does return 401.
    """
    try:
        api(base, "/user/", token)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit(f"select-tags: token rejected ({e.code}). An empty repository "
                     f"list from Quay is indistinguishable from a dead token, so "
                     f"refusing rather than reporting an empty desired state.")
        raise


def main():
    base, org, keep, token_file = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    token = open(token_file).read().strip()
    check_auth(base, token)

    selected, repos = [], {}
    for repo in (r["name"] for r in api(base, f"/repository?namespace={org}&limit=100", token)["repositories"]):
        tags = api(base, f"/repository/{org}/{repo}/tag/?limit=100&onlyActiveTags=true", token)["tags"]
        # Quay lists a tag once per revision; the newest entry is the live one.
        digest = {}
        for t in tags:
            digest.setdefault(t["name"], t["manifest_digest"])

        versions = sorted((t for t in digest if SEMVER.match(t)),
                          key=lambda t: tuple(int(x) for x in SEMVER.match(t).groups()),
                          reverse=True)[:keep]
        wanted = dict.fromkeys(versions)
        # Floating tags (latest, stable, a major/minor alias) ride along whenever they
        # point at content already being sent. Their digest moves between runs, which is
        # exactly why the map has to be rebuilt and resent every time.
        carried = {d for t, d in digest.items() if t in wanted}
        for tag, dig in digest.items():
            if tag not in wanted and dig in carried:
                wanted[tag] = None

        if not wanted:
            continue
        repos[f"{org}/{repo}"] = {t: digest[t] for t in wanted}
        selected += [f"{org}/{repo}:{t}" for t in wanted]

    json.dump({"selected": selected, "repos": repos}, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
