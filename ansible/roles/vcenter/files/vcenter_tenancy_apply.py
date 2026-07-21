#!/usr/bin/env python3
"""Apply soft multi-tenancy on vCenter: folders, resource pools, RBAC.

Invoked from roles/vcenter/tasks/tenancy.yml with JSON on stdin:

{
  "hostname": "...",
  "username": "...",
  "password": "...",
  "validate_certs": false,
  "datacenter": "Datacenter",
  "migrate_legacy": true,
  "legacy_tenant": "platform",
  "legacy_folders": ["mgt", "prod", ...],
  "resource_pool_parents": ["Cluster", "Lidcombe"],
  "platform_admins": ["IDM\\\\long", ...],
  "tenants": [
    {
      "name": "acme",
      "folder": "acme",
      "resource_pool": "acme",
      "admin_principals": ["IDM\\\\acme-admin-1", "IDM\\\\long"]
    }
  ],
  "admin_role": "Admin",
  "revoke_root_admins": ["IDM\\\\acme-admin-1", ...]
}

Isolation model (soft multi-tenancy, work-style):
  - One VM folder per tenant under tenants/<name>
  - One resource pool per tenant under each configured compute parent
  - Tenant admin_principals get Admin (propagate) on THAT tenant's folder
    AND on THAT tenant's RP under every parent
  - The same principal may appear under multiple tenants → multiple RPs
  - Platform admins get Admin on rootFolder (full estate)

Prints a JSON summary on stdout (no secrets).
"""

from __future__ import annotations

import json
import ssl
import sys
from typing import Any

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vmodl, vim


def connect(cfg: dict[str, Any]):
    ctx = None
    if not cfg.get("validate_certs", False):
        ctx = ssl._create_unverified_context()
    return SmartConnect(
        host=cfg["hostname"],
        user=cfg["username"],
        pwd=cfg["password"],
        sslContext=ctx,
    )


def role_id_by_name(auth, name: str) -> int:
    for r in auth.roleList:
        if r.name == name:
            return r.roleId
    raise SystemExit(f"role not found: {name}")


def dc_vm_folder(content, dc_name: str) -> vim.Folder:
    for dc in content.rootFolder.childEntity:
        if isinstance(dc, vim.Datacenter) and dc.name == dc_name:
            return dc.vmFolder
    raise SystemExit(f"datacenter not found: {dc_name}")


def child_folder(parent: vim.Folder, name: str) -> vim.Folder | None:
    for c in parent.childEntity:
        if isinstance(c, vim.Folder) and c.name == name:
            return c
    return None


def ensure_folder(parent: vim.Folder, name: str) -> tuple[vim.Folder, bool]:
    existing = child_folder(parent, name)
    if existing is not None:
        return existing, False
    created = parent.CreateFolder(name)
    if isinstance(created, vim.Folder):
        return created, True
    wait_task(created)
    return created.info.result, True


def wait_task(task) -> None:
    while task.info.state in ("running", "queued"):
        pass
    if task.info.state != "success":
        raise SystemExit(f"task failed: {task.info.error}")


def move_into(parent: vim.Folder, entity) -> bool:
    if entity.parent == parent:
        return False
    wait_task(parent.MoveIntoFolder_Task([entity]))
    return True


def find_compute(content, name: str):
    """Match cluster or standalone host ComputeResource by inventory name."""
    view = content.viewManager.CreateContainerView(
        content.rootFolder,
        [vim.ClusterComputeResource, vim.ComputeResource],
        True,
    )
    try:
        for obj in view.view:
            if obj.name == name:
                return obj
    finally:
        view.Destroy()
    return None


def _allocation(shares: int = 4000) -> vim.ResourceAllocationInfo:
    alloc = vim.ResourceAllocationInfo()
    alloc.reservation = 0
    alloc.expandableReservation = True
    alloc.limit = -1
    alloc.shares = vim.SharesInfo(
        level=vim.SharesInfo.Level.normal,
        shares=shares,
    )
    return alloc


