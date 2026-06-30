# -*- coding: utf-8 -*-
"""FreeIPA IDAM helper filters (Ansible filter plugins).

Two small, general-purpose filters the role's reconcile uses:

  freeipa_idam_merge(base, extra, key="name", union_fields=None)
      Append `extra` onto `base`, deduped by `key`. Used to layer generated objects
      (e.g. the RBAC overlay from freeipa_rbac.py) onto a hand-written / exported native
      baseline without a separate var: baseline wins on a name collision, listed
      `union_fields` are unioned into the base item.

  freeipa_idam_orphans(found, desired, match, protected=None)
      Compute the orphan object names to delete per type, for the authoritative object
      reconcile: found names that contain the scope marker `match`, are NOT desired, and
      are NOT protected. A blank `match` yields nothing (fail-safe).

(The earlier access-matrix compilers were retired in favour of the thin RBAC overlay in
freeipa_rbac.py — the overlay generates only role groups + nesting + memberships, and every
other object stays native; see roles/freeipa_server/README.md.)
"""
from __future__ import annotations


# ── merge generated objects onto the baseline (native keys) ───────────────────
def _union_into(target, item, fields):
    """Union each of `fields` (list values) from `item` into `target` in place."""
    for field in fields:
        combined = list(target.get(field) or [])
        for value in (item.get(field) or []):
            if value not in combined:
                combined.append(value)
        target[field] = combined


def freeipa_idam_merge(base, extra, key="name", union_fields=None):
    """Append `extra` onto `base`, deduped by `key`.

    Order: every base item first (baseline is the base), then the genuinely-new extra
    items. On a `key` collision the base item is authoritative and the extra is dropped —
    UNLESS union_fields is given, in which case those list fields are unioned into the base
    item (e.g. a generated role group's `user`/`group` lists unioned onto a native group).
    """
    union_fields = union_fields or []
    merged = [dict(x) if isinstance(x, dict) else x for x in (base or [])]
    index = {x.get(key): i for i, x in enumerate(merged) if isinstance(x, dict)}
    for item in (extra or []):
        if not isinstance(item, dict):
            merged.append(item)
            continue
        name = item.get(key)
        if name not in index:
            index[name] = len(merged)
            merged.append(item)
            continue
        _union_into(merged[index[name]], item, union_fields)
    return merged


# ── orphan reconcile — what to DELETE (managed, in scope, no longer declared) ──
def _is_orphan(name, match, want, protected):
    """A name is an orphan iff it carries the scope marker, isn't desired, isn't protected."""
    return bool(name) and match in name and name not in want and name not in protected


def freeipa_idam_orphans(found, desired, match, protected=None):
    """Compute the orphan object names to delete, per object type.

    `found`    : {type: [names currently in the realm]} (from `ipa <type>-find <match>`)
    `desired`  : {type: [names declared this run]}
    `match`    : the scope marker that EVERY managed name contains (e.g. "acme-prod") —
                 a name is only ever eligible for deletion if it CONTAINS this, so other
                 tenants/environments and unrelated objects are never touched.
    `protected`: names that must never be deleted (e.g. freeipa_idam_protected_groups).

    Returns {type: [orphan names]}. An empty/blank `match` yields NOTHING (fail-safe:
    never delete the whole realm because the scope marker was unset).
    """
    if not match:
        return {otype: [] for otype in (found or {})}
    protected = set(protected or [])
    out = {}
    for otype, names in (found or {}).items():
        want = set((desired or {}).get(otype) or [])
        out[otype] = [n for n in (names or []) if _is_orphan(n, match, want, protected)]
    return out


# ── normalize name-only object lists (accept bare-string shorthand) ───────────
def freeipa_idam_named(items):
    """Normalize a name-only object list to dicts: a bare string ``s`` becomes
    ``{'name': s}``; a mapping is passed through unchanged. Lets terse shorthand
    (e.g. ``freeipa_idam_hbacsvcs: [cockpit]``) work alongside the full
    ``[{name: cockpit, description: ...}]`` form, instead of crashing the
    downstream ``map(attribute='name')`` with 'str object has no attribute name'."""
    out = []
    for item in items or []:
        out.append({"name": item} if isinstance(item, str) else item)
    return out


# ── scope a captured snapshot to a tenant/env slice (export) ─────────────────
_SCOPE_ID_FIELDS = ("name", "zone_name")


def _scope_identifier(item):
    """The string a scope substring is matched against for one captured object:
    its ``name``, or ``zone_name`` for a DNS-records group."""
    if isinstance(item, dict):
        for field in _SCOPE_ID_FIELDS:
            value = item.get(field)
            if value:
                return str(value)
    return ""


def freeipa_export_scope(export, scopes, mode="include"):
    """Slice a captured FreeIPA snapshot by object-name substring, so one realm
    can be carved into per-tenant/env inventories.

    ``export`` : the parsed snapshot dict (meta + server_* scalars + object lists).
    ``scopes`` : a substring or list of substrings (e.g. ``acme-prod-``).
    ``mode``   : ``include`` keeps objects whose identifier CONTAINS any scope (the
                 tenant/env slice); ``exclude`` keeps objects whose identifier
                 contains NONE of them (the global 'outliers' — users, DNS,
                 ``platform-*``, built-ins — for the shared/auth inventory).

    Only object lists (lists of dicts) are filtered, matched on each item's
    ``name`` (or ``zone_name`` for DNS records). Scalar keys and non-object lists
    (``meta``, ``realm``, ``domain``, ``forwarders``) pass through unchanged.
    An empty ``scopes`` returns the snapshot untouched (no filtering)."""
    if isinstance(scopes, str):
        scopes = [scopes]
    scopes = [s for s in (scopes or []) if s]
    if not scopes:
        return export
    exclude = (mode == "exclude")

    def keep(item):
        ident = _scope_identifier(item)
        hit = any(s in ident for s in scopes)
        return (not hit) if exclude else hit

    out = {}
    for key, value in (export or {}).items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            out[key] = [item for item in value if keep(item)]
        else:
            out[key] = value
    return out


class FilterModule:
    def filters(self):
        return {
            "freeipa_idam_merge": freeipa_idam_merge,
            "freeipa_idam_orphans": freeipa_idam_orphans,
            "freeipa_idam_named": freeipa_idam_named,
            "freeipa_export_scope": freeipa_export_scope,
        }
