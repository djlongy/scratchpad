"""Project catalog segments into per-platform lists you declare in YAML.

`network_catalog` already enriches every segment — names, `on_<platform>`
flags, defaults merged. This turns those rows into the exact shape a module
loops over, using nothing but ordinary Ansible template syntax:

    hypervisor:
      port_groups:
        platform: hypervisor
        fields:
          name:   "{{ seg.name }}"
          switch: "{{ seg.hv_vswitch }}"
          vlan:   "{{ seg.vlan_id }}"
          ports:  "{{ seg.hv_num_ports | default(0) }}"

`seg` is the current segment, the way `item` is the current loop element.
Ordinary play variables are in scope too, so a site-wide default reads the
way you would write it anywhere else:

    switch: "{{ seg.hv_vswitch | default(hv_switch_name) }}"

`{{ }}`, `~`, `| default(...)`, interpolation — all mean what they mean
everywhere else in Ansible. Someone editing a view never needs to know this
file exists.

HOW THE `{{ }}` SURVIVES. Ansible templates a variable the moment it is
referenced, so a views dict declared as a plain var would be rendered before
any row exists — against a scope with no `seg` in it. Load it through a
filter chain instead, whose output Ansible does not re-template:

    _views: "{{ _segments | network_views(lookup('file', path) | from_yaml) }}"

`!unsafe` is not an alternative: on a mapping it preserves the top-level
strings and EMPTIES the nested ones, silently destroying the view specs.

Spec keys — `platform` and `fields` required, the rest optional:

    platform   membership label; selects rows whose on_<platform> is true.
               Required, EXCEPT on a chained view (see `source`)
    source     name of an EARLIER view; this view projects that view's rows
               instead of the segments. Mutually exclusive with `platform`
    fields     output_key: "{{ seg.<source> }}"
    where      extra equality filters, ANDed, before projection
    unique_by  list of OUTPUT keys; one row per distinct combination
    group_by   a SEGMENT field; emits {bucket: [rows]} instead of [rows]
    consts     fixed values stamped on every row
    append     hand-maintained rows, passed through untouched. With
               `group_by` this is a mapping of bucket -> rows

Every rejection here exists because the alternative is a wrong list reaching
a device quietly: a typo'd key that changes which rows ship, a `where` that
can never match, an `append` of the wrong shape.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from jinja2 import StrictUndefined, is_undefined, meta, pass_context
from jinja2.nativetypes import NativeEnvironment

try:  # keeps the unit tests runnable without ansible installed, as the
    # sibling network_catalog.py is — it imports nothing from ansible at all.
    from ansible.errors import AnsibleFilterError
except ImportError:  # pragma: no cover
    class AnsibleFilterError(Exception):  # type: ignore[no-redef]
        """Stand-in so this module imports under a bare pytest run."""


LOOP_VAR = "seg"

# Field templates render HERE, never on the caller's (Ansible's) environment.
# ansible-core 2.19+ resolves a missing key to an UndefinedMarker, and the
# templating layer SHORT-CIRCUITS any filter a Marker is handed — so
# `| default(...)` never ran and the marker rode out as the field value,
# failing every view that has an optional field. A plain NativeEnvironment
# keeps native types (the reason the caller env was used at all) AND classic
# Jinja undefined semantics, so default() means what it says. An undefined
# that still reaches output stays fatal via the is_undefined() guard below.
_ENV = NativeEnvironment(undefined=StrictUndefined)

SPEC_KEYS = frozenset({"platform", "fields", "where", "consts", "append",
                       "unique_by", "group_by", "source"})


# ── errors ────────────────────────────────────────────────────────────────


def _fail(label: str, msg: str) -> None:
    """One line, always naming the view, so the fix is obvious from the log."""
    raise AnsibleFilterError(f"network_views [{label}]: {msg}")


def _brief(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _suggest(wanted: str, known: Iterable[str]) -> str:
    close = difflib.get_close_matches(str(wanted), sorted(known), n=1, cutoff=0.7)
    return f" Did you mean {close[0]!r}?" if close else ""


# ── validation ────────────────────────────────────────────────────────────


def _value_kind(value: Any) -> str:
    """A COARSE type name, for comparing what the config says against the data.

    Coarse on purpose. Ansible hands YAML scalars through as `AnsibleUnicode`
    and `AnsibleUnsafeText`, so a `type(x).__name__` comparison called every
    correct string `where` a type mismatch and ABORTED THE PLAY — the segments
    are built in Python and carry plain `str`, this views file is loaded
    through `from_yaml` and carries the subclass. bool is tested before int
    because a bool IS an int in Python.

    This rule used to live in BOTH engines, copied, and diverged twice in two
    rounds. It was then shared from network_catalog by path-import. Neither is
    needed now: the catalog's only caller was its own view engine, which is
    gone, so the question is asked in exactly one place — here.
    """
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (str, bytes)):
        return "text"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, Mapping):
        return "a mapping"
    if isinstance(value, (list, tuple)):
        return "a list"
    return type(value).__name__


def _check_rows(rows: Any, label: str) -> List[Mapping[str, Any]]:
    if rows is None:
        _fail(label, "rows is None — the segment list did not resolve. This "
                     "filter takes _segments from the network catalog.")
    if isinstance(rows, (str, bytes, Mapping)):
        _fail(label, f"rows must be a LIST of segments, got "
                     f"{type(rows).__name__} {_brief(rows)}")
    listed = list(rows)
    for index, row in enumerate(listed):
        if not isinstance(row, Mapping):
            _fail(label, f"rows[{index}] must be a mapping, got "
                         f"{type(row).__name__} {_brief(row)}")
    return listed


def _check_spec(spec: Any, label: str) -> None:
    if not isinstance(spec, Mapping):
        _fail(label, f"a view must be a mapping, got {type(spec).__name__} "
                     f"{_brief(spec)}")

    for key in spec:
        if key not in SPEC_KEYS:
            _fail(label, f"unknown key {key!r}.{_suggest(key, SPEC_KEYS)} "
                         f"Known keys: {', '.join(sorted(SPEC_KEYS))}")

    platform = spec.get("platform")
    source = spec.get("source")
    if source is not None:
        if not isinstance(source, str) or not source.strip():
            _fail(label, f"`source:` must name one earlier view, got "
                         f"{type(source).__name__} {_brief(source)}")
        if platform:
            # The trap this whole key brings with it. A chained view reads the
            # PROJECTED rows of another view — output keys, no on_<platform>
            # flags — so a platform filter here matches nothing and the view
            # comes out empty with no explanation.
            _fail(label, f"`source:` and `platform:` cannot both be set. This "
                         f"view reads the projected rows of {source!r}, which "
                         f"carry output keys rather than segments, so there is "
                         f"no on_<platform> flag to filter on. Filter in "
                         f"{source!r} itself, or with `where:` here.")
    elif not platform:
        _fail(label, "no `platform:` — a view must say which segments it takes")
    if platform is not None and not isinstance(platform, str):
        _fail(label, f"`platform:` must be one label string, got "
                     f"{type(platform).__name__} {_brief(platform)}")

    fields = spec.get("fields")
    if not fields:
        _fail(label, "no `fields:` — nothing to emit")
    if not isinstance(fields, Mapping):
        _fail(label, f"`fields:` must be a mapping of output_key: "
                     f'"{{{{ seg.source }}}}", got {type(fields).__name__}')
    for key, source in fields.items():
        if not isinstance(source, str):
            _fail(label, f"fields.{key} must be a template string, got "
                         f"{type(source).__name__} {_brief(source)}")

    for optional in ("where", "consts"):
        value = spec.get(optional)
        if value is not None and not isinstance(value, Mapping):
            _fail(label, f"`{optional}:` must be a mapping, got "
                         f"{type(value).__name__} {_brief(value)}")

    unique_by = spec.get("unique_by")
    if unique_by is not None:
        if isinstance(unique_by, str):
            _fail(label, f"`unique_by:` takes a LIST of output keys, not a "
                         f"bare name — write [{unique_by}]. One shape means "
                         f"one row of one key reads the same as several.")
        if not isinstance(unique_by, Sequence):
            _fail(label, f"`unique_by:` must be a list of output keys, got "
                         f"{type(unique_by).__name__} {_brief(unique_by)}")
        emitted = set(fields) | set(spec.get("consts") or {})
        for key in unique_by:
            if key not in emitted:
                _fail(label, f"`unique_by:` names {key!r}, which is not an "
                             f"output key of this view.{_suggest(key, emitted)} "
                             f"Dedup runs on the projected rows, so it names a "
                             f"key from `fields:`/`consts:` "
                             f"({', '.join(sorted(emitted))}), not a segment "
                             f"field.")

    clash = set(spec.get("consts") or {}) & set(fields)
    if clash:
        _fail(label, f"`consts:` and `fields:` both define "
                     f"{', '.join(sorted(map(str, clash)))} — the const would "
                     f"silently win. Rename one.")


def _check_platform(rows: Sequence[Mapping[str, Any]], platform: str,
                    label: str) -> None:
    """A platform no segment carries would build nothing and say nothing."""
    declared: Set[str] = set()
    for row in rows:
        # BOOLEAN on_* keys only. The catalog emits one per declared platform
        # and they are always bools; an ordinary segment field that happens to
        # start with on_ (`on_call_team: ops`) is data, not a platform, and
        # used to make `platform: call_team` look declared.
        declared.update(str(k)[3:] for k, v in row.items()
                        if str(k).startswith("on_") and isinstance(v, bool))
    if rows and platform not in declared:
        _fail(label, f"`platform: {platform}` is not a platform this catalog "
                     f"knows — no segment carries 'on_{platform}', so this "
                     f"view would build nothing."
                     f"{_suggest(platform, declared)} "
                     f"Declared: {', '.join(sorted(declared)) or '<none>'}")


def _check_where(rows: Sequence[Mapping[str, Any]], where: Mapping[str, Any],
                 label: str) -> None:
    """A `where` that cannot match is a bug, not a filter."""
    known: Set[str] = set()
    for row in rows:
        known.update(str(k) for k in row)
    for field, wanted in where.items():
        if field not in known:
            _fail(label, f"`where:` filters on {field!r}, which no segment "
                         f"has.{_suggest(field, known)}")
        # COARSE kinds, not type().__name__ — and no exemption for lists and
        # dicts, which used to skip the check entirely and go silently empty.
        seen = {_value_kind(r[field]) for r in rows
                if field in r and r[field] is not None}
        got = _value_kind(wanted)
        if seen and got not in seen:
            _fail(label, f"`where: {{{field}: {wanted!r}}}` is {got} but "
                         f"segments carry {field!r} as {' / '.join(sorted(seen))}"
                         f" — the comparison can never match. Quote or unquote "
                         f"the value.")


def _resolve_source(source: str, namespace: str, built: Mapping[str, Any],
                    label: str) -> List[Mapping[str, Any]]:
    """The rows of an earlier view, for a chained one.

    A bare name resolves inside the current NAMESPACE; a dotted one is taken
    whole. `built` holds only the views finished so far, so "declared earlier"
    needs no separate bookkeeping — a forward reference is simply absent.
    """
    # Type-checked HERE, not only in _check_spec: the driver has to resolve the
    # source before it can build the view, so this is the first code to touch
    # the value. `"." in 7` is a TypeError, and a raw one at that.
    if not isinstance(source, str) or not source.strip():
        _fail(label, f"`source:` must name one earlier view, got "
                     f"{type(source).__name__} {_brief(source)}")
    ref = source if "." in source else f"{namespace}.{source}"
    if ref not in built:
        _fail(label, f"`source:` names {source!r}, which is not a view "
                     f"declared earlier.{_suggest(ref, built)} Views chain in "
                     f"file order, so the source has to appear above this one. "
                     f"Declared so far: {', '.join(sorted(built)) or '<none>'}")
    rows = built[ref]
    if isinstance(rows, Mapping):
        # The trap round 3 fixed in the catalog, which arrived here the moment
        # group_by did: a grouped view emits {bucket: [rows]}, so chaining off
        # it yields nothing and says nothing.
        _fail(label, f"`source:` names {source!r}, which is a GROUPED view "
                     f"(it declares group_by, so it emits buckets rather than "
                     f"rows). This view would build nothing. Chain from a flat "
                     f"view, or drop group_by from {source!r}.")
    return rows


def _check_group_by(rows: Sequence[Mapping[str, Any]], group_by: Any,
                    label: str) -> None:
    """`group_by` names a SEGMENT field, and every kept row must carry it.

    Unlike `unique_by`, which names an OUTPUT key: grouping happens before
    projection, so it reads the source row. A field no segment has would put
    every row in one bucket named "", and a consumer indexing
    `<view>.<site>` would get an undefined-variable error pointing at the
    consumer rather than at the view.
    """
    if not isinstance(group_by, str) or not group_by.strip():
        _fail(label, f"`group_by:` must be the name of one segment field, got "
                     f"{type(group_by).__name__} {_brief(group_by)}")
    known: Set[str] = set()
    for row in rows:
        known.update(str(k) for k in row)
    if rows and group_by not in known:
        _fail(label, f"`group_by:` names {group_by!r}, which no segment "
                     f"carries.{_suggest(group_by, known)} Grouping runs on "
                     f"the SOURCE segments, so it names a segment field, not "
                     f"an output key.")


def _check_append(value: Any, label: str, group_by: Any = None) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        _fail(label, f"`append:` is a string, not a list: {_brief(value)} — if "
                     f"it is a template it did not resolve to a list")
    if isinstance(value, Mapping):
        if group_by:
            # A grouped view returns buckets, so hand-maintained rows have to
            # say which bucket they join. That is checked in _check_grouped_append.
            _fail(label, f"`append:` reached the flat check on a grouped view "
                         f"— this is a bug in network_views, not in {label}")
        _fail(label, f"`append:` must be a LIST of rows, got one mapping "
                     f"{_brief(value)}. Wrap it: [ {{...}} ]")
    try:
        listed = list(value)
    except TypeError:
        _fail(label, f"`append:` must be a list of rows, got "
                     f"{type(value).__name__} {_brief(value)}")
    for index, row in enumerate(listed):
        if not isinstance(row, Mapping):
            _fail(label, f"`append:`[{index}] must be a row mapping, got "
                         f"{type(row).__name__} {_brief(row)}")
    return listed


def _check_grouped_append(value: Any, label: str) -> Dict[str, List[Any]]:
    """On a grouped view, `append` is a MAPPING of bucket -> rows.

    A list here is the easy mistake and the catalog's engine ignored it in
    silence — hand-maintained rows simply never appeared. Bucket keys are
    stringified to match the generated ones, so `append: {10: [...]}` lands in
    bucket "10" rather than sitting in an unreachable integer key.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        _fail(label, f"this view has `group_by:`, so it emits buckets and "
                     f"`append:` must be a MAPPING of bucket name -> rows, "
                     f"got {type(value).__name__} {_brief(value)}. Write "
                     f"`append: {{<bucket>: [ {{...}} ]}}`.")
    return {str(bucket): _check_append(rows, f"{label}.append[{bucket}]")
            for bucket, rows in value.items()}


