#!/usr/bin/python3
"""Load the operator-facing API/scrub surface for splunk_config engines.

The surface defines *what* is harvested or applied (conf names, stock apps,
forbidden keys). Ansible materialises it from defaults/main.yml; Python never
owns the authoritative list — only a built-in fallback for unit tests / bare CLI.

See roles/splunk_config/README.md → "Extending the capture surface".
"""
from __future__ import annotations

import json
import os
from typing import Any

# Fallback when --surface is omitted (unit tests, ad-hoc CLI). Keep aligned with
# defaults/main.yml — Ansible is authoritative in real runs.
_DEFAULT: dict[str, Any] = {
    "version": 1,
    "conf_files": [
        "server", "web", "indexes", "inputs", "outputs", "props", "transforms",
        "authentication", "authorize", "limits", "distsearch", "collections",
        "alert_actions", "savedsearches", "eventtypes", "tags", "fields",
        "macros", "workflow_actions", "health", "serverclass", "ui-prefs",
        "app", "audit",
    ],
    "stock_conf_files": [
        "savedsearches", "eventtypes", "tags", "macros", "props", "transforms",
        "inputs", "indexes", "app", "ui-prefs", "workflow_actions",
    ],
    "stock_app_names": [
        "search", "launcher", "learned", "legacy", "sample_app", "framework",
        "gettingstarted", "introspection_generator_addon", "appsbrowser",
        "user-prefs", "alert_logevent", "alert_webhook", "splunk_httpinput",
        "SplunkForwarder", "SplunkLightForwarder", "splunk_gdi",
        "splunk_enterprise_on_docker", "journald_input", "audit_trail",
        "splunk_archiver", "splunk_ingest_actions", "splunk_internal_metrics",
    ],
    "stock_app_prefixes": [
        "splunk_", "Splunk_", "DA-ITSI-", "SA-", "TA-",
        "splunk-", "SplunkDeployment",
    ],
    "apply_forbidden_keys": [
        "sslPassword", "pass4SymmKey", "password", "auth_password",
        "bindDNpassword", "secret", "token", "accessKey", "secret_key",
    ],
    "capture_views_for_custom_apps": True,
    "capture_views_for_stock_apps": False,
}


def load_surface(path: str | None = None) -> dict[str, Any]:
    """Load surface JSON from path, or return a copy of the built-in default."""
    if not path:
        return json.loads(json.dumps(_DEFAULT))
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"surface file must be a JSON object: {path}")
    # Fill any missing keys from default so partial surfaces still work.
    merged = json.loads(json.dumps(_DEFAULT))
    for key, val in data.items():
        if key.startswith("_"):
            continue
        merged[key] = val
    return merged


def is_stock_app(name: str, surface: dict[str, Any]) -> bool:
    names = set(surface.get("stock_app_names") or [])
    prefixes = tuple(surface.get("stock_app_prefixes") or ())
    return name in names or name.startswith(prefixes)


def conf_files_for_app(app: str, surface: dict[str, Any], *, stock: bool | None = None) -> list[str]:
    if stock is None:
        stock = is_stock_app(app, surface)
    if stock:
        return list(surface.get("stock_conf_files") or [])
    return list(surface.get("conf_files") or [])


def surface_path_next_to_script() -> str:
    """Optional committed surface beside this module (rarely used)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_surface.json")
