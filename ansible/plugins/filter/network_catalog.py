"""Network catalog engine — one SSOT of network segments, many platform lists.

FULL REFERENCE: plugins/filter/docs/network_catalog.md — every option, its
default, what it does, worked examples and expected output.

ESTATE-AGNOSTIC. This module knows nothing about any particular site, role,
zone, platform or naming convention: every one of those comes from the config
dict it is handed. Drop it into any Ansible repo, point it at a segment matrix,
and it produces exactly the row shapes that repo's modules loop over.

Used once, from group_vars, so consumers see plain data rather than a filter
call at every site:

    __catalog: "{{ network_underlays | network_catalog(config) }}"
    _net: "{{ __catalog.views }}"     # _net.hypervisor.port_groups, _net.switches.vlans

Config keys (all optional except the segments themselves):

    pools             spec-driven segments (instances x roles) merged with
                      the hand-written ones
    pools_file        path to a YAML file holding those pools, read directly
                      so its `{{ }}` templates never meet Ansible's templar.
                      Ignored when `pools` is given inline
    names             name RECIPES — how to build each name from tokens
    name_default      which recipe is the segment's primary name
    platforms         platform names allowed in a segment's platforms[]
    required          {all: [field], by_platform: {platform: [field]}}
    defaults          fields every segment inherits unless it overrides
    partition_fields  fields to build by/cidrs_by partitions from
    uniqueness_scope  fields bounding one L2 domain; duplicate vlan/name
                      checks group by these first. Unset = estate-wide
    pool_max_segments ceiling on what one pool may generate. 0 or unset means
                      the built-in default (2000); negatives are refused

Per-segment fields the engine reads directly: vlan_id, platforms[], instance
(feeds the instance / instance_nn name tokens), and whatever your recipes and
views reference. Everything else passes through untouched.

Returns a dict with: segments, by, cidrs_by, vlan_ids_by_platform,
vlan_ranges_by_platform, by_key, keys, names, vlan_ids, tagged_vlan_ids,
operator_cidrs, derived_names, name_overrides, errors, missing,
duplicate_names, duplicate_vlans.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml

# Tokens the engine contributes on top of the segment's own fields. Every other
# token in a name recipe is simply a field name on the segment.
VLAN_TOKENS = ("vlan", "vlan_label", "vid")

# Keys consumed by the pool generator itself; everything else a pool declares is
# stamped onto each segment it emits.
POOL_GENERATOR_KEYS = frozenset(
    {"vlan_base", "instances", "vlan_stride", "roles", "subnet_base",
     "subnet_stride", "gateway_offset", "subnet_index",
     # `key`/`name` are this pool's own templates. Everything else a pool
     # declares — including name_parts/name_case — is stamped onto each
     # segment, because those are the SEGMENT's naming recipe, not the
     # generator's.
     "key", "name"}
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

# Fields `enrich` computes and overwrites UNCONDITIONALLY. Declaring one is not
# an override — it is a value that is silently discarded. `tagged: false` on a
# tagged VLAN read exactly like a way to keep a segment off a trunk, and was
# not one. Value = what the engine derives it from, for the error message.
COMPUTED_FIELDS = {
    "tagged": "vlan_id",
    "gateway_cidr": "gateway and the subnet prefix",
    "derived_name": "the default name recipe",
    F_KEY: "the segment's own key in the matrix",
}

# Field names the engine itself reads or computes. A name RECIPE may not take
# one: a segment carrying that field pins the recipe's name to its own value,
# so a `gateway` recipe plus `gateway: 10.0.0.1` makes the primary name an IP
# address. `name` is deliberately absent — pinning `name:` IS the documented
# mechanism, and `description`/`instance` are ordinary user data.
ENGINE_FIELDS = frozenset({
    F_KEY, F_VLAN_ID, F_PLATFORMS, F_SUBNET, "gateway", "netmask", "prefixlen",
    "tagged", "gateway_cidr", "names", "operator_source", "derived_name",
    "desc_template", "role", "pool",
})


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


def _string_where_list_expected(label: str, value: Any) -> Optional[str]:
    """A bare string where a list belongs is dropped, not read.

    `_as_list` returns [] for a str on purpose — a string IS a sequence in
    Python and iterating it character-by-character would be worse. But every
    caller then saw "not configured" instead of "configured wrongly", so
    `uniqueness_scope: site` silently compared estate-wide and `roles: app`
    silently generated nothing. A segment's `platforms:` already refuses this;
    the rest did not.
    """
    if isinstance(value, (str, bytes)):
        return (f"{label} must be a LIST, got the string {value!r} — "
                f"write [{value}]")
    return None


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce to int; unparseable degrades to the default instead of raising."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_BOOL_TRUE = frozenset({"true", "yes", "on", "y", "t", "1"})
_BOOL_FALSE = frozenset({"false", "no", "off", "n", "f", "0", ""})


def _as_bool(value: Any, default: bool = False) -> bool:
    """Coerce YAML-ish truth to a real bool.

    `bool("false")` is True, which is how a quoted flag ends up enabling the
    thing it was meant to disable — for `operator_source` that means a subnet
    silently joining bastion and proxy allow-lists. Anything unrecognised
    degrades to `default` and is reported separately by _validate, so an
    unreadable flag never widens access on its own.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    return default


