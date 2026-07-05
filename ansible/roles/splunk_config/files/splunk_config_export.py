#!/usr/bin/python3
"""Scrub + classify a raw Splunk config capture into a committable snapshot.

Runs on the ANSIBLE CONTROL NODE (pure stdlib — no Splunk libraries, no PyYAML).
The companion tasks (roles/splunk_config/tasks/export/*) tar each Splunk
container's `etc/` subtree out of the swarm into a raw capture directory; this
script turns that raw tree into the snapshot the repo actually stores:

  * copies the config-authoritative files (confs, dashboard XML, nav, lookups,
    metadata) into <out>/ under a tier/instance layout,
  * SCRUBS every secret before it lands on disk (splunk.secret, etc/passwd,
    `$1$`/`$6$`/`$7$`-encrypted values, sslPassword, pass4SymmKey, credential
    stanzas, passwords.conf) — recording each redaction,
  * writes <out>/SECRETS-SCRUBBED.md (human index of what was removed),
  * emits the manifest as ONE JSON object on stdout (the caller renders it to
    <out>/manifest.yml). All diagnostics go to stderr so stdout stays pure JSON.

Design principle: read-only against the source, a
deliberate skip-list for stock/secret material, and a self-documenting partial
result (sections absent on this estate are simply omitted, never faked).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys

# ── Repeated literals (S1192) ────────────────────────────────────────────────
LOCAL, DEFAULT, METADATA = "local", "default", "metadata"
APPS_DIR, USERS_DIR, SYSTEM_LOCAL = "apps", "users", os.path.join("system", "local")
SERVERCLASS = "serverclass.conf"
PASSWORDS_CONF = "passwords.conf"  # pragma: allowlist secret  (a filename, not a secret)
SCRUBBED_TOKEN = "<SCRUBBED:vault>"
SRC_VAULT = "vault"
CONF_SUFFIX = ".conf"
UI_VIEWS = os.path.join("local", "data", "ui", "views")

# Splunk-shipped apps: capture only their `local/` overrides, never `default/`
# (that ships with Splunk / the app and is not the operator's declarative state).
# Genuinely custom / third-party apps (not matched here) are captured WHOLE.
STOCK_APP_NAMES = frozenset({
    "search", "launcher", "learned", "legacy", "sample_app", "framework",
    "gettingstarted", "introspection_generator_addon", "appsbrowser",
    "user-prefs", "alert_logevent", "alert_webhook", "splunk_httpinput",
    "SplunkForwarder", "SplunkLightForwarder", "splunk_gdi",
})
STOCK_APP_PREFIXES = ("splunk_", "Splunk_", "DA-ITSI-", "SA-", "TA-")

# Conf keys whose VALUE is always a secret → redacted (case-insensitive match).
SECRET_KEYS = frozenset({
    "sslpassword", "pass4symmkey", "password", "binddnpassword",
    "sslkeysfilepassword", "sslrootcapath_password", "clientsecret",
    "hec_token", "token", "sharedsecret", "federated.password",
})
# Values already Splunk-encrypted (splunk.secret-bound) → redacted regardless of key.
ENCRYPTED_VALUE_RE = re.compile(r"^\$[1-7]\$")
# Stanza header, e.g. "[sslConfig]" or "[credential:...:...]".
STANZA_RE = re.compile(r"^\[(?P<name>.*)\]\s*$")
# key = value (Splunk conf; whitespace around '=' optional).
KV_RE = re.compile(r"^(?P<key>[^=\[\s][^=]*?)\s*=\s*(?P<val>.*)$")

# Files never copied into the snapshot (secret material re-seeded from Vault).
NEVER_COPY_BASENAMES = frozenset({"splunk.secret", "passwd", PASSWORDS_CONF})

OUT_OF_SCOPE_V1 = [
    "kvstore_data",   # collections.conf definitions captured; data deferred
    "index_data",     # indexes.conf definitions captured; bucket data is not config
    "licensing",      # license files/stacks not captured in v1
]


def _log(msg: str) -> None:
    """Diagnostics to stderr — stdout must stay pure JSON for the caller."""
    print(msg, file=sys.stderr)


def is_stock_app(name: str) -> bool:
    """True when the app ships with Splunk (capture local/ only, skip default/)."""
    return name in STOCK_APP_NAMES or name.startswith(STOCK_APP_PREFIXES)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _redact_reason(key: str, value: str) -> bool:
    """Whether a conf key/value pair must be redacted."""
    return key.strip().lower() in SECRET_KEYS or bool(ENCRYPTED_VALUE_RE.match(value.strip()))


def scrub_conf_text(text: str, rel_path: str, scrubbed: list[dict]) -> str:
    """Return conf text with secret values replaced by SCRUBBED_TOKEN, appending
    one record per redaction to `scrubbed`. Stanza context is tracked so the
    record names the [stanza] the key lived under."""
    stanza = ""
    out_lines = []
    for line in text.splitlines():
        header = STANZA_RE.match(line)
        if header:
            stanza = header.group("name")
            out_lines.append(line)
            continue
        pair = KV_RE.match(line)
        if pair and _redact_reason(pair.group("key"), pair.group("val")):
            key = pair.group("key").strip()
            scrubbed.append({"path": rel_path, "stanza": stanza, "key": key, "source": SRC_VAULT})
            out_lines.append(f"{key} = {SCRUBBED_TOKEN}")
        else:
            out_lines.append(line)
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(out_lines) + trailing


def _copy_file(src: str, dst: str, rel_path: str, scrubbed: list[dict]) -> None:
    """Copy one file into the snapshot, scrubbing it first when it is a .conf.

    Uses copyfile (content only) + a normal 0644 mode — NOT copy2 — so Splunk's
    read-only (0444) source modes are not propagated into the committed snapshot.
    The dest is made writable first so a control-plane bundle captured from
    several peers (each carrying the same read-only file) overwrites cleanly."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        os.chmod(dst, 0o644)
    if src.endswith(CONF_SUFFIX):
        text = scrub_conf_text(_read_text(src), rel_path, scrubbed)
        with open(dst, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        shutil.copyfile(src, dst)
    os.chmod(dst, 0o644)


def _copy_tree(src_dir: str, dst_dir: str, rel_base: str, scrubbed: list[dict]) -> int:
    """Recursively copy src_dir → dst_dir, scrubbing confs and skipping secret
    files. Returns the number of files copied."""
    copied = 0
    for root, _dirs, files in os.walk(src_dir):
        for name in files:
            if name in NEVER_COPY_BASENAMES:
                scrubbed.append({"path": os.path.join(rel_base, name),
                                 "stanza": "", "key": name, "source": SRC_VAULT})
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, src_dir)
            _copy_file(src, os.path.join(dst_dir, rel), os.path.join(rel_base, rel), scrubbed)
            copied += 1
    return copied


