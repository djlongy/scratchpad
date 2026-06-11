# roles/artifactory/filter_plugins/data_shaping.py
# Data-structure transforms for the export pipeline — kept separate from
# yaml_pretty.py, which only formats already-shaped data into pretty YAML.
# These operate on the data (dict in → smaller dict out) and are
# format-agnostic, so they serve both the YAML and JSON export paths.

from __future__ import annotations


def drop_empty(data):
    """Recursively remove mapping keys whose value is "empty" — None, an empty
    string, an empty list, or an empty dict — pruning children first so a map
    that becomes empty after its own keys are dropped is removed too.

    Artifactory returns unset fields as '' (a few as null) and unset
    collections as []/{}; absent == empty == API default on apply, so dropping
    them is lossless for round-trip and keeps As-Built vars small.

    CRITICAL: only None/''/[]/{} count as empty. Falsy-but-meaningful values
    (`false`, `0`, `0.0`) are KEPT — they are real settings, not absence. The
    comparisons below never match a bool/number (e.g. `0 == ''` is False).
    """
    def is_empty(v):
        return v is None or v == '' or v == [] or v == {}

    def prune(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                pv = prune(v)
                if not is_empty(pv):
                    out[k] = pv
            return out
        if isinstance(node, list):
            # Prune inside each element but keep the list's length/order.
            return [prune(v) for v in node]
        return node

    return prune(data)


# ── compare / merge (As-Built vs saved IaC) ────────────────────────────────
# Identity key for each managed section's list items, so diffs and merges
# match objects by their natural key rather than list position. Dotted paths
# (e.g. general_data.name) address a nested key.
SECTION_IDENTITY = {
    'artifactory_local_repositories': 'key',
    'artifactory_remote_repositories': 'key',
    'artifactory_virtual_repositories': 'key',
    'artifactory_federated_repositories': 'key',
    'artifactory_groups': 'name',
    'artifactory_users': 'name',
    'artifactory_permissions': 'name',
    'artifactory_projects': 'project_key',
    'artifactory_environments': 'name',
    'artifactory_ldap_settings': 'key',
    'artifactory_ldap_groups': 'name',
    'artifactory_vault_configs': 'key',
    'artifactory_xray_policies': 'name',
    'artifactory_xray_watches': 'general_data.name',
    'artifactory_xray_ignore_rules': 'id',
    'artifactory_xray_reports': 'name',
    'artifactory_replications': 'repoKey',
    'artifactory_webhooks': 'key',
}


def _get_path(obj, path):
    """Resolve a dotted path (general_data.name) against a mapping; None if absent."""
    cur = obj
    for part in path.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def config_diff(saved, asbuilt, identity=None, ignore=None):
    """Diff two role-state dicts (saved IaC vs As-Built export) section by
    section. Keyed-list sections are matched by their identity key; everything
    else is compared whole. `ignore` is a list of section keys to skip (export
    metadata like sidecar-file pointers). Returns only sections that differ:

        {section: {added:[...], removed:[...], changed:[{id,before,after}]}}   # keyed lists
        {section: {before:..., after:...}}                                     # scalars/maps
    """
    identity = identity or SECTION_IDENTITY
    ignore = set(ignore or [])
    saved = saved or {}
    asbuilt = asbuilt or {}
    result = {}
    for sec in sorted(set(list(saved.keys()) + list(asbuilt.keys()))):
        if sec in ignore:
            continue
        sv, ab, idk = saved.get(sec), asbuilt.get(sec), identity.get(sec)
        if idk and (isinstance(sv, list) or isinstance(ab, list)):
            sv_map = {_get_path(i, idk): i for i in (sv or []) if isinstance(i, dict)}
            ab_map = {_get_path(i, idk): i for i in (ab or []) if isinstance(i, dict)}
            added = [ab_map[k] for k in ab_map if k not in sv_map]
            removed = [sv_map[k] for k in sv_map if k not in ab_map]
            changed = [{'id': k, 'before': sv_map[k], 'after': ab_map[k]}
                       for k in ab_map if k in sv_map and ab_map[k] != sv_map[k]]
            if added or removed or changed:
                result[sec] = {'added': added, 'removed': removed, 'changed': changed}
        elif sv != ab and (sv or ab):
            result[sec] = {'before': sv, 'after': ab}
    return result


def diff_summary(diff):
    """One-line-per-section counts for a config_diff result (human report)."""
    lines = []
    for sec in sorted(diff):
        d = diff[sec]
        if 'added' in d:
            lines.append("%-40s +%d ~%d -%d" % (
                sec, len(d['added']), len(d['changed']), len(d['removed'])))
        else:
            lines.append("%-40s changed (scalar/map)" % sec)
    return lines or ["no differences — As-Built matches saved IaC"]


def merge_sections(saved, asbuilt, sections, identity=None):
    """Surgical section merge for MR-driven IaC: return a copy of `saved` with
    each name in `sections` replaced by its As-Built value. Sections not named
    are left untouched, so a reviewer pulls only the slice they intend."""
    out = dict(saved or {})
    for sec in (sections or []):
        if sec in (asbuilt or {}):
            out[sec] = asbuilt[sec]
    return out


class FilterModule(object):
    def filters(self):
        return {
            'drop_empty': drop_empty,
            'config_diff': config_diff,
            'diff_summary': diff_summary,
            'merge_sections': merge_sections,
        }
