#!/usr/bin/python3
"""Capture Splunk configuration via the management REST API into a raw tree.

Primary use case: reverse-engineer an unknown production Splunk (API only —
no Docker, no host mounts, no HashiCorp Vault) so the estate can be recreated
in a test environment.

Produces the same layout as the container-based capture path so
splunk_config_export.py can scrub + classify it:

  <raw>/<label>/
    _capture.json
    estate_inventory.json   # human/machine summary for recreation planning
    RECREATE.md             # operator checklist: what to stand up in the lab
    etc/
      system/local/*.conf
      apps/<app>/local/*.conf
      apps/<app>/local/data/ui/views/*.xml

Uses only the stdlib (urllib). Talks to splunkd on the management port
(typically :8089) — not the web UI reverse-proxy.

Semantic note: REST configs/conf-* returns *effective* configuration for the
app context (defaults + local + inherited), not a pure on-disk local/ layer.
That is what you want when nobody knows how production was configured and the
goal is "make a test box that behaves like it".
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Content keys that are REST metadata, never conf settings.
META_KEYS = frozenset({
    "disabled", "eai:type", "eai:acl", "eai:attributes", "eai:userName",
    "eai:appName", "eai:digest", "eai:data",
})

# Surface (conf lists / stock apps) lives in Ansible defaults — see
# roles/splunk_config/defaults/main.yml and README "Extending the capture surface".
_SURFACE_MOD = None


def _surface_mod():
    global _SURFACE_MOD
    if _SURFACE_MOD is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "splunk_config_surface.py")
        spec = importlib.util.spec_from_file_location("splunk_config_surface", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        _SURFACE_MOD = mod
    return _SURFACE_MOD


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


class SplunkClient:
    def __init__(self, base_url: str, username: str, password: str, verify_tls: bool):
        self.base = base_url.rstrip("/")
        self.auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        if verify_tls:
            self.ctx = ssl.create_default_context()
        else:
            self.ctx = ssl._create_unverified_context()  # noqa: S323 — operator opt-in

    def get_json(self, path: str, params: dict | None = None) -> dict:
        qs = urllib.parse.urlencode(params or {})
        url = f"{self.base}{path}"
        if qs:
            url = f"{url}?{qs}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Basic {self.auth}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"GET {path} -> HTTP {err.code}: {body}") from err

    def get_json_ok(self, path: str, params: dict | None = None) -> dict | None:
        try:
            return self.get_json(path, params)
        except RuntimeError as err:
            if "HTTP 404" in str(err) or "HTTP 403" in str(err):
                return None
            raise


def _write_conf(path: str, stanzas: dict[str, dict[str, str]]) -> int:
    """Write a .conf file from stanza→kv map. Returns key count written."""
    if not stanzas:
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines: list[str] = []
    keys = 0
    for stanza in sorted(stanzas):
        lines.append(f"[{stanza}]")
        for key in sorted(stanzas[stanza]):
            val = stanzas[stanza][key]
            lines.append(f"{key} = {val}")
            keys += 1
        lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    os.chmod(path, 0o600)
    return keys


def _entry_kv(content: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in content.items():
        if key in META_KEYS or key.startswith("eai:"):
            continue
        if val is None:
            continue
        if isinstance(val, bool):
            out[key] = "1" if val else "0"
        elif isinstance(val, (int, float)):
            out[key] = str(val)
        elif isinstance(val, str):
            out[key] = val
        # skip lists/dicts — not conf-native
    return out


def _entry_acl_app(entry: dict) -> str:
    acl = entry.get("acl") or {}
    if isinstance(acl, dict) and acl.get("app"):
        return str(acl["app"])
    content = entry.get("content") or {}
    eai = content.get("eai:acl") or {}
    if isinstance(eai, dict) and eai.get("app"):
        return str(eai["app"])
    return ""


def harvest_conf(
    client: SplunkClient,
    owner: str,
    app: str,
    conf_name: str,
    *,
    owner_app: str | None = None,
) -> dict[str, dict[str, str]]:
    """Return stanza→kv for one conf file.

    When owner_app is set, only stanzas whose ACL app matches are kept —
    that drops inherited product defaults visible through the REST merge.
    """
    data = client.get_json_ok(
        f"/servicesNS/{urllib.parse.quote(owner)}/{urllib.parse.quote(app)}"
        f"/configs/conf-{urllib.parse.quote(conf_name)}",
        {"output_mode": "json", "count": "0"},
    )
    if not data:
        return {}
    want = owner_app
    stanzas: dict[str, dict[str, str]] = {}
    for entry in data.get("entry", []):
        name = entry.get("name") or entry.get("title") or ""
        if not name or name.startswith("_"):
            continue
        if want is not None and _entry_acl_app(entry) != want:
            continue
        kv = _entry_kv(entry.get("content") or {})
        if kv:
            stanzas[name] = kv
    return stanzas


def harvest_views(client: SplunkClient, owner: str, app: str, dest_dir: str) -> int:
    """Write view XML for views owned by this app only (acl.app == app)."""
    data = client.get_json_ok(
        f"/servicesNS/{urllib.parse.quote(owner)}/{urllib.parse.quote(app)}/data/ui/views",
        {"output_mode": "json", "count": "0"},
    )
    if not data:
        return 0
    count = 0
    views_dir = os.path.join(dest_dir, "local", "data", "ui", "views")
    for entry in data.get("entry", []):
        name = entry.get("name") or ""
        if not name or name.startswith("_"):
            continue
        if _entry_acl_app(entry) != app:
            continue
        content = entry.get("content") or {}
        xml = content.get("eai:data") or content.get("data") or ""
        if not xml or not isinstance(xml, str):
            continue
        os.makedirs(views_dir, exist_ok=True)
        path = os.path.join(views_dir, f"{name}.xml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(xml if xml.endswith("\n") else xml + "\n")
        os.chmod(path, 0o600)
        count += 1
    return count

def list_apps(client: SplunkClient) -> list[dict[str, Any]]:
    data = client.get_json("/services/apps/local", {"output_mode": "json", "count": "0"})
    apps = []
    for entry in data.get("entry", []):
        name = entry.get("name") or ""
        if not name:
            continue
        content = entry.get("content") or {}
        apps.append({
            "name": name,
            "disabled": bool(content.get("disabled")),
            "version": content.get("version") or "",
            "label": content.get("label") or name,
        })
    return sorted(apps, key=lambda a: a["name"].lower())


def server_info(client: SplunkClient) -> dict[str, Any]:
    data = client.get_json("/services/server/info", {"output_mode": "json"})
    entries = data.get("entry") or []
    if not entries:
        return {}
    return entries[0].get("content") or {}


def _stanza_names(client: SplunkClient, conf_name: str, app: str = "system") -> list[str]:
    stanzas = harvest_conf(client, "nobody", app, conf_name, owner_app=app)
    return sorted(stanzas.keys())


def build_estate_inventory(
    client: SplunkClient,
    info: dict[str, Any],
    apps: list[dict[str, Any]],
    app_summaries: list[dict[str, Any]],
    base_url: str,
) -> dict[str, Any]:
    """Machine-readable summary used to plan a recreation in another environment."""
    roles = info.get("server_roles") or info.get("role") or []
    if isinstance(roles, str):
        roles = [roles]

    indexes = _stanza_names(client, "indexes")
    # Drop internal noise for the checklist (keep full conf on disk).
    index_names = [i for i in indexes if not i.startswith("_") or i in ("_internal", "_audit")]

    inputs = harvest_conf(client, "nobody", "system", "inputs", owner_app="system")
    input_summary = {
        "stanza_count": len(inputs),
        "http_tokens": sorted(k for k in inputs if k.startswith("http://") or k.startswith("http:")),
        "splunktcp": sorted(k for k in inputs if "splunktcp" in k.lower() or k.startswith("splunktcp")),
        "monitors": sorted(k for k in inputs if k.startswith("monitor://")),
        "scripts": sorted(k for k in inputs if k.startswith("script://")),
    }

    auth = harvest_conf(client, "nobody", "system", "authentication", owner_app="system")
    auth_types = sorted(auth.keys())

    custom_apps = [a for a in app_summaries if not a.get("stock")]
    stock_touched = [a for a in app_summaries if a.get("stock")]

    return {
        "purpose": "recreation_planning",
        "source_api": base_url,
        "product": {
            "version": info.get("version") or "unknown",
            "build": info.get("build") or "",
            "server_name": info.get("serverName") or info.get("host") or "",
            "cpu_arch": info.get("cpu_arch") or "",
            "os_name": info.get("os_name") or "",
            "os_version": info.get("os_version") or "",
            "server_roles": list(roles),
            "license_state": info.get("licenseState") or info.get("license_state") or "",
            "license_keys": info.get("licenseKeys") or [],
            "number_of_cores": info.get("numberOfCores") or info.get("number_of_cores"),
            "physical_memory_mb": info.get("physicalMemoryMB") or info.get("physical_memory_mb"),
        },
        "apps": {
            "installed": apps,
            "custom_exported": custom_apps,
            "stock_with_local_overrides": stock_touched,
        },
        "indexes": {
            "names": index_names,
            "count": len(index_names),
        },
        "inputs": input_summary,
        "authentication_stanzas": auth_types,
        "outputs_stanzas": _stanza_names(client, "outputs"),
        "server_conf_stanzas": _stanza_names(client, "server")[:40],
    }


def write_recreate_md(path: str, inv: dict[str, Any]) -> None:
    """Operator checklist: stand up a test instance that mirrors production."""
    product = inv.get("product") or {}
    apps = inv.get("apps") or {}
    custom = apps.get("custom_exported") or []
    indexes = (inv.get("indexes") or {}).get("names") or []
    inputs = inv.get("inputs") or {}
    lines = [
        "# Recreate this Splunk estate in a test environment",
        "",
        "Generated by `splunk_config` API export. No Docker access and no Vault",
        "were required to produce this snapshot — only the management REST API.",
        "",
        "## Source",
        "",
        f"- API: `{inv.get('source_api', '')}`",
        f"- Server name: `{product.get('server_name', '')}`",
        f"- Version: **{product.get('version', 'unknown')}** (build {product.get('build', '?')})",
        f"- Roles: `{', '.join(product.get('server_roles') or []) or 'n/a'}`",
        f"- OS: {product.get('os_name', '')} {product.get('os_version', '')} / {product.get('cpu_arch', '')}",
        f"- License state: {product.get('license_state', '') or 'unknown'}",
        "",
        "## What this snapshot contains",
        "",
        "- Scrubbed conf bundles under the snapshot directory (safe for git).",
        "- Secrets (if any were captured) as **flat files** under the role",
        "  `files/secrets/<stack>/` — not for git.",
        "- Conf values that looked like secrets are placeholders",
        "  (`<SCRUBBED:secrets>`).",
        "",
        "## Rebuild checklist",
        "",
        "1. **Install the same major/minor Splunk version**",
        f"   - Target: `{product.get('version', 'unknown')}`",
        "   - Lab path: `roles/splunk_docker` (all-in-one) or your work image.",
        "2. **Restore scrubbed config**",
        "   - Copy `instance-*/system-local/*.conf` → `$SPLUNK_HOME/etc/system/local/`",
        "   - Copy each `instance-*/apps/<name>/` → `$SPLUNK_HOME/etc/apps/<name>/`",
        "   - Or re-run `splunk_config` with `--tags apply` against a lab container.",
        "3. **Re-seed secrets** from the flat-file secrets dir (or re-enter via UI).",
        "4. **Install custom apps** present in production:",
    ]
    if custom:
        for app in custom:
            lines.append(f"   - `{app.get('name')}` ({app.get('files', 0)} local files exported)")
    else:
        lines.append("   - *(none detected beyond stock apps with local overrides)*")
    lines += [
        "5. **Indexes to expect** (from API; create or let conf create them):",
    ]
    if indexes:
        for name in indexes[:50]:
            lines.append(f"   - `{name}`")
        if len(indexes) > 50:
            lines.append(f"   - … and {len(indexes) - 50} more (see estate_inventory.json)")
    else:
        lines.append("   - *(none listed — check system-local/indexes.conf)*")
    lines += [
        "6. **Inputs surface** (stanza counts / kinds):",
        f"   - Total input stanzas: {inputs.get('stanza_count', 0)}",
        f"   - HEC / http: {', '.join(inputs.get('http_tokens') or []) or 'none listed'}",
        f"   - splunktcp: {', '.join(inputs.get('splunktcp') or []) or 'none listed'}",
        f"   - monitor://: {len(inputs.get('monitors') or [])} path(s)",
        f"   - script://: {len(inputs.get('scripts') or [])}",
        "7. **Authentication** stanzas: "
        + (", ".join(f'`{s}`' for s in (inv.get('authentication_stanzas') or [])) or "*(see authentication.conf)*"),
        "8. **Outputs** stanzas: "
        + (", ".join(f'`{s}`' for s in (inv.get('outputs_stanzas') or [])) or "*(see outputs.conf)*"),
        "",
        "## Gaps API cannot fill",
        "",
        "- Index **bucket data** and kvstore **data** (definitions only).",
        "- License key files (re-apply your lab license).",
        "- Plaintext of scrubbed passwords / HEC tokens (re-issue in the lab).",
        "- Exact on-disk `local/` vs `default/` layering (API is effective config).",
        "- Anything only reachable on nodes without management-port access.",
        "",
        "## Next step in the lab",
        "",
        "```bash",
        "# After standing up a blank Splunk of the same version:",
        "ansible-playbook … --tags apply \\",
        "  -e splunk_config_secrets_backend=file",
        "```",
        "",
        "Or manually merge the scrubbed conf tree into `$SPLUNK_HOME/etc` and restart.",
        "",
    ]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def export_api(
    base_url: str,
    username: str,
    password: str,
    raw_inst_dir: str,
    *,
    label: str,
    verify_tls: bool,
    include_disabled_apps: bool,
    surface: dict[str, Any] | None = None,
) -> dict[str, Any]:
    surf_mod = _surface_mod()
    surface = surface or surf_mod.load_surface(None)
    conf_files = list(surface.get("conf_files") or [])
    stock_conf_files = list(surface.get("stock_conf_files") or [])
    views_custom = bool(surface.get("capture_views_for_custom_apps", True))
    views_stock = bool(surface.get("capture_views_for_stock_apps", False))

    client = SplunkClient(base_url, username, password, verify_tls)
    info = server_info(client)
    version = info.get("version") or "unknown"
    _log(f"connected to {base_url} (splunk {version})")
    _log(f"surface: {len(conf_files)} conf_files, {len(stock_conf_files)} stock_conf_files")

    etc = os.path.join(raw_inst_dir, "etc")
    os.makedirs(etc, exist_ok=True)

    # ── system/local (only ACL-owned by system) ─────────────────────────────
    sys_local = os.path.join(etc, "system", "local")
    sys_files = 0
    for conf_name in conf_files:
        stanzas = harvest_conf(
            client, "nobody", "system", conf_name, owner_app="system",
        )
        if not stanzas:
            continue
        written = _write_conf(os.path.join(sys_local, f"{conf_name}.conf"), stanzas)
        if written:
            sys_files += 1
            _log(f"  system/local/{conf_name}.conf  ({len(stanzas)} stanzas)")

    # ── apps (only stanzas/views whose acl.app matches the app) ─────────────
    apps = list_apps(client)
    app_summaries = []
    for app in apps:
        name = app["name"]
        if app["disabled"] and not include_disabled_apps:
            continue
        stock = surf_mod.is_stock_app(name, surface)
        # Stock apps: knowledge-object confs only; custom: full surface conf set.
        conf_names = stock_conf_files if stock else conf_files
        app_dir = os.path.join(etc, "apps", name)
        local_dir = os.path.join(app_dir, "local")
        files_written = 0
        for conf_name in conf_names:
            stanzas = harvest_conf(
                client, "nobody", name, conf_name, owner_app=name,
            )
            if not stanzas:
                continue
            # app.conf alone is not operator state worth snapshotting for stock apps.
            if stock and conf_name == "app" and len(stanzas) <= 2:
                continue
            n = _write_conf(os.path.join(local_dir, f"{conf_name}.conf"), stanzas)
            if n:
                files_written += 1
        want_views = views_stock if stock else views_custom
        if want_views:
            views = harvest_views(client, "nobody", name, app_dir)
            if views:
                files_written += views
                _log(f"  apps/{name}: {views} views")
        if files_written:
            app_summaries.append({"name": name, "stock": stock, "files": files_written})
            _log(f"  apps/{name}: {files_written} files (stock={stock})")

    inventory = build_estate_inventory(client, info, apps, app_summaries, base_url)
    inv_path = os.path.join(raw_inst_dir, "estate_inventory.json")
    with open(inv_path, "w", encoding="utf-8") as handle:
        json.dump(inventory, handle, indent=2, default=str)
        handle.write("\n")
    os.chmod(inv_path, 0o600)
    recreate_path = os.path.join(raw_inst_dir, "RECREATE.md")
    write_recreate_md(recreate_path, inventory)
    os.chmod(recreate_path, 0o600)
    _log(f"wrote estate inventory + RECREATE.md → {raw_inst_dir}")

    meta = {
        "service": label,
        "node": "api",
        "role_hint": "standalone",
        "capture_via": "api",
        "api_url": base_url,
        "splunk_version": version,
        "server_name": info.get("serverName") or info.get("host") or "",
        "apps_seen": [a["name"] for a in apps],
        "apps_exported": [a["name"] for a in app_summaries],
        "system_conf_files": sys_files,
        "purpose": "unknown_estate_recreation",
    }
    with open(os.path.join(raw_inst_dir, "_capture.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2)
        handle.write("\n")
    os.chmod(os.path.join(raw_inst_dir, "_capture.json"), 0o600)
    _log(f"wrote raw capture → {raw_inst_dir}  "
         f"(system_confs={sys_files}, apps={len(app_summaries)})")
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="splunkd base URL, e.g. https://host:8089")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--raw-inst", required=True, help="output instance dir (<raw>/<label>)")
    parser.add_argument("--label", default="standalone")
    parser.add_argument("--verify-tls", action="store_true", default=False)
    parser.add_argument("--include-disabled-apps", action="store_true", default=False)
    parser.add_argument(
        "--surface",
        default=None,
        help="JSON surface from Ansible (conf_files, stock apps). "
             "Omit only for unit tests — role always passes this.",
    )
    args = parser.parse_args()
    surface = _surface_mod().load_surface(args.surface)
    meta = export_api(
        args.url,
        args.user,
        args.password,
        args.raw_inst,
        label=args.label,
        surface=surface,
        verify_tls=args.verify_tls,
        include_disabled_apps=args.include_disabled_apps,
    )
    json.dump(meta, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