def count_conf_stanzas(path: str) -> int:
    """Number of [stanza] headers in a conf file (0 if absent)."""
    if not os.path.isfile(path):
        return 0
    return sum(1 for line in _read_text(path).splitlines() if STANZA_RE.match(line))


def _dashboard_count(app_local_dir: str) -> int:
    views = os.path.join(app_local_dir, "data", "ui", "views")
    if not os.path.isdir(views):
        return 0
    return sum(1 for f in os.listdir(views) if f.endswith(".xml"))


def capture_app(app_src: str, app_dst: str, app_name: str, scrubbed: list[dict]) -> dict:
    """Copy one app into the snapshot per the scope rule (local/ + metadata/ always;
    default/ only for non-stock apps). Returns an app summary for the manifest."""
    whole = not is_stock_app(app_name)
    subdirs = [LOCAL, METADATA] + ([DEFAULT] if whole else [])
    files = 0
    for sub in subdirs:
        src = os.path.join(app_src, sub)
        if os.path.isdir(src):
            files += _copy_tree(src, os.path.join(app_dst, sub),
                                os.path.join(app_name, sub), scrubbed)
    return {
        "name": app_name,
        "whole": whole,
        "files": files,
        "dashboards": _dashboard_count(os.path.join(app_src, LOCAL)),
        "saved_searches": count_conf_stanzas(os.path.join(app_src, LOCAL, "savedsearches.conf")),
    }


def capture_apps_dir(apps_src: str, apps_dst: str, scrubbed: list[dict]) -> list[dict]:
    """Capture every app under an apps/ directory. Returns app summaries (apps
    that contributed no files are dropped — nothing custom to record)."""
    if not os.path.isdir(apps_src):
        return []
    summaries = []
    for app_name in sorted(os.listdir(apps_src)):
        app_src = os.path.join(apps_src, app_name)
        if not os.path.isdir(app_src):
            continue
        summary = capture_app(app_src, os.path.join(apps_dst, app_name), app_name, scrubbed)
        if summary["files"]:
            summaries.append(summary)
    return summaries


