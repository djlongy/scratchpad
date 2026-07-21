#!/usr/bin/python3
"""Apply a scrubbed splunk_config snapshot via the management REST API.

Primary use case: take a snapshot produced by splunk_config_api_export.py and
push conf stanzas + views back to a Splunk instance over :8089 (no Docker,
no host mounts). Intended for lab recreate and round-trip validation.

Skips:
  - keys whose values are scrub placeholders (<SCRUBBED:…>)
  - empty values
  - well-known secret files (passwd, splunk.secret) — reseed separately

Scope:
  all          — every instance-*/system-local + apps/*/local conf in the snapshot
  custom_apps  — only non-stock apps under apps/ (safer default for live targets)
  system       — system-local conf only

Uses only the stdlib (urllib). Talks to splunkd management port, not the web UI.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SCRUB_RE = re.compile(r"^<SCRUBBED")
CONF_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")

# Surface (stock apps, forbidden keys) from Ansible defaults — not this file.
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


def is_stock_app(name: str, surface: dict[str, Any] | None = None) -> bool:
    surf = surface or _surface_mod().load_surface(None)
    return _surface_mod().is_stock_app(name, surf)


def is_scrubbed(val: str) -> bool:
    return bool(SCRUB_RE.match((val or "").strip()))


class SplunkClient:
    def __init__(self, base_url: str, username: str, password: str, verify_tls: bool):
        self.base = base_url.rstrip("/")
        self.auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        if verify_tls:
            self.ctx = ssl.create_default_context()
        else:
            self.ctx = ssl._create_unverified_context()  # noqa: S323 — operator opt-in

    def request(
        self,
        method: str,
        path: str,
        form: dict[str, str] | None = None,
        *,
        params: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        qs = dict(params or {})
        qs.setdefault("output_mode", "json")
        url = f"{self.base}{path}?{urllib.parse.urlencode(qs)}"
        body = None
        headers = {
            "Authorization": f"Basic {self.auth}",
            "Accept": "application/json",
        }
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=120) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw.strip():
                    return resp.status, {}
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            try:
                payload: Any = json.loads(raw) if raw.strip() else raw
            except json.JSONDecodeError:
                payload = raw[:400]
            return err.code, payload

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict:
        code, body = self.request("GET", path, params=params)
        if code >= 400:
            raise RuntimeError(f"GET {path} -> HTTP {code}: {body}")
        if not isinstance(body, dict):
            raise RuntimeError(f"GET {path} -> non-JSON body")
        return body


def parse_conf(text: str) -> dict[str, dict[str, str]]:
    """Parse a simple Splunk .conf into stanza → key → value."""
    stanzas: dict[str, dict[str, str]] = {}
    current = "default"
    stanzas.setdefault(current, {})
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = CONF_SECTION_RE.match(line)
        if m:
            current = m.group(1)
            stanzas.setdefault(current, {})
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if key:
            stanzas[current][key] = val
    # Drop empty default if unused.
    if not stanzas.get("default"):
        stanzas.pop("default", None)
    return stanzas


def filter_props(
    props: dict[str, str],
    *,
    skip_forbidden: bool,
    forbidden_keys: set[str] | frozenset[str] | None = None,
) -> tuple[dict[str, str], int]:
    """Drop scrubbed / empty / forbidden keys. Returns (kept, skipped_count)."""
    blocked = forbidden_keys or set()
    kept: dict[str, str] = {}
    skipped = 0
    for key, val in props.items():
        if val is None or str(val).strip() == "":
            skipped += 1
            continue
        if is_scrubbed(str(val)):
            skipped += 1
            continue
        if skip_forbidden and key in blocked:
            skipped += 1
            continue
        kept[key] = str(val)
    return kept, skipped


def _msg_text(body: Any) -> str:
    if isinstance(body, dict):
        msgs = body.get("messages") or []
        if msgs and isinstance(msgs, list):
            parts = []
            for m in msgs:
                if isinstance(m, dict):
                    parts.append(str(m.get("text") or m))
                else:
                    parts.append(str(m))
            return "; ".join(parts)[:300]
        return str(body)[:300]
    return str(body)[:300]


def upsert_stanza(
    client: SplunkClient,
    app: str,
    conf_name: str,
    stanza: str,
    props: dict[str, str],
    *,
    dry_run: bool,
) -> str:
    """Create or update one stanza. Returns status: created|updated|dry_run|error:…"""
    owner = "nobody"
    app_q = urllib.parse.quote(app, safe="")
    conf_q = urllib.parse.quote(conf_name, safe="")
    stanza_q = urllib.parse.quote(stanza, safe="")
    base = f"/servicesNS/{owner}/{app_q}/configs/conf-{conf_q}"

    if dry_run:
        return "dry_run"

    # Prefer update; create on 404.
    code, body = client.request("POST", f"{base}/{stanza_q}", props)
    if code in (200, 201):
        return "updated"
    if code == 404:
        form = {"name": stanza, **props}
        code2, body2 = client.request("POST", base, form)
        if code2 in (200, 201):
            return "created"
        if code2 == 409:
            # Race / exists under alternate encoding — retry update.
            code3, body3 = client.request("POST", f"{base}/{stanza_q}", props)
            if code3 in (200, 201):
                return "updated"
            return f"error:update-after-409 HTTP {code3}: {_msg_text(body3)}"
        return f"error:create HTTP {code2}: {_msg_text(body2)}"
    if code == 409:
        # Exists but update path failed oddly — try create was wrong; re-update.
        return f"error:update HTTP {code}: {_msg_text(body)}"
    return f"error:update HTTP {code}: {_msg_text(body)}"


def upsert_view(
    client: SplunkClient,
    app: str,
    name: str,
    xml: str,
    *,
    dry_run: bool,
) -> str:
    owner = "nobody"
    app_q = urllib.parse.quote(app, safe="")
    name_q = urllib.parse.quote(name, safe="")
    base = f"/servicesNS/{owner}/{app_q}/data/ui/views"
    if dry_run:
        return "dry_run"
    # Update first.
    code, body = client.request("POST", f"{base}/{name_q}", {"eai:data": xml})
    if code in (200, 201):
        return "updated"
    if code == 404:
        code2, body2 = client.request("POST", base, {"name": name, "eai:data": xml})
        if code2 in (200, 201):
            return "created"
        return f"error:create-view HTTP {code2}: {_msg_text(body2)}"
    return f"error:update-view HTTP {code}: {_msg_text(body)}"


def discover_instance_dirs(snapshot_dir: str) -> list[str]:
    out = []
    for name in sorted(os.listdir(snapshot_dir)):
        path = os.path.join(snapshot_dir, name)
        if os.path.isdir(path) and name.startswith("instance-"):
            out.append(path)
    return out


def should_include_app(app: str, scope: str, surface: dict[str, Any]) -> bool:
    if scope == "all":
        return True
    if scope == "system":
        return False
    if scope == "custom_apps":
        return not is_stock_app(app, surface)
    return True


def apply_conf_file(
    client: SplunkClient,
    app: str,
    conf_path: str,
    *,
    dry_run: bool,
    skip_forbidden: bool,
    forbidden_keys: set[str],
    stats: dict[str, int],
    errors: list[str],
) -> None:
    conf_name = os.path.splitext(os.path.basename(conf_path))[0]
    text = open(conf_path, encoding="utf-8", errors="replace").read()
    stanzas = parse_conf(text)
    for stanza, props in stanzas.items():
        kept, skipped = filter_props(
            props, skip_forbidden=skip_forbidden, forbidden_keys=forbidden_keys,
        )
        stats["keys_skipped"] += skipped
        if not kept:
            stats["stanzas_empty"] += 1
            continue
        result = upsert_stanza(
            client, app, conf_name, stanza, kept, dry_run=dry_run,
        )
        if result.startswith("error:"):
            stats["errors"] += 1
            errors.append(f"{app}/{conf_name}.conf [{stanza}]: {result}")
        else:
            stats[result] = stats.get(result, 0) + 1
            stats["stanzas_ok"] += 1


def apply_views(
    client: SplunkClient,
    app: str,
    views_dir: str,
    *,
    dry_run: bool,
    stats: dict[str, int],
    errors: list[str],
) -> None:
    if not os.path.isdir(views_dir):
        return
    for name in sorted(os.listdir(views_dir)):
        if not name.endswith(".xml"):
            continue
        path = os.path.join(views_dir, name)
        xml = open(path, encoding="utf-8", errors="replace").read()
        view_name = name[:-4]
        result = upsert_view(client, app, view_name, xml, dry_run=dry_run)
        if result.startswith("error:"):
            stats["errors"] += 1
            errors.append(f"{app}/views/{view_name}: {result}")
        else:
            stats[result] = stats.get(result, 0) + 1
            stats["views_ok"] += 1


def apply_snapshot(
    client: SplunkClient,
    snapshot_dir: str,
    *,
    scope: str,
    dry_run: bool,
    skip_forbidden: bool,
    max_errors: int,
    surface: dict[str, Any] | None = None,
) -> dict[str, Any]:
    surface = surface or _surface_mod().load_surface(None)
    forbidden = set(surface.get("apply_forbidden_keys") or [])
    stats: dict[str, int] = {
        "stanzas_ok": 0,
        "stanzas_empty": 0,
        "views_ok": 0,
        "keys_skipped": 0,
        "errors": 0,
        "created": 0,
        "updated": 0,
        "dry_run": 0,
        "conf_files": 0,
        "instances": 0,
        "apps": 0,
    }
    errors: list[str] = []
    instances = discover_instance_dirs(snapshot_dir)
    if not instances:
        raise RuntimeError(
            f"No instance-* directories under {snapshot_dir}. "
            "Export a snapshot first."
        )

    for inst in instances:
        stats["instances"] += 1
        sys_local = os.path.join(inst, "system-local")
        if scope in ("all", "system") and os.path.isdir(sys_local):
            for fname in sorted(os.listdir(sys_local)):
                if not fname.endswith(".conf"):
                    continue
                stats["conf_files"] += 1
                apply_conf_file(
                    client, "system", os.path.join(sys_local, fname),
                    dry_run=dry_run, skip_forbidden=skip_forbidden,
                    forbidden_keys=forbidden,
                    stats=stats, errors=errors,
                )
                if stats["errors"] >= max_errors:
                    break

        apps_root = os.path.join(inst, "apps")
        if scope != "system" and os.path.isdir(apps_root):
            for app in sorted(os.listdir(apps_root)):
                if not should_include_app(app, scope, surface):
                    continue
                app_dir = os.path.join(apps_root, app)
                local = os.path.join(app_dir, "local")
                if not os.path.isdir(local):
                    continue
                stats["apps"] += 1
                for root, _dirs, files in os.walk(local):
                    # conf files only at local/ root (not nested metadata)
                    for fname in files:
                        if not fname.endswith(".conf"):
                            continue
                        # only conf at top of local/ (Splunk convention)
                        rel = os.path.relpath(os.path.join(root, fname), local)
                        if os.path.dirname(rel) not in ("", "."):
                            continue
                        stats["conf_files"] += 1
                        apply_conf_file(
                            client, app, os.path.join(root, fname),
                            dry_run=dry_run, skip_forbidden=skip_forbidden,
                            forbidden_keys=forbidden,
                            stats=stats, errors=errors,
                        )
                        if stats["errors"] >= max_errors:
                            break
                    if stats["errors"] >= max_errors:
                        break
                views = os.path.join(local, "data", "ui", "views")
                apply_views(
                    client, app, views,
                    dry_run=dry_run, stats=stats, errors=errors,
                )
                if stats["errors"] >= max_errors:
                    break
        if stats["errors"] >= max_errors:
            break

    return {
        "scope": scope,
        "dry_run": dry_run,
        "snapshot_dir": snapshot_dir,
        "stats": stats,
        "errors": errors[:50],
        "error_count": len(errors),
        "ok": stats["errors"] == 0,
    }


def server_info(client: SplunkClient) -> dict[str, Any]:
    data = client.get_json("/services/server/info", {"output_mode": "json"})
    entries = data.get("entry") or []
    if not entries:
        return {}
    return entries[0].get("content") or {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="splunkd base URL, e.g. https://host:8089")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--snapshot", required=True, help="scrubbed snapshot dir (has instance-*)")
    parser.add_argument(
        "--scope",
        choices=("custom_apps", "system", "all"),
        default="custom_apps",
        help="what to push (default: custom_apps — safest on a live box)",
    )
    parser.add_argument("--verify-tls", action="store_true", default=False)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument(
        "--allow-secret-keys",
        action="store_true",
        default=False,
        help="Do not skip forbidden secret key names (still skips <SCRUBBED> values)",
    )
    parser.add_argument("--max-errors", type=int, default=50)
    parser.add_argument(
        "--surface",
        default=None,
        help="JSON surface from Ansible (stock apps, forbidden keys). "
             "Omit only for unit tests — role always passes this.",
    )
    args = parser.parse_args()

    surface = _surface_mod().load_surface(args.surface)
    client = SplunkClient(args.url, args.user, args.password, args.verify_tls)
    info = server_info(client)
    version = info.get("version") or "unknown"
    _log(f"connected to {args.url} (splunk {version}) scope={args.scope} dry_run={args.dry_run}")

    result = apply_snapshot(
        client,
        args.snapshot,
        scope=args.scope,
        dry_run=args.dry_run,
        skip_forbidden=not args.allow_secret_keys,
        max_errors=args.max_errors,
        surface=surface,
    )
    result["target"] = {"url": args.url, "version": version}
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    stats = result["stats"]
    _log(
        f"done: conf_files={stats['conf_files']} stanzas_ok={stats['stanzas_ok']} "
        f"views_ok={stats['views_ok']} skipped_keys={stats['keys_skipped']} "
        f"errors={stats['errors']}"
    )
    for err in result["errors"][:20]:
        _log(f"  ERR {err}")
    if not result["ok"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