# ── rendering ─────────────────────────────────────────────────────────────


def _is_native(env: Any) -> bool:
    """Native environments return real types; plain ones stringify everything.

    Probed rather than read off a class name or an ansible-core version, so it
    survives changes to the templating internals.
    """
    cached = getattr(env, "_network_views_native", None)
    # Re-probe on anything that is not a bool, not just on None. The cache
    # lives as an attribute ON the environment, so anything else setting that
    # name — or a stale value of the wrong shape — otherwise decides how every
    # view in the process renders, for the life of the process.
    if not isinstance(cached, bool):
        try:
            cached = isinstance(env.from_string("{{ 1 }}").render(), int)
        except Exception:  # a failed probe means "assume not native"
            cached = False
        try:
            env._network_views_native = cached
        except (AttributeError, TypeError):  # frozen or proxied environment
            pass
    return cached


def _foreign_names(env: Any, source: str) -> List[str]:
    """Names a field expression reads other than the loop variable.

    A field is rendered with ONLY `seg` in scope, so a play variable here is
    always undefined. Naming it turns a bare "undefined" into a fix.
    """
    try:
        return sorted(n for n in meta.find_undeclared_variables(_ENV.parse(source))
                      if n != LOOP_VAR)
    except Exception:
        return []


def _field_scope(env: Any, ctx: Any, fields: Mapping[str, str]) -> Dict[str, Any]:
    """Play variables the field expressions name, resolved once per view.

    Fields would otherwise see only `seg`, which makes the obvious thing —
    `{{ seg.vswitch | default(site_default) }}` — fail for no good reason.
    Resolved here rather than per row because a play variable does not vary
    by row, and still by name only: enumerating an Ansible context forces
    every lazy variable to evaluate, which in this estate means live Vault
    round trips and secrets dragged into scope for nothing.
    """
    if env is None or ctx is None:
        return {}
    names: Set[str] = set()
    for source in fields.values():
        names.update(_foreign_names(env, source))

    scope: Dict[str, Any] = {}
    for name in names:
        try:
            resolved = ctx.resolve(name)
        except Exception:  # not in scope; let the render report it
            continue
        # is_undefined, not a class-name string: Ansible's subclass is called
        # AnsibleUndefined, so a name comparison silently admits it to scope.
        if resolved is not None and not is_undefined(resolved):
            scope[name] = resolved
    return scope


