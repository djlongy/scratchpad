"""Network catalog engine — one SSOT of network segments, many platform lists.

FULL REFERENCE: filter_plugins/docs/network_catalog.md — every option, its
default, what it does, worked examples and expected output.

ESTATE-AGNOSTIC. This module knows nothing about any particular site, role,
zone, platform or naming convention: every one of those comes from the config
dict it is handed. Drop it into any Ansible repo, point it at a segment matrix,
and it produces exactly the row shapes that repo's modules loop over.

Used once, from group_vars, so consumers see plain data rather than a filter
call at every site:

    __catalog: "{{ network_underlays | network_catalog(config) }}"
    _net: "{{ __catalog.views }}"     # _net.esxi.port_groups, _net.dell.vlans

Config keys (all optional except the segments themselves):

    pools             spec-driven segments (instances x roles) merged with
                      the hand-written ones
    names             name RECIPES — how to build each name from tokens
    name_default      which recipe is the segment's primary name
    views             {namespace: {list: spec}} — the output row shapes; the
                      outer key is a free output label, each spec's `platform`
                      key is the membership filter (they need not match)
    platforms         platform names allowed in a segment's platforms[]
    required          {all: [field], by_platform: {platform: [field]}}
    defaults          fields every segment inherits unless it overrides
    partition_fields  fields to build by/cidrs_by partitions from
    desc_template     str.format fallback for a segment without a description

Per-segment fields the engine reads directly: vlan_id, platforms[], instance
(feeds the instance / instance_nn name tokens), and whatever your recipes and
views reference. Everything else passes through untouched.

Returns a dict with: segments, views, by, cidrs_by, vlan_ids_by_platform,
vlan_ranges_by_platform, by_key, keys, names, vlan_ids, tagged_vlan_ids,
operator_cidrs, derived_names, name_overrides, errors, missing,
duplicate_names, duplicate_vlans.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Tokens the engine contributes on top of the segment's own fields. Every other
# token in a name recipe is simply a field name on the segment.
VLAN_TOKENS = ("vlan", "vlan_label", "vid")

# Keys consumed by the pool generator itself; everything else a pool declares is
# stamped onto each segment it emits.
POOL_GENERATOR_KEYS = frozenset(
    {"vlan_base", "instances", "vlan_stride", "roles", "key_parts", "key_case",
     "key_sep", "subnet_base", "subnet_stride", "gateway_offset", "subnet_index"}
)

# Repeated literals (SonarQube S1192).
F_PLATFORMS = "platforms"
F_INSTANCE = "instance"
F_VLAN_ID = "vlan_id"
F_SUBNET = "subnet"
F_NAME = "name"
F_KEY = "key"
CASE_UPPER = "upper"
CASE_LOWER = "lower"


# ──────────────────────────────────────────────────────────────────────────
# small helpers
# ──────────────────────────────────────────────────────────────────────────
def _as_dict(value: Any) -> Dict[str, Any]:
    """Coerce Ansible's mapping types (and None) to a plain dict."""
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> List[Any]:
    """Coerce to a plain list. Strings are NOT sequences here, deliberately."""
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return []
    if isinstance(value, Sequence):
        return list(value)
    return []


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce to int; unparseable degrades to the default instead of raising."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_group(token: Any) -> bool:
    """A nested list inside `parts` is a glue group (joined with no separator)."""
    return isinstance(token, Sequence) and not isinstance(token, (str, bytes))


def _apply_case(text: str, case: str) -> str:
    if case == CASE_UPPER:
        return text.upper()
    if case == CASE_LOWER:
        return text.lower()
    return text


