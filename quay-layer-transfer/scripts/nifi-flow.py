#!/usr/bin/env python3
"""Build the two NiFi flows through the REST API, then start them. Stdlib only.

usage: nifi-flow.py [--url https://localhost:18090] [--user admin] [--password ...] [--reset]

low-side  (send):  ListFile /data/low-export  -> FetchFile -> UnpackContent(tar)
                   -> RouteOnAttribute: blobs -> DetectDuplicate (Redis, key = blob digest)
                                          metadata (index.json, oci-layout, manifest.json) always
                   -> MergeContent (TAR per original archive) -> UpdateAttribute -> PutFile /data/diode
high-side (recv):  ListFile /data/diode -> FetchFile -> UnpackContent(tar)
                   -> PutFile /data/high-store/incoming/<transfer>/<path>
--reset stops, empties and deletes both groups first (idempotent rebuild).
"""
import argparse
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request

REDIS = "dedupe-redis:6379"
LOW = {"export": "/data/low-export", "diode": "/data/diode"}
HIGH = {"diode": "/data/diode", "store": "/data/high-store/incoming"}


class Nifi:
    def __init__(self, url, user, password):
        self.api = url.rstrip("/") + "/nifi-api"
        self.types = {}
        self.token = None
        self.user, self.password = user, password
        self.ctx = ssl.create_default_context()      # the lab NiFi has a self-signed certificate
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def login(self):
        data = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
        req = urllib.request.Request(self.api + "/access/token", data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            self.token = r.read().decode()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    # --- lookups -------------------------------------------------------------------------
    def bundle(self, kind, type_name):
        """kind = processor-types | controller-service-types; returns (type, bundle) for a class name."""
        if kind not in self.types:
            key = "processorTypes" if kind == "processor-types" else "controllerServiceTypes"
            self.types[kind] = {t["type"]: t["bundle"] for t in self.call("GET", f"/flow/{kind}")[key]}
        full = next((t for t in self.types[kind] if t.endswith("." + type_name)), None)
        if not full:
            raise SystemExit(f"{type_name} not available in this NiFi ({kind})")
        return full, self.types[kind][full]

    def root(self):
        return self.call("GET", "/flow/process-groups/root")["processGroupFlow"]["id"]

    def group_named(self, parent, name):
        for g in self.call("GET", f"/process-groups/{parent}/process-groups")["processGroups"]:
            if g["component"]["name"] == name:
                return g
        return None

    # --- building ------------------------------------------------------------------------
    def group(self, parent, name, x):
        return self.call("POST", f"/process-groups/{parent}/process-groups",
                         {"revision": {"version": 0}, "component": {"name": name, "position": {"x": x, "y": 0}}})["id"]

    def service(self, pg, type_name, name, props):
        t, b = self.bundle("controller-service-types", type_name)
        return self.call("POST", f"/process-groups/{pg}/controller-services",
                         {"revision": {"version": 0}, "component": {"type": t, "bundle": b, "name": name, "properties": props}})

    def enable(self, svc):
        self.call("PUT", f"/controller-services/{svc['id']}/run-status",
                  {"revision": svc["revision"], "state": "ENABLED"})
        for _ in range(30):
            state = self.call("GET", f"/controller-services/{svc['id']}")["component"]["state"]
            if state == "ENABLED":
                return
            time.sleep(1)
        raise SystemExit(f"{svc['component']['name']} did not enable: {state}")

    def processor(self, pg, type_name, name, y, props, terminate=(), schedule=None):
        t, b = self.bundle("processor-types", type_name)
        config = {"properties": props, "autoTerminatedRelationships": list(terminate)}
        if schedule:
            config["schedulingPeriod"] = schedule
        return self.call("POST", f"/process-groups/{pg}/processors",
                         {"revision": {"version": 0},
                          "component": {"type": t, "bundle": b, "name": name, "position": {"x": 0, "y": y * 150},
                                        "config": config}})["id"]

    def connect(self, pg, src, dst, rels):
        self.call("POST", f"/process-groups/{pg}/connections",
                  {"revision": {"version": 0},
                   "component": {"source": {"id": src, "groupId": pg, "type": "PROCESSOR"},
                                 "destination": {"id": dst, "groupId": pg, "type": "PROCESSOR"},
                                 "selectedRelationships": list(rels)}})

    def start(self, pg):
        self.call("PUT", f"/flow/process-groups/{pg}", {"id": pg, "state": "RUNNING"})

    # --- teardown ------------------------------------------------------------------------
    def remove(self, g):
        pg = g["id"]
        self.call("PUT", f"/flow/process-groups/{pg}", {"id": pg, "state": "STOPPED"})
        time.sleep(2)
        self.call("POST", f"/process-groups/{pg}/empty-all-connections-requests")
        self.call("PUT", f"/flow/process-groups/{pg}/controller-services", {"id": pg, "state": "DISABLED"})
        time.sleep(2)
        g = self.call("GET", f"/process-groups/{pg}")
        self.call("DELETE", f"/process-groups/{pg}?version={g['revision']['version']}&clientId=nifi-flow")


def build_low(n, root):
    pg = n.group(root, "low-side (send)", 0)
    pool = n.service(pg, "RedisConnectionPoolService", "dedupe redis pool",
                     {"Redis Mode": "Standalone", "Connection String": REDIS})
    n.enable(pool)
    cache = n.service(pg, "RedisDistributedMapCacheClientService", "transferred-layers cache",
                      {"redis-connection-pool": pool["id"]})
    n.enable(cache)

    ls = n.processor(pg, "ListFile", "list exports", 0,
                     {"Input Directory": LOW["export"], "File Filter": r"transfer-.*\.tar", "Minimum File Age": "5 sec",
                      "Recurse Subdirectories": "false"}, schedule="10 sec")
    fetch = n.processor(pg, "FetchFile", "fetch archive", 1,
                        {"Completion Strategy": "Move File", "Move Destination Directory": LOW["export"] + "/sent"},
                        terminate=("not.found", "permission.denied", "failure"))
    unpack = n.processor(pg, "UnpackContent", "unpack tar", 2, {"Packaging Format": "tar"},
                         terminate=("original", "failure"))
    route = n.processor(pg, "RouteOnAttribute", "blob or metadata", 3,
                        {"Routing Strategy": "Route to Property name", "blob": "${path:contains('blobs/sha256')}"})
    dedupe = n.processor(pg, "DetectDuplicate", "seen before? (redis)", 4,
                         {"Cache Entry Identifier": "${filename}", "Distributed Cache Service": cache["id"]},
                         terminate=("duplicate", "failure"))
    merge = n.processor(pg, "MergeContent", "repack only new blobs", 5,
                        # A bin must close on age only: with a reachable minimum, MergeContent merges a
                        # bin as soon as the minimum is met and nothing is queued, so the small metadata
                        # entries would leave in their own archive before the blobs clear DetectDuplicate.
                        {"Merge Strategy": "Bin-Packing Algorithm", "Merge Format": "TAR",
                         "Correlation Attribute Name": "fragment.identifier", "Attribute Strategy": "Keep Only Common Attributes",
                         "Minimum Number of Entries": "1000000", "Maximum Number of Entries": "1000000",
                         "Max Bin Age": "30 sec", "Maximum number of Bins": "10", "Keep Path": "true"},
                        terminate=("original", "failure"))
    rename = n.processor(pg, "UpdateAttribute", "name the dedup archive", 6,
                         {"filename": "${segment.original.filename:substringBeforeLast('.tar')}-dedup.tar"})
    put = n.processor(pg, "PutFile", "to the diode", 7,
                      # fail, not replace: a second archive with the same name means a bin split, and
                      # silently overwriting the first would lose entries
                      {"Directory": LOW["diode"], "Conflict Resolution Strategy": "fail", "Create Missing Directories": "true"},
                      terminate=("success", "failure"))
    n.connect(pg, ls, fetch, ["success"])
    n.connect(pg, fetch, unpack, ["success"])
    n.connect(pg, unpack, route, ["success"])
    n.connect(pg, route, dedupe, ["blob"])
    n.connect(pg, route, merge, ["unmatched"])
    n.connect(pg, dedupe, merge, ["non-duplicate"])
    n.connect(pg, merge, rename, ["merged"])
    n.connect(pg, rename, put, ["success"])
    return pg


def build_high(n, root):
    pg = n.group(root, "high-side (receive)", 600)
    ls = n.processor(pg, "ListFile", "list received archives", 0,
                     {"Input Directory": HIGH["diode"], "File Filter": r"transfer-.*-dedup\.tar", "Minimum File Age": "5 sec",
                      "Recurse Subdirectories": "false"}, schedule="10 sec")
    fetch = n.processor(pg, "FetchFile", "fetch archive", 1,
                        {"Completion Strategy": "Move File", "Move Destination Directory": HIGH["diode"] + "/received"},
                        terminate=("not.found", "permission.denied", "failure"))
    unpack = n.processor(pg, "UnpackContent", "unpack tar", 2, {"Packaging Format": "tar"},
                         terminate=("original", "failure"))
    put = n.processor(pg, "PutFile", "into the store", 3,
                      {"Directory": HIGH["store"] + "/${segment.original.filename:substringBeforeLast('-dedup.tar')}/${path}",
                       "Conflict Resolution Strategy": "replace", "Create Missing Directories": "true"},
                      terminate=("success", "failure"))
    n.connect(pg, ls, fetch, ["success"])
    n.connect(pg, fetch, unpack, ["success"])
    n.connect(pg, unpack, put, ["success"])
    return pg


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="https://localhost:18090")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="nifiadmin1234")
    ap.add_argument("--reset", action="store_true", help="delete existing low/high groups first")
    args = ap.parse_args()
    n = Nifi(args.url, args.user, args.password)
    for _ in range(60):
        try:
            n.login()
            about = n.call("GET", "/flow/about")["about"]
            break
        except Exception:  # noqa: BLE001 - NiFi still starting
            time.sleep(5)
    else:
        raise SystemExit("NiFi not reachable")
    print(f"NiFi {about['version']} at {args.url}")
    root = n.root()
    for name in ("low-side (send)", "high-side (receive)"):
        g = n.group_named(root, name)
        if g and args.reset:
            n.remove(g)
            print(f"removed {name}")
        elif g:
            raise SystemExit(f"{name} exists; use --reset to rebuild")
    low, high = build_low(n, root), build_high(n, root)
    n.start(low)
    n.start(high)
    print(f"started low-side {low} and high-side {high}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