def classify_role(etc_dir: str, hint: str) -> str:
    """Best-effort Splunk role from system/local/server.conf, falling back to the
    Ansible-supplied hint. Reads [clustering] mode and [shclustering]."""
    server_conf = os.path.join(etc_dir, SYSTEM_LOCAL, "server.conf")
    if not os.path.isfile(server_conf):
        return hint or "standalone"
    mode = _clustering_mode(_read_text(server_conf))
    return mode or hint or "standalone"


def _clustering_mode(text: str) -> str:
    """Map server.conf clustering stanzas to a role name, or '' if none."""
    stanza = ""
    shc = False
    cluster = ""
    for line in text.splitlines():
        header = STANZA_RE.match(line)
        if header:
            stanza = header.group("name")
            if stanza == "shclustering":
                shc = True
            continue
        pair = KV_RE.match(line)
        if pair and stanza == "clustering" and pair.group("key").strip() == "mode":
            cluster = pair.group("val").strip()
    if cluster == "manager" or cluster == "master":
        return "cluster_manager"
    if cluster in ("peer", "slave"):
        return "indexer"
    if shc or cluster == "searchhead":
        return "search_head"
    return ""


def _staging(etc_dir: str, rel: str) -> str:
    return os.path.join(etc_dir, rel)


def capture_instance(inst_dir: str, out_dir: str, manifest: dict, scrubbed: list[dict]) -> None:
    """Process one captured instance directory: classify it, capture its
    control-plane staging dirs (if any), its local apps, system/local, and users."""
    meta = _instance_meta(inst_dir)
    etc = os.path.join(inst_dir, "etc")
    role = classify_role(etc, meta.get("role_hint", ""))
    _record_wellknown_secrets(etc, scrubbed)
    _capture_control_planes(etc, out_dir, manifest, scrubbed)
    key = f"instance-{role}-{meta.get('service', 'unknown')}"
    apps = capture_apps_dir(os.path.join(etc, APPS_DIR), os.path.join(out_dir, key, APPS_DIR), scrubbed)
    system_local = _capture_system_local(etc, os.path.join(out_dir, key), scrubbed)
    users = _capture_users(etc, os.path.join(out_dir, key, USERS_DIR), scrubbed)
    manifest["tiers"]["instances"].append({
        "role": role,
        "service": meta.get("service"),
        "node": meta.get("node"),
        "system_local": system_local,
        "apps": [a["name"] for a in apps],
        "app_detail": apps,
        "users": users,
    })


# Well-known secret files that live OUTSIDE the captured config dirs (never
# copied into the snapshot; re-seeded from Vault on apply). Recorded here so the
# scrubbed index documents them. (rel-path, note)
WELLKNOWN_SECRETS = (
    ("passwd", "local users re-seeded from Vault (usernames+roles captured only)"),
    (os.path.join("auth", "splunk.secret"),
     "splunk.secret restored from Vault to keep encrypted values valid"),
)


def _record_wellknown_secrets(etc: str, scrubbed: list[dict]) -> None:
    for rel, note in WELLKNOWN_SECRETS:
        if os.path.isfile(os.path.join(etc, rel)):
            scrubbed.append({"path": rel, "stanza": "", "key": os.path.basename(rel),
                             "source": SRC_VAULT, "note": note})


def _instance_meta(inst_dir: str) -> dict:
    path = os.path.join(inst_dir, "_capture.json")
    if os.path.isfile(path):
        return json.loads(_read_text(path))
    return {}


def _capture_system_local(etc: str, dst_key_dir: str, scrubbed: list[dict]) -> list[str]:
    src = _staging(etc, SYSTEM_LOCAL)
    if not os.path.isdir(src):
        return []
    _copy_tree(src, os.path.join(dst_key_dir, "system-local"), SYSTEM_LOCAL, scrubbed)
    return sorted(f for f in os.listdir(src) if f.endswith(CONF_SUFFIX))


def _capture_users(etc: str, dst_dir: str, scrubbed: list[dict]) -> list[str]:
    src = os.path.join(etc, USERS_DIR)
    if not os.path.isdir(src):
        return []
    _copy_tree(src, dst_dir, USERS_DIR, scrubbed)
    return sorted(u for u in os.listdir(src) if os.path.isdir(os.path.join(src, u)))


# Control-plane staging dirs → the manifest tier they populate.
CONTROL_PLANES = (
    ("manager-apps", "cluster_manager"),
    (os.path.join("shcluster", "apps"), "shc_deployer"),
    ("deployment-apps", "deployment_server"),
)