def _bool_is_readable(value: Any) -> bool:
    """Would _as_bool understand this, or is it falling back to the default?"""
    if value is None or isinstance(value, (bool, int, float)):
        return True
    return str(value).strip().lower() in (_BOOL_TRUE | _BOOL_FALSE)


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


def _strict_env(env: Any) -> Any:
    """The caller's Jinja environment, but an undefined name RAISES.

    Jinja's default Undefined renders as an empty string, so a typo'd token
    was not an error — it was a shorter key. `{{ pool }}-{{ nope }}` gave every
    member of the pool the key `p-`, which then collapsed under the collision
    check into one surviving segment, reported as a name clash rather than as
    the typo it was.

    `| default(...)` still works: the default filter inspects an undefined
    value rather than rendering it, so an optional token stays optional.
    """
    if env is None:
        return None
    try:
        from jinja2 import StrictUndefined

        return env.overlay(undefined=StrictUndefined)
    except (ImportError, AttributeError, TypeError):  # pragma: no cover
        # An environment that cannot be overlaid still renders; it just keeps
        # the lenient undefined. Better a permissive render than no catalog.
        return env


def _render_template(env: Any, source: str, tokens: Mapping[str, Any],
                     where: str) -> str:
    """Render one `{{ }}` pool template against a member's tokens.

    Pools are loaded through `lookup('file', ...) | from_yaml` for the same
    reason views are: Ansible renders a variable the moment it is referenced,
    so a `{{ pool }}` sitting in group_vars is templated before any member
    exists. A filter chain's output is not re-templated, which is what lets
    the braces survive to here.
    """
    if env is None:
        raise ValueError(
            f"{where}: '{source}' uses {{{{ }}}} but no Jinja environment "
            f"reached the generator. Pools using templates must be loaded "
            f"with `lookup('file', ...) | from_yaml`."
        )
    try:
        return str(env.from_string(source).render(dict(tokens)))
    except Exception as exc:
        raise ValueError(f"{where}: '{source}' failed — {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────
# Stage 0 — pools
# ──────────────────────────────────────────────────────────────────────────
def _normalise_roles(raw: Any) -> List[Dict[str, Any]]:
    """`roles:` -> [{name, offset, fields}]. Offset is the LIST POSITION.

        roles: [app, {db: {offset: 5, mtu: 1500}}]

    A mapping form used to be accepted too, with its own offset rules. Two
    grammars for one list is two things to learn and two code paths to test,
    and no pool ever used the second.
    """
    specs: List[Dict[str, Any]] = []
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
    # A negative index walks BACKWARDS out of the pool's own block and still
    # produces a perfectly valid network, so it validated clean and shipped —
    # subnet_base 10.0.0.0/24 with a role offset of -5 became 9.255.251.0/24.
    # It happens whenever `subnet_index: vlan` meets a role offset that puts a
    # member below vlan_base.
    if index < 0:
        return (f"pools.{pool_key}: computed subnet index for '{seg_key}' is "
                f"{index}, which is below the pool's own base {base_net} — a "
                f"role offset is putting this member under vlan_base. Raise "
                f"vlan_base or drop the negative offset.")

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
        gateway = subnet.network_address + gateway_offset
        # `in subnet` is true for the network and broadcast addresses too, so
        # the existing bounds check let gateway_offset 255 on a /24 through.
        # Neither is assignable to an interface.
        if not _is_usable_host(subnet, gateway):
            return (f"pools.{pool_key}: gateway {gateway} for '{seg_key}' is "
                    f"the {'network' if gateway == subnet.network_address else 'broadcast'}"
                    f" address of {subnet}, which no interface can hold")
        seg.setdefault("gateway", str(gateway))
    return None


def _is_usable_host(subnet: Any, address: Any) -> bool:
    """Can an interface actually take this address on this subnet?

    NEVER materialise `hosts()` to answer this. A /64 has 2**64 addresses and
    building that set never returns — an IPv6 `subnet_base` with any
    `gateway_offset` hung the whole play, with no output to say why. A /8 is
    the same bug with a smaller exponent: 16M address objects.

    A /31, /32, /127 and /128 have no network/broadcast convention, so every
    address in them is usable. Otherwise the network address is not. IPv4 also
    reserves the last address as broadcast; IPv6 does not — there the last
    address is an ordinary interface address, which is exactly what `hosts()`
    models for each family. Verified equal to `set(hosts())` on both.
    """
    if address not in subnet:
        return False
    if subnet.num_addresses <= 2:
        return True
    if address == subnet.network_address:
        return False
    return subnet.version != 4 or address != subnet.broadcast_address


DEFAULT_POOL_MAX_SEGMENTS = 2000


def load_pools_file(path: Any) -> Tuple[Any, List[str]]:
    """Read a pools file from disk. Returns (parsed, errors).

    Pools carry `{{ }}` templates for their keys and names, and Ansible renders
    a group_vars value the moment it is referenced — before any member exists
    to render against. Reading the file HERE keeps those braces out of the
    templar entirely, and turns a misspelled path into one named error instead
    of a lookup traceback thrown from the middle of templating `__catalog`.

    An unset path is not an error: a catalog may legitimately have no pools.
    """
    text = str(path or "").strip()
    if not text:
        return None, []
    try:
        with open(text, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle), []
    except OSError as exc:
        return None, [
            f"pools_file '{text}' could not be read — {exc.strerror or exc}"
        ]
    except yaml.YAMLError as exc:
        return None, [f"pools_file '{text}' is not valid YAML — {exc}"]


def _resolve_pool_cap(raw: Any) -> Tuple[int, List[str]]:
    """The per-pool segment ceiling, and any complaint about how it was set.

    0 and unset both mean "use the default" — the wiring layer passes 0 for an
    undeclared setting, so the two cannot be told apart and are documented as
    the same thing. A NEGATIVE cap is different: every pool then trips the
    over-limit guard and the message reads as though the estate were too big,
    when the cap itself is the bug.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_POOL_MAX_SEGMENTS, []
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POOL_MAX_SEGMENTS, [
            f"pool_max_segments {raw!r} is not a number — using the default "
            f"{DEFAULT_POOL_MAX_SEGMENTS}"
        ]
    if cap == 0:
        return DEFAULT_POOL_MAX_SEGMENTS, []
    if cap < 0:
        return DEFAULT_POOL_MAX_SEGMENTS, [
            f"pool_max_segments is {cap} — it must be a POSITIVE integer "
            f"(0 or unset means the default {DEFAULT_POOL_MAX_SEGMENTS}). "
            f"Using the default; no pool was refused for being over a "
            f"negative limit."
        ]
    return cap, []


def _pool_guards(plan: Mapping[str, Any], cap: int) -> List[str]:
    """Refuse a pool before expanding it, not after.

    Both of these are typos that look like configuration. A stride at or below
    the widest role offset makes instance N+1 start inside instance N's block,
    so members quietly share VLANs — caught today only as a duplicate, which
    names the collision but not the cause. And nothing bounded the expansion:
    `instances: 5000` across five roles is 25,000 segments built before anyone
    can object.
    """
    errs: List[str] = []
    roles = plan["roles"]
    widest = max((role["offset"] for role in roles), default=0)
    if plan["instances"] > 1 and plan["stride"] <= widest:
        errs.append(
            f"pools.{plan['pool_key']}: vlan_stride is {plan['stride']} but the "
            f"roles span offsets 0..{widest}, so instance 2 would start inside "
            f"instance 1's block and members would share VLANs. Use a stride "
            f"of at least {widest + 1}."
        )
    total = plan["instances"] * len(roles)
    if total > cap:
        errs.append(
            f"pools.{plan['pool_key']}: would generate {total} segments "
            f"({plan['instances']} instances x {len(roles)} roles), over the "
            f"{cap} limit. Raise pool_max_segments if that is genuinely "
            f"wanted; otherwise check instances."
        )
    return errs


def expand_pools(pools: Any, env: Any = None,
                 cap: int = DEFAULT_POOL_MAX_SEGMENTS
                 ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
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
    if pools is not None and not isinstance(pools, Mapping):
        # A pools file written as a LIST parsed fine, coerced to {} and left a
        # catalog that reported zero errors and zero pools — indistinguishable
        # from not having any. Views already refuse the wrong root shape; so
        # does this now.
        return out, [
            f"pools must be a mapping of pool name -> spec, got "
            f"{type(pools).__name__} — check the top level of the pools file"
        ]
    env = _strict_env(env)
    for pool_key, raw_pool in _as_dict(pools).items():
        if raw_pool is not None and not isinstance(raw_pool, Mapping):
            errors.append(
                f"pools.{pool_key} must be a mapping of pool settings, got "
                f"{type(raw_pool).__name__}"
            )
            continue
        pool = _as_dict(raw_pool)
        if not pool:
            continue
        plan, addr_errors = _pool_plan(pool_key, pool)
        errors.extend(addr_errors)
        guard_errors = _pool_guards(plan, cap)
        if guard_errors:
            # Emit nothing for a pool whose shape is wrong: the members would
            # be built from the same bad numbers the error is about.
            errors.extend(guard_errors)
            continue
        errors.extend(_collect_pool_members(plan, env, out))
    return out, errors


def _collect_pool_members(plan: Mapping[str, Any], env: Any,
                          out: Dict[str, Dict[str, Any]]) -> List[str]:
    """Insert one pool's members into `out`; return what went wrong."""
    errors: List[str] = []
    for key, seg, err in _pool_members(plan, env):
        if err:
            errors.append(err)
        if key is None:
            # No identity — the error above says why. Emitting it anyway is
            # what put `<pool>-error-<n>-<role>` segments into live views.
            continue
        if key in out:
            # Silently overwriting loses a whole segment from every derived
            # view while the health check still reads clean, so a colliding
            # key is an error, not a last-one-wins.
            errors.append(
                f"pool '{plan['pool_key']}': generated key '{key}' collides with an "
                f"earlier generated segment (vlan {out[key].get(F_VLAN_ID)} vs "
                f"{seg.get(F_VLAN_ID)}) — widen `key:` so each member is unique"
            )
            continue
        out[key] = seg
    return errors


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
        "base_net": base_net,
        "subnet_stride": _as_int(pool.get("subnet_stride", 1), 1) or 1,
        "gateway_offset": _as_int(pool.get("gateway_offset", 0)),
        "vlan_indexed": pool.get("subnet_index") == "vlan",
        "key_template": pool.get("key") if isinstance(pool.get("key"), str) else "",
        "name_template": pool.get("name") if isinstance(pool.get("name"), str) else "",
    }
    return plan, addr_errors


