#!/usr/bin/python3
"""Export live vSphere state into re-importable Ansible dictionaries.

Runs on the CONTROL NODE (delegate_to: localhost). Read-only pyVmomi against
vCenter (no ESXi SSH). Emits ONE JSON object on stdout:

  meta              — source, counts, skipped, import hints
  vcenter_config    — topology apply contract (roles/vcenter --tags topology)
  vcenter_esxi_hosts— host list for day-2 gates (vcenter_esxi_hosts)
  esxi_host_configs — per-host map of esxi_* vars (roles/esxi host_vars)

Environment: VCENTER_HOST, VCENTER_USER, VCENTER_PASS,
             VCENTER_VALIDATE_CERTS (default "false").

NO secrets are read or emitted.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from typing import Any, Callable

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

ROLE_DENYLIST = {
    "Admin", "ReadOnly", "NoAccess", "NoCryptographyAdmin", "View", "Anonymous",
    "VirtualMachinePowerUser", "VirtualMachineUser", "ResourcePoolAdministrator",
    "DatastoreConsumer", "NetworkAdministrator", "Tagging Admin",
}
PORTGROUP_DENYLIST = {"-DVUplinks-"}
FOLDER_DENYLIST = {
    "Discovered virtual machine",
    "vCLS",
    "Virtual Machines",
}
# Stock host port groups — still exportable; denylist only for noise if needed.
_ENV_VCENTER_HOST = "VCENTER_HOST"
_ENV_VALIDATE_CERTS = "VCENTER_VALIDATE_CERTS"

_SKIPPED: list[str] = []


def _safe(section: str, fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - best-effort export
        _SKIPPED.append(f"{section}: {exc}")
        return default


def is_builtin(name: str, denylist: set[str]) -> bool:
    return any(token in name for token in denylist) or name in denylist


def shape_cluster(obj: Any) -> dict:
    cfg = obj.configurationEx
    return {
        "name": obj.name,
        "ha_enabled": bool(cfg.dasConfig.enabled),
        "drs_enabled": bool(cfg.drsConfig.enabled),
        "drs_default_behaviour": str(cfg.drsConfig.defaultVmBehavior),
        "evc_mode": obj.summary.currentEVCModeKey or "",
    }


def shape_vds(obj: Any) -> dict:
    cfg = obj.config
    return {
        "name": obj.name,
        "version": cfg.productInfo.version,
        "mtu": cfg.maxMtu,
        "uplink_quantity": len(cfg.uplinkPortPolicy.uplinkPortName),
        "discovery_protocol": cfg.linkDiscoveryProtocolConfig.protocol,
        "discovery_operation": cfg.linkDiscoveryProtocolConfig.operation,
    }


def _security_block(prom: Any, mac: Any, forged: Any) -> dict | None:
    """Emit security only when at least one flag is true (import omits = inherit)."""
    p = bool(prom) if prom is not None else False
    m = bool(mac) if mac is not None else False
    f = bool(forged) if forged is not None else False
    if not (p or m or f):
        return None
    return {
        "allow_promiscuous": p,
        "allow_mac_change": m,
        "allow_forged_transmits": f,
    }


def shape_portgroup(obj: Any) -> dict:
    cfg = obj.config
    binding = "static" if cfg.type == "earlyBinding" else "ephemeral"
    vlan_spec = cfg.defaultPortConfig.vlan
    vlan_id = getattr(vlan_spec, "vlanId", None)
    if hasattr(vlan_spec, "vlanId") and isinstance(vlan_id, list):
        parts = []
        for r in vlan_id:
            start = getattr(r, "start", r)
            end = getattr(r, "end", start)
            parts.append(f"{start}-{end}" if start != end else str(start))
        vlan_id = ",".join(parts) if len(parts) > 1 else (parts[0] if parts else "0")
        trunk = True
    else:
        vlan_id = str(vlan_id if vlan_id is not None else 0)
        trunk = False
    entry: dict[str, Any] = {
        "name": obj.name,
        "vds": cfg.distributedVirtualSwitch.name,
        "vlan_id": vlan_id,
        "num_ports": cfg.numPorts,
        "port_binding": binding,
    }
    if trunk:
        entry["vlan_trunk"] = True
    try:
        sec = cfg.defaultPortConfig
        block = _security_block(
            getattr(sec.securityPolicy.allowPromiscuous, "value", None),
            getattr(sec.securityPolicy.macChanges, "value", None),
            getattr(sec.securityPolicy.forgedTransmits, "value", None),
        )
        if block:
            entry["security"] = block
    except Exception:  # noqa: BLE001
        pass
    return entry


def _view(content: Any, vimtype: Any) -> list:
    view_ref = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
    try:
        return list(view_ref.view)
    finally:
        view_ref.Destroy()


def _verified_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_default_certs()
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    return ctx


def _lab_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # NOSONAR
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False  # NOSONAR
    ctx.verify_mode = ssl.CERT_NONE  # NOSONAR
    return ctx


def _ssl_context() -> ssl.SSLContext:
    if os.environ.get(_ENV_VALIDATE_CERTS, "false").lower() == "true":
        return _verified_ssl_context()
    return _lab_ssl_context()


def _connect() -> Any:
    return SmartConnect(
        host=os.environ[_ENV_VCENTER_HOST],
        user=os.environ["VCENTER_USER"],
        pwd=os.environ["VCENTER_PASS"],
        sslContext=_ssl_context(),
    )


def _export_datacenter(content: Any) -> dict:
    datacenters = _view(content, vim.Datacenter)
    return {"name": datacenters[0].name} if datacenters else {}


def _export_clusters(content: Any) -> list[dict]:
    return [shape_cluster(c) for c in _view(content, vim.ClusterComputeResource)]


def _cluster_name_for_host(host: Any) -> str:
    parent = getattr(host, "parent", None)
    while parent is not None:
        if isinstance(parent, vim.ClusterComputeResource):
            return parent.name
        parent = getattr(parent, "parent", None)
    return ""


def _export_hosts(content: Any) -> list[dict]:
    out = []
    for host in _view(content, vim.HostSystem):
        entry: dict[str, Any] = {"hostname": host.name}
        cluster = _cluster_name_for_host(host)
        if cluster:
            entry["cluster"] = cluster
        out.append(entry)
    return out


def _export_vds(content: Any) -> list[dict]:
    return [shape_vds(s) for s in _view(content, vim.dvs.VmwareDistributedVirtualSwitch)]


def _export_portgroups(content: Any, include_stock: bool) -> list[dict]:
    out = []
    for pg in _view(content, vim.dvs.DistributedVirtualPortgroup):
        if not include_stock and is_builtin(pg.name, PORTGROUP_DENYLIST):
            continue
        out.append(shape_portgroup(pg))
    return out


def _export_resource_pools(content: Any) -> list[dict]:
    """Custom-named pools only (skip built-in root/nested name 'Resources')."""
    out = []
    for pool in _view(content, vim.ResourcePool):
        if pool.name == "Resources":
            continue
        owner = getattr(pool, "owner", None)
        cluster = ""
        if isinstance(owner, vim.ClusterComputeResource):
            cluster = owner.name
        elif isinstance(owner, vim.ComputeResource):
            cluster = owner.name
        entry: dict[str, Any] = {"name": pool.name}
        if cluster:
            entry["cluster"] = cluster
        out.append(entry)
    return out


def _walk_inventory_folders(
    folder: Any, folder_type: str, parent: str | None
) -> list[dict]:
    out: list[dict] = []
    for child in getattr(folder, "childEntity", []) or []:
        if not isinstance(child, vim.Folder):
            continue
        name = child.name
        if name in FOLDER_DENYLIST or is_builtin(name, FOLDER_DENYLIST):
            continue
        entry: dict[str, Any] = {
            "name": name,
            "folder_type": folder_type,
        }
        if parent:
            entry["parent"] = parent
        out.append(entry)
        out.extend(_walk_inventory_folders(child, folder_type, parent=name))
    return out


def _export_folders(content: Any) -> list[dict]:
    out: list[dict] = []
    for dc in _view(content, vim.Datacenter):
        type_roots = (
            ("vm", getattr(dc, "vmFolder", None)),
            ("host", getattr(dc, "hostFolder", None)),
            ("datastore", getattr(dc, "datastoreFolder", None)),
            ("network", getattr(dc, "networkFolder", None)),
        )
        for folder_type, root in type_roots:
            if root is None:
                continue
            out.extend(_walk_inventory_folders(root, folder_type, parent=None))
    return out


def _export_vds_hosts(content: Any) -> list[dict]:
    """Host → vmnics attached to each VDS (vmware_dvs_host apply contract)."""
    out: list[dict] = []
    for vds in _view(content, vim.dvs.VmwareDistributedVirtualSwitch):
        cfg = getattr(vds, "config", None)
        if cfg is None:
            continue
        for host_member in cfg.host or []:
            try:
                host_sys = host_member.config.host
                hostname = host_sys.name
                vmnics: list[str] = []
                backing = host_member.config.backing
                specs = getattr(backing, "pnicSpec", None) or []
                for spec in specs:
                    dev = getattr(spec, "pnicDevice", None)
                    if dev:
                        vmnics.append(dev)
                out.append({
                    "esxi_hostname": hostname,
                    "vmnics": vmnics,
                    "vds": vds.name,
                })
            except Exception as exc:  # noqa: BLE001
                _SKIPPED.append(f"vds_hosts/{vds.name}: {exc}")
    return out


def _pnic_key_to_device(host: Any) -> dict[str, str]:
    net = host.config.network
    mapping: dict[str, str] = {}
    for pnic in net.pnic or []:
        mapping[pnic.key] = pnic.device
    return mapping


def _host_vswitch_security(vs: Any) -> dict | None:
    try:
        pol = vs.spec.policy.security
        return _security_block(
            getattr(pol, "allowPromiscuous", None),
            getattr(pol, "macChanges", None),
            getattr(pol, "forgedTransmits", None),
        )
    except Exception:  # noqa: BLE001
        return None


def _export_one_host_config(host: Any) -> dict[str, Any]:
    """Per-host dict matching roles/esxi defaults (esxi_* keys)."""
    net = host.config.network
    pnic_map = _pnic_key_to_device(host)

    vswitches: list[dict] = []
    for vs in net.vswitch or []:
        nics = [pnic_map[k] for k in (vs.pnic or []) if k in pnic_map]
        entry: dict[str, Any] = {
            "name": vs.name,
            "nics": nics,
            "mtu": int(vs.mtu) if vs.mtu else 1500,
        }
        sec = _host_vswitch_security(vs)
        if sec:
            entry["security"] = sec
        # Teaming active/standby if present
        try:
            teaming = vs.spec.policy.nicTeaming
            # activeNic may be device names or pnic keys depending on build
            active_dev = [
                pnic_map.get(a, a) for a in (teaming.nicOrder.activeNic or [])
            ]
            standby_dev = [
                pnic_map.get(s, s) for s in (teaming.nicOrder.standbyNic or [])
            ]
            if active_dev or standby_dev:
                entry["teaming"] = {
                    "active_adapters": active_dev,
                    "standby_adapters": standby_dev,
                }
        except Exception:  # noqa: BLE001
            pass
        vswitches.append(entry)

    portgroups: list[dict] = []
    for pg in net.portgroup or []:
        spec = pg.spec
        # Skip uplink-ish or empty
        name = spec.name
        if not name:
            continue
        entry = {
            "name": name,
            "vswitch": spec.vswitchName,
            "vlan_id": int(spec.vlanId) if spec.vlanId is not None else 0,
        }
        try:
            sec_pol = spec.policy.security
            block = _security_block(
                getattr(sec_pol, "allowPromiscuous", None),
                getattr(sec_pol, "macChanges", None),
                getattr(sec_pol, "forgedTransmits", None),
            )
            if block:
                entry["security"] = block
        except Exception:  # noqa: BLE001
            pass
        portgroups.append(entry)

    cfg: dict[str, Any] = {
        "esxi_api_hostname": host.name,
        "esxi_vswitches": vswitches,
        "esxi_portgroups": portgroups,
    }

    # DNS
    try:
        dns = net.dnsConfig
        if dns:
            if dns.address:
                cfg["esxi_dns_servers"] = list(dns.address)
            if dns.searchDomain:
                cfg["esxi_dns_search"] = list(dns.searchDomain)
            if dns.domainName:
                cfg["esxi_domain"] = dns.domainName
            if dns.hostName:
                cfg["esxi_hostname"] = dns.hostName
    except Exception as exc:  # noqa: BLE001
        _SKIPPED.append(f"host/{host.name}/dns: {exc}")

    # NTP
    try:
        ntp = host.config.dateTimeInfo.ntpConfig
        if ntp and ntp.server:
            cfg["esxi_ntp_servers"] = list(ntp.server)
    except Exception as exc:  # noqa: BLE001
        _SKIPPED.append(f"host/{host.name}/ntp: {exc}")

    # SSH / TSM-SSH service
    try:
        for svc in host.config.service.service or []:
            if svc.key in ("TSM-SSH", "TSM"):
                running = bool(svc.running)
                if svc.key == "TSM-SSH":
                    cfg["esxi_ssh_enabled"] = running
                    cfg["esxi_ssh_service_policy"] = "on" if running else "off"
    except Exception as exc:  # noqa: BLE001
        _SKIPPED.append(f"host/{host.name}/ssh: {exc}")

    return cfg


def _export_esxi_host_configs(content: Any) -> dict[str, dict]:
    configs: dict[str, dict] = {}
    for host in _view(content, vim.HostSystem):
        try:
            configs[host.name] = _export_one_host_config(host)
        except Exception as exc:  # noqa: BLE001
            _SKIPPED.append(f"esxi_host_configs/{host.name}: {exc}")
    return configs


def _build_vcenter_config(content: Any, include_stock: bool) -> dict:
    return {
        "datacenter": _safe("datacenter", lambda: _export_datacenter(content), {}),
        "clusters": _safe("clusters", lambda: _export_clusters(content), []),
        "hosts": _safe("hosts", lambda: _export_hosts(content), []),
        "vds": _safe("vds", lambda: _export_vds(content), []),
        "portgroups": _safe(
            "portgroups", lambda: _export_portgroups(content, include_stock), []),
        "resource_pools": _safe("resource_pools", lambda: _export_resource_pools(content), []),
        "folders": _safe("folders", lambda: _export_folders(content), []),
        "vds_hosts": _safe("vds_hosts", lambda: _export_vds_hosts(content), []),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--include-stock", action="store_true",
        help="also export built-in roles/portgroups")
    parser.add_argument(
        "--topology-only", action="store_true",
        help="skip per-host ESXi day-2 export (faster)")
    args = parser.parse_args()

    si = _connect()
    try:
        content = si.RetrieveContent()
        vcenter_config = _build_vcenter_config(content, args.include_stock)
        esxi_host_configs: dict[str, dict] = {}
        if not args.topology_only:
            esxi_host_configs = _safe(
                "esxi_host_configs",
                lambda: _export_esxi_host_configs(content),
                {},
            )

        vcenter_esxi_hosts = [
            {"hostname": h["hostname"]}
            for h in vcenter_config.get("hosts") or []
        ]

        counts: dict[str, Any] = {
            k: (1 if v else 0) if k == "datacenter" else len(v)
            for k, v in vcenter_config.items()
        }
        counts["esxi_host_configs"] = len(esxi_host_configs)

        doc = {
            "meta": {
                "source": os.environ[_ENV_VCENTER_HOST],
                "bundle": "full" if not args.topology_only else "A-topology",
                "skipped": _SKIPPED,
                "counts": counts,
                "import": {
                    "vcenter_config": (
                        "inventories/<env>/group_vars/vcenter.yml "
                        "→ ansible-playbook … --tags topology"
                    ),
                    "vcenter_esxi_hosts": (
                        "same group_vars (or all) → enable host day-2 gates as needed"
                    ),
                    "esxi_host_configs": (
                        "split each key into inventories/<env>/host_vars/<host>.yml "
                        "as esxi_* vars → roles/esxi --tags networking,ntp,dns,ssh"
                    ),
                },
            },
            "vcenter_config": vcenter_config,
            "vcenter_esxi_hosts": vcenter_esxi_hosts,
            "esxi_host_configs": esxi_host_configs,
        }
        json.dump(doc, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    finally:
        Disconnect(si)


if __name__ == "__main__":
    main()
