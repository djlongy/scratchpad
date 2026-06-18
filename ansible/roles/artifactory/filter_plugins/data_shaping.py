# roles/artifactory/filter_plugins/data_shaping.py
# Data-structure transforms for the export pipeline — kept separate from
# yaml_pretty.py, which only formats already-shaped data into pretty YAML.
# These operate on the data (dict in → smaller dict out) and are
# format-agnostic, so they serve both the YAML and JSON export paths.

from __future__ import annotations

import re


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


# ── compare (As-Built vs saved IaC) ────────────────────────────────────────
# Identity key for each managed section's list items, so diffs match objects by
# their natural key rather than list position. Dotted paths
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


# ── system-config descriptor (XML) → PATCH-ready YAML config ────────────────
# The global config descriptor can only be GET as XML (no native YAML read), and
# the raw-XML full-replace POST is version-bound (xsd-tagged) and carries
# master-key-encrypted secrets, so it does not port across instances/versions.
# The supported, version-tolerant restore path is the YAML PATCH
# (Content-Type: application/yaml), whose schema mirrors the descriptor's element
# names FLATTENED — but named collections are KEYED MAPS (by key/name), not the
# XML's <plural><singular>…</singular></plural> wrapper. This transform converts
# an xmltodict parse of the descriptor into that PATCH-ready shape. Validated
# live against Artifactory 7.156.2 (all blocks PATCH 200).

# Top-level blocks NOT emitted into the system-config YAML:
#  * repositories/replications — managed by the role's own dedicated sections
#    (artifactory_*_repositories / artifactory_replications), not system-config.
#  * keyPairs — GPG signing PRIVATE keys (per-instance secret, cannot port).
#  * xrayConfig is KEPT but its secret leaf (password) is stripped below; it is
#    an internal binding, review before promoting across instances.
#  * revision — server-managed optimistic-lock counter; never send it.
#  * addons — UI state (showAddonsInfoCookie is generated; PATCH rejects it).
_CONFIG_EXCLUDE = {
    'localRepositories', 'remoteRepositories', 'virtualRepositories',
    'federatedRepositories', 'releaseBundlesRepositories',
    'localReplications', 'remoteReplications',
    'keyPairs', 'revision', 'addons', '@xmlns',
}

# XML list-wrapper -> (child element name, identity field). xmltodict renders
# <plural><singular key=…>…</singular></plural> as {plural: {singular: [..]}};
# the PATCH wants {plural: {<identity>: {…}}} keyed by the item's natural id.
# Applied at ANY depth (propertySets nest properties → predefinedValues).
_CONFIG_WRAP = {
    'backups': ('backup', 'key'),
    'proxies': ('proxy', 'key'),
    'reverseProxies': ('reverseProxy', 'key'),
    'propertySets': ('propertySet', 'name'),
    'repoLayouts': ('repoLayout', 'name'),
    'properties': ('property', 'name'),
    'predefinedValues': ('predefinedValue', 'value'),
    'retentionPolicies': ('retentionPolicy', 'name'),
}

# Leaf keys whose VALUE is a secret encrypted under the source instance's master
# key — stripped so the export carries no unportable/secret material. Matched
# case-insensitively on the whole leaf-key name (so 'key'/'apiKey' differ).
_SECRET_RE = re.compile(
    r'(password|passwd|secret|sslkey|privatekey|passphrase|refreshtoken'
    r'|clientsecret|apikey|encryptionkey|bindpassword|managerpassword'
    r'|masterkey)$', re.I)

# Identity-provider sub-blocks under <security>: managed by the role's own
# integrations (LDAP/SSO) and secret-bearing — dropped from system-config YAML.
_SECURITY_SUBKEYS_DROP = {
    'ldapSettings', 'ldapGroupSettings', 'crowdSettings',
    'samlSettings', 'oauthSettings', 'httpSsoSettings',
}


def _coerce_scalar(v):
    """xmltodict yields every leaf as a string; restore native bool/int so the
    YAML PATCH receives typed values (string cron/host expressions untouched)."""
    if isinstance(v, str):
        low = v.lower()
        if low in ('true', 'false'):
            return low == 'true'
        if re.fullmatch(r'-?\d+', v):
            return int(v)
    return v


def _clean_config(node, parent=None):
    """Recursively reshape an xmltodict descriptor node into PATCH-ready form:
    drop @attrs/secrets/empties, unwrap list-wrappers into keyed maps, and
    coerce scalar types. Empties (None/''/[]/{}) are pruned so unset fields are
    simply absent (PATCH is additive — absent leaves existing values alone)."""
    if isinstance(node, dict):
        out = {}
        for k, val in node.items():
            if k.startswith('@') or _SECRET_RE.search(k):
                continue
            if parent == 'security' and k in _SECURITY_SUBKEYS_DROP:
                continue
            if k in _CONFIG_WRAP:
                child, idf = _CONFIG_WRAP[k]
                inner = val.get(child) if isinstance(val, dict) else val
                if inner is None:
                    continue
                items = inner if isinstance(inner, list) else [inner]
                keyed = {}
                for it in items:
                    ci = _clean_config(it, k)
                    if not isinstance(ci, dict):
                        continue
                    idv = ci.pop(idf, None)
                    if idv is not None:
                        keyed[idv] = ci
                if keyed:
                    out[k] = keyed
                continue
            cv = _clean_config(val, k)
            if cv not in (None, '', [], {}):
                out[k] = cv
        return out
    if isinstance(node, list):
        return [_clean_config(x, parent) for x in node]
    return _coerce_scalar(node)


def descriptor_to_config(parsed):
    """Convert an xmltodict parse of the Artifactory config descriptor into the
    PATCH-ready (application/yaml) config dict — the version-tolerant restore
    payload. `parsed` may be the full {'config': {...}} document or the inner
    config mapping. Returns {} for empty/invalid input."""
    if not isinstance(parsed, dict):
        return {}
    cfg = parsed.get('config', parsed)
    if not isinstance(cfg, dict):
        return {}
    top = {k: v for k, v in cfg.items() if k not in _CONFIG_EXCLUDE}
    return _clean_config(top)


class FilterModule(object):
    def filters(self):
        return {
            'drop_empty': drop_empty,
            'config_diff': config_diff,
            'diff_summary': diff_summary,
            'descriptor_to_config': descriptor_to_config,
        }