def _capture_control_planes(etc: str, out_dir: str, manifest: dict, scrubbed: list[dict]) -> None:
    """Capture manager-apps / shcluster/apps / deployment-apps when present on
    this instance, marking the matching manifest tier present."""
    for rel, tier in CONTROL_PLANES:
        src = _staging(etc, rel)
        if not os.path.isdir(src):
            continue
        tier_key = rel.replace(os.sep, "-")
        apps = capture_apps_dir(src, os.path.join(out_dir, tier_key), scrubbed)
        entry = manifest["tiers"][tier]
        entry["present"] = True
        entry["apps"] = [a["name"] for a in apps]
        entry["app_detail"] = apps
        if tier == "deployment_server":
            entry["serverclasses"] = _serverclasses(etc)


def _serverclasses(etc: str) -> list[str]:
    path = os.path.join(etc, SYSTEM_LOCAL, SERVERCLASS)
    names = []
    if not os.path.isfile(path):
        return names
    for line in _read_text(path).splitlines():
        header = STANZA_RE.match(line)
        if header and header.group("name").startswith("serverClass:"):
            names.append(header.group("name").split(":", 2)[1])
    return sorted(set(names))


def _write_secrets_index(out_dir: str, scrubbed: list[dict]) -> None:
    lines = [
        "# Secrets scrubbed from this snapshot",
        "",
        "Every entry below was removed BEFORE the snapshot was written and is "
        "re-seeded from HashiCorp Vault on apply. None of these values is in git.",
        "",
        "| Path | Stanza | Key | Re-seed source |",
        "|------|--------|-----|----------------|",
    ]
    for item in scrubbed:
        lines.append(f"| `{item['path']}` | {item['stanza'] or '—'} | "
                     f"`{item['key']}` | {item['source']} |")
    lines.append("")
    with open(os.path.join(out_dir, "SECRETS-SCRUBBED.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _empty_manifest(source_stack: str, splunk_version: str) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "meta": {
            "captured_at": now,
            "source_stack": source_stack,
            "splunk_version": splunk_version,
            "tiers_detected": [],
            "skipped": [],
            "counts": {},
        },
        "tiers": {
            "cluster_manager": {"present": False},
            "shc_deployer": {"present": False},
            "deployment_server": {"present": False},
            "instances": [],
        },
        "scrubbed_secrets": [],
        "out_of_scope_v1": list(OUT_OF_SCOPE_V1),
    }


def _finalise(manifest: dict, scrubbed: list[dict]) -> None:
    """Fill meta.tiers_detected / meta.counts from the captured content."""
    tiers = manifest["tiers"]
    detected = [name for name in ("cluster_manager", "shc_deployer", "deployment_server")
                if tiers[name].get("present")]
    detected += sorted({inst["role"] for inst in tiers["instances"]})
    # dedupe, preserving first-seen order
    manifest["meta"]["tiers_detected"] = list(dict.fromkeys(detected))
    dashboards = sum(a.get("dashboards", 0)
                     for inst in tiers["instances"] for a in inst.get("app_detail", []))
    apps = len({a for inst in tiers["instances"] for a in inst.get("apps", [])})
    manifest["scrubbed_secrets"] = scrubbed
    manifest["meta"]["counts"] = {
        "instances": len(tiers["instances"]),
        "apps": apps,
        "dashboards": dashboards,
        "scrubbed_secrets": len(scrubbed),
    }


def run(raw_dir: str, out_dir: str, source_stack: str, splunk_version: str) -> dict:
    """Capture every instance under raw_dir into out_dir; return the manifest."""
    manifest = _empty_manifest(source_stack, splunk_version)
    scrubbed: list[dict] = []
    os.makedirs(out_dir, exist_ok=True)
    for label in sorted(os.listdir(raw_dir)):
        inst_dir = os.path.join(raw_dir, label)
        if os.path.isdir(inst_dir) and os.path.isdir(os.path.join(inst_dir, "etc")):
            _log(f"capturing instance: {label}")
            capture_instance(inst_dir, out_dir, manifest, scrubbed)
    _finalise(manifest, scrubbed)
    _write_secrets_index(out_dir, scrubbed)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True, help="raw capture directory (per-instance etc/ trees)")
    parser.add_argument("--out", required=True, help="snapshot output directory")
    parser.add_argument("--source-stack", default="splunk", help="stack name for the manifest")
    parser.add_argument("--splunk-version", default="unknown", help="Splunk version for the manifest")
    args = parser.parse_args()
    manifest = run(args.raw, args.out, args.source_stack, args.splunk_version)
    json.dump(manifest, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