def _is_empty(value: Any) -> bool:
    """Missing-or-blank, for required-field validation."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _compress_ranges(ids: Sequence[int]) -> List[str]:
    """Sorted ints -> range tokens: [7, 8, 10] -> ['7-8', '10'].

    Ready for CLI trunk lines via Jinja batch+join, so consumers need no
    range logic of their own.
    """
    out: List[str] = []
    start = prev = None
    for i in ids:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            out.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = i
    if start is not None:
        out.append(str(start) if start == prev else f"{start}-{prev}")
    return out


def _prefixlen_of(seg: Mapping[str, Any]) -> int:
    subnet = seg.get(F_SUBNET)
    if isinstance(subnet, str) and "/" in subnet:
        try:
            return int(subnet.split("/", 1)[1])
        except ValueError:
            return 0
    try:
        return int(seg.get("prefixlen") or 0)
    except (TypeError, ValueError):
        return 0


# ──────────────────────────────────────────────────────────────────────────
# Stage 0 — pools
# ──────────────────────────────────────────────────────────────────────────
def _normalise_roles(raw: Any) -> List[Dict[str, Any]]:
    """Both roles forms -> [{name, offset, fields}].

    dict: {app: {offset: 0, ...}}     offset explicit, else declaration order
    list: [app, {db: {...}}]          offset is the list position
    """
    specs: List[Dict[str, Any]] = []
    if isinstance(raw, Mapping):
        for position, (rname, rmeta) in enumerate(raw.items()):
            fields = _as_dict(rmeta)
            specs.append(
                {
                    F_NAME: rname,
                    "offset": _as_int(fields.get("offset", position), position),
                    "fields": fields,
                }
            )
        return specs
    for position, entry in enumerate(_as_list(raw)):
        if isinstance(entry, Mapping):
            rname = next(iter(entry), None)
            fields = _as_dict(entry.get(rname))
        else:
            rname, fields = entry, {}
        specs.append(
            {
                F_NAME: rname,
                "offset": _as_int(fields.get("offset", position), position),
                "fields": fields,
            }
        )
    return specs


def _render_key(parts: Iterable[Any], tokens: Mapping[str, Any], sep: str, case: str) -> str:
    rendered: List[str] = []
    for token in parts:
        if _is_group(token):
            rendered.append("".join(str(tokens.get(t, "")) for t in token))
        else:
            rendered.append(str(tokens.get(token, "")))
    return _apply_case(sep.join(p for p in rendered if p), case)


def _pool_segment(pool: Mapping[str, Any], role: Mapping[str, Any], vlan: int, n: int,
                  pool_key: str) -> Dict[str, Any]:
    seg = {k: v for k, v in pool.items() if k not in POOL_GENERATOR_KEYS}
    seg.update({k: v for k, v in role["fields"].items() if k != "offset"})
    seg.update({F_VLAN_ID: vlan, "role": role[F_NAME], F_INSTANCE: n,
                "pool": pool_key})
    return seg


def _parse_pool_addressing(pool_key: str, pool: Mapping[str, Any]
                           ) -> Tuple[Optional[Any], List[str]]:
    """The pool's subnet_base as a parsed network, or None + a named error."""
    spec = pool.get("subnet_base")
    if not spec:
        return None, []
    try:
        return ipaddress.ip_network(str(spec)), []
    except ValueError:
        return None, [f"pools.{pool_key}: subnet_base '{spec}' is not a valid network"]


def _stamp_pool_subnet(seg: Dict[str, Any], base_net: Any, index: int, stride: int,
                       gateway_offset: int, pool_key: str, seg_key: str) -> Optional[str]:
    """Compute one generated segment's subnet (+ netmask, gateway).

    Explicitly declared values always win (setdefault semantics); returns an
    error string when the computed network falls outside the address space.
    """
    step = base_net.num_addresses * stride
    try:
        subnet = ipaddress.ip_network(
            (int(base_net.network_address) + index * step, base_net.prefixlen)
        )
    except ValueError:
        return (f"pools.{pool_key}: computed subnet for '{seg_key}' "
                f"(index {index}) falls outside the address space of {base_net}")
    seg[F_SUBNET] = str(subnet)
    seg.setdefault("netmask", str(subnet.netmask))
    if gateway_offset:
        seg.setdefault("gateway", str(subnet.network_address + gateway_offset))
    return None