def _pool_member_identity(plan: Mapping[str, Any], seg: Dict[str, Any],
                          tokens: Mapping[str, Any], n: int,
                          role: Mapping[str, Any], env: Any
                          ) -> Tuple[Optional[str], Optional[str]]:
    """One member's catalog key, with its `name:` template stamped onto it.

    Returns (None, error) when the member has no usable identity — a template
    that failed to render, or one that rendered blank. Such a member is not
    generated at all. It used to be: a failed render still emitted the segment
    under the placeholder key `<pool>-error-<n>-<role>`, carrying a real
    vlan_id and real on_<platform> flags, so a reported error still shipped
    four segments into every derived view under a name nobody wrote.
    """
    where = f"pools.{plan['pool_key']}"
    # `key:` is the only grammar: the separators, literal text and order are
    # all visible in the one string.
    try:
        key = _render_template(env, plan["key_template"], tokens, f"{where}.key")
        if plan["name_template"]:
            seg[F_NAME] = _render_template(env, plan["name_template"], tokens,
                                           f"{where}.name")
    except ValueError as exc:
        return None, str(exc)
    if not str(key).strip():
        return None, (
            f"{where}: instance {n} role '{role[F_NAME]}' rendered a BLANK key "
            f"from {plan['key_template']!r} — nothing can "
            f"address a segment with no key, so it is not generated. Check "
            f"every token that key names actually exists on this pool."
        )
    return key, None