OMIT_PREFIX = "__omit_place_holder__"


def _is_omit(value: Any, scope: Mapping[str, Any]) -> bool:
    """Did this field render to Ansible's `omit` placeholder?

    `omit` resolves to a random-suffixed marker string, so it is matched two
    ways: against the token actually in scope for this view (exact, and what
    Ansible itself compares), and against the well-known prefix, which covers
    a view that reached the marker without naming `omit` directly — through a
    play variable that already held it, say.

    Deliberately NOT imported from ansible.* — this module keeps working under
    a bare pytest run, like its sibling.
    """
    if not isinstance(value, str):
        return False
    token = scope.get("omit")
    if isinstance(token, str) and value == token:
        return True
    return value.startswith(OMIT_PREFIX)


def _compile(env: Any, fields: Mapping[str, str],
             label: str = "view") -> Dict[str, Any]:
    """Compile each field template once per view, not once per row.

    from_string parses and code-generates every call. Doing that per field
    per row is quadratic in catalog size for no gain — the source never
    changes between rows, only the bindings do.

    This is also where a field that is not valid template syntax surfaces —
    `_foreign_names` has already swallowed the same parse error to build the
    scope. It used to escape as a raw TemplateSyntaxError naming neither the
    view nor the field, which for a file of a dozen fields is a hunt.
    """
    compiled: Dict[str, Any] = {}
    for key, source in fields.items():
        try:
            compiled[key] = _ENV.from_string(source)
        except AnsibleFilterError:
            raise
        except Exception as exc:
            _fail(label, f"fields.{key} = {source!r} is not valid template "
                         f"syntax: {exc}")
    return compiled


