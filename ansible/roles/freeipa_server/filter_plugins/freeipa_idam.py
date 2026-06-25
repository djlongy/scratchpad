# -*- coding: utf-8 -*-
"""FreeIPA IDAM access-matrix compilers (Ansible filter plugins).

Two filters turn ONE declarative ``freeipa_idam_access_matrix`` into the role's
existing baseline ``freeipa_idam_*`` object lists — so the matrix is an OVERLAY
that the playbook unions onto the hand-written baseline. The role itself is never
touched; it keeps consuming the same baseline contract.

  freeipa_idam_access_objects(matrix, scope_tenant=None, scope_environment=None)
      -> {usergroups, hostgroups, hbac_rules, sudo_rules, hbacsvcs, sudo_commands,
          automember_rules}

  freeipa_idam_user_grants(users, matrix, scope_tenant=None, scope_environment=None)
      -> [ {<identity fields>, groups:[role groups]} , ... ]   (matrix-only keys stripped)

Matrix vocabulary (all keys self-documenting):
  object_naming    prefixes + templates for the generated object names
  tenants          tenant -> { environments: [...] }            (what EXISTS)
  applications     app  -> { description, privilege_overrides }
  privilege_levels level -> { hbac_services, sudo_commands }     (the access tiers)
  host_automember  fqdn_pattern -> auto hostgroup membership
  access_grants    name -> { application, privilege_level, tenants }  (a scoped grant)
  role_sets        name -> [access_grant, ...]                   (a role = set of grants)

Object model (per generated cell tenant/environment/application/privilege_level):
  role group   grant group        users are members of THIS
  user group   policy group       nests the role group; HBAC/sudo target THIS
  host group   hostgroup          scope for the HBAC/sudo rules
  hbac rule    usergroup=[ug], hostgroup=[hg], service=<hbac_services>
  sudo rule    usergroup=[ug], hostgroup=[hg], cmd/cmdcategory
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

# The FIVE generated object types — a fixed set (you can't add more; the compiler emits
# exactly these). Each key is an object type; its value is the literal name prefix.
DEFAULT_PREFIXES = {
    "role_group": "role", "user_group": "ug", "host_group": "hg",
    "hbac_rule": "hbac", "sudo_rule": "sudo",
}

# How each object's full name is assembled. Same five keys as prefixes. Placeholders:
#   {role_group_prefix} {user_group_prefix} {host_group_prefix} {hbac_rule_prefix}
#   {sudo_rule_prefix} {tenant} {environment} {application} {privilege_level}
DEFAULT_TEMPLATES = {
    "role_group": "{role_group_prefix}-{tenant}-{environment}-{application}-{privilege_level}",
    "user_group": "{user_group_prefix}-{tenant}-{environment}-{application}-{privilege_level}",
    "host_group": "{host_group_prefix}-{tenant}-{environment}-{application}-{privilege_level}",
    "hbac_rule":  "{hbac_rule_prefix}-{tenant}-{environment}-{application}-{privilege_level}",
    "sudo_rule":  "{sudo_rule_prefix}-{tenant}-{environment}-{application}-{privilege_level}",
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _names(naming, tenant, env, app, level):
    """Resolve every object name for one cell from object_naming.templates."""
    prefixes = dict(DEFAULT_PREFIXES, **((naming or {}).get("prefixes") or {}))
    templates = dict(DEFAULT_TEMPLATES, **((naming or {}).get("templates") or {}))
    tokens = {
        "role_group_prefix": prefixes["role_group"],
        "user_group_prefix": prefixes["user_group"],
        "host_group_prefix": prefixes["host_group"],
        "hbac_rule_prefix": prefixes["hbac_rule"],
        "sudo_rule_prefix": prefixes["sudo_rule"],
        "tenant": tenant, "environment": env,
        "application": app, "privilege_level": level,
    }
    out = {}
    for key, tmpl in (("role", templates["role_group"]),
                      ("ug", templates["user_group"]),
                      ("hg", templates["host_group"]),
                      ("hbac", templates["hbac_rule"]),
                      ("sudo", templates["sudo_rule"])):
        try:
            out[key] = tmpl.format(**tokens)
        except KeyError as exc:
            raise AnsibleFilterError(
                "object_naming template %r references unknown placeholder %s; "
                "valid placeholders: %s" % (tmpl, exc, sorted(tokens)))
        except (IndexError, ValueError) as exc:
            raise AnsibleFilterError(
                "object_naming template %r is malformed: %s" % (tmpl, exc))
    return out


def _expand_environments(env_spec, tenant_envs):
    """Resolve an access_grant tenant's environment selector against the tenant's envs.

    env_spec may be: None ('all'), 'all', a str, a list, or a dict with
    include ('all'|str|list) and optional exclude (list). Always intersected with
    the tenant's declared environments (auto-intersect — an env the tenant lacks
    is simply skipped).
    """
    declared = list(tenant_envs or [])
    if env_spec is None:
        chosen, exclude = declared, []
    elif isinstance(env_spec, str):
        chosen, exclude = (declared if env_spec == "all" else [env_spec]), []
    elif isinstance(env_spec, (list, tuple)):
        chosen, exclude = list(env_spec), []
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


def _scoped_tenants(gname, grant, matrix, scope_tenant):
    """Yield (tenant, tdef, tenant_envs) for an access_grant, honouring the tenant scope."""
    tenants = matrix.get("tenants") or {}
    for tenant, tdef in (grant.get("tenants") or {}).items():
        if tenant not in tenants:
            raise AnsibleFilterError(
                "access_grant '%s': tenant '%s' is not defined in tenants" % (gname, tenant))
        if scope_tenant and tenant != scope_tenant:
            continue
        yield tenant, tdef, (tenants.get(tenant) or {}).get("environments") or []


def _cells_for_access_grant(gname, grant, matrix, scope_tenant, scope_environment):
    """Expand one access_grant into concrete (tenant, env, application, level) cells."""
    app = grant.get("application")
    level = grant.get("privilege_level")
    if not app or not level:
        raise AnsibleFilterError(
            "access_grant '%s' must define both 'application' and 'privilege_level'" % gname)
    cells = []
    for tenant, tdef, tenant_envs in _scoped_tenants(gname, grant, matrix, scope_tenant):
        for env in _expand_environments((tdef or {}).get("environments"), tenant_envs):
            if not scope_environment or env == scope_environment:
                cells.append((tenant, env, app, level))
    return cells


def _effective_access(matrix, app, level):
    """hbac_services + sudo_commands for (application, privilege_level): the level's
    tier, with an optional per-app override (applications.<app>.privilege_overrides)."""
    leveldef = (matrix.get("privilege_levels") or {}).get(level) or {}
    hbac = list(leveldef.get("hbac_services") or [])
    sudo = list(leveldef.get("sudo_commands") or [])
    appdef = (matrix.get("applications") or {}).get(app) or {}
    override = (appdef.get("privilege_overrides") or {}).get(level) or {}
    if "hbac_services" in override:
        hbac = list(override["hbac_services"] or [])
    if "sudo_commands" in override:
        sudo = list(override["sudo_commands"] or [])
    return hbac, sudo


# ── filter 1: access objects ─────────────────────────────────────────────────
def _register_groups(usergroups, names, tag):
    """role group (membership) + policy user group that NESTS it."""
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
    """Build an anchored fqdn regex from host_automember.fqdn_pattern. Placeholders
    {tenant}, {environment}, {application}, {domain} are substituted with regex-ESCAPED
    literal values; {instance} is a raw regex fragment (default [0-9]+); every other
    character of the pattern (dots, dashes) is escaped, and the result is anchored ^…$."""
    values = {"tenant": tenant, "environment": env, "application": app, "domain": domain or ""}
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
                    "host_automember.fqdn_pattern: unknown placeholder %s "
                    "(valid: tenant, environment, application, domain, instance)" % part)
        elif part:
            out.append(re.escape(part))
    return "^" + "".join(out) + "$"


def _register_automember(rules, am, names, tag, tenant, env, app):
    """Optionally emit one hostgroup automember rule (fqdn regex) per hostgroup, so
    enrolled hosts wire themselves into the host group. No-op unless the matrix declares
    host_automember.fqdn_pattern. Deduped by hostgroup (shared across privilege levels)."""
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
    naming = matrix.get("object_naming") or {}
    applications = matrix.get("applications") or {}
    privilege_levels = matrix.get("privilege_levels") or {}
    access_grants = matrix.get("access_grants") or {}
    host_automember = matrix.get("host_automember") or {}
    stock = set(matrix.get("stock_hbacsvcs") or DEFAULT_STOCK_HBACSVCS)
    acc = {key: {} for key in (
        "usergroups", "hostgroups", "hbac_rules", "sudo_rules",
        "hbacsvcs", "sudo_commands", "automember_rules")}

    for gname, grant in access_grants.items():
        app, level = grant.get("application"), grant.get("privilege_level")
        if app not in applications:
            raise AnsibleFilterError(
                "access_grant '%s': application '%s' is not in applications" % (gname, app))
        if level not in privilege_levels:
            raise AnsibleFilterError(
                "access_grant '%s': privilege_level '%s' is not in privilege_levels"
                % (gname, level))
        hbac_services, sudo_cmds = _effective_access(matrix, app, level)

        for tenant, env, app, level in _cells_for_access_grant(
                gname, grant, matrix, scope_tenant, scope_environment):
            names = _names(naming, tenant, env, app, level)
            tag = "%s/%s/%s/%s" % (tenant, env, app, level)
            _register_groups(acc["usergroups"], names, tag)
            acc["hostgroups"].setdefault(names["hg"], {
                "name": names["hg"], "description": "Hostgroup %s" % tag})
            _register_hbac(acc["hbac_rules"], acc["hbacsvcs"], names, tag, hbac_services, stock)
            _register_sudo(acc["sudo_rules"], acc["sudo_commands"], names, tag, sudo_cmds)
            _register_automember(acc["automember_rules"], host_automember, names, tag,
                                 tenant, env, app)

    return {key: list(val.values()) for key, val in acc.items()}


# ── filter 2: user grants ────────────────────────────────────────────────────
def _grant_to_access_grants(gname, user_name, access_grants, role_sets):
    """A person's grant name resolves to a list of access_grant names: a role_set
    expands to its members; otherwise the name must itself be an access_grant."""
    if gname in role_sets:
        return list(role_sets[gname] or [])
    if gname in access_grants:
        return [gname]
    raise AnsibleFilterError(
        "user '%s': grant '%s' is not a defined role_set or access_grant"
        % (user_name, gname))


def _resolve_user_groups(user, matrix, naming, access_grants, role_sets,
                         scope_tenant, scope_environment):
    """Union of a user's pre-existing groups and the role groups from its grants
    (each grant being a role_set or a single access_grant)."""
    groups = list(user.get("groups") or [])
    for gname in user.get("grants") or []:
        for an in _grant_to_access_grants(gname, user["name"], access_grants, role_sets):
            for tenant, env, app, level in _cells_for_access_grant(
                    an, access_grants[an], matrix, scope_tenant, scope_environment):
                role = _names(naming, tenant, env, app, level)["role"]
                if role not in groups:
                    groups.append(role)
    return groups


def _validate_role_sets(role_sets, access_grants):
    """Fail-fast: every role_set must reference real access_grants (even if unused)."""
    for rsname, members in role_sets.items():
        for an in (members or []):
            if an not in access_grants:
                raise AnsibleFilterError(
                    "role_set '%s': member '%s' is not a defined access_grant" % (rsname, an))


def freeipa_idam_user_grants(users, matrix, scope_tenant=None, scope_environment=None):
    matrix = matrix or {}
    naming = matrix.get("object_naming") or {}
    access_grants = matrix.get("access_grants") or {}
    role_sets = matrix.get("role_sets") or {}
    _validate_role_sets(role_sets, access_grants)

    compiled = []
    for user in users or []:
        if not isinstance(user, dict) or not user.get("name"):
            raise AnsibleFilterError("each user must be a mapping with a 'name'")
        clean = {k: v for k, v in user.items() if k not in ("grants", "assignments")}
        clean["groups"] = _resolve_user_groups(
            user, matrix, naming, access_grants, role_sets, scope_tenant, scope_environment)
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