def _pool_members(plan: Mapping[str, Any], env: Any = None):
    """Yield (key, segment, error) for every instance x role the pool declares.

    A key of None means the member has no identity and must not be emitted;
    the error alongside it says why.
    """
    emitted = 0
    for n in range(1, plan["instances"] + 1):
        base = plan["base_vlan"] + (n - 1) * plan["stride"]
        for role in plan["roles"]:
            vlan = base + role["offset"]
            seg = _pool_segment(plan["pool"], role, vlan, n, plan["pool_key"])
            tokens = _pool_key_tokens(seg, plan["pool_key"], n, vlan, role)
            key, identity_error = _pool_member_identity(plan, seg, tokens, n,
                                                        role, env)
            if key is None:
                yield None, None, identity_error
                continue
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
    """The segment's own fields plus the pool-only tokens `key:` may name."""
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
    # max(0, ...): a negative pad lands inside the format spec as "0-3d" and
    # f-string raises. Validation reports it; this keeps the render itself
    # total so one bad recipe cannot abort the whole catalog.
    vid = f"{vlan_num:0{max(0, _as_int(spec.get('vlan_pad') or 0))}d}"
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
            # A nested list here would be used as a dict key — unhashable —
            # so one level of glue is the whole grammar. Validation names it;
            # skipping keeps the render total.
            if any(_is_group(t) for t in token):
                continue
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
            "gateway_cidr": _gateway_cidr(gateway, prefixlen, seg.get(F_SUBNET)),
            "names": names,
            F_NAME: primary,
            "derived_name": names.get(default_recipe, ""),
            # Declared or absent. A `desc_template` str.format fallback used
            # to live here — a THIRD string sublanguage beside the name recipes
            # and the view fields, with an empty estate template and no segment
            # ever setting its own. It described nothing.
            "description": str(seg.get("description") or ""),
            "operator_source": _as_bool(seg.get("operator_source")),
            F_INSTANCE: seg.get(F_INSTANCE, ""),
        }
    )
    # on_<platform> booleans for every DECLARED platform, so selectattr() needs
    # no 'contains' test and the engine still knows no platform by name.
    for platform in _as_list(cfg.get("platforms")):
        seg[f"on_{platform}"] = platform in platforms
    return seg




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


