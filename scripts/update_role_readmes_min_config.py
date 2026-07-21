#!/usr/bin/env python3
"""Insert/update ## Minimum configuration in role READMEs per ansible-design skill.

Rules:
- Place between Key variables and Usage
- group_vars/<inventory_group>.yml by default
- All Required vars; When-X only when example enables them (skip When-X by default
  unless --include-when)
- Secrets as {{ vault_* }} refs when name looks secret-ish
- Never invent vars; only from Key variables table
- Omit section if no Required rows
- Do not touch examples/*/README.md (only roles/*/README.md)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_RE = re.compile(
    r"^\|\s*(?:\*\*)?Required(?:\*\*)?\s*\|\s*`([^`]+)`\s*\|",
    re.MULTILINE,
)
# Also catch "Required (per entry)" style — skip those for top-level min config
WHEN_RE = re.compile(
    r"^\|\s*(?:\*\*)?When\s+([^|*]+?)(?:\*\*)?\s*\|\s*`([^`]+)`\s*\|",
    re.MULTILINE,
)
SECRETISH = re.compile(
    r"(password|secret|token|key|passphrase|credential|bind_dn_password)",
    re.I,
)


def inventory_group(role_name: str) -> str:
    # Conventional group name: pluralish or role_hosts
    return f"{role_name}_hosts"


def placeholder_for(var: str) -> str:
    if SECRETISH.search(var):
        # vault_secret_<role>_<leaf> style ref
        leaf = var
        for prefix in (
            "freeipa_server_",
            "freeipa_client_",
            "hashicorp_vault_",
            "vcenter_svc_",
            "vcenter_",
            "splunk_forwarder_",
            "mattermost_",
            "certificate_authority_",
            "ssh_agent_key_",
            "vault_pki_",
            "stroom_",
            "swarm_stack_",
            "yum_repos_",
            "nfs_client_",
            "semaphore_",
            "artifactory_",
            "vsphere_vm_",
            "freeipa_server_docker_",
        ):
            if leaf.startswith(prefix):
                leaf = leaf[len(prefix) :]
                break
        return f'"{{{{ vault_secret_{leaf} }}}}"'
    if var.endswith("_domain") or var == "domain":
        return "example.internal"
    if "url" in var.lower() or var.endswith("_addr"):
        return '"https://service.example.internal"'
    if "path" in var.lower() or var.endswith("_mount"):
        return f'"/opt/{var.split("_")[0]}"'
    if "nodes" in var.lower() or var.endswith("_hosts"):
        return f'"{{{{ groups[\'{inventory_group(var.split("_")[0])}\'] }}}}"'
    if var.endswith("_server") or "hostname" in var.lower() or var.endswith("_fqdn"):
        return "service.example.internal"
    if var.endswith("_port"):
        return "8200"
    if "template" in var.lower():
        return "/path/to/compose.yml.j2"
    if "package_url" in var.lower():
        return '"https://mirror.example.internal/package.tgz"'
    if "datacenter" in var.lower() or "cluster" in var.lower():
        return "DC1"
    if "content" in var.lower() and "key" in var.lower():
        return '"{{ vault_secret_ssh_private_key }}"'
    return f'"REPLACE_ME_{var}"'


def extract_required_vars(text: str) -> list[str]:
    vars_: list[str] = []
    for m in REQUIRED_RE.finditer(text):
        raw = m.group(1).strip()
        # skip composite cells like "a / b / c" or "a or b"
        if " or " in raw or " / " in raw:
            # take first backticked-looking token only if single
            parts = re.split(r"\s+(?:or|/)\s+", raw)
            for p in parts:
                p = p.strip().strip("`")
                if p and " " not in p and not p.startswith("{"):
                    vars_.append(p)
            continue
        if " " in raw and not raw.startswith("{"):
            # e.g. "domain (→ freeipa_server_domain)"
            m2 = re.search(r"`?([a-zA-Z0-9_]+)`?", raw)
            if m2:
                vars_.append(m2.group(1))
            continue
        vars_.append(raw)
    # dedupe preserve order
    seen = set()
    out = []
    for v in vars_:
        if v not in seen and not v.startswith("("):
            seen.add(v)
            out.append(v)
    return out


def build_min_config(role: str, required: list[str]) -> str:
    group = inventory_group(role)
    lines = [
        "## Minimum configuration",
        "",
        f"```yaml",
        f"# group_vars/{group}.yml",
        "---",
        "# Required",
    ]
    for v in required:
        lines.append(f"{v}: {placeholder_for(v)}")
    lines.extend(["```", ""])
    return "\n".join(lines)


def process_readme(path: Path, dry_run: bool = False) -> str:
    text = path.read_text(encoding="utf-8")
    if "/examples/" in str(path):
        return "skip-examples"

    # Only role root README
    if path.name != "README.md":
        return "skip"

    if "## Key variables" not in text or "## Usage" not in text:
        return "skip-structure"

    required = extract_required_vars(text)
    # Only count Required rows in the Key variables section
    kv_start = text.find("## Key variables")
    usage_start = text.find("## Usage")
    if kv_start < 0 or usage_start < 0 or usage_start < kv_start:
        return "skip-order"
    kv_block = text[kv_start:usage_start]
    required = extract_required_vars(kv_block)

    if not required:
        # Ensure no stale Minimum configuration if present with empty need
        if "## Minimum configuration" in text:
            # remove empty/stale section between Key vars and Usage
            new = re.sub(
                r"\n## Minimum configuration\n.*?(?=\n## Usage\n)",
                "\n",
                text,
                count=1,
                flags=re.DOTALL,
            )
            if new != text and not dry_run:
                path.write_text(new, encoding="utf-8")
            return "removed-empty" if new != text else "ok-no-required"
        return "ok-no-required"

    section = build_min_config(path.parent.name, required)

    if "## Minimum configuration" in text:
        new = re.sub(
            r"\n## Minimum configuration\n.*?(?=\n## Usage\n)",
            "\n" + section.rstrip() + "\n",
            text,
            count=1,
            flags=re.DOTALL,
        )
        action = "updated"
    else:
        # insert before ## Usage
        new = text[:usage_start] + section + text[usage_start:]
        action = "inserted"

    if new != text and not dry_run:
        path.write_text(new, encoding="utf-8")
    return action if new != text else "unchanged"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("roles_root", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    root = args.roles_root
    if not root.is_dir():
        print(f"not a dir: {root}", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    for readme in sorted(root.glob("*/README.md")):
        status = process_readme(readme, dry_run=args.dry_run)
        counts[status] = counts.get(status, 0) + 1
        print(f"{status:16} {readme.parent.name}")

    print("---")
    for k, v in sorted(counts.items()):
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