def ensure_resource_pool(
    parent_rp: vim.ResourcePool, name: str
) -> tuple[vim.ResourcePool | None, bool, str | None]:
    """Return (pool|None, created, warning|None).

    Empty clusters / unsupported parents raise NotSupported — surface as warning.
    """
    for child in parent_rp.resourcePool or []:
        if child.name == name:
            return child, False, None
    spec = vim.ResourceConfigSpec()
    spec.cpuAllocation = _allocation(4000)
    spec.memoryAllocation = _allocation(163840)
    try:
        created = parent_rp.CreateResourcePool(name, spec)
    except vim.fault.DuplicateName:
        for child in parent_rp.resourcePool or []:
            if child.name == name:
                return child, False, None
        return None, False, f"DuplicateName for pool {name!r} but not found under parent"
    except vmodl.fault.NotSupported as exc:
        return None, False, f"CreateResourcePool not supported on parent: {exc.msg}"
    except Exception as exc:  # noqa: BLE001 — surface to summary
        return None, False, f"CreateResourcePool failed: {type(exc).__name__}: {exc}"
    if isinstance(created, vim.ResourcePool):
        return created, True, None
    try:
        wait_task(created)
        return created.info.result, True, None
    except Exception as exc:  # noqa: BLE001
        return None, False, f"CreateResourcePool task failed: {exc}"


def grant(
    auth,
    entity,
    principal: str,
    role_id: int,
    is_group: bool = False,
    propagate: bool = True,
) -> str:
    existing = None
    for p in auth.RetrieveEntityPermissions(entity, False):
        if p.principal == principal and p.group == is_group:
            existing = p
            break
    if (
        existing is not None
        and existing.roleId == role_id
        and existing.propagate == propagate
    ):
        return "unchanged"
    perm = vim.AuthorizationManager.Permission()
    perm.entity = entity
    perm.principal = principal
    perm.group = is_group
    perm.roleId = role_id
    perm.propagate = propagate
    auth.SetEntityPermissions(entity, [perm])
    return "updated" if existing is not None else "created"


def revoke_root(auth, root, principal: str, is_group: bool = False) -> str:
    found = False
    for p in auth.RetrieveEntityPermissions(root, False):
        if p.principal == principal and p.group == is_group:
            found = True
            break
    if not found:
        return "absent"
    auth.RemoveEntityPermission(root, principal, is_group)
    return "removed"


def tenant_pool_name(tenant: dict[str, Any]) -> str:
    return tenant.get("resource_pool") or tenant.get("name") or tenant["folder"]