def _validate_addressing(key: str, seg: Mapping[str, Any], source: str) -> List[str]:
    """L3 sanity: the subnet parses, and gateway and netmask agree with it.

    A gateway outside its own subnet and a netmask that contradicts the CIDR
    prefix both used to validate clean and only surface as a device rejecting
    the config — or worse, accepting it and blackholing the segment.
    """
    errs: List[str] = []
    raw_subnet = seg.get(F_SUBNET)
    if not raw_subnet:
        return errs
    try:
        network = ipaddress.ip_network(str(raw_subnet), strict=False)
    except ValueError as exc:
        return [f"{source}.{key}: subnet '{raw_subnet}' is not a valid CIDR ({exc})"]

    gateway = seg.get("gateway")
    if gateway:
        try:
            errs.extend(_gateway_placement(key, network,
                                           ipaddress.ip_address(str(gateway)), source))
        except ValueError as exc:
            errs.append(f"{source}.{key}: gateway '{gateway}' is not an IP ({exc})")

    netmask = seg.get("netmask")
    if netmask and str(netmask) != str(network.netmask):
        errs.append(
            f"{source}.{key}: netmask {netmask} contradicts subnet {network} "
            f"(which is {network.netmask})"
        )
    return errs


def _gateway_placement(key: str, network: Any, gateway: Any,
                       source: str) -> List[str]:
    """Is a hand-written gateway an address an interface can actually hold?

    The POOL path has refused a network/broadcast gateway since round 3; the
    hand-written path only checked `in network`, which is true for both of
    them. Same subnet, same wrong gateway, two different answers depending on
    which file the segment happened to be written in.
    """
    if gateway not in network:
        return [f"{source}.{key}: gateway {gateway} is outside its subnet "
                f"{network} — nothing on this segment can reach it"]
    if _is_usable_host(network, gateway):
        return []
    role = "network" if gateway == network.network_address else "broadcast"
    return [f"{source}.{key}: gateway {gateway} is the {role} address of "
            f"{network}, which no interface can hold"]


def _gateway_cidr(gateway: Any, prefixlen: int, subnet: Any) -> str:
    """`<gateway>/<prefixlen>`, but only when the two belong to each other.

    A v6 gateway against a v4 subnet built 'fd00::1/24' — a string that is not
    an address in any family, handed on to whatever templates a device's
    interface line. The mismatch is reported separately; this stops the
    nonsense value being built at all.
    """
    if not gateway or not prefixlen:
        return ""
    try:
        address = ipaddress.ip_address(str(gateway))
    except ValueError:
        return ""
    if isinstance(subnet, str) and subnet:
        try:
            if ipaddress.ip_network(subnet, strict=False).version != address.version:
                return ""
        except ValueError:
            return ""
    return f"{address}/{prefixlen}"


