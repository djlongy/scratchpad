# -*- coding: utf-8 -*-
"""FreeIPA IDAM access-matrix compilers (Ansible filter plugins).

Two filters turn ONE declarative ``freeipa_idam_access_matrix`` into the role's
existing baseline ``freeipa_idam_*`` object lists — so the matrix is an OVERLAY
that the playbook unions onto the hand-written baseline. The role itself is never
touched; it keeps consuming the same baseline contract.

  freeipa_idam_access_objects(matrix, scope_tenant=None, scope_environment=None)
      -> {usergroups, hostgroups, hbac_rules, sudo_rules, hbacsvcs, sudo_commands}

  freeipa_idam_user_grants(users, matrix, scope_tenant=None, scope_environment=None)
      -> [ {<identity fields>, groups:[role groups]} , ... ]   (matrix-only keys stripped)

Object model (per generated cell tenant/env/app/privilege):
  role-<...>  grant group        users are members of THIS
  ug-<...>    policy user group  nests the role group; HBAC/sudo target THIS
  hg-<...>    hostgroup          scope for the HBAC/sudo rules
  hbac-<...>  HBAC rule          usergroup=[ug], hostgroup=[hg], service=<hbac_services>
  sudo-<...>  sudo rule          usergroup=[ug], hostgroup=[hg], cmd/cmdcategory

Every object name comes from a configurable template in ``naming.templates`` — the
token ORDER is yours to rearrange; nothing here hard-codes the layout.
"""
from __future__ import annotations

import re

try:                                          # real Ansible at runtime …
    from ansible.errors import AnsibleFilterError
except ImportError:                           # … plain Python under pytest
    class AnsibleFilterError(Exception):
        pass


# Stock HBAC services that ship with every FreeIPA — never re-created as custom.
DEFAULT_STOCK_HBACSVCS = [
    "sshd", "sudo", "sudo-i", "su", "su-l",
    "login", "crond", "ftp", "gdm", "gdm-password",
]

DEFAULT_PREFIXES = {
    "role": "role", "usergroup": "ug", "hostgroup": "hg",
    "hbac": "hbac", "sudo": "sudo",
}

