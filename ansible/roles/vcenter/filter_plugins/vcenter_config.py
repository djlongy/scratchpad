# -*- coding: utf-8 -*-
"""vCenter config-as-code helper filters (Ansible filter plugins).

  vcenter_config_validate(config, builtins) -> list[str]
      Referential integrity check for the vcenter_config dict. Returns a list of
      error strings (empty = valid). A reference resolves if the target name is
      declared in `config` OR present in the matching `builtins` list.

  vcenter_config_orphans(found, desired, scope, protected=None) -> list[str]
      Names in `found` that contain `scope`, are NOT in `desired`, and are NOT in
      `protected`. Blank `scope` -> [] (fail-safe: an empty scope never prunes).
"""
from __future__ import annotations

try:                                          # real Ansible at runtime …
    from ansible.errors import AnsibleFilterError
except ImportError:                           # … plain Python under pytest
    class AnsibleFilterError(Exception):
        pass


# Reference field keys, hoisted so no literal repeats >=3x (SonarQube S1192).
CLUSTER_KEY = "cluster"
CATEGORY_KEY = "category"


def _names(items: list | None) -> list[str]:
    return [i.get("name") for i in (items or []) if isinstance(i, dict) and i.get("name")]


def _known_cluster_names(config: dict, builtins: dict) -> set[str]:
    return set(_names(config.get("clusters"))) | set(builtins.get("clusters") or [])


def _known_vds_names(config: dict, builtins: dict) -> set[str]:
    return set(_names(config.get("vds"))) | set(builtins.get("vds") or [])


def _known_category_names(config: dict, builtins: dict) -> set[str]:
    return set(_names(config.get("tag_categories"))) | set(builtins.get("tag_categories") or [])


def _host_cluster_errors(hosts: list, cluster_names: set[str]) -> list[str]:
    return [
        f"host '{host.get('hostname')}' references unknown cluster '{host[CLUSTER_KEY]}'"
        for host in hosts
        if host.get(CLUSTER_KEY) and host[CLUSTER_KEY] not in cluster_names
    ]


def _portgroup_vds_errors(portgroups: list, vds_names: set[str]) -> list[str]:
    return [
        f"portgroup '{pg.get('name')}' references unknown vds '{pg['vds']}'"
        for pg in portgroups
        if pg.get("vds") and pg["vds"] not in vds_names
    ]


def _resource_pool_cluster_errors(resource_pools: list, cluster_names: set[str]) -> list[str]:
    return [
        f"resource_pool '{rp.get('name')}' references unknown cluster '{rp[CLUSTER_KEY]}'"
        for rp in resource_pools
        if rp.get(CLUSTER_KEY) and rp[CLUSTER_KEY] not in cluster_names
    ]


def _tag_category_errors(tags: list, category_names: set[str]) -> list[str]:
    return [
        f"tag '{tag.get('name')}' references unknown category '{tag[CATEGORY_KEY]}'"
        for tag in tags
        if tag.get(CATEGORY_KEY) and tag[CATEGORY_KEY] not in category_names
    ]


def vcenter_config_validate(config: dict, builtins: dict | None = None) -> list[str]:
    """Referential integrity check for a vcenter_config dict.

    Every cross-reference (host->cluster, portgroup->vds, resource_pool->cluster,
    tag->tag_category) must resolve to a name declared in `config` or present in
    the matching `builtins` allow-list. Returns one human-readable error string
    per unresolved reference; an empty list means the config is valid.
    """
    if not isinstance(config, dict):
        raise AnsibleFilterError("vcenter_config_validate: config must be a mapping")
    builtins = builtins or {}

    cluster_names = _known_cluster_names(config, builtins)
    vds_names = _known_vds_names(config, builtins)
    category_names = _known_category_names(config, builtins)

    return (
        _host_cluster_errors(config.get("hosts") or [], cluster_names)
        + _portgroup_vds_errors(config.get("portgroups") or [], vds_names)
        + _resource_pool_cluster_errors(config.get("resource_pools") or [], cluster_names)
        + _tag_category_errors(config.get("tags") or [], category_names)
    )


def vcenter_config_orphans(
    found: list[str], desired: list[str], scope: str,
    protected: list[str] | None = None,
) -> list[str]:
    """Names in `found` that are in `scope`, not `desired`, and not `protected`.

    A blank `scope` returns [] unconditionally — fail-safe, so an unset scope
    never prunes the whole inventory.
    """
    if not scope:
        return []
    desired_set = set(desired or [])
    protected_set = set(protected or [])
    return [n for n in (found or [])
            if scope in n and n not in desired_set and n not in protected_set]


class FilterModule:
    def filters(self) -> dict:
        return {
            "vcenter_config_validate": vcenter_config_validate,
            "vcenter_config_orphans": vcenter_config_orphans,
        }