def _validate_segment(key: str, seg: Mapping[str, Any], cfg: Mapping[str, Any],
                      source: str) -> List[str]:
    errs: List[str] = []
    known = _as_list(cfg.get("platforms"))
    raw_vlan = seg.get(F_VLAN_ID)
    if raw_vlan is None:
        errs.append(f"{source}.{key}: vlan_id is required")
    else:
        try:
            vlan_num = int(raw_vlan)
        except (TypeError, ValueError):
            errs.append(
                f"{source}.{key}: vlan_id '{raw_vlan}' is not a number (degraded to 0)"
            )
        else:
            # 0 is the untagged/native case and stays legal. 4095 is reserved
            # by 802.1Q and anything above it cannot exist on the wire, but
            # both used to validate clean and only fail at the device.
            if not 0 <= vlan_num <= 4094:
                errs.append(
                    f"{source}.{key}: vlan_id {vlan_num} is outside the 802.1Q "
                    f"range — tagged VLANs are 1..4094 (0 = untagged; 4095 is "
                    f"reserved)"
                )
    errs.extend(_validate_addressing(key, seg, source))
    if not _bool_is_readable(seg.get("operator_source")):
        errs.append(
            f"{source}.{key}: operator_source '{seg.get('operator_source')}' is "
            f"not a recognisable boolean — treated as false, so this subnet is "
            f"NOT in the operator allow-lists. Use true or false unquoted."
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
    errs.extend(_validate_computed_fields(key, seg, cfg, source))
    return errs


def _validate_computed_fields(key: str, seg: Mapping[str, Any],
                              cfg: Mapping[str, Any], source: str) -> List[str]:
    """A segment may not declare what the engine computes.

    Every one of these is overwritten by `enrich`, so the declared value never
    reaches a device — but it reads, in the file, exactly like configuration.
    """
    errs = [
        f"{source}.{key}: '{field}' is computed by the engine (from {owner}) "
        f"and a declared value is silently discarded — remove it."
        for field, owner in COMPUTED_FIELDS.items() if field in seg
    ]
    # prefixlen is an INPUT when there is no subnet to derive it from, and an
    # output when there is. Only the second case is a discarded value.
    if F_SUBNET in seg and "prefixlen" in seg:
        errs.append(
            f"{source}.{key}: 'prefixlen' is computed from subnet "
            f"'{seg[F_SUBNET]}' and a declared value is silently discarded — "
            f"remove it. (A segment with no subnet may declare prefixlen.)"
        )
    for platform in _as_list(cfg.get("platforms")):
        if f"on_{platform}" in seg:
            errs.append(
                f"{source}.{key}: 'on_{platform}' is computed from platforms[] "
                f"and a declared value is silently discarded — add or remove "
                f"'{platform}' in platforms instead."
            )
    return errs


def _validate_name_graph(cfg: Mapping[str, Any]) -> List[str]:
    """`name_default` resolves, and every `from:` points somewhere usable."""
    recipes = _as_dict(cfg.get("names"))
    errs: List[str] = []
    default = cfg.get("name_default")
    if default and default not in recipes:
        errs.append(
            f"name_default '{default}' is not a declared recipe (have: "
            f"{', '.join(recipes) or 'none'}) — every segment's primary name "
            f"would be blank"
        )
    order = list(recipes)
    for rname, raw in recipes.items():
        parent = _as_dict(raw).get("from")
        if not parent:
            continue
        if parent not in recipes:
            errs.append(f"names.{rname}: from '{parent}' is not a declared "
                        f"recipe — this recipe inherits nothing")
        elif parent == rname:
            errs.append(f"names.{rname}: from '{parent}' refers to itself")
        elif order.index(parent) > order.index(rname):
            # `from` inherits the parent AS RESOLVED FOR THIS SEGMENT, which
            # only exists once the parent has run. A forward reference falls
            # back to the parent's raw definition, losing the segment's own
            # overrides. Every cycle contains at least one forward edge, so
            # this catches circular `from` chains too.
            errs.append(f"names.{rname}: from '{parent}' is declared LATER — a "
                        f"recipe can only inherit one declared before it")
    return errs


def _validate_primary_names(segments: Sequence[Mapping[str, Any]],
                            cfg: Mapping[str, Any]) -> List[str]:
    """Segments whose PRIMARY name came out blank.

    Nothing downstream can address one: it becomes a nameless port group, a
    nameless VLAN. The four-list gate checks errors, missing fields and
    duplicates — a single blank name is none of those, so it shipped.

    Skipped when the recipe set is empty or the default recipe is already
    reported missing; both would report every segment for one root cause.
    """
    recipes = _as_dict(cfg.get("names"))
    default = cfg.get("name_default") or (next(iter(recipes), ""))
    if not recipes or default not in recipes:
        return []
    errs: List[str] = []
    for seg in segments:
        primary = seg.get(F_NAME, "")
        if not isinstance(primary, (str, bytes)):
            # A field named after a recipe PINS that name, so a recipe sharing
            # its name with a list-valued field makes the primary name a list.
            errs.append(
                f"{seg[F_KEY]}: primary name is a {type(primary).__name__}, not "
                f"a name — recipe '{default}' shares its name with a field on "
                f"this segment, and that field's value pinned it"
            )
        elif not str(primary).strip():
            errs.append(
                f"{seg[F_KEY]}: primary name is blank — recipe '{default}' "
                f"produced nothing for this segment"
            )
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


def _hashable(value: Any) -> Any:
    """A set-safe stand-in for a value that might be a list or a dict.

    A name is normally a string, but a recipe named after a list-valued field
    pins the primary name to that list, and the duplicate scan then died on
    `value in seen` — a TypeError raised while BUILDING the return dict, so
    the whole catalog aborted and the validation naming the real problem was
    never readable. `_dedupe` has taken this precaution all along.
    """
    if isinstance(value, (str, int, float, bool, tuple, type(None))):
        return value
    return repr(value)


def _duplicates(values: Iterable[Any]) -> List[Any]:
    seen: set = set()
    reported: set = set()
    dupes: List[Any] = []
    for value in values:
        marker = _hashable(value)
        if marker in seen and marker not in reported:
            reported.add(marker)
            dupes.append(value)
        seen.add(marker)
    return dupes


def _duplicates_scoped(segments: Iterable[Mapping[str, Any]], field: str,
                       scope: Sequence[str]) -> List[Any]:
    """Values repeated WITHIN a scope group, in first-seen order.

    Segments carrying none of the scope fields land in one group together, so
    a catalog that never declares `site` behaves exactly as it did before.
    """
    groups: Dict[Tuple[str, ...], List[Any]] = {}
    for seg in segments:
        key = tuple(str(seg.get(name, "")) for name in scope)
        groups.setdefault(key, []).append(seg[field])

    dupes: List[Any] = []
    for values in groups.values():
        for value in _duplicates(values):
            if value not in dupes:
                dupes.append(value)
    return dupes


def _validate_pool_specs(pools: Any) -> List[str]:
    """Shape and precedence complaints about the pool specs themselves."""
    errs: List[str] = []
    for pool_key, raw_pool in _as_dict(pools).items():
        pool = _as_dict(raw_pool)
        problem = _string_where_list_expected(f"pools.{pool_key}.roles",
                                              pool.get("roles"))
        if problem:
            errs.append(problem)
        if isinstance(pool.get("roles"), Mapping):
            errs.append(
                f"pools.{pool_key}: `roles:` must be a LIST — the offset is the "
                f"list position. Per-role settings go in a single-key mapping "
                f"inside it: roles: [app, {{db: {{offset: 5}}}}]"
            )
        key_template = pool.get(F_KEY)
        if not (isinstance(key_template, str) and key_template.strip()):
            errs.append(
                f"pools.{pool_key}: `key:` is required — it is the template "
                f"that names each generated segment, e.g. "
                f'"{{{{ pool }}}}-{{{{ instance_nn }}}}-{{{{ role }}}}".'
            )
    return errs


def _validate(underlays: Mapping[str, Any], generated: Mapping[str, Any],
              hand: Mapping[str, Any], cfg: Mapping[str, Any],
              pools: Any = None) -> List[str]:
    errs: List[str] = []
    if not underlays:
        errs.append("no segments — declare the segment matrix and/or pools")
    for key in generated:
        if key in hand:
            errs.append(
                f"pool-generated key '{key}' collides with a hand-written segment "
                "(the hand-written one wins)"
            )
    for label, value in (("uniqueness_scope", cfg.get("uniqueness_scope")),
                         ("partition_fields", cfg.get("partition_fields")),
                         ("platforms", cfg.get("platforms"))):
        problem = _string_where_list_expected(label, value)
        if problem:
            errs.append(problem)
    errs.extend(_validate_name_graph(cfg))
    for recipe_name, raw_recipe in _as_dict(cfg.get("names")).items():
        recipe = _as_dict(raw_recipe)
        if recipe_name in ENGINE_FIELDS:
            errs.append(
                f"names.{recipe_name}: a recipe may not be named after a field "
                f"the engine reads. A segment carrying '{recipe_name}' pins the "
                f"name to that value — a `gateway` recipe on a segment with "
                f"`gateway: 10.0.0.1` makes the primary name an IP address. "
                f"Rename the recipe."
            )
        if _as_int(recipe.get("vlan_pad") or 0) < 0:
            errs.append(
                f"names.{recipe_name}: vlan_pad is "
                f"{recipe.get('vlan_pad')!r} — a pad cannot be negative"
            )
        for token in _as_list(recipe.get("parts")):
            if _is_group(token) and any(_is_group(t) for t in token):
                errs.append(
                    f"names.{recipe_name}: parts contains a glue group nested "
                    f"more than one deep ({token!r}). A group glues plain "
                    f"tokens; it cannot contain another group."
                )

    errs.extend(_validate_pool_specs(pools if pools is not None
                                     else cfg.get("pools")))

    for key, raw in underlays.items():
        seg = _as_dict(raw)
        source = "pools" if key in generated and key not in hand else "underlays"
        if not seg:
            errs.append(f"{source}.{key} must be a dict")
            continue
        errs.extend(_validate_segment(key, seg, cfg, source))
    return errs


# ──────────────────────────────────────────────────────────────────────────
# entry point
# ──────────────────────────────────────────────────────────────────────────
def network_catalog(underlays: Any, config: Any = None,
                    env: Any = None) -> Dict[str, Any]:
    """Turn a segment matrix into every per-platform list, plus validation."""
    cfg = _as_dict(config)
    hand = _as_dict(underlays)
    root_errors: List[str] = []
    if underlays is not None and not isinstance(underlays, Mapping):
        root_errors.append(
            f"network_underlays must be a dict of segments, got {type(underlays).__name__}"
        )
    # No default here on purpose. What bounds an L2 domain is an estate fact,
    # so it belongs in the config the estate edits, not in this file — a
    # Python fallback would be a behaviour nobody reading the YAML can see.
    # Unset means compare estate-wide, which is what a catalog with no site
    # field wants anyway.
    scope = [str(f) for f in _as_list(cfg.get("uniqueness_scope"))]
    # Inline `pools` wins, so a caller (and the unit suite) can hand the
    # generator a dict directly; `pools_file` is the wiring the estate uses.
    pools = cfg.get("pools")
    if pools is None:
        pools, root_file_errors = load_pools_file(cfg.get("pools_file"))
        root_errors.extend(root_file_errors)
    cap, cap_errors = _resolve_pool_cap(cfg.get("pool_max_segments"))
    root_errors.extend(cap_errors)
    generated, pool_errors = expand_pools(pools, env, cap)

    merged: Dict[str, Any] = dict(generated)
    merged.update(hand)  # a hand-written key wins, so one pool member can be pinned

    segments = [enrich(key, _as_dict(raw), cfg) for key, raw in merged.items()]
    by, cidrs_by, vlan_ids_by_platform = partitions(
        segments, _as_list(cfg.get("partition_fields"))
    )
    tagged = [s for s in segments if s["tagged"]]

    return {
        "segments": segments,
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
        "errors": root_errors + pool_errors
                  + _validate(merged, generated, hand, cfg, pools)
                  + _validate_primary_names(segments, cfg),
        "missing": _missing_fields(merged, cfg),
        "duplicate_names": _duplicates_scoped(segments, F_NAME, scope),
        "duplicate_vlans": _duplicates_scoped(tagged, F_VLAN_ID, scope),
    }


try:  # optional: only the filter path needs it, tests call the plain function
    from jinja2 import pass_context as _pass_context
except ImportError:  # pragma: no cover
    def _pass_context(func):  # type: ignore[misc]
        return func


@_pass_context
def _network_catalog_filter(ctx: Any, underlays: Any,
                            config: Any = None) -> Dict[str, Any]:
    """Filter entry point — hands the generator a Jinja environment.

    network_catalog() itself stays a plain function so the unit suite can call
    it without a context, as it has all along.
    """
    return network_catalog(underlays, config,
                           env=getattr(ctx, "environment", None))


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {"network_catalog": _network_catalog_filter}
