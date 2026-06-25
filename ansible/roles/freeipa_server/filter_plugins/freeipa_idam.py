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


def _cells_for_access_set(sname, aset, matrix, scope_tenant, scope_environment):
    """Expand one access_set into concrete (tenant, env, app, privilege) cells."""
    app = aset.get("app")
    priv = aset.get("privilege")
    if not app or not priv:
        raise AnsibleFilterError(
            "access_set '%s' must define both 'app' and 'privilege'" % sname)
    tenants = matrix.get("tenants") or {}
    cells = []
    for tenant, tdef in (aset.get("tenants") or {}).items():
        if tenant not in tenants:
            raise AnsibleFilterError(
                "access_set '%s': tenant '%s' is not defined in tenants" % (sname, tenant))
        if scope_tenant and tenant != scope_tenant:
            continue
        tenant_envs = (tenants.get(tenant) or {}).get("environments") or []
        for env in _expand_environments((tdef or {}).get("environments"), tenant_envs):
            if scope_environment and env != scope_environment:
                continue
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
def freeipa_idam_access_objects(matrix, scope_tenant=None, scope_environment=None):
    matrix = matrix or {}
    naming = matrix.get("naming") or {}
    apps = matrix.get("apps") or {}
    privileges = matrix.get("privileges") or {}
    access_sets = matrix.get("access_sets") or {}
    stock = set(matrix.get("stock_hbacsvcs") or DEFAULT_STOCK_HBACSVCS)

    usergroups, hostgroups = {}, {}
    hbac_rules, sudo_rules = {}, {}
    hbacsvcs, sudo_commands = {}, {}

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
            n = _names(naming, tenant, env, app, priv)
            tag = "%s/%s/%s/%s" % (tenant, env, app, priv)

            usergroups.setdefault(n["role"], {
                "name": n["role"], "description": "Grant group %s" % tag})
            ug = usergroups.setdefault(n["ug"], {
                "name": n["ug"], "description": "Policy group %s" % tag, "group": []})
            if n["role"] not in ug["group"]:
                ug["group"].append(n["role"])

            hostgroups.setdefault(n["hg"], {
                "name": n["hg"], "description": "Hostgroup %s" % tag})

            if hbac_services:
                hbac_rules.setdefault(n["hbac"], {
                    "name": n["hbac"], "description": "HBAC %s" % tag,
                    "usergroup": [n["ug"]], "hostgroup": [n["hg"]],
                    "service": list(hbac_services), "state": "enabled"})
                for svc in hbac_services:
                    if svc not in stock:
                        hbacsvcs.setdefault(svc, {
                            "name": svc, "description": "%s (custom HBAC service)" % svc})

            if sudo_cmds:
                rule = {
                    "name": n["sudo"], "description": "Sudo %s" % tag,
                    "usergroup": [n["ug"]], "hostgroup": [n["hg"]],
                    "runasusercategory": "all", "runasgroupcategory": "all",
                    "state": "enabled"}
                if [c.upper() for c in sudo_cmds] == ["ALL"]:
                    rule["cmdcategory"] = "all"
                else:
                    rule["cmd"] = list(sudo_cmds)
                    for cmd in sudo_cmds:
                        sudo_commands.setdefault(cmd, {"name": cmd})
                sudo_rules.setdefault(n["sudo"], rule)

    return {
        "usergroups": list(usergroups.values()),
        "hostgroups": list(hostgroups.values()),
        "hbac_rules": list(hbac_rules.values()),
        "sudo_rules": list(sudo_rules.values()),
        "hbacsvcs": list(hbacsvcs.values()),
        "sudo_commands": list(sudo_commands.values()),
    }


# ── filter 2: user grants ────────────────────────────────────────────────────
def freeipa_idam_user_grants(users, matrix, scope_tenant=None, scope_environment=None):
    matrix = matrix or {}
    naming = matrix.get("naming") or {}
    access_sets = matrix.get("access_sets") or {}

    compiled = []
    for user in users or []:
        if not isinstance(user, dict) or not user.get("name"):
            raise AnsibleFilterError("each user must be a mapping with a 'name'")
        groups = list(user.get("groups") or [])

        for sname in user.get("grants") or []:
            aset = access_sets.get(sname)
            if aset is None:
                raise AnsibleFilterError(
                    "user '%s': grant '%s' is not a defined access_set"
                    % (user["name"], sname))
            app, priv = aset.get("app"), aset.get("privilege")
            for tenant, env, app, priv in _cells_for_access_set(
                    sname, aset, matrix, scope_tenant, scope_environment):
                role = _names(naming, tenant, env, app, priv)["role"]
                if role not in groups:
                    groups.append(role)

        clean = {k: v for k, v in user.items() if k not in ("grants", "assignments")}
        clean["groups"] = groups
        compiled.append(clean)
    return compiled


class FilterModule:
    def filters(self):
        return {
            "freeipa_idam_access_objects": freeipa_idam_access_objects,
            "freeipa_idam_user_grants": freeipa_idam_user_grants,
        }