def expand_pools(pools: Any) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Emit `instances` x `roles` segments per pool, plus any addressing errors.

    vlan = vlan_base + (n - 1) * vlan_stride + role_offset      # n = 1..instances
    vlan_stride defaults to the role count, which tight-packs instances back
    to back.

    With `subnet_base`, each generated segment lacking an explicit subnet gets
    a computed one: the Nth network of that size (N = emission order, or the
    segment's VLAN distance from vlan_base with `subnet_index: vlan`), stepped
    by `subnet_stride` networks; netmask derived, gateway at `gateway_offset`.
    """
    out: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    for pool_key, raw_pool in _as_dict(pools).items():
        pool = _as_dict(raw_pool)
        if not pool:
            continue
        plan, addr_errors = _pool_plan(pool_key, pool)
        errors.extend(addr_errors)
        for key, seg, err in _pool_members(plan):
            if err:
                errors.append(err)
            if key in out:
                # Silently overwriting loses a whole segment from every derived
                # view while the health check still reads clean, so a colliding
                # key is an error, not a last-one-wins.
                errors.append(
                    f"pool '{plan['pool_key']}': generated key '{key}' collides with an "
                    f"earlier generated segment (vlan {out[key].get(F_VLAN_ID)} vs "
                    f"{seg.get(F_VLAN_ID)}) — widen key_parts so each member is unique"
                )
                continue
            out[key] = seg
    return out, errors


def _pool_plan(pool_key: str, pool: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Resolve every knob a pool's emission loop needs, once per pool."""
    roles = _normalise_roles(pool.get("roles"))
    base_net, addr_errors = _parse_pool_addressing(pool_key, pool)
    plan = {
        "pool": pool,
        "pool_key": pool_key,
        "roles": roles,
        "stride": _as_int(pool.get("vlan_stride", len(roles)), len(roles)),
        "base_vlan": _as_int(pool.get("vlan_base", 0)),
        "instances": _as_int(pool.get("instances", 1), 1),
        "key_parts": _as_list(pool.get("key_parts")) or ["pool", F_INSTANCE, "role"],
        "key_case": pool.get("key_case", CASE_LOWER),
        "key_sep": pool.get("key_sep", "-"),
        "base_net": base_net,
        "subnet_stride": _as_int(pool.get("subnet_stride", 1), 1) or 1,
        "gateway_offset": _as_int(pool.get("gateway_offset", 0)),
        "vlan_indexed": pool.get("subnet_index") == "vlan",
    }
    return plan, addr_errors


def _pool_members(plan: Mapping[str, Any]):
    """Yield (key, segment, error) for every instance x role the pool declares."""
    emitted = 0
    for n in range(1, plan["instances"] + 1):
        base = plan["base_vlan"] + (n - 1) * plan["stride"]
        for role in plan["roles"]:
            vlan = base + role["offset"]
            seg = _pool_segment(plan["pool"], role, vlan, n, plan["pool_key"])
            key = _render_key(plan["key_parts"],
                              _pool_key_tokens(seg, plan["pool_key"], n, vlan, role),
                              plan["key_sep"], plan["key_case"])
            err = None
            if plan["base_net"] is not None and F_SUBNET not in seg:
                index = (vlan - plan["base_vlan"]) if plan["vlan_indexed"] else emitted
                err = _stamp_pool_subnet(seg, plan["base_net"], index,
                                         plan["subnet_stride"], plan["gateway_offset"],
                                         plan["pool_key"], key)
            # The index advances per emitted segment even when a segment
            # declares its own subnet, so pinning one member never shifts
            # its neighbours' addressing.
            emitted += 1
            yield key, seg, err


def _pool_key_tokens(seg: Mapping[str, Any], pool_key: str, n: int,
                     vlan: int, role: Mapping[str, Any]) -> Dict[str, Any]:
    """The segment's own fields plus the pool-only tokens key_parts may name."""
    tokens = dict(seg)
    tokens.update(
        {
            "pool": pool_key,
            F_INSTANCE: str(n),
            "instance_nn": f"{n:02d}",
            "vid": str(vlan),
            "offset": str(role["offset"]),
        }
    )
    return tokens


# ──────────────────────────────────────────────────────────────────────────
# Stage 1 — names
# ──────────────────────────────────────────────────────────────────────────
def _name_tokens(seg: Mapping[str, Any], key: str, spec: Mapping[str, Any],
                 vlan_num: int) -> Dict[str, Any]:
    vlan_prefix = spec.get("vlan_prefix") or ""
    vid = f"{vlan_num:0{_as_int(spec.get('vlan_pad') or 0)}d}"
    inst = seg.get(F_INSTANCE, "")
    # A non-numeric instance cannot be zero-padded; degrade to the raw string
    # rather than raising (the "almost nothing raises" contract).
    inst_str = str(inst)
    try:
        inst_nn = f"{int(inst):02d}" if inst_str != "" else ""
    except (TypeError, ValueError):
        inst_nn = inst_str
    tokens = dict(seg)
    tokens.update(
        {
            F_KEY: key,
            "vlan": f"{vlan_prefix}{vid}",
            "vlan_label": vlan_prefix,
            "vid": vid,
            F_INSTANCE: inst_str,
            "instance_nn": inst_nn,
            "prefix": str(spec.get("prefix") or ""),
            "suffix": str(spec.get("suffix") or ""),
        }
    )
    return tokens


def _render_parts(spec: Mapping[str, Any], tokens: Mapping[str, Any]) -> List[str]:
    """Ordered parts, with glue groups flattened and dropped tokens removed."""
    parts_spec = _as_list(spec.get("parts"))
    drop = set(_as_list(spec.get("drop_tokens")))
    prefix, suffix = tokens["prefix"], tokens["suffix"]
    rendered: List[str] = []
    if prefix and "prefix" not in parts_spec:
        rendered.append(prefix)
    for token in parts_spec:
        if _is_group(token):
            # The whole group goes if any of its tokens is dropped — that is how
            # "the same name minus the VLAN part" removes a glued VLANnn.
            if any(t in drop for t in token):
                continue
            rendered.append("".join(str(tokens.get(t, "")) for t in token))
        elif token not in drop:
            rendered.append(str(tokens.get(token, "")))
    if suffix and "suffix" not in parts_spec:
        rendered.append(suffix)
    return [p for p in rendered if p]


SHORTHAND = {
    "name_parts": "parts",
    "name_case": "case",
    "name_sep": "sep",
    "name_prefix": "prefix",
    "name_suffix": "suffix",
    "vlan_prefix": "vlan_prefix",
    "vlan_pad": "vlan_pad",
}


def _resolve_spec(rname: str, recipes: Mapping[str, Any], seg: Mapping[str, Any],
                  default_recipe: str, resolved: Mapping[str, Any]) -> Dict[str, Any]:
    """Recipe spec for ONE segment: inherited, then per-segment overrides.

    `from` inherits the referenced recipe AS ALREADY RESOLVED FOR THIS SEGMENT,
    so a "same name minus the VLAN part" recipe still tracks a segment that
    overrode the parts list. The referenced recipe must be declared earlier.
    """
    base = _as_dict(recipes.get(rname))
    spec: Dict[str, Any] = {}
    parent = base.get("from")
    if parent:
        spec.update(_as_dict(resolved.get(parent)) or _as_dict(recipes.get(parent)))
    spec.update(base)
    spec.update(_as_dict(_as_dict(seg.get("names")).get(rname)))
    if rname == default_recipe:
        spec.update(
            {dest: seg[src] for src, dest in SHORTHAND.items() if src in seg}
        )
    return spec


def build_names(seg: Mapping[str, Any], key: str, recipes: Mapping[str, Any],
                default_recipe: str, vlan_num: int) -> Dict[str, str]:
    """Every recipe's name for one segment, in declaration order."""
    names: Dict[str, str] = {}
    resolved: Dict[str, Any] = {}
    for rname in recipes:
        spec = _resolve_spec(rname, recipes, seg, default_recipe, resolved)
        resolved[rname] = spec
        tokens = _name_tokens(seg, key, spec, vlan_num)
        joined = str(spec.get("sep", "-")).join(_render_parts(spec, tokens))
        names[rname] = _apply_case(joined, spec.get("case", "keep"))
    return names


# ──────────────────────────────────────────────────────────────────────────
# Stage 2 — enrichment
# ──────────────────────────────────────────────────────────────────────────
def _describe(seg: Mapping[str, Any], key: str, primary: str, vlan_num: int,
              fallback: str) -> str:
    declared = seg.get("description")
    if declared:
        return str(declared)
    template = seg.get("desc_template") or fallback
    if not template:
        return ""
    ctx = dict(seg)
    ctx.update({F_KEY: key, F_NAME: primary, "vid": vlan_num})
    try:
        return str(template).format(**ctx)
    except (KeyError, IndexError, ValueError):
        return ""


def enrich(key: str, raw: Mapping[str, Any], cfg: Mapping[str, Any]) -> Dict[str, Any]:
    """One segment -> one flat row. User fields pass through untouched."""
    seg: Dict[str, Any] = dict(_as_dict(cfg.get("defaults")))
    seg.update(_as_dict(raw))
    try:
        vlan_num = int(seg.get(F_VLAN_ID) or 0)
    except (TypeError, ValueError):
        vlan_num = 0
    platforms = _as_list(seg.get(F_PLATFORMS))
    recipes = _as_dict(cfg.get("names"))
    default_recipe = cfg.get("name_default") or (next(iter(recipes), ""))
    names = build_names(seg, key, recipes, default_recipe, vlan_num)

    # A field named after a recipe pins that name, overriding the tokens.
    for rname, built in names.items():
        seg.setdefault(rname, built)
    primary = seg.get(default_recipe, "")
    prefixlen = _prefixlen_of(seg)
    gateway = seg.get("gateway")

    seg.update(
        {
            F_KEY: key,
            F_VLAN_ID: vlan_num,
            F_PLATFORMS: platforms,
            "tagged": vlan_num > 0,
            "prefixlen": prefixlen,
            "gateway_cidr": f"{gateway}/{prefixlen}" if gateway and prefixlen else "",
            "names": names,
            F_NAME: primary,
            "derived_name": names.get(default_recipe, ""),
            "description": _describe(seg, key, primary, vlan_num,
                                     cfg.get("desc_template") or ""),
            "operator_source": bool(seg.get("operator_source", False)),
            F_INSTANCE: seg.get(F_INSTANCE, ""),
        }
    )
    # on_<platform> booleans for every DECLARED platform, so selectattr() needs
    # no 'contains' test and the engine still knows no platform by name.
    for platform in _as_list(cfg.get("platforms")):
        seg[f"on_{platform}"] = platform in platforms
    return seg


# ──────────────────────────────────────────────────────────────────────────
# Stage 3 — views
# ──────────────────────────────────────────────────────────────────────────
def _render_field(spec: Any, ctx: Mapping[str, Any]) -> Tuple[bool, Any]:
    """(emit?, value) for one output field."""
    if isinstance(spec, Mapping):
        return _render_mapping_field(spec, ctx)
    if isinstance(spec, str):
        return _render_string_field(spec, ctx)
    return True, spec


def _render_mapping_field(spec: Mapping[str, Any],
                          ctx: Mapping[str, Any]) -> Tuple[bool, Any]:
    """A literal `const`, or a nested `group` gated by `emit_when_any`."""
    if "const" in spec:
        return True, spec["const"]
    if "group" not in spec:
        return False, None
    watch = _as_list(spec.get("emit_when_any"))
    if watch and not any(ctx.get(w) for w in watch):
        return False, None
    nested = {}
    for out_key, inner in _as_dict(spec["group"]).items():
        emit, value = _render_field(inner, ctx)
        if emit:
            nested[out_key] = value
    return True, nested


def _render_string_field(spec: str, ctx: Mapping[str, Any]) -> Tuple[bool, Any]:
    """A `{token}` format string, or a plain field name copied from the row."""
    if "{" in spec:
        try:
            return True, spec.format(**ctx)
        except (KeyError, IndexError, ValueError):
            return False, None
    # A plain field name is copied only when the source row actually has it,
    # so odd segments and appended rows do not sprout null keys.
    return (spec in ctx), ctx.get(spec)


def _build_row(spec: Mapping[str, Any], ctx: Mapping[str, Any]) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    for out_key, field_spec in _as_dict(spec.get("fields")).items():
        emit, value = _render_field(field_spec, ctx)
        if emit:
            row[out_key] = value
    for out_key in _as_list(spec.get("omit_if_falsy")):
        if out_key in row and not row[out_key]:
            del row[out_key]
    return row


def _keep(row: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    platform = spec.get("platform")
    if platform is not None and platform not in _as_list(row.get(F_PLATFORMS)):
        return False
    return all(row.get(f) == v for f, v in _as_dict(spec.get("where")).items())


def _dedupe(rows: List[Dict[str, Any]], field: str) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for row in rows:
        marker = row.get(field)
        if not isinstance(marker, (str, int, float, bool, tuple, type(None))):
            marker = repr(marker)  # lists/dicts can't be set members; still dedupes
        if marker not in seen:
            seen.add(marker)
            out.append(row)
    return out


def _finish(rows: List[Dict[str, Any]], spec: Mapping[str, Any],
            extra: Any) -> List[Dict[str, Any]]:
    out = _dedupe(rows, spec["unique_by"]) if "unique_by" in spec else list(rows)
    out.extend(_as_list(extra))
    if "sort_by" in spec:
        out.sort(key=lambda r: r.get(spec["sort_by"]))
    return out


def build_view(spec: Mapping[str, Any], source_rows: Sequence[Mapping[str, Any]]) -> Any:
    """Project rows through one view. Returns a list, or a dict when group_by."""
    consts = _as_dict(spec.get("consts"))
    group_by = spec.get("group_by")
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    flat: List[Dict[str, Any]] = []

    for row in source_rows:
        if not _keep(row, spec):
            continue
        bucket = flat
        if group_by is not None:
            bucket = buckets.setdefault(str(row.get(group_by, "")), [])
        ctx: Dict[str, Any] = dict(row)
        ctx.update(consts)
        # index/index0 count within the GROUP, so a per-group ordinal is right.
        ctx.update({"index": len(bucket) + 1, "index0": len(bucket)})
        bucket.append(_build_row(spec, ctx))

    append = spec.get("append")
    if group_by is None:
        return _finish(flat, spec, append)
    # Bucket keys are stringified, so append keys must be too. Append-only
    # groups still get a bucket — a purely hand-maintained site is not
    # silently dropped from a grouped view.
    extras = {str(k): v for k, v in _as_dict(append).items()}
    for extra_key in extras:
        buckets.setdefault(extra_key, [])
    return {key: _finish(rows, spec, extras.get(key)) for key, rows in buckets.items()}


def build_views(views_cfg: Any, segments: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """{namespace: {view: rows}} — the output namespaces users read.

    The outer key is a free output label (it becomes `views.<namespace>` and
    qualifies bare `source:` references). Segment membership is filtered by
    each spec's `platform:` key — the two need not match.
    """
    out: Dict[str, Dict[str, Any]] = {}
    built: Dict[str, Any] = {}
    for namespace, group in _as_dict(views_cfg).items():
        out[namespace] = {}
        for view_name, raw_spec in _as_dict(group).items():
            spec = _as_dict(raw_spec)
            source = spec.get("source")
            rows: Sequence[Mapping[str, Any]] = segments
            if source:
                ref = source if "." in source else f"{namespace}.{source}"
                candidate = built.get(ref)
                rows = candidate if isinstance(candidate, list) else []
            result = build_view(spec, rows)
            out[namespace][view_name] = result
            built[f"{namespace}.{view_name}"] = result
    return out


# ──────────────────────────────────────────────────────────────────────────
# Stage 4 — partitions and validation
# ──────────────────────────────────────────────────────────────────────────
def partitions(segments: Sequence[Mapping[str, Any]],
               fields: Iterable[str]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """(by, cidrs_by, vlan_ids_by_platform) keyed by FIELD then by VALUE."""
    by: Dict[str, Dict[str, List[Any]]] = {}
    cidrs: Dict[str, Dict[str, List[Any]]] = {}
    for field in fields:
        by[field], cidrs[field] = _partition_by_field(segments, field)

    plat_rows, plat_cidrs, plat_vlans = _partition_by_platform(segments)
    by["platform"] = plat_rows
    cidrs["platform"] = plat_cidrs
    return by, cidrs, {p: sorted(set(v)) for p, v in plat_vlans.items()}


def _partition_by_field(segments: Sequence[Mapping[str, Any]], field: str
                        ) -> Tuple[Dict[str, List[Any]], Dict[str, List[Any]]]:
    """(rows, subnets) grouped by one field's values. Blank values are skipped."""
    rows_map: Dict[str, List[Any]] = {}
    cidr_map: Dict[str, List[Any]] = {}
    for row in segments:
        value = row.get(field)
        if value is None or str(value) == "":
            continue
        rows_map.setdefault(str(value), []).append(row)
        if row.get(F_SUBNET):
            cidr_map.setdefault(str(value), []).append(row[F_SUBNET])
    return rows_map, cidr_map


def _partition_by_platform(segments: Sequence[Mapping[str, Any]]
                           ) -> Tuple[Dict[str, List[Any]], Dict[str, List[Any]],
                                      Dict[str, List[int]]]:
    """(rows, subnets, tagged vlan ids) grouped by platform membership."""
    plat_rows: Dict[str, List[Any]] = {}
    plat_cidrs: Dict[str, List[Any]] = {}
    plat_vlans: Dict[str, List[int]] = {}
    for row in segments:
        for platform in _as_list(row.get(F_PLATFORMS)):
            plat_rows.setdefault(platform, []).append(row)
            if row.get(F_SUBNET):
                plat_cidrs.setdefault(platform, []).append(row[F_SUBNET])
            if row.get("tagged"):
                plat_vlans.setdefault(platform, []).append(row[F_VLAN_ID])
    return plat_rows, plat_cidrs, plat_vlans


def _validate_segment(key: str, seg: Mapping[str, Any], cfg: Mapping[str, Any],
                      source: str) -> List[str]:
    errs: List[str] = []
    known = _as_list(cfg.get("platforms"))
    raw_vlan = seg.get(F_VLAN_ID)
    if raw_vlan is None:
        errs.append(f"{source}.{key}: vlan_id is required")
    else:
        try:
            int(raw_vlan)
        except (TypeError, ValueError):
            errs.append(
                f"{source}.{key}: vlan_id '{raw_vlan}' is not a number (degraded to 0)"
            )
    raw = seg.get(F_PLATFORMS)
    if raw is not None and isinstance(raw, (str, Mapping)):
        errs.append(f"{source}.{key}: platforms must be a LIST, got {type(raw).__name__}")
    elif known:
        for platform in _as_list(raw):
            if platform not in known:
                errs.append(
                    f"{source}.{key}: unknown platform '{platform}' "
                    f"(allowed: {', '.join(known)})"
                )
    for rname in _as_dict(seg.get("names")):
        if rname not in _as_dict(cfg.get("names")):
            errs.append(f"{source}.{key}: names.'{rname}' is not a recipe in the name config")
    return errs


def _validate_views(cfg: Mapping[str, Any]) -> List[str]:
    errs: List[str] = []
    known = _as_list(cfg.get("platforms"))
    seen: List[str] = []
    for namespace, group in _as_dict(cfg.get("views")).items():
        for view_name, raw in _as_dict(group).items():
            label = f"views.{namespace}.{view_name}"
            if not isinstance(raw, Mapping):
                errs.append(f"{label} must be a dict, got {type(raw).__name__}")
                continue
            errs.extend(_validate_one_view(_as_dict(raw), label, namespace, seen, known))
            # `source:` references resolve against the NAMESPACE — never the
            # spec's platform filter, which may differ (dell vs switches).
            seen.append(f"{namespace}.{view_name}")
    return errs


def _validate_one_view(spec: Mapping[str, Any], label: str, namespace: str,
                       seen: Sequence[str], known: Sequence[Any]) -> List[str]:
    """Source resolves, platform filter is declared, fields is present."""
    errs: List[str] = []
    source = spec.get("source")
    if source:
        ref = source if "." in source else f"{namespace}.{source}"
        if ref not in seen:
            errs.append(f"{label}: source '{source}' is not a view declared earlier")
    # The view's `platform:` FILTER is checked against the declared
    # platforms; the outer namespace key is a free label and is not.
    filter_platform = spec.get("platform")
    if filter_platform and known and filter_platform not in known:
        errs.append(
            f"{label}: platform '{filter_platform}' is not in the declared platforms"
        )
    if not _as_dict(spec.get("fields")):
        errs.append(f"{label}: fields is required (output_key: source_field)")
    return errs


def _missing_fields(underlays: Mapping[str, Any], cfg: Mapping[str, Any]) -> List[str]:
    required = _as_dict(cfg.get("required"))
    always = _as_list(required.get("all"))
    by_platform = _as_dict(required.get("by_platform"))
    out: List[str] = []
    for key, raw in underlays.items():
        seg = _as_dict(raw)
        fields = list(always)
        for platform in _as_list(seg.get(F_PLATFORMS)):
            fields.extend(_as_list(by_platform.get(platform)))
        for field in dict.fromkeys(fields):
            if field not in seg or _is_empty(seg.get(field)):
                out.append(f"{key}: missing or empty {field}")
    return out


def _duplicates(values: Iterable[Any]) -> List[Any]:
    seen, dupes = set(), []
    for value in values:
        if value in seen and value not in dupes:
            dupes.append(value)
        seen.add(value)
    return dupes


def _validate(underlays: Mapping[str, Any], generated: Mapping[str, Any],
              hand: Mapping[str, Any], cfg: Mapping[str, Any]) -> List[str]:
    errs: List[str] = []
    if not underlays:
        errs.append("no segments — declare the segment matrix and/or pools")
    for key in generated:
        if key in hand:
            errs.append(
                f"pool-generated key '{key}' collides with a hand-written segment "
                "(the hand-written one wins)"
            )
    for key, raw in underlays.items():
        seg = _as_dict(raw)
        source = "pools" if key in generated and key not in hand else "underlays"
        if not seg:
            errs.append(f"{source}.{key} must be a dict")
            continue
        errs.extend(_validate_segment(key, seg, cfg, source))
    errs.extend(_validate_views(cfg))
    return errs


# ──────────────────────────────────────────────────────────────────────────
# entry point
# ──────────────────────────────────────────────────────────────────────────
def network_catalog(underlays: Any, config: Any = None) -> Dict[str, Any]:
    """Turn a segment matrix into every per-platform list, plus validation."""
    cfg = _as_dict(config)
    hand = _as_dict(underlays)
    root_errors: List[str] = []
    if underlays is not None and not isinstance(underlays, Mapping):
        root_errors.append(
            f"network_underlays must be a dict of segments, got {type(underlays).__name__}"
        )
    generated, pool_errors = expand_pools(cfg.get("pools"))

    merged: Dict[str, Any] = dict(generated)
    merged.update(hand)  # a hand-written key wins, so one pool member can be pinned

    segments = [enrich(key, _as_dict(raw), cfg) for key, raw in merged.items()]
    by, cidrs_by, vlan_ids_by_platform = partitions(
        segments, _as_list(cfg.get("partition_fields"))
    )
    tagged = [s for s in segments if s["tagged"]]

    return {
        "segments": segments,
        "views": build_views(cfg.get("views"), segments),
        "by": by,
        "cidrs_by": cidrs_by,
        "vlan_ids_by_platform": vlan_ids_by_platform,
        "vlan_ranges_by_platform": {
            p: _compress_ranges(v) for p, v in vlan_ids_by_platform.items()
        },
        "by_key": {s[F_KEY]: s for s in segments},
        "keys": [s[F_KEY] for s in segments],
        "names": [s[F_NAME] for s in segments],
        "vlan_ids": sorted({s[F_VLAN_ID] for s in segments}),
        "tagged_vlan_ids": sorted({s[F_VLAN_ID] for s in tagged}),
        "vlan_by_key": {s[F_KEY]: s[F_VLAN_ID] for s in segments},
        "name_by_key": {s[F_KEY]: s[F_NAME] for s in segments},
        "subnet_by_key": {s[F_KEY]: s[F_SUBNET] for s in segments if s.get(F_SUBNET)},
        "gateway_by_key": {s[F_KEY]: s["gateway"] for s in segments if s.get("gateway")},
        "key_by_vlan": {str(s[F_VLAN_ID]): s[F_KEY] for s in segments},
        "operator_cidrs": [s[F_SUBNET] for s in segments
                           if s["operator_source"] and s.get(F_SUBNET)],
        "derived_names": {s[F_KEY]: s["derived_name"] for s in segments},
        "name_overrides": [
            {F_KEY: s[F_KEY], F_NAME: s[F_NAME], "derived_name": s["derived_name"]}
            for s in segments if s[F_NAME] != s["derived_name"]
        ],
        "errors": root_errors + pool_errors + _validate(merged, generated, hand, cfg),
        "missing": _missing_fields(merged, cfg),
        "duplicate_names": _duplicates(s[F_NAME] for s in segments),
        "duplicate_vlans": _duplicates(s[F_VLAN_ID] for s in tagged),
    }


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {"network_catalog": network_catalog}