DEFAULT_TEMPLATES = {
    "role_group": "{role_prefix}-{tenant}-{environment}-{app}-{privilege}",
    "usergroup":  "{usergroup_prefix}-{tenant}-{environment}-{app}-{privilege}",
    "hostgroup":  "{hostgroup_prefix}-{tenant}-{environment}-{app}-{privilege}",
    "hbacrule":   "{hbac_prefix}-{tenant}-{environment}-{app}-{privilege}",
    "sudorule":   "{sudo_prefix}-{tenant}-{environment}-{app}-{privilege}",
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _names(naming, tenant, env, app, priv):
    """Resolve every object name for one cell from the naming templates."""
    prefixes = dict(DEFAULT_PREFIXES, **((naming or {}).get("prefixes") or {}))
    templates = dict(DEFAULT_TEMPLATES, **((naming or {}).get("templates") or {}))
    tokens = {
        "role_prefix": prefixes["role"],
        "usergroup_prefix": prefixes["usergroup"],
        "hostgroup_prefix": prefixes["hostgroup"],
        "hbac_prefix": prefixes["hbac"],
        "sudo_prefix": prefixes["sudo"],
        "tenant": tenant, "environment": env, "app": app, "privilege": priv,
    }
    out = {}
    for key, tmpl in (("role", templates["role_group"]),
                      ("ug", templates["usergroup"]),
                      ("hg", templates["hostgroup"]),
                      ("hbac", templates["hbacrule"]),
                      ("sudo", templates["sudorule"])):
        try:
            out[key] = tmpl.format(**tokens)
        except KeyError as exc:
            raise AnsibleFilterError(
                "freeipa naming template %r references unknown placeholder %s; "
                "valid placeholders: %s" % (tmpl, exc, sorted(tokens)))
        except (IndexError, ValueError) as exc:
            raise AnsibleFilterError(
                "freeipa naming template %r is malformed: %s" % (tmpl, exc))
    return out


def _expand_environments(env_spec, tenant_envs):
    """Resolve an access_set tenant's environment selector against the tenant's envs.

    env_spec may be: None ('all'), 'all', a str, a list, or a dict with
    include ('all'|str|list) and optional exclude (list). Always intersected with
    the tenant's declared environments (auto-intersect — an env the tenant lacks
    is simply skipped).
    """
    declared = list(tenant_envs or [])
    if env_spec is None:
        chosen = declared
        exclude = []
    elif isinstance(env_spec, str):
        chosen = declared if env_spec == "all" else [env_spec]
        exclude = []
    elif isinstance(env_spec, (list, tuple)):
        chosen = list(env_spec)
        exclude = []
    elif isinstance(env_spec, dict):
        inc = env_spec.get("include", "all")
        if inc in (None, "all"):
            chosen = declared
        elif isinstance(inc, str):
            chosen = [inc]
        else:
            chosen = list(inc)
        exclude = list(env_spec.get("exclude") or [])
    else:
        raise AnsibleFilterError(
            "environments selector must be 'all', a list, or an include/exclude "
            "mapping, got %r" % (env_spec,))
    return [e for e in chosen if e in declared and e not in exclude]


def _scoped_tenants(sname, aset, matrix, scope_tenant):
    """Yield (tenant, tdef, tenant_envs) for an access_set, honouring the tenant scope."""
    tenants = matrix.get("tenants") or {}
    for tenant, tdef in (aset.get("tenants") or {}).items():
        if tenant not in tenants:
            raise AnsibleFilterError(
                "access_set '%s': tenant '%s' is not defined in tenants" % (sname, tenant))
        if scope_tenant and tenant != scope_tenant:
            continue
        yield tenant, tdef, (tenants.get(tenant) or {}).get("environments") or []


def _cells_for_access_set(sname, aset, matrix, scope_tenant, scope_environment):
    """Expand one access_set into concrete (tenant, env, app, privilege) cells."""
    app = aset.get("app")
    priv = aset.get("privilege")
    if not app or not priv:
        raise AnsibleFilterError(
            "access_set '%s' must define both 'app' and 'privilege'" % sname)
    cells = []
    for tenant, tdef, tenant_envs in _scoped_tenants(sname, aset, matrix, scope_tenant):
        for env in _expand_environments((tdef or {}).get("environments"), tenant_envs):
            if not scope_environment or env == scope_environment:
                cells.append((tenant, env, app, priv))
    return cells


def _effective_access(matrix, app, priv):
    """hbac_services + sudo_commands for (app, privilege): the privilege tier, with
    an optional per-app override (apps.<app>.privileges.<priv>)."""
    privdef = (matrix.get("privileges") or {}).get(priv) or {}
    hbac = list(privdef.get("hbac_services") or [])
    sudo = list(privdef.get("sudo_commands") or [])
    appdef = (matrix.get("apps") or {}).get(app) or {}
    override = (appdef.get("privileges") or {}).get(priv) or {}
    if "hbac_services" in override:
        hbac = list(override["hbac_services"] or [])
    if "sudo_commands" in override:
        sudo = list(override["sudo_commands"] or [])
    return hbac, sudo


# ── filter 1: access objects ─────────────────────────────────────────────────
def _register_groups(usergroups, names, tag):
    """role group (membership) + policy ug group that NESTS it."""
    usergroups.setdefault(names["role"], {
        "name": names["role"], "description": "Grant group %s" % tag})
    ug = usergroups.setdefault(names["ug"], {
        "name": names["ug"], "description": "Policy group %s" % tag, "group": []})
    if names["role"] not in ug["group"]:
        ug["group"].append(names["role"])


def _register_hbac(hbac_rules, hbacsvcs, names, tag, hbac_services, stock):
    if not hbac_services:
        return
    hbac_rules.setdefault(names["hbac"], {
        "name": names["hbac"], "description": "HBAC %s" % tag,
        "usergroup": [names["ug"]], "hostgroup": [names["hg"]],
        "service": list(hbac_services), "state": "enabled"})
    for svc in hbac_services:
        if svc not in stock:
            hbacsvcs.setdefault(svc, {
                "name": svc, "description": "%s (custom HBAC service)" % svc})


def _register_sudo(sudo_rules, sudo_commands, names, tag, sudo_cmds):
    if not sudo_cmds:                       # [] → no sudo rule for this cell
        return
    rule = {
        "name": names["sudo"], "description": "Sudo %s" % tag,
        "usergroup": [names["ug"]], "hostgroup": [names["hg"]],
        "runasusercategory": "all", "runasgroupcategory": "all", "state": "enabled"}
    if [c.upper() for c in sudo_cmds] == ["ALL"]:
        rule["cmdcategory"] = "all"
    else:
        rule["cmd"] = list(sudo_cmds)
        for cmd in sudo_cmds:
            sudo_commands.setdefault(cmd, {"name": cmd})
    sudo_rules.setdefault(names["sudo"], rule)


def _fqdn_regex(pattern, domain, instance, tenant, env, app):
    """Build an anchored fqdn regex from a host-naming pattern. Placeholders {tenant},
    {environment}, {app}, {domain} are substituted with regex-ESCAPED literal values;
    {instance} is a raw regex fragment (default [0-9]+); every other character of the
    pattern (dots, dashes) is escaped. e.g. "{tenant}-{app}-{instance}.{environment}.x.io"
    → "^acme\\-grafana\\-[0-9]+\\.dev\\.x\\.io$"."""
    values = {"tenant": tenant, "environment": env, "app": app, "domain": domain or ""}
    out = []
    for part in re.split(r"(\{[a-z_]+\})", pattern):
        if part.startswith("{") and part.endswith("}"):
            name = part[1:-1]
            if name == "instance":
                out.append(instance)
            elif name in values:
                out.append(re.escape(values[name]))
            else:
                raise AnsibleFilterError(
                    "automember.fqdn_pattern: unknown placeholder %s "
                    "(valid: tenant, environment, app, domain, instance)" % part)
        elif part:
            out.append(re.escape(part))
    return "^" + "".join(out) + "$"


def _register_automember(rules, am, names, tag, tenant, env, app):
    """Optionally emit one hostgroup automember rule (fqdn regex) per hostgroup, so
    enrolled hosts wire themselves into the hg-… group. No-op unless the matrix declares
    automember.fqdn_pattern. Deduped by hostgroup (shared across privileges)."""
    pattern = am.get("fqdn_pattern")
    if not pattern:
        return
    rules.setdefault(names["hg"], {
        "name": names["hg"],
        "automember_type": "hostgroup",
        "description": "Auto-membership for %s" % tag,
        "inclusive": [{"key": "fqdn", "expression": _fqdn_regex(
            pattern, am.get("domain"), am.get("instance", "[0-9]+"), tenant, env, app)}]})


def freeipa_idam_access_objects(matrix, scope_tenant=None, scope_environment=None):
    matrix = matrix or {}
    naming = matrix.get("naming") or {}
    apps = matrix.get("apps") or {}
    privileges = matrix.get("privileges") or {}
    access_sets = matrix.get("access_sets") or {}
    automember = matrix.get("automember") or {}
    stock = set(matrix.get("stock_hbacsvcs") or DEFAULT_STOCK_HBACSVCS)
    acc = {key: {} for key in (
        "usergroups", "hostgroups", "hbac_rules", "sudo_rules",
        "hbacsvcs", "sudo_commands", "automember_rules")}

    for sname, aset in access_sets.items():
        app, priv = aset.get("app"), aset.get("privilege")
        if app not in apps:
            raise AnsibleFilterError(
                "access_set '%s': app '%s' is not in apps" % (sname, app))
        if priv not in privileges:
            raise AnsibleFilterError(
                "access_set '%s': privilege '%s' is not in privileges" % (sname, priv))
        hbac_services, sudo_cmds = _effective_access(matrix, app, priv)

        for tenant, env, app, priv in _cells_for_access_set(
                sname, aset, matrix, scope_tenant, scope_environment):
            names = _names(naming, tenant, env, app, priv)
            tag = "%s/%s/%s/%s" % (tenant, env, app, priv)
            _register_groups(acc["usergroups"], names, tag)
            acc["hostgroups"].setdefault(names["hg"], {
                "name": names["hg"], "description": "Hostgroup %s" % tag})
            _register_hbac(acc["hbac_rules"], acc["hbacsvcs"], names, tag, hbac_services, stock)
            _register_sudo(acc["sudo_rules"], acc["sudo_commands"], names, tag, sudo_cmds)
            _register_automember(acc["automember_rules"], automember, names, tag, tenant, env, app)

    return {key: list(val.values()) for key, val in acc.items()}


# ── filter 2: user grants ────────────────────────────────────────────────────
def _resolve_user_groups(user, matrix, naming, access_sets, scope_tenant, scope_environment):
    """Union of a user's pre-existing groups and the role groups from its grants."""
    groups = list(user.get("groups") or [])
    for sname in user.get("grants") or []:
        aset = access_sets.get(sname)
        if aset is None:
            raise AnsibleFilterError(
                "user '%s': grant '%s' is not a defined access_set" % (user["name"], sname))
        for tenant, env, app, priv in _cells_for_access_set(
                sname, aset, matrix, scope_tenant, scope_environment):
            role = _names(naming, tenant, env, app, priv)["role"]
            if role not in groups:
                groups.append(role)
    return groups


def freeipa_idam_user_grants(users, matrix, scope_tenant=None, scope_environment=None):
    matrix = matrix or {}
    naming = matrix.get("naming") or {}
    access_sets = matrix.get("access_sets") or {}

    compiled = []
    for user in users or []:
        if not isinstance(user, dict) or not user.get("name"):
            raise AnsibleFilterError("each user must be a mapping with a 'name'")
        clean = {k: v for k, v in user.items() if k not in ("grants", "assignments")}
        clean["groups"] = _resolve_user_groups(
            user, matrix, naming, access_sets, scope_tenant, scope_environment)
        compiled.append(clean)
    return compiled


# ── filter 3: merge generated objects onto the baseline (native keys) ─────────
def _union_into(target, item, fields):
    """Union each of `fields` (list values) from `item` into `target` in place."""
    for field in fields:
        combined = list(target.get(field) or [])
        for value in (item.get(field) or []):
            if value not in combined:
                combined.append(value)
        target[field] = combined


def freeipa_idam_merge(base, extra, key="name", union_fields=None):
    """Append `extra` onto `base`, deduped by `key` — so the matrix-generated objects
    layer onto the EXISTING baseline list (e.g. an exported snapshot already sitting in
    group_vars under its native freeipa_idam_* key), not a separate var.

    Order: every base item first (baseline is the base), then the genuinely-new extra
    items. On a `key` collision the base item is authoritative and the extra is dropped —
    UNLESS union_fields is given, in which case those list fields are unioned into the
    base item (used for users: a person present in both keeps the union of their groups).
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


class FilterModule:
    def filters(self):
        return {
            "freeipa_idam_access_objects": freeipa_idam_access_objects,
            "freeipa_idam_user_grants": freeipa_idam_user_grants,
            "freeipa_idam_merge": freeipa_idam_merge,
        }