def main() -> int:
    cfg = json.load(sys.stdin)
    si = connect(cfg)
    try:
        content = si.RetrieveContent()
        auth = content.authorizationManager
        root = content.rootFolder
        admin_role = role_id_by_name(auth, cfg.get("admin_role", "Admin"))
        vm_root = dc_vm_folder(content, cfg["datacenter"])
        rp_parents = list(cfg.get("resource_pool_parents") or [])

        summary: dict[str, Any] = {
            "folders_created": [],
            "folders_moved": [],
            "resource_pools_created": [],
            "resource_pools_existing": [],
            "grants": [],
            "revokes": [],
            "warnings": [],
        }

        # ── Folders ────────────────────────────────────────────────────────
        platform, created = ensure_folder(vm_root, "platform")
        if created:
            summary["folders_created"].append("platform")
        tenants_folder, created = ensure_folder(vm_root, "tenants")
        if created:
            summary["folders_created"].append("tenants")

        templates = child_folder(vm_root, "templates")
        if templates is not None and templates.parent != platform:
            if move_into(platform, templates):
                summary["folders_moved"].append("templates -> platform/templates")
        elif child_folder(platform, "templates") is None:
            _, created = ensure_folder(platform, "templates")
            if created:
                summary["folders_created"].append("platform/templates")

        tenant_folders: dict[str, vim.Folder] = {}
        for t in cfg.get("tenants") or []:
            name = t["folder"]
            f, created = ensure_folder(tenants_folder, name)
            tenant_folders[name] = f
            if created:
                summary["folders_created"].append(f"tenants/{name}")

        if cfg.get("migrate_legacy"):
            legacy_name = cfg.get("legacy_tenant", "platform")
            dest = tenant_folders.get(legacy_name) or ensure_folder(
                tenants_folder, legacy_name
            )[0]
            tenant_folders[legacy_name] = dest
            for fname in cfg.get("legacy_folders") or []:
                src = child_folder(vm_root, fname)
                if src is None:
                    continue
                if src.parent == dest:
                    continue
                if move_into(dest, src):
                    summary["folders_moved"].append(
                        f"{fname} -> tenants/{legacy_name}/{fname}"
                    )

        # ── Resource pools (one per tenant under each compute parent) ──────
        # Map: (parent_name, pool_name) -> ResourcePool
        tenant_rps: dict[tuple[str, str], vim.ResourcePool] = {}
        compute_cache: dict[str, Any] = {}

        for parent_name in rp_parents:
            compute = find_compute(content, parent_name)
            if compute is None:
                summary["warnings"].append(
                    f"resource_pool parent not found: {parent_name}"
                )
                continue
            compute_cache[parent_name] = compute
            root_rp = compute.resourcePool
            # Empty clusters cannot host child RPs (NotSupported) — skip early.
            host_count = len(getattr(compute, "host", []) or [])
            if host_count == 0 and isinstance(compute, vim.ClusterComputeResource):
                summary["warnings"].append(
                    f"skip resource pools on empty cluster {parent_name!r}"
                )
                continue
            for t in cfg.get("tenants") or []:
                pool_name = tenant_pool_name(t)
                rp, created, warn = ensure_resource_pool(root_rp, pool_name)
                label = f"{parent_name}/{pool_name}"
                if warn:
                    summary["warnings"].append(f"{label}: {warn}")
                    continue
                if rp is None:
                    continue
                key = (parent_name, pool_name)
                tenant_rps[key] = rp
                if created:
                    summary["resource_pools_created"].append(label)
                else:
                    summary["resource_pools_existing"].append(label)

        # ── Platform admins: full VC ───────────────────────────────────────
        for principal in cfg.get("platform_admins") or []:
            status = grant(
                auth, root, principal, admin_role, is_group=False, propagate=True
            )
            summary["grants"].append(
                {
                    "principal": principal,
                    "object": "rootFolder",
                    "role": "Admin",
                    "status": status,
                }
            )

        # ── Tenant admins: folder + each RP for that tenant ────────────────
        # Same principal listed under multiple tenants → multiple RPs (work model).
        for t in cfg.get("tenants") or []:
            folder_name = t["folder"]
            pool_name = tenant_pool_name(t)
            entity = tenant_folders.get(folder_name)
            if entity is None:
                entity, created = ensure_folder(tenants_folder, folder_name)
                if created:
                    summary["folders_created"].append(f"tenants/{folder_name}")
                tenant_folders[folder_name] = entity

            principals = list(t.get("admin_principals") or [])
            for principal in principals:
                status = grant(
                    auth, entity, principal, admin_role, is_group=False, propagate=True
                )
                summary["grants"].append(
                    {
                        "principal": principal,
                        "object": f"tenants/{folder_name}",
                        "role": "Admin",
                        "status": status,
                    }
                )

                for parent_name in rp_parents:
                    rp = tenant_rps.get((parent_name, pool_name))
                    if rp is None:
                        continue
                    status = grant(
                        auth, rp, principal, admin_role, is_group=False, propagate=True
                    )
                    summary["grants"].append(
                        {
                            "principal": principal,
                            "object": f"resourcePool:{parent_name}/{pool_name}",
                            "role": "Admin",
                            "status": status,
                        }
                    )

        # ── Revoke mistaken root Admin from pure tenant principals ─────────
        for principal in cfg.get("revoke_root_admins") or []:
            status = revoke_root(auth, root, principal, is_group=False)
            summary["revokes"].append(
                {"principal": principal, "object": "rootFolder", "status": status}
            )

        # Re-affirm platform admins (idempotent)
        for principal in cfg.get("platform_admins") or []:
            grant(auth, root, principal, admin_role, is_group=False, propagate=True)

        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    finally:
        Disconnect(si)


if __name__ == "__main__":
    raise SystemExit(main())