def _render_row(env: Any, row: Mapping[str, Any], fields: Mapping[str, str],
                label: str, scope: Mapping[str, Any],
                templates: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, source in fields.items():
        try:
            value = templates[key].render({**scope, LOOP_VAR: row})
        except AnsibleFilterError:
            raise
        except Exception as exc:  # reported with the field and row that broke
            unresolved = [n for n in _foreign_names(env, source) if n not in scope]
            hint = (f" No variable named {', '.join(repr(n) for n in unresolved)} "
                    f"is in scope here — check the spelling, or read it off the "
                    f"segment as {LOOP_VAR}.<field>." if unresolved else "")
            _fail(label, f"fields.{key} = {source!r} failed on segment "
                         f"{row.get('key', '<no key>')!r}: {exc}.{hint}")
        # A native environment hands back the Undefined OBJECT when the whole
        # template is one expression, so an unresolved field rode into the row
        # and surfaced far away — a RepresenterError at yaml dump, or a null
        # pushed at a device. Stop it at the row that produced it.
        if is_undefined(value):
            # Same hint as the exception path above. Under StrictUndefined a
            # bare unknown NAME lands here rather than raising, so without
            # this the more useful half of the message — which name is missing
            # and where to read it from — was lost exactly when it was needed.
            unresolved = [n for n in _foreign_names(env, source)
                          if n not in scope and n != LOOP_VAR]
            hint = (f" No variable named "
                    f"{', '.join(repr(n) for n in unresolved)} is in scope "
                    f"here — check the spelling, or read it off the segment "
                    f"as {LOOP_VAR}.<field>." if unresolved else "")
            _fail(label, f"fields.{key} = {source!r} is undefined on segment "
                         f"{row.get('key', '<no key>')!r}. Add "
                         f"`| default(...)` if the field is genuinely "
                         f"optional, or fix the name.{hint}")
        if _is_omit(value, scope):
            # `| default(omit)` is how every Ansible user spells "leave this
            # key out", and it MUST be honoured here rather than left to the
            # caller. Ansible strips omit placeholders when it post-validates
            # a task's arguments, so this appeared to work through set_fact —
            # but `_net` is built in group_vars, which is templated lazily and
            # never post-validated. There the placeholder survived, and a row
            # shipped `__omit_place_holder__84fcd962...` to a device as a real
            # value. Drop the key here so both paths agree.
            continue
        out[key] = value
    return out


def _render_scope(env: Any, ctx: Any, value: Any, label: str = "view") -> Any:
    """Render a spec-level value against the CALLER's variables, not a row.

    `append`, `where` and `consts` reference ordinary play variables, and the
    lookup-file route leaves every string un-templated. Only the names a
    template actually reads are resolved: enumerating an Ansible context
    forces every lazy variable to evaluate, which in this estate means live
    Vault round trips and secrets dragged into scope for no reason.
    """
    if isinstance(value, Mapping):
        # Only VALUES are rendered. A `{{ }}` in a mapping KEY is left alone,
        # which is what Ansible itself does with module-arg keys — rendering
        # one here would make this engine disagree with every other place a
        # key is written.
        return {k: _render_scope(env, ctx, v, label) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        # tuple as well as list: YAML never produces one, but a caller passing
        # a spec built in Python did, and its templates went through unrendered.
        return [_render_scope(env, ctx, v, label) for v in value]
    if not (isinstance(value, str) and "{{" in value) or env is None or ctx is None:
        return value

    scope: Dict[str, Any] = {}
    try:
        names = meta.find_undeclared_variables(_ENV.parse(value))
    except Exception:
        names = set()
    for name in names:
        try:
            resolved = ctx.resolve(name)
        except Exception:
            continue
        if resolved is not None and not is_undefined(resolved):
            scope[name] = resolved
    try:
        return _ENV.from_string(value).render(scope)
    except AnsibleFilterError:
        raise
    except Exception as exc:
        # A play variable whose own value contains `{{ }}` reaches a native
        # environment as a template inside a template: `{{ 1 }}` is literal-
        # eval'd as a set containing a set, and the resulting
        # `TypeError: unhashable type: 'set'` escaped raw — no view, no key,
        # nothing to act on.
        _fail(label, f"{value!r} failed to render against the play's "
                     f"variables: {exc}. A variable used here must hold a "
                     f"plain value — if it holds a template of its own, "
                     f"resolve it before the view reads it.")


def _dedupe(rows: List[Dict[str, Any]], unique_by: Any,
            label: str = "view") -> List[Dict[str, Any]]:
    """One row per distinct combination of the named output keys.

    A list rather than a single key because uniqueness is usually composite:
    once VLAN 10 exists at two sites, `[id]` would wrongly collapse two real
    L2 domains and `[id, site]` is the truthful key. First occurrence wins,
    so the order the catalog declared is preserved.
    """
    if not unique_by:
        return rows
    seen: Set[Tuple[Any, ...]] = set()
    kept: List[Dict[str, Any]] = []
    for row in rows:
        # A key that is absent or null on a row makes every such row look
        # identical, so they collapse into one and the rest vanish without a
        # word. Refuse rather than guess which of them was wanted.
        blank = [k for k in unique_by
                 if row.get(k) is None or str(row.get(k)).strip() == ""]
        if blank:
            _fail(label, f"`unique_by:` needs {', '.join(map(repr, blank))} on "
                         f"every row, but it is null or blank on the row from "
                         f"segment {row.get('key', '<no key>')!r}. Rows missing "
                         f"the key would all dedupe together and silently drop "
                         f"each other.")
        signature = tuple(_hashable(row.get(key)) for key in unique_by)
        if signature in seen:
            continue
        seen.add(signature)
        kept.append(row)
    return kept


def _hashable(value: Any) -> Any:
    """Lists and dicts cannot go in a set; compare them by their text."""
    if isinstance(value, (list, dict, set)):
        return repr(value)
    return value


def _as_names(lists: Any) -> List[str]:
    """List names in a namespace, tolerating a malformed namespace body."""
    return [str(k) for k in lists] if isinstance(lists, Mapping) else []


# ── the filters ───────────────────────────────────────────────────────────


def _build(env: Any, ctx: Any, rows: Any, spec: Any, label: str) -> Any:
    rows = _check_rows(rows, label)
    _check_spec(spec, label)

    chained = spec.get("source") is not None
    platform = spec.get("platform")
    if not chained:
        _check_platform(rows, platform, label)

    where = _render_scope(env, ctx, spec.get("where") or {}, label)
    if where:
        _check_where(rows, where, label)

    # No native-environment check any more: fields render on _ENV, which is
    # native by construction, so the caller's environment cannot cost us types.
    # ansible-core 2.19+ also removed the jinja2_native toggle this used to
    # tell people to set, so the advice had outlived the setting.

    # A chained view's rows ARE the selection its source made, so there is no
    # membership flag to test — every row it was handed is in.
    flag = None if chained else f"on_{platform}"
    consts = _render_scope(env, ctx, spec.get("consts") or {}, label)
    fields = spec["fields"]
    scope = _field_scope(env, ctx, fields)
    templates = _compile(env, fields, label)

    group_by = spec.get("group_by")
    if group_by is not None:
        _check_group_by(rows, group_by, label)

    unique_by = spec.get("unique_by")
    appended = _render_scope(env, ctx, spec.get("append"), label)

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    flat: List[Dict[str, Any]] = []
    for row in rows:
        if flag is not None and not row.get(flag):
            continue
        if any(row.get(f) != v for f, v in where.items()):
            continue
        shaped = _render_row(env, row, fields, label, scope, templates)
        shaped.update(consts)
        if group_by is None:
            flat.append(shaped)
        else:
            buckets.setdefault(str(row.get(group_by, "")), []).append(shaped)

    # `append` rows are hand-maintained and pass through untouched, which
    # includes not being deduped against the generated ones.
    if group_by is None:
        return _dedupe(flat, unique_by, label) + _check_append(appended, label)

    extras = _check_grouped_append(appended, label)
    # A bucket that exists ONLY in `append` still gets emitted — a site whose
    # rows are entirely hand-maintained must not vanish from a grouped view
    # just because no segment happens to land in it.
    for bucket in extras:
        buckets.setdefault(bucket, [])
    return {
        bucket: _dedupe(rows_in, unique_by, f"{label}[{bucket}]")
                + extras.get(bucket, [])
        for bucket, rows_in in buckets.items()
    }


@pass_context
def network_view(ctx: Any, rows: Any, spec: Any, label: str = "view") -> Any:
    """Build one declared list — or a mapping of buckets, with `group_by`."""
    if isinstance(spec, Mapping) and spec.get("source") is not None:
        _fail(label, "`source:` chains one view off another, so it only works "
                     "through the `network_views` filter, which builds a whole "
                     "file in order. A single `network_view` call has nothing "
                     "to chain from.")
    return _build(getattr(ctx, "environment", None), ctx, rows, spec, label)


@pass_context
def network_views(ctx: Any, rows: Any, views: Any) -> Dict[str, Dict[str, Any]]:
    """Build every declared list: {namespace: {list_name: [rows]}}."""
    if not isinstance(views, Mapping):
        raise AnsibleFilterError(
            f"network_views: the view file must be a mapping of namespace -> "
            f"list -> spec, got {type(views).__name__} {_brief(views)}"
        )
    env = getattr(ctx, "environment", None)
    built: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []
    # Flat "<namespace>.<name>" -> rows, for `source:` to chain against.
    by_ref: Dict[str, Any] = {}
    for namespace, lists in views.items():
        # A list is addressed as "<namespace>.<name>" — by the role, by
        # `network_views_select`, and in every error message here. A dot in
        # either half makes that reference ambiguous, and the role split it on
        # the FIRST dot: `ns.with.dots.x` resolved to namespace `ns`, list
        # `with`, printed VARIABLE IS NOT DEFINED, and exited 0.
        for part, what in ((namespace, "namespace"), *((n, "list name")
                                                       for n in _as_names(lists))):
            if "." in str(part):
                raise AnsibleFilterError(
                    f"network_views: {what} {part!r} contains a dot. Lists are "
                    f"addressed as <namespace>.<name>, so a dot makes the "
                    f"reference ambiguous. Use - or _ instead."
                )
        if not isinstance(lists, Mapping):
            raise AnsibleFilterError(
                f"network_views [{namespace}]: a namespace must hold a mapping "
                f"of list name -> spec, got {type(lists).__name__} "
                f"{_brief(lists)}"
            )
        # Every view is independent, so one bad spec must not hide the others.
        # It used to: the first _fail aborted the filter, so a file with three
        # mistakes took three runs to fix — and each run re-validated the two
        # you had not seen yet. Structural problems ABOVE this level (a dotted
        # name, a namespace that is not a mapping) still raise immediately:
        # those break addressing, so nothing below them can be trusted.
        built[namespace] = {}
        for name, spec in lists.items():
            label = f"{namespace}.{name}"
            try:
                # `source:` resolves against the views finished SO FAR, which
                # is what makes "declared earlier" free rather than bookkeeping.
                view_rows = rows
                if isinstance(spec, Mapping) and spec.get("source") is not None:
                    view_rows = _resolve_source(spec["source"], namespace,
                                                by_ref, label)
                result = _build(env, ctx, view_rows, spec, label)
                built[namespace][name] = result
                by_ref[label] = result
            except AnsibleFilterError as exc:
                problems.append(str(exc))

    if problems:
        raise AnsibleFilterError(
            f"network_views: {len(problems)} of the declared lists cannot be "
            f"built. All of them, so this is one fix-and-rerun:\n  - "
            + "\n  - ".join(problems)
        )
    return built


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self) -> Dict[str, Any]:
        return {"network_view": network_view, "network_views": network_views}
